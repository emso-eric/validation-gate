# The automation

Two workflows, and the split between them is the design:

| Workflow | Runs | On | Job |
|---|---|---|---|
| [`pr-gate.yml`](pr-gate.yml) | before the merge | GitHub-hosted | decide whether a change may land, and merge it |
| [`validate.yml`](validate.yml) | after the merge | self-hosted, in `/opt/gate` | validate the datasets and federate them |

`pr-gate.yml` is the lock. `validate.yml` is the deployment. Nothing reaches
`main` except through a pull request whose checks passed, and nothing reaches
the central ERDDAP except through `main`.

---

# Part 1 — for data managers

## What happens to your pull request

```
you open a pull request
        │
        ├─ authorize   do you own every path it changes?
        │                  └─ no  → ❌ refused, nothing merged
        │
        ├─ registry    does the registry still parse?
        │                  └─ no  → ❌ refused, nothing merged
        │
        └─ automerge   does it change *only* facility records?
                           ├─ yes → merged automatically, deployment starts
                           └─ no  → left for a human to merge
```

Both `authorize` and `registry` are **required checks**, and the branch rule
requires **zero approving reviews**. The checks are the review — there is no
human in the loop for a data pull request.

## Why yours might not have merged itself

`automerge` only fires when **every** changed path looks exactly like
`federation/<facility>/<name>.yaml`. Not the README in that folder, not the
`.template`, not a nested path.

So a pull request that adds a dataset *and* fixes a typo in a README will pass
both checks and then wait for a human. That is intended: code, workflows and
the access list are administrator-authored by definition, and merging them
automatically would deploy a change to the gate unreviewed — and would merge the
first commit the moment it went green, which makes ordinary iterative work
impossible.

It also will not fire on a **draft** pull request. Mark it ready for review.

## After the merge

The deployment runs on EMSO's own server, and does three things: fast-forward
to your commit, re-check the registry, then validate and federate.

Read the **Federation status** summary at the bottom of the run. A run
reporting blocked or unreachable datasets is *not* a failed run — that is the
normal state of a federation of independent servers, and only an infrastructure
failure turns the run red.

