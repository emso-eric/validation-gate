# The gate implementation

> **For maintainers.** Data managers need nothing in this directory.

Five modules, roughly 1,700 lines, no framework. The entry point is `gate.py`
at the repository root, which is argument parsing and terminal output only —
all behaviour lives here.

| Module | Lines | Responsibility |
|---|---|---|
| [`config.py`](config.py) | 211 | policy and paths from `validation-gate.yaml`; the database URL |
| [`federation.py`](federation.py) | 223 | the YAML registry → `Dataset` objects; run-mode selection |
| [`validation_gate.py`](validation_gate.py) | 631 | one run end to end: score, record, federate |
| [`db.py`](db.py) | 414 | every SQL statement the gate runs |
| [`erddap.py`](erddap.py) | 210 | `datasets.xml` generation and per-dataset reloads |

The dependency direction is one-way: `validation_gate.py` imports the other
four; none of them import it.

---

## A run, end to end

`ValidationGate.run_validation(how, trigger)` is the whole story:

```
connect()                    open PostgreSQL, apply database/sql/*.sql
try_acquire_run_lock()       pg_try_advisory_lock(0x454D534F) → exit 2 if held
cleanup_stale_runs()         mark orphaned 'running' rows failed
build_federation()           parse federation/, all-or-nothing
sync_federation()            mirror registry into the DB → {datasetID: pk}
get_datasets(how)            select all | new | pending
shuffle_datasets()           interleave by server
_prepare_engine()            download specs + vocabularies ONCE
start_run()                  open the run row
_validate_all()              thread pool → insert_validation() per dataset
_deploy()                    rebuild datasets.xml, flag what is new
finish_run()                 close the run row + federation snapshot
```

Exit codes: `0` everything checked is healthy, `1` the run completed but
something is not healthy, `2` the gate could not start.

Only `2` is a failure. `1` is the normal state of a federation of third-party
servers, and the workflow treats it accordingly.

---

## `config.py`

`Config` is a dataclass loaded from `validation-gate.yaml`, which must contain
a `config:` mapping. Two properties worth knowing:

**Unknown keys are an error.** `Config.load` compares the YAML keys against the
dataclass fields and raises `ConfigError` listing anything unrecognised. A typo
in the policy file fails the run rather than silently keeping a default. The
removed `database_url` and `database_env_file` keys have their own explicit
error telling you where credentials moved to.

**The database URL is resolved lazily**, on first access, not at load time.
`gate.py check` runs offline and must work on a checkout with no `.env` — which
is every CI checkout, since `.env` is gitignored.

### Credentials

`$GATE_DATABASE_URL` wins if set. Otherwise the URL is built from exactly five
variables in the repository-root `.env`:

```
postgresql://POSTGRES_USER:POSTGRES_PASSWORD@POSTGRES_HOST:POSTGRES_PORT/POSTGRES_DB
```

`POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` are the same variables
the postgres image itself consumes, so they cannot drift from the running
server. `POSTGRES_HOST` and `POSTGRES_PORT` are ignored by that image and exist
only to tell the gate where to reach it.

The env path is **not configurable**, deliberately: it is the same file
`docker-compose.yaml` reads. `read_env_file` is a parser, not a shell `source`
— the file holds a password and sourcing it would execute anything else it
contained. `${...}` interpolation is not performed, because the postgres image
does not perform it either.

User, password and database are **percent-encoded** into the URL. A password
containing `@ : / ? #` or `%` is legal in PostgreSQL and would otherwise
silently corrupt it.

---

## `federation.py`

See [`../federation/README.md`](../federation/README.md) for the file format
and the `Dataset` derived properties. In summary: `build_federation()` walks
the registry, raises `RegistryError` naming the file on any problem, and
enforces datasetID uniqueness across the whole federation.

`get_datasets(how)` implements the three modes. `new` matches
`NEVER_VALIDATED = {"unknown", "pending"}` — absent from the database, and
mirrored by `sync_federation` but never validated, both mean the same thing to
a run. `all` needs no database; the other two do.

---

## `validation_gate.py`

### Concurrency

The single most important property, and the reason for several odd-looking
lines:

- **`_prepare_engine()` runs once, before the pool.** `init_emso_metadata()`
  downloads the specifications and ~50 vocabulary resources; after that
  `metadata_report()` only reads them, which is what makes the compliance pass
  safe to run in threads.
- **`os.chdir` is called once, in `_validate_all`, before the pool starts.** The
  engine resolves its cache by the literal name `.emso` relative to the working
  directory. `chdir` is process-global, so a thread calling it would race —
  setting it once means no thread ever does.
- **`stdout` is redirected for the whole pass, not per call.** The engine prints
  pandas frames and progress tables straight to stdout and `quiet=True` does not
  cover all of it; on 207 datasets that is thousands of lines around the gate's
  own output. `sys.stdout` is process-wide, so swapping it per thread would race.
  The gate logs to **stderr**, so progress still appears.

`shuffle_datasets()` interleaves by hostname. Registry order groups every
dataset of a server together, which points the whole pool at one regional
ERDDAP at a time — slow for the gate and rude to the facility.

### Status and the four states

```python
VALIDATION_STATES = ("healthy", "blocked", "unreachable", "unhandled")
```

`validate()` **never raises**: every failure is a status. The distinction that
matters is between the last two.

