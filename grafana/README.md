# The dashboards

> **For maintainers.** Data managers read the dashboards; they do not configure
> them.

Grafana 13, provisioned entirely from files. Nothing is clicked into existence:
the datasource and both dashboards are created on start from
`grafana/provisioning/`, so a rebuilt container comes back identical.

```
grafana/
├── provisioning/
│   ├── datasources/gate.yaml    the PostgreSQL connection
│   └── dashboards/gate.yaml     the file provider
├── dashboards/                  the dashboards themselves
│   ├── federation-overview.json
│   └── dataset-history.json
├── data/                        Grafana's own state (gitignored)
└── .env                         UI preferences
```

Default at `http://<host>:3000`, admin credentials in the repository-root
`.env`.

---

## The two dashboards

### EMSO Federation Overview — `emso-federation-overview`

The one to open first. Where does the federation stand?

| Panel | View |
|---|---|
| Declared / Healthy / Blocked / Unreachable / Unhandled / Mean required score | `v_federation_summary` |
| Declared vs federated over time | `v_federation_timeline` |
| Current status mix (pie) | `v_federation_summary` |
| Per-facility health | `v_facility_summary` |
| Needs attention | `v_attention` |
| Regressed since last healthy | `v_regressions` |
| Facility mismatches | `v_facility_mismatch` |
| Recent runs | `v_runs` |

### EMSO Dataset History — `emso-dataset-history`

One dataset, drilled down: current status, required score, operational and
keyword verdicts as stat tiles; then the score timeseries, every validation on
record, the current failure list, and the registry declaration.

Backed by `v_score_history` and `v_dataset_current`.

---

## Everything goes through a view

This is the rule worth preserving. **No panel contains a join.** Every query
reads a `v_*` view, so dashboards hold panel layout and nothing else, and a
column can move without editing JSON.

If a panel needs data no view provides, add the view in
[`../database/sql/002_views.sql`](../database/sql/002_views.sql) and point the
panel at it. Do not put the join in the dashboard.

### The timeline trap

"Declared vs federated over time" reads `v_federation_timeline`, which exposes
the **`federation_*`** columns of `run` — the state of the whole federation when
each run finished.

Do **not** switch it to the `datasets_*` columns. Those are what that run
checked: `run new` checks two datasets and reports 0 blocked, `run all` checks
everything and reports 140, and the chart then swings between them according to
the run mode rather than according to anything about the federation. That bug
is the reason both sets of columns exist. See
[`../database/README.md`](../database/README.md).

---

## Provisioning

### The datasource

`provisioning/datasources/gate.yaml` — uid `emso-gate-db`, type `postgres`,
marked `isDefault`. Its connection details are **environment variables**, set
by `docker-compose.yaml` from the root `.env`:

```yaml
url:      ${GATE_DB_HOST}      # database:5432, the compose service name
user:     ${GATE_DB_USER}
database: ${GATE_DB_NAME}
secureJsonData:
  password: ${GATE_DB_PASSWORD}
```

That indirection is the point: the datasource is not another place credentials
have to be kept in sync. Rotating with `./gate.py password-rotate` updates the
root `.env`, and Grafana picks the new password up on restart.

Panels reference the datasource by **uid**, not by name, so renaming it in the
UI does not break them.

### The dashboard provider

`provisioning/dashboards/gate.yaml` watches `/var/lib/grafana/dashboards`
(bind-mounted read-only from `grafana/dashboards/`) every 30 s, into the folder
`EMSO`.

`allowUiUpdates: true` and `editable: true`, so you can experiment in the
browser — but **edits made in the UI are overwritten** when the file changes,
and are lost when the container is recreated. To keep a change:

1. Edit the panel in the UI until it is right.
2. **Dashboard settings → JSON Model**, copy it.
3. Paste into the file in `grafana/dashboards/`, commit it.

Keep the `uid` stable — that is what links a saved URL to a dashboard.

---

## The container

```yaml
grafana:
  image: grafana/grafana:13.0-slim
  user: "${UID:-1000}:${GID:-1000}"
  depends_on:
    database: { condition: service_healthy }
  volumes:
    - ./grafana/provisioning:/etc/grafana/provisioning:ro
    - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
    - ./grafana/data:/var/lib/grafana
```

Grafana defaults to uid 472; it is pinned to the host user so nothing in the
working tree ends up owned by a uid you cannot clean up without `sudo`.
**`grafana/data` must belong to that user** — `./gate.py autodeploy` creates it
correctly.

Both provisioning mounts are read-only: the running container cannot rewrite
its own configuration.

`grafana/.env` holds UI preferences only — light theme by default, analytics
reporting and update checks off. The admin credentials are
`GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` in the **root** `.env`.

---

## Troubleshooting

**"No data" in every panel.** The datasource cannot reach PostgreSQL. Check
`docker compose logs grafana` for authentication failures — usually the root
`.env` password was changed without restarting Grafana, since it reads
`GATE_DB_PASSWORD` at start.

**The admin password in `.env` does not work.** Grafana copies
`GF_SECURITY_ADMIN_PASSWORD` into its own database on **first start** and
ignores it afterwards. Editing `.env` changes nothing on an existing install.
`./gate.py password-rotate` handles this by running `grafana-cli` inside the
container; see [`../OPERATIONS.md`](../OPERATIONS.md).

**A dashboard reverted.** The file provider re-read the JSON on disk and
discarded the UI edit. Export the JSON model and commit it.

**Panels are empty but the database has rows.** Check the panel's time range
against `last_checked_at` — after a quiet period the default window can fall
entirely before the most recent run.
