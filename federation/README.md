# The federation registry

Which datasets the central EMSO ERDDAP federates. This directory belongs to
the regional facilities; the policy that judges them is `validation-gate.yaml`
at the repository root, which does not.

Every `.yaml` and `.yml` under `federation/` is scanned, at any depth.

## One file, one ERDDAP server

```yaml
service:
  url: "https://data.obsea.es"        # /erddap is appended when missing

  datasets:
    OBSEA_seabed_station_TS_L1b:      # defaults: tabledap, derived URL
    OBSEA_seabed_station_TS_L1c:
      url: "https://other/erddap/tabledap/Renamed"
      type: "tabledap"                # tabledap | griddap
      defaultQuery: "time%2CTEMP&time>=2015-04-15T00%3A00%3A00Z"
```

A bare datasetID with nothing after it is the common case and the form to
copy. Everything else is an override and reads like one.

**The facility is the directory**, not anything inside the file. Moving a file
between directories moves its datasets between facilities and nothing else has
to change. The file name is the service - conventionally whose server it is -
and carries no meaning; rename freely. The directory names mirror the
`emso-erddap` facility directories exactly, so the two repositories stay
diffable: the Balearic Sea facility is `federation/Balearic_Sea/`, and `OBSEA`
is the name of the ERDDAP server inside it, not a facility of its own.

A facility that federates from more than one server gets more than one file:

```
federation/Ligurian_Sea/EMSO-FR.yaml       10 datasets
federation/Ligurian_Sea/OSUPYTHEAS.yaml    21 datasets
```

## Fields

There are five keys in total, and **anything else under a datasetID is a hard
error**. A silently ignored `defaultquery` would produce a federation whose
graphs are wrong in a way nobody can see from the registry.

| under `service:` | |
| --- | --- |
| `url` | **required.** The ERDDAP base URL. `/erddap` is appended when missing. |
| `datasets` | **required.** Mapping of datasetID to its overrides, or nothing. |

| per dataset | |
| --- | --- |
| `url` | Full source URL. Only needed when it is not `<service url>/<type>/<datasetID>`. |
| `type` | `tabledap` or `griddap`. Defaults to `tabledap`. There is no service-wide default. |
| `defaultQuery` | Percent-encoded; becomes `<defaultGraphQuery>` in `datasets.xml`. |

**There is no `enabled` key, and no `comment` or `label`.** A dataset is either
declared or it is not: to take one out of the federation, delete its line. The
gate marks it `removed_at` in the database rather than forgetting it, so its
validation history survives and comes back if it is re-declared. Writing
`enabled: false` under a datasetID fails the registry check by name.

Every datasetID must match `^[a-zA-Z][a-zA-Z0-9_.\-]*$` and be unique across
the *whole* federation, not just your file. ERDDAP keys the federation on the
datasetID and rejects the entire `datasets.xml` over one illegal one, which
would be a silent hole in everybody's federation, not just yours.

## Before you commit

```bash
./gate.py check
```

Offline, no database, no compliance engine, a second or two. It catches
malformed YAML, a missing `service` or `datasets` mapping, illegal datasetIDs,
duplicates across facilities, and unknown keys. The same check runs in CI
before anything is validated or deployed, because the gate builds the
federation all-or-nothing: one malformed file means no `datasets.xml` is
written at all.

To see your compliance score before the gate does, run the same engine
locally:

```bash
pip install emso-metadata-harmonizer
python3 -c 'from emso_metadata_harmonizer import metadata_report; \
  metadata_report("https://<your-erddap>/erddap/info/<datasetID>/index.html", \
                  specs_version="v1.0.7")'
```

Use the same `specs_version` as the root `validation-gate.yaml`, or your
numbers will not match the gate's.

## When a dataset is blocked

There is no report tree on disk. The engine's report is stored verbatim in
PostgreSQL, in `validation.report_json`, and that is the only copy - the gate
writes the JSON into a temporary directory that is removed when the run ends.

Read it through Grafana (the *EMSO* folder: **Needs attention** is sorted
closest to the threshold first, and the per-dataset dashboard shows every
score a dataset has ever had), or from the central node:

```bash
./gate.py status                       # attention, regressions, recent runs
./gate.py list blocked --rf <Facility> # your facility only
./gate.py list blocked --out mine.csv  # the same, as CSV
```

Datasets a point or two short usually share one fixable template, so fixing it
once clears several together.

After a fix, nothing in this directory needs to change: press **Run workflow**
on the `validation-gate` action. It runs `pending`, which re-checks everything
not currently healthy, and it needs only the write access you already have to
push here.

## Example

`EXAMPLE.yaml.template` is a commented reference. It is not loaded: only
`.yaml` and `.yml` are scanned.
