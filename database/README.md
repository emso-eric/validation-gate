# The gate database

> **For maintainers.** Data managers never touch this.

PostgreSQL 17. The schema is two files, applied in order and idempotent, so
`init_db()` runs them on every connection:

| File | Contents |
|---|---|
| [`sql/001_schema.sql`](sql/001_schema.sql) | five tables and their indexes |
| [`sql/002_views.sql`](sql/002_views.sql) | inline migrations, then nine views |

Everything that reads the database — `gate.py`, Grafana, you with `psql` —
goes through a **view**. Dashboards hold panel layout, not schema knowledge, so
a column can move without editing JSON.

---

## The central design decision

**There is no current-state table.** The current state of a dataset is its most
recent `validation` row, exposed as `v_dataset_current`.

Everything follows from that:

- The gate **only ever inserts**. Nothing in `validation` is updated after the
  run that wrote it, so there is no state to keep in sync and no write path that
  can corrupt history.
- **`degraded` does not exist as a status.** "Was healthy, now is not" is a
  question about history, and history is what the `validation` table already is.
  It is derived instead, as `v_regressions`.
- A dataset is **federated when its latest status is `healthy`**. Nothing else.

---

## Tables

```
facility ──< service ──< dataset ──< validation >── run
```

### `facility` / `service` / `dataset` — what git declares

Mirrored from `federation/` by `sync_federation()` on every run. `facility.slug`
is the directory name; `service.name` is the registry file stem;
`dataset.dataset_id` is `UNIQUE` because ERDDAP keys the whole federation on it.

`first_seen` / `last_seen` are maintained on every sync. **`dataset.removed_at`
is set when a dataset disappears from the registry — the row is never deleted**,
because its validation history is the answer to "why was this dropped?".
`v_dataset_current` filters on `removed_at IS NULL`.

### `run` — one execution of the gate

Records the mode, the trigger, **the engine that produced every score**
(`harmonizer_version`, `specs_version`) and **the policy it enforced**
(`threshold`, `operational_required`, `keywords_required`).

Pinning the engine version per run is what makes a federation-wide score shift
attributable: telling "the data changed" from "the specifications changed"
after the fact is the hard part of any compliance incident.

#### Two scopes of counter — read this before touching Grafana

The column prefix does **not** tell you the scope. Three groups:

| Columns | Scope | Source |
|---|---|---|
| `datasets_checked`, `datasets_healthy`, `datasets_blocked`, `datasets_unreachable`, `datasets_unhandled` | **this run only** | the run's own `Counter` |
| `datasets_declared`, `datasets_federated` | the whole federation | `len(all_datasets)` at `start_run`; rows written to `datasets.xml` at `_deploy` |
| `federation_healthy`, `federation_blocked`, `federation_unreachable`, `federation_unhandled`, `federation_pending` | the whole federation | counted from `v_dataset_current` |

The first group is the trap. An incremental `run new` checks two datasets and
reports `datasets_blocked = 0`; `run all` checks everything and reports, say, 140.
Plotting those produces a chart that swings between the two according to which
*kind of run* happened — not according to anything about the federation. That
bug is why the `federation_*` group exists.

`federation_*` is counted in the same `UPDATE` that closes the run, so it is
the state of everything at the moment that run finished, whether the run looked
at it or not. **Those are the ones to plot for status.**

`datasets_declared` and `datasets_federated` are safe to plot despite the
prefix: both are federation-wide. `datasets_federated` is `len(current)` from
`_deploy()`, which is built from `get_healthy_datasets()` — every healthy
dataset, not just this run's.

`NULL` in a `federation_*` column means "not recorded" — true for runs
predating these columns that were not reconstructible. Never 0 in that case.

### `validation` — immutable history

One row per `(run_id, dataset_pk)`, `UNIQUE` on the pair. Three rules run
through the whole table:

**1. `NULL` is not zero.** `required_score` is `NULL` when the engine never
scored the dataset. "Could not be evaluated" and "evaluated badly" are different
outcomes and only the second is the facility's to fix. A 0 would drag every
average down and make an unreachable server look like bad metadata.

**2. The policy is stored per verdict.** `threshold`, `operational_required`
and `keywords_required` are copied onto every row. Raising the bar later does
not rewrite the history of why past datasets were admitted.

**3. The engine's full JSON is kept** in `report_json` (`JSONB`), so a future
policy change can be replayed against past runs without re-validating the
federation:

```sql
SELECT dataset_id, report_json -> 'metadata' -> 'required'
FROM validation v JOIN dataset d ON d.id = v.dataset_pk
WHERE v.run_id = 42;
```

