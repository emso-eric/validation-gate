# Validation Gate Database

PostgreSQL: the federation's memory. `sql/` is applied in filename order by
`./gate.py init-db`, and every statement is idempotent
(`CREATE TABLE IF NOT EXISTS`, `CREATE OR REPLACE VIEW`), so re-running it
against a live database is safe. Every SQL statement the gate runs lives in
`validation_gate/db.py`; the connection is `autocommit`.

```
sql/001_schema.sql   tables
sql/002_views.sql    everything Grafana and the CLI read
```

## What is recorded, and why the separation matters

| table | contents |
| --- | --- |
| `facility` | `slug` (the `federation/` directory name), `first_seen`, `last_seen` |
| `service` | one ERDDAP server: `facility_id`, `name` (the file stem), `url`, `registry_path`; unique per `(facility, name)` |
| `dataset` | `dataset_id` **globally unique**, `source_url`, `protocol`, `erddap_type`, `default_query`, `registry_path`, `removed_at` |
| `run` | one execution: `mode`, `trigger`, the engine and specifications version that scored it, the policy it enforced, `outcome` (`running` \| `success` \| `failed`), and two sets of counters - see below |
| `validation` | one `(run, dataset)` verdict, unique on that pair. Immutable - nothing here is updated after the run that wrote it |

### The two sets of counters on `run`

`run` answers two different questions, and conflating them produces a chart
that measures the schedule rather than the federation.

| columns | question |
| --- | --- |
| `datasets_checked`, `datasets_healthy`, `datasets_blocked`, `datasets_unreachable`, `datasets_unhandled` | **What did this run do?** Only the datasets it actually looked at. A `run new` that finds nothing new checks zero datasets and every one of these is 0. |
| `federation_healthy`, `federation_blocked`, `federation_unreachable`, `federation_unhandled`, `federation_pending` | **What is the federation now?** Every declared dataset, counted from `v_dataset_current` when the run finished, whether this run looked at it or not. |

Plot the second set. The first set made `blocked` swing between 0 and 140
according to whether the run was `new` or `all` - a real signal about the CI
schedule and a completely false one about compliance.

`datasets_declared` and `datasets_federated` are already federation-wide, so
they have no second version. The snapshot satisfies
`healthy + blocked + unreachable + unhandled + pending = datasets_declared`.

A `NULL` in the `federation_*` columns means "not recorded" - a run from before
these columns existed that could not be reconstructed. Never 0: the same rule
as `required_score`, and Grafana draws a gap rather than a cliff.

Runs that predate the columns are back-filled once, from the `validation`
table, by `sql/002_views.sql`: the state of a dataset at time *T* is its newest
validation at or before *T*, which is exactly the rule `v_dataset_current`
applies at `now()`. Datasets already dropped from the registry by then are
excluded, so an old point counts the federation as it stood rather than as it
stands today. The backfill is idempotent and skips rows that already have a
snapshot.

### Three rules run through the whole schema

**There is no current-state table.** The current state of a dataset is simply
its newest `validation` row, exposed as `v_dataset_current`. Nothing can drift
out of sync with it, and `degraded` does not need to exist as a status: "was
healthy, now is not" is a question about history, and the table *is* the
history - `v_regressions` answers it.

**`NULL` is not zero.** A dataset the engine never scored stores
`required_score = NULL`. A regional outage must not show up as a compliance
collapse, and "could not be evaluated" must stay distinguishable from
"evaluated and scored badly" - only the second is the facility's to fix.

**Every verdict carries its own policy.** `validation` stores the threshold and
the two required-test flags in force when it was written; `run` stores the
engine and specifications version. Raising the bar later does not rewrite the
history of why past datasets were admitted, and a federation-wide score shift
can be attributed to the specifications rather than to the data.

Datasets that leave the registry are marked `removed_at`, never deleted: their
validation history is the answer to "why was this dropped?". Re-declaring one
clears `removed_at` and its history comes back with it.

## Views

Grafana and the CLI read views, never tables. Dashboards then hold panel
configuration rather than business logic.

| view | for |
| --- | --- |
| `v_dataset_current` | The spine: `DISTINCT ON (dataset)` latest validation of every dataset still declared. `status` falls back to `'pending'`, `federated = (status = 'healthy')`. |
| `v_facility_summary` | Per-facility counts by status, mean required score, oldest check. |
| `v_federation_summary` | The same federation-wide, one row, for stat tiles. |
| `v_attention` | Everything not healthy, worst first, with `points_short`. Never-scored datasets sort **last**: they need an ERDDAP fixed, not metadata. |
| `v_facility_mismatch` | Datasets whose self-declared `emso_facility` disagrees with the directory they are filed under. Almost always a metadata error, and invisible without joining the two sources. |
| `v_runs` | Run history with the policy each one enforced and its duration. |
| `v_score_history` | Every score a dataset has ever had, with the engine that produced it. |
| `v_federation_timeline` | The federation over time, one point per finished run. `declared`, `healthy`, `blocked`, `unreachable`, `unhandled`, `pending` and `federated` are federation-wide; the `checked_*` columns are that run's own tally. |
| `v_regressions` | Healthy at some point, not healthy now, with `last_healthy_at`. This is what `degraded` would have been, derived instead of stored. |

`pending` is not a recorded status: it is what `v_dataset_current` reports for a
dataset that is declared but has never been validated.

## Operating

The database is configured entirely from the repository-root `.env`, which
`./gate.py autodeploy` creates:

```bash
./gate.py autodeploy        # writes .env and database/.env, generates secrets
docker compose up -d database
./gate.py init-db
```

Backup:

```bash
docker compose exec database pg_dump -U gate emso_gate | gzip > gate-$(date +%F).sql.gz
```

`report_json` holds the compliance engine's report verbatim. It is the bulk of
the data, and what makes a future policy change replayable against past runs
without re-validating the federation. It is also the **only** copy - the gate
writes the engine's JSON into a temporary directory that is removed when the
run ends, so there is no report tree on disk. If the database grows
uncomfortable, that column is the first thing to prune, at the cost of that
replayability.

Rotating the password is `./gate.py password-rotate`, which `ALTER ROLE`s the
live cluster *before* touching `.env`, so a failure leaves the deployment
consistent.