To trigger a run yourself without changing anything, see
["Re-running the gate by hand"](../../README.md#re-running-the-gate-by-hand).

---

# Part 2 — for maintainers

## `pr-gate.yml` — the lock

### Why `pull_request_target`

On a `pull_request` event GitHub runs the workflow file **from the pull
request**. A contributor with write access could edit this file in their own
branch to neuter the authorization step, and the check would go green.
`pull_request_target` always runs the copy on the base branch, which only an
administrator can change — so the rule cannot be edited by the change it is
judging.

That is the whole reason this is the root of trust, and it dictates everything
below.

### The cost, and how it is paid

`pull_request_target` grants a privileged token, and this repository's default
workflow permission is `write`. Three separate mitigations:

| Job | Permissions | Checks out | Executes PR code |
|---|---|---|---|
| `authorize` | workflow-level `contents: read`, `pull-requests: read` | `github.base_ref` | no |
| `registry` | `contents: read` | `pull_request.head.sha` | **yes** |
| `automerge` | `contents: write`, `pull-requests: write`, `actions: write` | **nothing** | no |

The workflow-level permission is pinned read-only. `registry` is the only job
that executes the contributor's code and it holds the narrowest token available
and no secrets — and it is ordered *after* `authorize`, so an unauthorized
change is never even parsed. `automerge` holds write permissions but checks
nothing out and executes no repository code; it only calls the API.

Neither of the first two runs on the self-hosted runner, deliberately: an
unmerged pull request must never execute on the host that serves the central
ERDDAP.

### `authorize`

Covered in full in [`../access/README.md`](../access/README.md). Its other
output is `data_only`, which gates `automerge`.

That flag is computed by **counting** matches rather than with `grep -qv`:

```bash
total=$(wc -l < changed.txt)
records=$(grep -cE '^federation/[^/]+/[^/]+\.yaml$' changed.txt || true)
```

Whether `-q` and `-v` together report "a line failed to match" is not
consistent across grep implementations — ugrep returns 1 where GNU grep returns
0 — and silently inverting this test would auto-merge code. A count compared
against a line count cannot be read two ways. It is also recorded *before* the
authorization verdict, because that step exits non-zero when it denies and a
later `echo` would never run.

### `registry`

Runs `gate.py check` on the pull request's own code: YAML syntax, unknown keys,
illegal datasetIDs, and duplicates across **every** facility, not just the one
touched.

The gate builds the federation all-or-nothing, so one malformed record means no
`datasets.xml` is written at all. Catching it here is the difference between a
red check on one pull request and the whole federation serving a stale file.

Its dependency list is deliberately **not** `requirements.txt`:

```yaml
run: pip install --quiet 'psycopg2-binary>=2.9,<3' 'PyYAML>=6.0' 'rich>=13.0'
```

`requirements.txt` pins `emso-metadata-harmonizer`, which needs compiled netCDF
and UDUNITS and is the reason the gate runs in a container on the deployment
host. `gate.py check` never reaches it — the engine is imported *inside* the
scoring functions, not at module level. Those three are what `validation_gate`
imports on the way in.

> If `gate.py check` ever fails here with `ModuleNotFoundError`, something grew
> a module-level import and this list needs the new name. Do not "fix" it by
> installing `requirements.txt`.

### `automerge`

```yaml
if: needs.authorize.outputs.data_only == 'true'
    && github.event.pull_request.draft == false
```

The ruleset remains the authority. `GITHUB_TOKEN` is in no bypass list, so
`gh pr merge` succeeds only while `authorize` and `registry` are green on the
head commit — this job cannot merge anything the rules would refuse. It merges
directly rather than arming GitHub's auto-merge because it already knows both
checks passed: it needed them.

Two implementation details that are load-bearing:

**Every `gh` call names the repository with `-R "$REPO"`.** The job has no
checkout step, so there is no git remote for `gh` to infer the repository from.
Without it, every call fails with `fatal: not a git repository`. Do not "fix"
that by adding `actions/checkout` — checking out a pull request under
`pull_request_target` is precisely what the job structure avoids.

**The post-merge check reads `--json state`, not `--json merged`.** There is no
`merged` field on `gh pr view`; asking for one is itself an error, which would
make the safety net fail on every attempt.

The retry loop exists because GitHub can take a moment to publish the
conclusions the ruleset reads, so an early attempt may be refused for a merge
that is valid seconds later. Five attempts, ten seconds apart, then a hard
failure that says nothing was merged and nothing deployed.

**Then it dispatches the deployment explicitly:**

```bash
gh workflow run validate.yml -R "$REPO" --ref main -f mode=new -f trigger=push
```

This is not optional. A push made with `GITHUB_TOKEN` raises **no push event**,
so `validate.yml` would never run and the federation would quietly stop updating
while every pull request went green. `workflow_dispatch` is one of the two
documented exceptions to that rule.

`mode=new` is what a human push used to do: score what this merge added, not
the whole federation. `trigger=push` keeps the runs table honest — this is a push to `main`,
performed by the gate rather than by a person.

### What stays manual, permanently

A pull request touching the workflows, the gate code or `access.yaml` sets
`data_only=false` and will not self-merge. This includes changes to the gate's
own automation, which cannot therefore deploy themselves unreviewed.

## `validate.yml` — the deployment

### Triggers

| Event | mode | trigger |
|---|---|---|
| push to `main` | `new` | `push` |
| `workflow_dispatch` | the chosen input (default `pending`) | the chosen input (default `manual`) |

`paths-ignore` covers `**.md` and `LICENSE`. A run is not free — it takes the
advisory lock, regenerates `datasets.xml` and reloads the central ERDDAP — and
prose cannot change what the gate would decide. Every path that *can* change a
decision (federation records, `validation-gate.yaml`, `access.yaml`, the
workflows) still triggers a run, and `workflow_dispatch` is unaffected by path
filters.

The `mode`/`trigger` inputs exist for one caller: the `automerge` job. Leave
both at their defaults when running by hand.

### Where it runs

`runs-on: [self-hosted, validation-gate]`, `working-directory: $GATE_DEPLOYMENT`
(default `/opt/gate`, overridable with the `GATE_DEPLOYMENT` repository
variable).

Everything runs **in the deployment**, not in the runner's throwaway workspace,
so there is no `actions/checkout`. The gate writes `datasets.xml` where the
central ERDDAP reads it and keeps the ~115 MB compliance cache warm between
runs; a copy in the workspace would do neither.

### Steps

1. **Pull the latest commit.** Refuses to run on any ref but `refs/heads/main`
   — the manual button can be fired from any branch, and the fast-forward would
   otherwise deploy that branch to the live node. Then `git merge --ff-only`,
   which fails loudly if the deployment has diverged instead of discarding local
   commits. It also checks `venv/bin/python` exists and says how to create it.
2. **Enforce access rules** (`push` events only) — the backstop described in
   [`../access/README.md`](../access/README.md).
3. **Validate the registry** — `gate.py check`, offline, seconds.
4. **Validate, record and federate** — `gate.py run "$MODE" --trigger "$TRIGGER"`.
5. **Federation summary** — `gate.py summary` into the step summary, always.

### Exit codes are policy

```
0  every checked dataset is healthy      → ::notice
1  completed, some blocked/unreachable   → ::warning, job still green
2  another instance holds the lock       → ::error, job fails
```

Exit 1 means "the run completed and something is not healthy", which is the
normal state of a federation of third-party servers, not a build failure. Only
exit 2 and above fail the job. This is why the summary counts are what a
facility actually reads after pressing the button.

### Concurrency and timeouts

`concurrency: validation-gate` with `cancel-in-progress: false`. Two concurrent
runs would race on `datasets.xml` and the loser would deploy a stale file. The
real serialisation is the gate's own PostgreSQL advisory lock, which covers
anything running on the host including a manual `./gate.py run`; the concurrency
group just avoids queueing runs that would exit 2 on arrival.

`timeout-minutes: 120`, because `run pending` can probe every declared dataset
on third-party servers. Without it, one hung HTTP request holds the advisory
lock and every later run exits 2 until somebody notices.

### No database secret

There is no `GATE_DATABASE_URL` in this workflow. The runner is the database
host, so credentials come from `$GATE_DEPLOYMENT/.env`, which is where
`gate.py password-rotate` writes them. A repository secret would be a second
copy of the same password that rotation cannot reach, and it would drift
silently the first time the password changed.

## Changing a workflow

Both files are judged by the gate itself: a workflow change is not
`data_only`, so it goes through review and is merged by hand. Because
`pull_request_target` runs the **base branch** copy, a change to `pr-gate.yml`
is *not* exercised by its own pull request — it takes effect only after the
merge. Reopening an older pull request is the cheapest way to exercise the new
version, since `pull_request_target` fires on `opened`, `synchronize`,
`reopened` and `ready_for_review`, but **not** when the base branch moves.
