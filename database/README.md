# Validation Gate Database

PostgreSQL: the federation's memory. `sql/` is applied in filename order by
`gate init-db`, and every file is idempotent, so re-running it against an
existing database is safe.

```
sql/001_schema.sql   tables
sql/002_views.sql    everything Grafana reads
```

## What is recorded, and why the separation matters

| | |
| --- | --- |
| `facility` / `service` / `dataset` | What git declares, mirrored on every run. Kept after a dataset leaves the registry, because its history is the answer to "why was this dropped?" |
| `run` | One execution: the policy it enforced, the engine and specifications version that scored it, the commit it validated. |
| `validation` | One `(run, dataset)` result. Immutable - nothing here is updated after the run that wrote it. |
| `federation_state` | The derived current state, one row per dataset. This is what `datasets.xml` is built from. |
| `federation_snapshot` | The federation's shape at each run, including runs that changed nothing - otherwise the Grafana timeline would be a scatter of deployment days rather than a line. |

Two rules run through the whole schema:

**`NULL` is not zero.** A dataset the engine never scored stores
`required_score = NULL`. A regional outage must not show up as a compliance
collapse, and "could not be evaluated" must stay distinguishable from
"evaluated and scored badly" - only the second is the facility's fault.

**Every verdict carries its own policy.** `validation` stores the threshold and
the two required-test flags in force when it was written; `run` stores the
specifications version and a `policy_fingerprint`. Raising the bar later does
not rewrite the history of why past datasets were admitted, and a
federation-wide score shift can be attributed to the specifications rather
than to the data.

## Views

| view | for |
| --- | --- |
| `v_dataset_current` | Current state of every declared dataset. The spine the others build on. |
| `v_remediation_queue` | Everything not federated, closest to the threshold first. Never-evaluated datasets sort last: they need an ERDDAP fixed, not metadata. |
| `v_facility_health` | Per-facility counts and mean score. |
| `v_facility_mismatch` | Datasets whose `emso_regional_facility_name` disagrees with the directory they are filed under. |
| `v_runs` | Run history with the policy each one enforced. |
| `v_federation_timeline` | Declared vs federated vs mean compliance, over time. |
| `v_score_history` | Every score a dataset has ever had. |
| `v_lowest_scores` | The worst 50 currently on record. |
| `v_exceptions` | Datasets federated only because `validation-gate.yaml` lists them. A list nobody looks at is how a temporary exception becomes permanent. |
| `v_attention` | Everything needing attention, in one list. |

Grafana reads views, never tables. Dashboards then hold panel configuration
rather than business logic, and a change to what "degraded" means is one
migration instead of an audit of every panel.

## Operating

```bash
cp .env.example .env                       # repository root
cp database/.env.example database/.env     # the values must match
docker compose up -d database
docker compose run --rm gate init-db
```

Backup:

```bash
docker compose exec database pg_dump -U gate emso_gate | gzip > gate-$(date +%F).sql.gz
```

`report_json` holds the compliance engine's report verbatim. It is the bulk of
the data, and also what makes a future policy change replayable against past
runs without re-validating the federation. If the database grows
uncomfortable, that column is the first thing to prune - the on-disk copy
under `gate/reports/` survives it.
