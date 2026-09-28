# EMSO Validation Gate

The central node of the EMSO ERIC federation. Datasets are declared in git; the
gate scores each one against the EMSO Metadata Specifications and federates
only the compliant ones into the central ERDDAP by rewriting `datasets.xml`.

```
gate.py               the command line
validation_gate/      the gate: config, registry, database, ERDDAP, the run
validation-gate.yaml  the policy            the central team owns this
federation/           WHAT is declared      the facilities own this
erddap/               the central ERDDAP    datasets.xml is generated here
database/             the federation's memory: schema + views
grafana/              dashboards over those views
cache/.emso           the compliance engine's specifications and vocabularies
.github/              CI, and who may change what
```

## How it works

1. **Sync.** `federation/**.yaml` is mirrored into PostgreSQL. Datasets that
   left the registry are marked `removed_at`, never deleted - their history is
   the answer to "why was this dropped?"
2. **Select.** `new` (never validated), `pending` (everything not currently
   healthy) or `all`. A one-line registry change validates one dataset, not the
   whole federation.
3. **Score.** Hand each dataset to `emso-metadata-harmonizer` - the same package
   the facilities run locally for shift-left pre-validation - and read its JSON
   report. The specifications and vocabularies are downloaded once up front, so
   the compliance pass itself runs in a thread pool (`workers`, currently 20),
   interleaved across servers so one regional ERDDAP is not hammered.
4. **Judge.** `required >= threshold`, plus the operational and keyword tests
   when they are enforced. Every rule is evaluated even after one has failed: a
   data manager needs all the reasons at once.
5. **Record.** One immutable row per `(run, dataset)`, carrying the policy that
   was in force and the engine's full JSON report. The run row records both
   what this run checked *and* a snapshot of the whole federation, because an
   incremental run's own tally says nothing about the 200 datasets it skipped.
6. **Deploy.** Rebuild `datasets.xml` from what the *database* says is healthy -
   not from this run, so an incremental run cannot drop the datasets it never
   looked at - and touch a flag file for the datasets that are new to the file,
   reloading one dataset instead of the whole federation.

The gate does not probe reachability separately; the engine already fails on an
unreachable dataset, and doing it twice fetched everything twice.

`reachable` and `compliant` are kept strictly apart everywhere. A dataset that
was never scored records `required_score = NULL`, never 0.

### The four statuses

| status | meaning | federated |
| --- | --- | --- |
| `healthy` | passed every enforced rule | yes |
| `blocked` | reachable and scored, failed at least one rule | no |
| `unreachable` | HTTP error or timeout; no score exists | no |
| `unhandled` | the compliance engine itself broke; no score exists | no |

`pending` is not a recorded status: it is what `v_dataset_current` reports for a
dataset that is declared but has never been validated.

There is no `degraded` status. "Was healthy, now is not" is a question about
history, and the `validation` table already *is* the history - `v_regressions`
answers it. **A dataset whose latest verdict is not `healthy` is dropped from
`datasets.xml` on the next deploy.** The registry row and its history stay.

## Quick start

```bash
./gate.py autodeploy    # the four .env files, random secrets, bind-mount dirs
```

`autodeploy` is idempotent and never overwrites an existing `.env` or any
service data: it copies each missing `.env` from its template, generates
`POSTGRES_PASSWORD`, `GF_SECURITY_ADMIN_PASSWORD` and `ERDDAP_flagKeyKey` into
the root `.env` (mode 600), writes `UID`/`GID` from the invoking user, creates
every directory `docker-compose.yaml` bind-mounts, and writes a valid empty
`erddap/datasets.xml`. Those last two matter: Docker creates a missing
bind-mount source itself, as root and always as a *directory*, which is the
most common first-run failure.

Then edit `.env` for this host - at minimum `ERDDAP_baseUrl` - and:

```bash
docker compose up -d
./gate.py init-db
./gate.py check          # validate the registry, offline
./gate.py run all        # first full validation
```

The same steps work without a host Python environment:
`docker compose run --rm gate <subcommand>`.

- ERDDAP: <http://localhost:8080/erddap>
- Grafana: <http://localhost:3000>, folder *EMSO*

The first validating run downloads the specifications and every controlled
vocabulary into `cache/.emso` (~115 MB). Keep that on persistent storage; a cold
cache is a few seconds per run, every run.

## The command line

`./gate.py` runs on the host once `pip install -r requirements.txt` succeeds.
On a host without the engine's compiled netCDF/UDUNITS libraries, use
`docker compose run --rm gate <subcommand>` instead; the arguments are the same.

