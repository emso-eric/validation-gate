# Operations

> **For maintainers.** Running, deploying and maintaining the central node.
> Data managers need only [`README.md`](README.md) and
> [`federation/README.md`](federation/README.md).

Service-specific detail lives with the service:
[`database/`](database/README.md) · [`erddap/`](erddap/README.md) ·
[`grafana/`](grafana/README.md). The code itself is documented in
[`validation_gate/`](validation_gate/README.md).

---

## What runs where

```
┌─ the host (e.g. /opt/gate) ────────────────────────────────┐
│                                                            │
│  gate.py ──────────────┐   the GitHub Actions self-hosted  │
│  (venv, or the `gate`  │   runner executes this            │
│   container)           │                                   │
│                        ▼                                   │
│  docker compose:  erddap · database · grafana              │
│                                                            │
│  writes erddap/datasets.xml + flag files ─────► erddap     │
│  writes validation history ──────────────────► database   │
└────────────────────────────────────────────────────────────┘
```

Four containers, but only three are services. `gate` sits behind
`profiles: ["tools"]`, so `docker compose up` does not start it — it is a
one-shot job for hosts without the compliance engine's compiled dependencies.

The gate shares ERDDAP's bind mounts deliberately: that is what lets it write
`datasets.xml` and request a per-dataset reload instead of restarting ERDDAP.

---

## First deployment

### Prerequisites

Docker with the Compose plugin, git, and — if you want to run the gate outside
its container — Python 3.12 plus compiled netCDF, UDUNITS and HDF5 (see the
[`Dockerfile`](Dockerfile) for the exact package list).

### Steps

```bash
git clone https://github.com/emso-eric/validation-gate.git /opt/gate
cd /opt/gate

./gate.py autodeploy          # config files, directories, secrets
docker compose up -d          # erddap, database, grafana
./gate.py init-db             # schema
./gate.py run all             # first full validation
```

### What `autodeploy` does

Three independent things, each skipped when already done, so it is safe to
re-run after any of them has been removed by hand:

1. **The four `.env` files**, copied from their templates. All four must exist
   — Compose fails with "env file not found" on a missing `env_file`, not just
   on the one carrying secrets.
2. **Every directory `docker-compose.yaml` bind-mounts**, including mount
   points nested inside another mount's target. Docker creates a missing bind
   source itself, *as root and as a directory* — so a first `docker compose up`
   on a fresh checkout leaves root-owned directories in the working tree, and
   where a file was expected (`datasets.xml`) a directory ERDDAP cannot read.
   Creating them from a process already running as the right user avoids both.
3. **An empty-but-valid `erddap/datasets.xml`**, so ERDDAP starts before the
   gate has ever run.

It only touches files. Nothing is started, and **nothing existing is ever
overwritten**. Secrets are generated *into* the root `.env` rather than applied
to a running service, because on a first deployment there is no server yet —
postgres reads `POSTGRES_PASSWORD` when it initialises its cluster. Rotating
later, against running services, is `password-rotate`.

### Configuration is two-layered

| File | Holds | Edit it |
|---|---|---|
| `.env` | every address, port, host and password, for all services | on a new server |
| `<service>/.env` | service-specific tuning | rarely |

`env_file` lists the service file **second**, so it wins where the same
variable appears in both. Compose also reads the root `.env` for `${VAR}`
interpolation in `docker-compose.yaml`. **Nothing secret belongs in
`docker-compose.yaml`.**

Root `.env`, in full:

| Variable | Default | Notes |
|---|---|---|
| `ERDDAP_PORT` | `8080` | |
| `GRAFANA_PORT` | `3000` | |
| `GATE_DB_PORT` | `127.0.0.1:5432` | localhost-only; widen to expose psql |
| `UID` / `GID` | `1000` | **the host user that owns the bind mounts** |
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | `gate` / — / `emso_gate` | read by the image *and* the gate |
| `POSTGRES_HOST` / `_PORT` | `127.0.0.1` / `5432` | ignored by the image; tells the gate where to connect |
| `GF_SECURITY_ADMIN_USER` / `_PASSWORD` | `admin` / — | Grafana admin |
| `ERDDAP_flagKeyKey` | — | ERDDAP's flag-URL key |

