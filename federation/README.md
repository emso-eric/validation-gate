# The federation registry

Which datasets the central EMSO ERDDAP federates. This directory belongs to
the regional facilities; the policy that judges them is
`gate/validation-gate.yaml`, which does not.

Every `.yaml` under `federation/` is scanned, at any depth.

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
copy. Everything else is an override and reads like one - `gate check` reports
how many datasets are still in the simple form, which is a decent proxy for
how much of the registry anyone has had to fight.

**The facility is the directory**, not anything inside the file. Moving a file
between directories moves its datasets between facilities and nothing else has
to change. The file name is the service - conventionally whose server it is -
and carries no meaning; rename freely.

A facility that federates from more than one server gets more than one file:

```
federation/Ligurian_Sea/EMSO-FR.yaml       10 datasets
federation/Ligurian_Sea/OSUPYTHEAS.yaml    21 datasets
```

## Fields

| under `service:` | |
| --- | --- |
| `url` | **required.** The ERDDAP base URL. `/erddap` is appended when missing. |
| `datasets` | **required.** Mapping of datasetID to its overrides, or nothing. |
| `type` | Default protocol for every dataset in the file. `tabledap` if absent. |
| `enabled` | `false` takes the whole service out of the federation without deleting it. |
| `label`, `comment` | Free text. |

| per dataset | |
| --- | --- |
| `url` | Full source URL. Only needed when it is not `<url>/<type>/<datasetID>`. |
| `type` | `tabledap` or `griddap`. |
| `defaultQuery` | Percent-encoded; becomes `<defaultGraphQuery>` in `datasets.xml`. |
| `enabled` | `false` removes this dataset from the federation. |
| `comment` | Free text. Changing it does **not** trigger a re-validation. |

## Before you commit

```bash
python3 -m gate.cli check
```

Offline, no database, no compliance engine, a second or two. It catches
malformed YAML, illegal datasetIDs, duplicates across facilities, and unknown
keys - a silently ignored `defaultquery` would otherwise produce a federation
whose graphs are wrong in a way nobody can see from the registry.

To see your compliance score before the gate does, run the same engine
locally:

```bash
pip install emso-metadata-harmonizer
python3 -c 'from emso_metadata_harmonizer import metadata_report; \
  metadata_report("https://<your-erddap>/erddap/info/<datasetID>/index.html", \
                  specs_version="v1.0.7")'
```

Use the same `specs_version` as `gate/validation-gate.yaml`, or your numbers
will not match the gate's.

## When a dataset is blocked

`gate/reports/<Facility>/<datasetID>/latest.txt` says exactly which required
attributes failed, and `latest.json` is the engine's report verbatim. The same
text is in PostgreSQL and on the Grafana remediation queue, closest to the
threshold first - datasets a point or two short usually share one fixable
template, so fixing it once clears several together.

The gate re-checks every failing dataset on the nightly sweep, so a fix gets a
new verdict the next morning without touching this directory.

## Example

`EXAMPLE.yaml.template` is a commented reference. It is not loaded: only
`.yaml` and `.yml` are scanned.