```bash
./gate.py run new         # never validated before (the CI default)
./gate.py run pending     # everything not currently healthy, new ones too
./gate.py run all         # the whole federation (forced rebuild)

./gate.py check           # validate and print the registry. No database.
./gate.py summary         # per-facility counts by status
./gate.py status          # summary, attention, regressions, recent runs
./gate.py list blocked    # datasets in one status bucket, grouped by facility
./gate.py init-db         # apply database/sql/*.sql and exit

./gate.py autodeploy      # configure a fresh checkout: .env files + secrets
./gate.py password-rotate # new secrets in .env, applied to the live services
```

`run` also takes `--trigger manual|push|schedule`, recorded against the run for
the audit trail. `list` takes a status (`healthy`, `blocked`, `unreachable`,
`unhandled`, `all`), `--rf FACILITY` to restrict it to one regional facility and
`--out FILE` to also write CSV. `autodeploy` and `password-rotate` take `--yes`
to skip the confirmation, and refuse to run unconfirmed on a non-tty.

Global flags work before or after the subcommand: `--repo`, `--config`,
`-l/--log-level debug|info|warning|error`, `--log-dir` (`''` disables the
rotating `logs/gate.log`).

Exit codes, because CI reads them:

| code | meaning |
| --- | --- |
| 0 | everything checked passed |
| 1 | the run completed and something failed validation |
| 2 | the gate itself could not run, or another instance already holds the lock |

Only one gate run at a time, enforced by a PostgreSQL session-level advisory
lock (`0x454D534F`). A second instance exits 2 without touching anything, and
the lock is released by the connection closing, so a hard crash leaves nothing
stale behind. Each run also marks any `running` row left by a killed process as
`failed`, so `v_runs` has no ghosts.

Logs go to stderr, the rich tables to stdout - which is also what lets the gate
swallow the engine's own printing without silencing its own progress.

## The policy

`validation-gate.yaml`, and nothing else:

```yaml
config:
  threshold: 90.0              # % of the specification's REQUIRED attributes
  operational_required: true   # the data is actually usable
  keywords_required: true      # keywords resolve in a controlled vocabulary
  specs_version: "v1.0.7"      # pinned on purpose
  workers: 20
  reload_method: hardflag      # hardflag | flag | none
  reload_minutes: 60
  keep_backups: 10
```

Paths (`federation_dir`, `datasets_xml`, `erddap_data_dir`, `templates_dir`,
`cache_dir`) are also set there; see the file's own comments. An unknown key is
a hard error - a silently ignored option is a policy nobody is enforcing.

