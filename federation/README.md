# The federation registry

This directory is the list of datasets EMSO offers. One directory per regional
facility, one YAML file per ERDDAP server inside it:

```
federation/
├── Balearic_Sea/
│   └── OBSEA.yaml            <- one ERDDAP server
├── Ligurian_Sea/
│   ├── EMSO-FR.yaml          <- two servers in one facility is fine
│   └── OSUPYTHEAS.yaml
└── EXAMPLE.yaml.template     <- copy this for a new server
```

`python3 gate.py check` prints the current contents — facilities, servers and
dataset counts — and is the authority; no count is repeated here, because a
number in prose goes stale the first time someone adds a dataset.

> **For data managers.** This is the only directory you need. The gate's
> configuration, code and workflows live elsewhere and you do not have to
> touch them.

The directory name is the **facility**, and it is also the unit of access:
everyone listed for `federation/Balearic_Sea` may edit anything inside it. The
file name is the **service** — a label for one ERDDAP server, not a facility of
its own.

---

## One file, one ERDDAP server

```yaml
service:
  url: https://data.obsea.es/erddap
  datasets:
    OBSEA_seabed_station_TS_L1b:
    OBSEA_seabed_station_TS_L1c:
    OBSEA_moored_buoy_meteo_L1b:
```

That is a complete, valid file. Three things are worth knowing:

- **A bare datasetID is the normal case.** The trailing colon is YAML for "this
  key has no value"; it is not a typo and it must be there.
- **The source URL is derived**, as `<url>/<type>/<datasetID>` — so
  `OBSEA_seabed_station_TS_L1b` above resolves to
  `https://data.obsea.es/erddap/tabledap/OBSEA_seabed_station_TS_L1b`.
- **`/erddap` is appended if you leave it off.** `https://data.obsea.es` and
  `https://data.obsea.es/erddap` mean the same thing here.

You only need a body under a datasetID to override one of those defaults.

---

## Fields

### `service.url` — required

The base URL of your ERDDAP. Normalised: a trailing slash is stripped and
`/erddap` is appended when missing.

It must be `http://` or `https://` and must not carry a `user:password@` — a
credential here would be committed to a public repository. The same applies to a
per-dataset `url` override. Both plain `http://` and `https://` are accepted; two
federated services use `http://` today.

A **private or internal address is allowed** — an IP, or an internal hostname.
The central node is the only host that has to reach your ERDDAP, so if it can
reach you over the internal network, that is enough to federate. Bear in mind
that the gate then reaches it from the central node, so the address has to be
resolvable and routable *from there*, not from your own machine.

### `service.datasets` — required, at least one

A mapping of datasetID to either nothing or an override body. A datasetID must
match `^[a-zA-Z][a-zA-Z0-9_.\-]*$` — a letter, then letters, digits, underscore,
dot or hyphen.

This is checked here because ERDDAP refuses the **entire** `datasets.xml` over
one illegal datasetID. Catching it in the pull request is the difference
between a red check on your branch and the whole federation serving a stale
file.

datasetIDs must also be **unique across the whole federation**, not just your
file — ERDDAP keys everything on them. If another facility already declares
your ID, the `registry` check names the file that claimed it.

### Per-dataset overrides

Only three keys are accepted. Anything else is rejected by name, rather than
ignored:

| Key | Default | When you need it |
|---|---|---|
| `type` | `tabledap` | `griddap` for gridded datasets |
| `url` | derived | the dataset is not at `<service url>/<type>/<datasetID>` |
| `defaultQuery` | none | to control the graph ERDDAP shows by default |

```yaml
service:
  url: https://data.obsea.es/erddap
  datasets:
    # the common case
    OBSEA_seabed_station_TS_L1b:

    # a gridded dataset
    OBSEA_model_output:
      type: griddap

    # somewhere other than the derived location
    OBSEA_special_case:
      url: https://different.host/erddap/tabledap/OBSEA_special_case

    # a default graph
    OBSEA_seabed_station_TS_L1c:
      defaultQuery: "time%2CTEMP&time>2015-04-15T00%3A00%3A00Z&.draw=linesAndMarkers"
```