`institution` and `emso_facility` are read from the dataset's own metadata and
drive `v_facility_mismatch`.

A `CHECK` constraint restricts `status` to the four known values, so a typo in
new code fails the insert rather than creating a fifth state nothing reports on.

---

## Views

| View | Answers |
|---|---|
| `v_dataset_current` | the current state of every declared dataset — **the base of everything else** |
| `v_facility_summary` | per-facility counts by status + mean required score |
| `v_federation_summary` | one row of federation-wide totals, for stat tiles |
| `v_attention` | everything not healthy, worst first, with how far short |
| `v_regressions` | healthy at some point in the past, not healthy now |
| `v_facility_mismatch` | the dataset's self-declared facility disagrees with its directory |
| `v_runs` | run history with duration |
| `v_score_history` | every score a dataset has ever had, for drill-down |
| `v_federation_timeline` | the federation over time, one point per finished run |

Three are worth explaining.

**`v_dataset_current`** is a `DISTINCT ON (d.id) ... ORDER BY d.id,
v.checked_at DESC NULLS LAST` — PostgreSQL's idiomatic latest-row-per-group.
The `LEFT JOIN` plus `COALESCE(v.status, 'pending')` is what gives a
newly-synced dataset the status `pending`: declared, mirrored, never validated.
That is why `run new` selects `{unknown, pending}` — both mean "no verdict yet".

**`v_attention`** sorts by `(required_score IS NULL)` first, so datasets that
were never scored sort **last**. They need an ERDDAP fixed, not metadata, and
putting them at the top would bury the ones a data manager can act on.
`points_short` is the distance to the threshold; `detail` is the failure list
joined, falling back to `error_message`.

**`v_facility_mismatch`** compares `lower(replace(emso_facility, ' ', '_'))`
against the directory name. Almost always a metadata error at the facility, and
invisible without joining the two sources.

### Inline migrations

`002_views.sql` starts with migrations that must run **before** any view
references the new shape: the `run.status` → `run.outcome` rename, folding the
old `aborted` outcome into `failed`, and backfilling `federation_*` for runs
that finished before those columns existed.

The backfill is reconstructible from the `validation` table alone — the state of
a dataset at a past moment is its latest validation *as of* that moment — and is
idempotent twice over (`WHERE federation_healthy IS NULL`, and only finished
runs). Adding a migration means adding it here, above the views.

---

## Operating

The database runs as the `database` service in
[`docker-compose.yaml`](../docker-compose.yaml), image `postgres:17.11-alpine`,
data in `database/pgdata`, bound to `127.0.0.1:5432` by default. It runs as
`${UID}:${GID}` rather than the image's default user, so nothing in the working
tree ends up owned by a uid you cannot clean up without `sudo`.

```bash
# schema only, then exit
./gate.py init-db

# a shell
docker compose exec database psql -U gate -d emso_gate

# from the host, if GATE_DB_PORT is published
psql "$(grep -E '^POSTGRES_' .env | ...)"   # or just use the container
```

Useful queries:

```sql
-- what needs attention right now
SELECT facility, dataset_id, status, required_score, points_short, detail
FROM v_attention LIMIT 20;

-- the federation over time (plot the federation_* columns, not datasets_*)
SELECT * FROM v_federation_timeline ORDER BY started_at;

-- one dataset's whole history
SELECT * FROM v_score_history WHERE dataset_id = 'OBSEA_seabed_station_TS_L1b';

-- did a specifications bump move the scores?
SELECT specs_version, AVG(required_score)
FROM validation v JOIN run r ON r.id = v.run_id
GROUP BY specs_version;
```

### Backups

`database/pgdata` is a bind mount, so a filesystem snapshot works, but the
supported route is a dump:

```bash
docker compose exec -T database pg_dump -U gate emso_gate | gzip > gate-$(date +%F).sql.gz
```

Nothing here is irreplaceable except the **history**: the registry is in git and
`datasets.xml` is regenerated from the database on every run. Losing the
database means losing the audit trail and one `run all` to rebuild the current
state.

### Credentials

`POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` come from the
repository-root `.env` — the same file the postgres image reads, so they cannot
drift from the running server. Rotate with `./gate.py password-rotate`, which
also issues the `ALTER ROLE` the running cluster needs: the image reads
`POSTGRES_PASSWORD` at `initdb` only, so editing `.env` alone changes nothing.
See [`../OPERATIONS.md`](../OPERATIONS.md).

`database/.env` holds only `POSTGRES_INITDB_ARGS`, which is read at cluster
creation and has no effect afterwards.