**No credential is configurable here**, because this file is committed. The
connection string is built from `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`POSTGRES_HOST`, `POSTGRES_PORT` and `POSTGRES_DB` in the repository-root
`.env` - the same file `docker-compose.yaml` reads - and `$GATE_DATABASE_URL`
overrides the whole thing, which is how the container and CI pass credentials.
A leftover `database_url:` or `database_env_file:` key is rejected by name
rather than ignored.

The optional-attribute percentage is recorded and reported, never gated on.

The specifications version is pinned so that a score can only move because the
data moved. Bumping it is a deliberate commit, and worth following with
`run all`.

## Triggers

| when | what |
| --- | --- |
| push to `main` | `validate.yml`: access check, offline registry check, then `run new` |
| `workflow_dispatch` | the same job without the access check, and `run pending` |

Both run as a single job on the self-hosted runner, labelled
`[self-hosted, validation-gate]`, inside the deployment at `$GATE_DEPLOYMENT`
(default `/opt/gate`, overridable with a repository variable). There is no
`actions/checkout`: the first step fast-forwards the deployment itself with
`git merge --ff-only`, so the gate writes `datasets.xml` where the central
ERDDAP reads it and keeps the ~115 MB compliance cache warm between runs. A
deployment that has diverged fails loudly rather than discarding local commits,
and only `main` is deployable.

`workflow_dispatch` is `run pending` rather than `run new` on purpose: it is the
button a facility presses after fixing its metadata, when nothing in the
registry has changed and `new` would therefore find nothing. It already requires
write permission on the repository, so every facility that can push its own
config can also press it, and nobody else can.

The offline `gate.py check` runs before `gate.py run`: the gate builds the
federation all-or-nothing, so one malformed file means no `datasets.xml` is
written at all. Failing early leaves the live federation serving the previous
file instead of one facility's bad push taking every facility down.

There is no scheduled sweep. `run pending` from a host crontab was considered
and is not installed; add it deliberately if a nightly re-check is wanted.

## Who may change what

GitHub has no per-folder write permission, so `.github/access/access.yaml` is
the single source that compiles into the mechanisms that do exist:

```bash
python3 .github/access/manage_access.py show         # the effective map
python3 .github/access/manage_access.py codeowners   # -> .github/CODEOWNERS
python3 .github/access/manage_access.py check --author X --changed-files F
python3 .github/access/manage_access.py sync --repository owner/name
```

**The `Enforce access rules` step in `validate.yml`** runs `check` against
`github.actor` - the pusher, not the commit author, since a commit can claim any
author but the push is authenticated - for every path in
`github.event.before..github.sha`. If any path is not owned, the job fails and
`gate.py run` never executes, so an unauthorised edit never reaches the central
ERDDAP. This is a brake, not a lock: the commit is already in the branch.

**A push ruleset** is the only thing that refuses the push itself. It needs a
private or internal repository, and it scales to all thirteen facilities as one
ruleset per facility - restricting that facility's folder, bypassed by that
facility's team - plus a base ruleset restricting everything outside
`federation/`. See [`.github/access/README.md`](.github/access/README.md).

`CODEOWNERS` and `access-guard.yml` are the pull-request path, and are dormant
while everything goes in by push. Run `codeowners` after every edit to
`access.yaml` and commit the result.

`sync` pushes the collaborator list (repository-wide `push`, or `admin` for a
group owning `"*"`) and needs `$GITHUB_TOKEN` with `repo` scope. Run it by hand,
never from CI - a workflow that can grant access is a workflow a commit can use
to grant itself access. It never removes a collaborator; stale ones are
reported.

`access.yaml` is committed, because CI cannot read a file that is not. It holds
GitHub usernames only - no email addresses. Usernames are already visible to
anyone who can read the history; an email address would not be.

## Documentation

- [`overview.md`](overview.md) - every component in rebuild-level detail
- [`federation/README.md`](federation/README.md) - declaring a dataset
- [`database/README.md`](database/README.md) - schema and views
- [`.github/access/README.md`](.github/access/README.md) - per-folder write access
- [`validation-gate.yaml`](validation-gate.yaml) - the policy, commented

## Operational notes

- **`datasets.xml` is a build artefact.** Never hand-edit it; the next run
  overwrites it, and it is gitignored for that reason. It is built from
  `erddap/templates/datasets_header.xml` and `datasets_footer.xml` with one
  `EDD{Table,Grid}FromErddap` proxy block per healthy dataset. Each build keeps a
  timestamped backup under `erddap/datasets.xml.backups/` (`keep_backups`), and
  the generated XML is parsed before it replaces the live file - a malformed
  `datasets.xml` makes ERDDAP keep the old one and email the admin, which is a
  silent outage. The file is written in place rather than renamed over: it is a
  bind-mounted *file* in the ERDDAP container, and replacing the inode would
  leave the container holding the old one.
- **Reloads.** `reload_method: hardflag` touches
  `erddap/data/hardFlag/<datasetID>`. Hard rather than soft because every
  dataset here is an `EDD*FromErddap` proxy: what ERDDAP caches is the
  *regional* dataset's variable list, so after a facility adds a variable a soft
  flag reloads the dataset while keeping the stale shape.
- **ERDDAP is configured from `erddap/.env`**, not a hand-maintained
  `setup.xml`. ERDDAP 2.x reads any setup.xml setting from `ERDDAP_<tagName>`,
  and the image prints every one it saw at startup:
  `docker compose logs erddap | grep "ERDDAP configured with" -A 40`.
- **Reports.** The engine's JSON report for every dataset is stored in
  PostgreSQL, in `validation.report_json`. That is the authoritative and only
  copy: the gate writes the engine's JSON into a temporary directory that is
  removed when the run ends. `report_json` is the bulk of the database and the
  first thing to prune if it grows uncomfortable - at the cost of being able to
  replay a policy change against past runs.
- **Idempotency.** A run over an already-healthy federation writes a run row,
  rewrites an identical `datasets.xml` and flags nothing.
- **Concurrency.** One gate run at a time; two would race on `datasets.xml` and
  the loser would deploy a stale file. The gate holds a PostgreSQL advisory
  lock, which is what serialises it against anything else on the host; the
  workflow also uses a concurrency group so runs that would exit 2 on arrival
  are simply not queued.
- **The container runs as the invoking user** (`user: "${UID:-1000}:${GID:-1000}"`).
  Everything the gate writes lands in the bind-mounted working tree, and a
  root-owned artefact there is what stops `./gate.py` working on the host
  afterwards. `./gate.py autodeploy` writes the real values into `.env`; export
  `UID`/`GID` or set them by hand if that file predates it.
- **Rotating secrets.** `./gate.py password-rotate` generates new values for
  `POSTGRES_PASSWORD`, `GF_SECURITY_ADMIN_PASSWORD` and `ERDDAP_flagKeyKey`,
  backs up the old `.env` alongside it, `ALTER ROLE`s the database and resets
  the Grafana admin password with `grafana-cli`. The PostgreSQL change happens
  *first*: if it fails, `.env` is untouched and the deployment is still
  consistent. `ERDDAP_flagKeyKey` applies on the next
  `docker compose up -d erddap`. The `.env.bckp.*` files it leaves behind hold
  the previous passwords and are exactly as secret as `.env` itself.