Get `UID`/`GID` wrong and every container writes files you cannot clean up
without `sudo`. Use `id -u` and `id -g`.

### The self-hosted runner

`validate.yml` runs on a runner labelled `self-hosted` **and**
`validation-gate`, with the repository checked out at `$GATE_DEPLOYMENT`
(default `/opt/gate`, overridable with a repository variable) and a venv beside
it:

```bash
cd /opt/gate
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

The workflow checks for `venv/bin/python` and fails with that instruction if it
is missing. It also refuses to run on any ref but `refs/heads/main`, and
fast-forwards with `--ff-only` so a diverged deployment fails loudly instead of
losing commits.

---

## The command line

`./gate.py` on the host, or `docker compose run --rm gate` without a local
Python environment. Every command accepts `--repo`, `--config`, `-l/--log-level`
and `--log-dir` (use `--log-dir ''` to disable the log file), both before and
after the subcommand.

### Daily

```bash
./gate.py status              # the full report — start here
./gate.py summary             # per-facility counts by status
./gate.py list blocked        # everything blocked, grouped by facility
./gate.py list all --rf Balearic_Sea --out /tmp/balearic.csv
```

`list` takes `healthy`, `blocked`, `unreachable`, `unhandled` or `all`,
optionally narrowed with `--rf <facility>` and dumped with `--out <file.csv>`.

`status` is `summary` plus what needs attention, what regressed since it was
last healthy, and the last five runs.

### Running the gate

```bash
./gate.py check               # registry only — offline, no database
./gate.py run new             # never-validated datasets (what a merge triggers)
./gate.py run pending         # everything not currently healthy
./gate.py run all             # the whole federation
./gate.py run all --trigger schedule
```

`--trigger` (`manual`, `push`, `schedule`) only labels the run in the history.

**Exit codes, because CI reads them:**

```
0  the run completed and every checked dataset is healthy
1  the run completed; at least one dataset is blocked or unreachable
2  the gate could not run — config error, database error, or another
   instance holds the advisory lock
```

**Only 2 is a failure.** 1 is the normal state of a federation of independent
third-party servers.

`check` is the one command that needs neither a database nor the compliance
engine, which is why CI can run it on a bare checkout.

### Administrative

```bash
./gate.py init-db             # apply database/sql/*.sql and exit; idempotent
./gate.py autodeploy [--yes]  # configure a fresh checkout
./gate.py password-rotate [--yes]
```

---

## Container lifecycle

```bash
docker compose up -d                  # erddap, database, grafana
docker compose ps
docker compose logs -f erddap
docker compose restart grafana
docker compose down                   # stop; bind-mounted data survives