**`defaultQuery` encoding — the one genuine trap.** Percent-encode the query
itself (`%2C` for a comma, `%3A` for a colon), but write `&`, `<` and `>`
literally. The gate escapes XML metacharacters when it generates
`datasets.xml`, so an `&amp;` written here is emitted as `&amp;amp;` and the
graph will not load.

---

## Before you commit

Run the registry check locally if you have Python. It needs no database, no
network and no credentials:

```bash
pip install 'PyYAML>=6.0' 'rich>=13.0' 'psycopg2-binary>=2.9,<3'
python3 gate.py check
```

It prints every facility, its server and its dataset count, and exits non-zero
on the first problem. This is exactly what the `registry` check runs on your
pull request, so a green run here means a green check there.

If you would rather not install anything, skip it — opening the pull request
runs the same check in a few seconds.

---

## What gets rejected, and why

The registry is built **all or nothing**: one malformed file means no
`datasets.xml` is written at all. That is why these are refused early and by
name, rather than guessed at.

| Message | Cause |
|---|---|
| `malformed YAML` | usually indentation, or a missing trailing colon |
| `expected a 'service' mapping` | the top-level `service:` key is missing |
| `service has no url` | `service.url` is absent or empty |
| `service has no datasets` | `datasets:` is missing or empty |
| `is not a legal ERDDAP datasetID` | the ID does not match the pattern above |
| `is already declared in <file>` | that datasetID exists elsewhere in the federation |
| `has unknown key(s)` | a typo such as `defaultquery` or `protocol` |
| `has type 'x'; expected tabledap, griddap` | unsupported protocol |
| `must be empty or a mapping` | a scalar under a datasetID, e.g. `MY_ID: tabledap` |
| `must start with http:// or https://` | a missing scheme, or one that is not HTTP |
| `must not carry credentials` | a `user:password@` in the URL |
| `has no hostname` | the URL has a scheme but no host, e.g. `http:///erddap` |

Note the last one: `MY_ID: tabledap` is **not** how you set the protocol.
Use `MY_ID:` followed by an indented `type: griddap`.

---

## After it is merged

Being in this file means the dataset is **declared**, not that it is federated.
Declaring it is your half; passing the compliance check is the other half, and
that is measured against your own ERDDAP's metadata.

A newly declared dataset is scored on the next run. If it passes it is written
into `datasets.xml` and the central ERDDAP is asked to reload just that dataset
— the rest of the federation keeps serving throughout.

If it does not pass, it is recorded as `blocked`, `unreachable` or `unhandled`,
with the reasons, and simply does not appear in the catalogue. Nothing else
breaks. See ["When your dataset is blocked"](../README.md#when-your-dataset-is-blocked).

**Removing a dataset** is deleting its line. The gate marks it removed and
drops it from `datasets.xml` on the next run, but keeps its validation history
— that history is the answer to "why was this dropped?".

---

## Adding a new ERDDAP server

1. Copy [`EXAMPLE.yaml.template`](EXAMPLE.yaml.template) to
   `federation/<Your_Facility>/<ServerName>.yaml`.
2. Set `service.url` and list your datasets.
3. If the facility directory is new, ask an administrator to add a group for it
   in [`.github/access/access.yaml`](../.github/access/access.yaml) — otherwise
   nobody owns the new folder and `authorize` will refuse every pull request
   that touches it, including yours.

---

## For maintainers

The loader is `validation_gate/federation.py`. It builds a `Dataset` per
declared ID and refuses to guess: every malformed input raises `RegistryError`
naming the file.

Two derived properties are worth knowing before changing anything:

- **`Dataset.info_url`** normalises everything to
  `<base>/info/<id>/index.html`. The compliance engine's URL parser understands
  `/erddap/tabledap/<id>` and `/erddap/info/<id>/index.html` but **not**
  `/erddap/griddap/<id>`, and the info form is the only one that works for both
  protocols.
- **`Dataset.server`** is the hostname, used by `shuffle_datasets()` to
  interleave work across servers so the thread pool does not point twenty
  concurrent requests at one regional ERDDAP.

`Federation.get_datasets(how)` implements the three run modes. `new` selects
datasets whose database status is `unknown` *or* `pending` — absent from the
database entirely, and mirrored by `sync_federation` but never validated, both
mean the same thing to a run.

`.regen_from_xml.py` regenerated these files from the old `emso-erddap` XML
fragments during the migration. It is kept for reference and is not part of any
workflow.