- **`unreachable`** — a transport failure. `_is_network_error` walks the
  exception's `__cause__`/`__context__` chain and matches **type names** against
  `_NETWORK_ERRORS`, so the gate does not import `requests`, `urllib3` and
  `httpx` just to catch them.
- **`unhandled`** — the engine itself broke. Deliberately distinct from a bad
  score: nobody should be asked to fix metadata over a `KeyError` in the tester.

There is a third `unreachable` path that is easy to miss: the engine returns
`None` whatever happens, and writes no JSON when it could not download the
data. A missing report file therefore means the server did not answer, even
though its metadata was readable.

No score exists for either state, and `required_score` stays `NULL` — never 0.
"Could not be evaluated" and "evaluated badly" are different outcomes and only
the second is the facility's to fix.

### `verdict()`

```python
if report.required_score < threshold:      failures.append(...)
if operational_required and not passed:    failures.append(...)
if keywords_required and not passed:       failures.append(...)
return not failures
```

Every rule is evaluated **even once one has failed**. A data manager needs every
reason at once, not the first one the gate happened to check.

`_score()` is strict about the engine's JSON: a non-dict payload, a missing
`metadata` section, a percentage outside 0–100 or a section without a `passed`
key all raise, which surfaces as `unhandled` rather than as a wrong verdict.

### `_deploy()`

```python
healthy  = db.get_healthy_datasets()      # from the DB, not from this run
previous = erddap.existing_dataset_ids()  # from the live file
erddap.build_datasets(healthy)
flagged  = [reload(id) for id in current - previous]
```

`datasets.xml` is built from **the database**, not from the results of the
current run. An incremental run must not drop the datasets it never looked at.
Only IDs new to the file are flagged for reload; the rest keep serving.

---

## `db.py`

Every SQL statement the gate runs is in this module. The schema lives in
[`../database/`](../database/README.md), which also documents the views.

**The gate only ever inserts.** Nothing in `validation` is updated after the
run that wrote it, so the current state of a dataset is simply its newest row —
which is what `v_dataset_current` returns. There is no current-state table to
keep in sync.

**`sync_federation()`** upserts facility → service → dataset and returns
`{datasetID: pk}`. Datasets no longer declared are marked `removed_at`, never
deleted: their history is the answer to "why was this dropped?".

**`try_acquire_run_lock()`** takes a session-level advisory lock on
`0x454D534F` ("EMSO"). Session-level means PostgreSQL releases it when the
connection closes — even on a hard crash — so no explicit release is needed and
stale locks never accumulate.

**`cleanup_stale_runs()`** marks orphaned `running` rows as failed. It is called
once per run immediately after the lock is acquired, so no live process can be
racing it.

**`finish_run()`** records two different things in one `UPDATE`:

- `datasets_*` — what *this run* checked. Small for an incremental run.
- `federation_*` — the state of the **whole** federation, counted from
  `v_dataset_current`.

Storing only the former is what made the Grafana timeline swing between 0 and
140 according to run mode rather than according to anything about the
federation. Both are written in the same statement as the outcome, so a run can
never carry counts from a different moment.

---

## `erddap.py`

`datasets.xml` is a **build artefact**. Hand-editing it is always wrong — the
next run overwrites it. Two safeguards, both learned the hard way:

- **The generated XML is parsed before it replaces the live file.** A malformed
  `datasets.xml` makes ERDDAP silently keep the previous one, so the federation
  looks fine while being hours stale.
- **Every build keeps a timestamped backup** under `erddap/datasets.xml.backups`,
  pruned to `keep_backups`. Rolling back is a copy rather than a re-run of the
  whole gate.

The file is written **in place**, not via `os.replace`. It is a bind-mounted
*file* in the ERDDAP container, and replacing the inode would leave the
container holding the old one.

Encoding is `ISO-8859-1` with `xmlcharrefreplace`, matching the templates.

Full detail, including the `hardflag` reload semantics, in
[`../erddap/README.md`](../erddap/README.md).

---

## Conventions

- **Never guess.** `ConfigError`, `RegistryError`, `DatabaseError` and
  `ErddapError` all exist so that an ambiguous input stops the run naming the
  file, rather than proceeding on a default. The one deliberate exception is
  `existing_dataset_ids()`, which returns an empty set on a malformed file —
  noisy (it flags everything) but safer than deploying against a wrong picture
  of the current state.
- **One logger, `"gate"`,** module-level in every file.
- **Errors are phrased for whoever has to fix them,** and say what did *not*
  happen: "Nothing was merged and nothing was deployed" beats a stack trace.
- **`rich` is imported inside the functions that print tables,** not at module
  level, so the offline `check` path stays light.
- **`emso_metadata_harmonizer` is imported inside the scoring functions.** This
  is load-bearing: it is what lets `gate.py check` run in CI without compiled
  netCDF and UDUNITS. A module-level import of it would break the `registry`
  job — see [`../.github/workflows/README.md`](../.github/workflows/README.md).

## Adding a validation rule

1. Add the field to `ValidationReport`.
2. Read it in `_score()` from the engine's JSON.
3. Add the condition to `verdict()`, appending a human-readable line to
   `failures`.
4. Add the column to `database/sql/001_schema.sql` — plus an idempotent
   `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for existing deployments, since
   `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists.
5. Add it to the `INSERT` in `insert_validation()`.
6. If it is configurable, add it to `Config`, to `validation-gate.yaml`, and to
   the per-verdict policy columns so raising the bar later does not rewrite the
   history of why past datasets were admitted.
