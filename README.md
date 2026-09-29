# EMSO Validation Gate

The EMSO central ERDDAP does not serve everything it is offered. It serves
everything that **passes a compliance check**, and this repository is that
check.

You declare your datasets in a small YAML file under `federation/`. The gate
reads that file, scores every dataset against the
[EMSO Metadata Specifications](https://github.com/emso-eric/emso-metadata-specifications),
and federates the ones that pass. Nothing else.

> **This page is for data managers** — people who publish datasets through
> EMSO and need them to appear in the central catalogue. It does not explain
> how the gate is built. If you maintain the code, start at
> [`OPERATIONS.md`](OPERATIONS.md) instead.

---

## New here? Start with this

**EMSO** — the European Multidisciplinary Seafloor and Water Column Observatory
— is a European Research Infrastructure Consortium (ERIC) that operates a
pan-European network of **14 regional facilities** across the Mediterranean,
the Black Sea, the North Atlantic and the Arctic Ocean. Each facility is run by
an independent national institution: a cabled coastal observatory, a deep-ocean
mooring, an open-ocean time-series station. They host physical, biogeochemical,
acoustic and imaging sensors, often on the same platform.

Those institutions keep full sovereignty over their own data. Nobody uploads
files to a central archive, and no central authority can rewrite a facility's
metadata. That constraint is political as much as technical, and it rules out
the obvious architecture of one big database.

**ERDDAP** is the data broker that makes the alternative work. It is a
widely-used scientific data server that takes datasets in formats like NetCDF
and serves them over a uniform REST API — the same query syntax and the same
output formats (CSV, JSON, NetCDF, graphs) whatever the underlying file.

**The federation** is what you get by combining those two. Every regional
facility runs *its own* ERDDAP, holding its own data. A **central ERDDAP** then
publishes a single catalogue of everything, using ERDDAP's `EDDTableFromErddap`
and `EDDGridFromErddap` dataset types — thin proxies that forward each request
to the regional node that actually holds the data. A user searches one
catalogue and queries one API; the bytes still come from the institution that
produced them. Nothing is copied, and nothing is centralised except the index.

That leaves one question: **what gets into the index?**

Federating a dataset means advertising it as EMSO data, so a dataset with
missing units, an unresolvable sensor identifier or a free-text keyword nobody
can look up degrades the whole catalogue. For two years compliance with the
EMSO Metadata Specifications was voluntary. The result was a large catalogue
with a low harmonization score — the percentage of the specification's required
tests a dataset actually passes.

**This repository is the mechanism that changed that.** It is the *validation
gate*: a CI/CD pipeline that acts as a circuit breaker on the central ERDDAP.
Facilities register a dataset by committing its URI here — never the data
itself. Every commit triggers an automated compliance audit against the
specifications, and only datasets that pass are written into the central
ERDDAP's configuration. Everything else stays on its regional server,
reachable, but not federated.

Switching it on cut the number of federated datasets sharply and pushed the
harmonization score close to full compliance, where it has stayed as facilities
fixed their metadata and their datasets came back.

If you want the design reasoning rather than the operating instructions, the
framework is described in *"A DataOps Framework for Distributed
Multidisciplinary In Situ Marine Data: The EMSO ERIC Infrastructure"*
(Martínez et al., IEEE Journal of Oceanic Engineering). Related repositories:
the [metadata specifications](https://github.com/emso-eric/emso-metadata-specifications)
(the single source of truth this gate validates against), the
[harmonizer toolbox](https://pypi.org/project/emso-metadata-harmonizer/)
(`pip install emso-metadata-harmonizer`, which facilities run locally *before*
publishing), and
[example datasets](https://github.com/emso-eric/example-datasets).

---

## What the gate does

Your data never moves. The central node holds no copy of it: each federated
dataset is a thin proxy that re-serves your own regional ERDDAP, so the
catalogue is a *view* over the facilities rather than a duplicate of them.

Every dataset ends up in exactly one of four states:

| Status | What it means | Federated? | Whose problem |
|---|---|---|---|
| **healthy** | Passed every enforced rule | ✅ yes | — |
| **blocked** | Reachable and scored, but failed a rule | ❌ no | yours: fix the metadata |
| **unreachable** | Your server did not answer | ❌ no | yours: check the server |
| **unhandled** | The compliance checker itself broke | ❌ no | ours: not your fault |

A dataset is **healthy** when all three of these hold:

- at least **90 %** of the specification's *required* attributes are present
  and valid — the *optional* percentage is recorded and reported, never gated on
- the **operational** tests pass — valid `variable_type`, the mandatory
  coordinates, and a `sensor_id` and `platform_id` that resolve
- the **keyword** tests pass — every keyword resolves in a controlled vocabulary

Those three numbers, and the specifications version they were measured against
(currently `v1.0.7`), live in [`validation-gate.yaml`](validation-gate.yaml).
That file belongs to the central team. You own `federation/`, which says which
datasets exist.

When a dataset is blocked, the gate records *every* rule that failed, not just
the first one — so you get the full list in one go rather than discovering them
one fix at a time.

---

## How to add your data

You add a dataset by adding one line to your facility's file in `federation/`.
A minimal entry is just the datasetID:

```yaml
service:
  url: https://data.obsea.es/erddap
  datasets:
    OBSEA_seabed_station_TS_L1b:
    OBSEA_seabed_station_CTD_L1c:      # <- your new dataset
```

The source URL is derived as `<url>/tabledap/<datasetID>`, so most entries need
nothing after the colon. See
[`federation/README.md`](federation/README.md) for the full field reference —
`griddap`, a custom URL, or a `defaultQuery` for the graph preview.

### In the browser (no git needed)

1. Navigate to your facility's file, for example
   `federation/Balearic_Sea/OBSEA.yaml`.
2. Press the **pencil** icon to edit it.
3. Add your datasetID, keeping the two-space indentation, and **do not forget
   the trailing colon**.
4. At the bottom choose **Create a new branch for this commit and start a pull
   request**, then **Propose changes**.
5. Press **Create pull request**.

That is the whole procedure. You do not need to install anything.

### From the command line

```bash
git clone https://github.com/emso-eric/validation-gate.git
cd validation-gate
git checkout -b add-ctd-dataset

# edit only files inside your own facility folder
$EDITOR federation/Balearic_Sea/OBSEA.yaml

git commit -am "OBSEA: add the L1c CTD timeseries"
git push -u origin add-ctd-dataset
gh pr create          # or open the pull request in the web UI
```

### What happens next

Nothing to chase, and nobody to email:

1. **`authorize`** checks that the account that opened the pull request owns
   every path it touches.
2. **`registry`** checks that the registry still parses — no unknown keys, no
   illegal or duplicated datasetIDs anywhere in the federation.
3. If both pass **and the pull request changes nothing but facility records**,
   it is **merged automatically** and the deployment starts. No human approval.

Your dataset appears in the central catalogue within a few minutes of the
checks going green — if it passes the compliance check. If it does not, it is
recorded as `blocked` with the reasons, and the rest of the federation is
unaffected.

A pull request that touches anything *other* than facility records — code,
workflows, the access list — does not auto-merge. That is deliberate: a typo in
the gate should not deploy itself unreviewed.

One thing you may see: if another facility merged while your pull request was
open, yours has to be brought up to date first, so that your records are checked
against theirs and not against a stale copy. If your branch lives in this
repository the gate does that for you and the checks simply run again. If you
worked from a **fork** it cannot, and it will ask you to press **Update
branch**. Neither case is a rejection.

---

## Who can edit the federation

Write access on GitHub is all-or-nothing: it cannot be granted per folder. So
the gate enforces per-folder ownership itself, from
[`.github/access/access.yaml`](.github/access/access.yaml).

The rule is simple: **you may change your own facility's folder, and nothing
else.** Administrators may change everything. If a pull request touches a path
you do not own, the `authorize` check fails and nothing is merged or federated.

To see the current map:

```bash
python3 .github/access/manage_access.py show
```

Getting access is not something the repository can do for you — deliberately,
since a tool that grants access is a tool a commit could use to grant itself
access. Ask an administrator to add your GitHub username to your facility's
group.

**The check identifies you by the account that opened the pull request**, which
GitHub authenticated when you clicked the button. Your git `user.email` does not
come into it, so committing as `you@laptop.local` is fine and there is nothing
to configure.

The flip side is that whoever *opens* a pull request answers for every path in
it. A pull request carrying someone else's commits is judged against you, not
them — including the one GitHub's **Revert** button creates, which it authors to
whoever pressed it.

Full detail in [`.github/access/README.md`](.github/access/README.md).

---

## Re-running the gate by hand

Often nothing in this repository changes but your dataset does — you fixed a
missing attribute in your own ERDDAP, and you want the gate to look again.
Pushing an empty commit is not the way. Use the button:

1. Go to the [**Actions**](https://github.com/emso-eric/validation-gate/actions)
   tab.
2. Choose the **validation-gate** workflow.
3. Press **Run workflow**, and pick a **mode**:

| Mode | Re-checks | Use it when |
|---|---|---|
| `pending` | everything not currently healthy | **the usual choice** — you fixed metadata and want a re-score |
| `new` | only datasets never validated before | you just added datasets and nothing else |
| `all` | the entire federation | a specifications bump, or a forced rebuild |

Leave **trigger** at `manual`; it only controls how the run is labelled in the
history.

`pending` is what you want after fixing metadata. Nothing in the registry
changed, so `new` would find nothing to do.

Some notes on what to expect:

- Runs are **serialised** — one gate run at a time. If a run is already going,
  yours queues rather than racing it.
- A run that reports blocked or unreachable datasets is **not a failed run**.
  That is the normal state of a federation of independent servers. Only an
  infrastructure failure shows up as a red run.
- The **Federation status** summary at the bottom of the run page is the part
  to read.

Anyone who can push to the repository can press this button, and nobody else.

---

## When your dataset is blocked

1. Open the run's **Federation status** summary, or the Grafana dashboard, and
   find your dataset.
2. The recorded failures name each rule that did not hold — a required-metadata
   score below the threshold, the operational tests, or the keyword tests.
3. Fix the metadata **in your own ERDDAP**, not here. The gate reads your
   server; it never edits your metadata.
4. Re-run with **mode `pending`** as above.

If the status is `unhandled`, stop — that means the compliance checker broke,
not your metadata. Report it rather than trying to fix your dataset.

---

## More information about

| Topic | Where | Who it is for |
|---|---|---|
| **Declaring datasets** — every field of the registry file, and what gets rejected | [`federation/README.md`](federation/README.md) | data managers |
| **Authorization** — who may change what, and how it is enforced | [`.github/access/README.md`](.github/access/README.md) | starts for data managers, then maintainers |
| **The automation** — the pull-request gate, auto-merge and the validation policy | [`.github/workflows/README.md`](.github/workflows/README.md) | starts for data managers, then maintainers |
| **The policy itself** — threshold, specifications version, reload behaviour | [`validation-gate.yaml`](validation-gate.yaml) | commented in full |
| **Running and deploying the gate** — the CLI, the containers, secrets | [`OPERATIONS.md`](OPERATIONS.md) | maintainers |
| **The code** — module by module, and how a run flows through it | [`validation_gate/README.md`](validation_gate/README.md) | maintainers |
| **The database** — schema, views, and how to query the history | [`database/README.md`](database/README.md) | maintainers |
| **The central ERDDAP** — `datasets.xml` generation and reloads | [`erddap/README.md`](erddap/README.md) | maintainers |
| **The dashboards** — panels, provisioning, and the views behind them | [`grafana/README.md`](grafana/README.md) | maintainers |

---

## Licence

MIT. Author: Enoc Martinez, Universitat Politècnica de Catalunya (UPC).