# the gate as a one-shot job
docker compose run --rm gate check
docker compose run --rm gate run new
docker compose run --rm gate status
```

The `gate` container runs as `${UID}:${GID}` and bind-mounts the repository at
`/app`. Everything it writes — `datasets.xml`, backups, `logs/`, the engine
cache — lands in the working tree, and a root-owned artefact there is what stops
`./gate.py` working on the host afterwards.

It also sets `GATE_DATABASE_URL` explicitly, overriding `POSTGRES_HOST`/`_PORT`
from the root `.env`: those point at the *published* address for a gate running
on the host, while inside the compose network the database answers to its
service name.

### Upgrading an image

Pinned on purpose — `erddap/erddap:v2.31.1`, `postgres:17.11-alpine`,
`grafana/grafana:13.0-slim`. Bump the tag in `docker-compose.yaml`, then:

```bash
docker compose pull <service>
docker compose up -d <service>
```

Take a database dump before a postgres major-version bump; the data directory
is not forward-compatible.

---

## Rotating secrets

```bash
./gate.py password-rotate
```

Three secrets, and **editing `.env` is never enough on its own** — each needs
something else to happen:

| Secret | Why the file is not enough | What the command does |
|---|---|---|
| `POSTGRES_PASSWORD` | the image reads it at `initdb` only | `ALTER ROLE` on the live cluster |
| `GF_SECURITY_ADMIN_PASSWORD` | Grafana copies it into its own DB on first start | `grafana-cli admin reset-admin-password` in the container |
| `ERDDAP_flagKeyKey` | read once at startup | nothing — **you** must recreate ERDDAP |

The order is chosen so no failure leaves an inconsistent deployment:

1. **PostgreSQL first**, with the *old* credentials. If this fails, `.env` is
   untouched and the deployment is still entirely consistent.
2. **Then `.env`**, after a timestamped `.bckp.<stamp>` copy, written via a
   temporary file and `os.replace` so an interruption cannot leave it
   half-written. Comments and ordering are preserved; a name not found is
   appended with a note rather than silently lost. If this write fails, the new
   password is **printed** rather than lost.
3. **Then Grafana**, which is non-fatal — the rotation already succeeded.

Afterwards:

```bash
docker compose up -d erddap     # apply the new flagKeyKey
```

Every flag URL issued under the old key stops working. The gate's own flagging
is unaffected: it touches files in `erddap/data/hardFlag/` and never uses a URL.

Without `--yes` on a non-interactive stdin the command refuses rather than
rotating unattended.

**There is no repository secret for the database.** The runner *is* the
database host, so `validate.yml` reads `$GATE_DEPLOYMENT/.env`. A repository
secret would be a second copy of the same password that rotation cannot reach,
and it would drift the first time the password changed.

---

## Monitoring

- **Grafana** at `:3000` — see [`grafana/README.md`](grafana/README.md).
- **`./gate.py status`** for the same picture in a terminal.
- **`logs/gate.log`**, rotated daily. `--log-dir ''` disables it.
- **The Actions tab** — every run's Federation status summary.

### Is a run in progress?

```sql
SELECT pid, state, query_start
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE l.locktype = 'advisory' AND l.objid = 1162695503;   -- 0x454D534F = "EMSO"
```

The lock is **session-level**, so PostgreSQL releases it when the connection
closes — even on a hard crash. Stale locks never accumulate and there is nothing
to clear by hand.

---

## Troubleshooting

**`exit 2` / "another gate instance is already running".** Expected when a run
overlaps a manual one. If nothing is running, the query above shows the holder.

**"no environment file at ./.env".** Run `./gate.py autodeploy`, or pass
`$GATE_DATABASE_URL`.

**"unknown option(s)" from `Config.load`.** A typo in `validation-gate.yaml`.
Unknown keys are an error by design — a mistyped policy key must not silently
keep its default.

**Root-owned files in the working tree.** A container ran without `UID`/`GID`
matching the host user. Fix them with `sudo chown -R $(id -u):$(id -g) .`, set
both in `.env`, and re-run `./gate.py autodeploy`.

**"the compliance engine broke" / `unhandled`.** A bug in
`emso-metadata-harmonizer`, not in the facility's metadata. Reproduce with
`-l debug`; the full JSON report is in `validation.report_json`.

**A run takes far longer than usual.** Almost always unreachable servers
holding connections. The per-run log line separates the average with and
without unreachable datasets, and `timeout-minutes: 120` bounds it in CI.

**A cold engine cache.** `cache/.emso` re-downloads ~50 resources. Keep it on
persistent storage; the path must end in `.emso`, because the engine resolves
its cache by that literal name relative to its working directory.

---

## Maintenance checklist

**Per release of the specifications.** Bump `specs_version` in
`validation-gate.yaml`, run `./gate.py run all`, then compare mean scores by
`specs_version` — the per-run engine columns exist for exactly this.

**Occasionally.** Prune `erddap/datasets.xml.backups` (automatic, to
`keep_backups`), check `logs/`, and dump the database:

```bash
docker compose exec -T database pg_dump -U gate emso_gate | gzip > gate-$(date +%F).sql.gz
```

**When adding a facility.** Create `federation/<Facility>/`, add the group to
`.github/access/access.yaml`, create the GitHub team. Nothing reconciles the
last two — see [`.github/access/README.md`](.github/access/README.md).

**Never restore `datasets.xml` from a backup as a fix.** It is a build artefact;
the next run overwrites it. Fix the registry or the policy and re-run.
