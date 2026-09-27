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
   the compliance pass itself runs in a thread pool (`workers`, default 10),
   interleaved across servers so one regional ERDDAP is not hammered.
4. **Judge.** `required >= threshold`, plus the operational and keyword tests
   when they are enforced. Every rule is evaluated even after one has failed: a
   data manager needs all the reasons at once.
5. **Record.** One immutable row per `(run, dataset)`, carrying the policy that
   was in force and the engine's full JSON report.
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
./setup.sh          # .env files, bind-mount files, database, schema, services
```

`setup.sh` is idempotent and never overwrites an existing `.env` or any service
data. What it does by hand:

```bash
cp .env.example .env                        # then fill in POSTGRES_PASSWORD
cp database/.env.example database/.env      # the values must match
cp erddap/.env.example erddap/.env          # at minimum, set ERDDAP_baseUrl
cp grafana/.env.example grafana/.env

docker compose up -d database erddap grafana
docker compose run --rm gate init-db
docker compose run --rm gate check          # validate the registry, offline
docker compose run --rm gate run all        # first full validation
```

- ERDDAP: <http://localhost:8080/erddap>
- Grafana: <http://localhost:3000>, folder *EMSO*

The first validating run downloads the specifications and every controlled
vocabulary into `cache/.emso` (~115 MB). Keep that on persistent storage; a cold
cache is a few seconds per run, every run.

## The command line

`./gate.py` runs on the host once `pip install -r requirements.txt` succeeds.
`./gate.sh` is the optional Docker wrapper for hosts without the engine's
compiled netCDF/UDUNITS libraries; it forwards every argument unchanged.

```bash
./gate.py run new         # never validated before (the CI default)
./gate.py run pending     # everything not currently healthy, new ones too
./gate.py run all         # the whole federation (nightly sweep, forced rebuild)

./gate.py check           # validate and print the registry. No database.
./gate.py summary         # per-facility counts by status
./gate.py status          # summary, attention, regressions, recent runs
./gate.py init-db         # apply database/sql/*.sql and exit
```

`run` also takes `--trigger manual|push|schedule`, recorded against the run for
the audit trail. Global flags work before or after the subcommand: `--repo`,
`--config`, `-l/--log-level debug|info|warning|error`, `--log-dir` (`''`
disables the rotating `logs/gate.log`).

Exit codes, because CI reads them:

| code | meaning |
| --- | --- |
| 0 | everything checked passed |
| 1 | the run completed and something failed validation |
| 2 | the gate itself could not run |

Logs go to stderr, the rich tables to stdout - which is also what lets the gate
swallow the engine's own printing without silencing its own progress.

Tab completion: `./gate.sh completion install` (appends a `source` line to
`~/.bashrc`), or source `gate-completion.bash` directly.

## The policy

`validation-gate.yaml`, and nothing else:

```yaml
config:
  threshold: 90.0              # % of the specification's REQUIRED attributes
  operational_required: true   # the data is actually usable
  keywords_required: true      # keywords resolve in a controlled vocabulary
  specs_version: "v1.0.7"      # pinned on purpose
  workers: 10
  reload_method: hardflag      # hardflag | flag | none
  reload_minutes: 60
  keep_backups: 10
```

Paths (`federation_dir`, `datasets_xml`, `erddap_data_dir`, `templates_dir`,
`cache_dir`) and `database_url` are also set there; see the file's own comments.
`$GATE_DATABASE_URL` overrides `database_url`, which is how the container and CI
pass credentials. An unknown key is a hard error - a silently ignored option is
a policy nobody is enforcing.

The optional-attribute percentage is recorded and reported, never gated on.

The specifications version is pinned so that a score can only move because the
data moved. Bumping it is a deliberate commit, and worth following with
`run all`.

## Triggers

| when | what |
| --- | --- |
| pull request to `production` | `validate.yml`'s offline registry check, plus `access-guard.yml`. A PR never deploys. |
| push to `production` | `validate.yml`: the registry check on a hosted runner, then `run new` on the self-hosted runner |
| `workflow_dispatch` | the same, with `mode` = `new`, `pending` or `all` |
| nightly 02:17 | `run pending`, from `.github/workflows/nightly.crontab` on the host |
| Sunday 03:17 | `run all`, same crontab |

The registry check is deliberately split out: it installs only `PyYAML` and
`rich`, needs no database and no compliance engine, and catches what most bad
commits actually are - malformed YAML, an illegal datasetID, a duplicate.

The nightly sweep is a crontab rather than a GitHub `schedule:` on purpose:
scheduled workflows only fire when the self-hosted runner is online and GitHub
delays them under load, which is the wrong reliability for a job whose purpose
is noticing what changed while nobody was looking. Install it by appending to
the existing crontab, never by replacing it - the file's header has the
commands.

`run pending` on the sweep means a facility that fixed its metadata overnight
gets a new verdict without touching the registry, and a healthy federation costs
nothing.

## Who may change what

GitHub has no per-folder write permission, so `.github/access/access.yaml`
compiles into the two mechanisms that do exist:

```bash
python3 .github/access/manage_access.py codeowners   # -> .github/CODEOWNERS
python3 .github/access/manage_access.py check --author X --changed-files F
python3 .github/access/manage_access.py show         # the effective map
python3 .github/access/manage_access.py sync --repository owner/name
```

CODEOWNERS requests a review from the owning group; the `access-guard` workflow
fails any pull request whose author does not own a changed path. Require both as
status checks on `production` and a data manager can edit their own facility and
nothing else.

`sync` pushes the collaborator list (repository-wide `push`, or `admin` for a
group owning `"*"`). Run it by hand, never from CI - a workflow that can grant
access is a workflow a commit can use to grant itself access. It never removes a
collaborator; stale ones are reported.

`access.yaml` holds real names and is gitignored - commit
`access.yaml.template`.

## Documentation

- [`overview.md`](overview.md) - every component in rebuild-level detail
- [`federation/README.md`](federation/README.md) - declaring a dataset
- [`database/README.md`](database/README.md) - schema and views
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
- **Idempotency.** A nightly run over a healthy federation writes a run row,
  rewrites an identical `datasets.xml` and flags nothing.
- **Concurrency.** One gate run at a time; two would race on `datasets.xml` and
  the loser would deploy a stale file. The workflow enforces this with a
  concurrency group, the crontab by running its two jobs an hour apart.
- **The container runs as the invoking user** (`user: "${UID:-1000}:${GID:-1000}"`).
  Everything the gate writes lands in the bind-mounted working tree, and a
  root-owned artefact there is what stops `./gate.py` working on the host
  afterwards. Export `UID`/`GID`, or set them in `.env`, if the defaults are not
  yours.
- **`gateOLD/`** is the previous implementation, kept for reference. Nothing
  imports it and nothing runs it.
