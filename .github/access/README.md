# Who may change what

GitHub write access is **repository-wide**. There is no per-folder write
permission: as far as the GitHub API is concerned, anyone who can edit
`federation/Azores/` can also edit `gate.py`. This directory is how the gate
closes that gap.

---

# Part 1 — for data managers

## The rule

**You may change your own facility's folder. Nothing else.** Administrators may
change everything.

If a pull request touches a path you do not own, the `authorize` check fails,
nothing is merged, and nothing is federated.

To see who owns what:

```bash
python3 .github/access/manage_access.py show
```

```
administrators: everything
    @EnocMartinez
Balearic_Sea: federation/Balearic_Sea
    @enoc-martinez-emso
    @luna-garcia
```

## Getting access

Ask an administrator to add your GitHub username to your facility's group in
[`access.yaml`](access.yaml).

Nothing in this repository can grant you access, deliberately — a tool that can
grant access is a tool a commit could use to grant itself access. It is always
two manual steps: the group in `access.yaml`, and the GitHub team membership.

## Why your pull request was refused

**"@you is not listed for N changed path(s)"** — the pull request touches
something outside your folder. Often this is accidental: an editor rewriting
line endings, a stray `.gitignore` edit, or a file moved *out* of your folder
(which counts as a change to your folder *and* to wherever it landed).

Drop those paths, or ask an administrator to make the change.

**"commit ... has an author email that is not linked to any GitHub account"** —
this is the one that catches people out.

The check does not read the name on your commit; it asks GitHub which *account*
your commit's email address belongs to. A commit made with
`you@laptop.local`, or with an institutional address you never added to GitHub,
resolves to no account at all. There is then no username to check against
`access.yaml`, and the gate refuses rather than guessing.

Two fixes, either is fine:

```bash
# tell GitHub about the address you already commit with
#   -> github.com/settings/emails, then re-run the check

# or commit with an address GitHub already knows
git config user.email "the-address-on-your-github-account"
git commit --amend --reset-author
git push --force-with-lease
```

**Merge commits are not judged.** Pressing "Update branch" creates a merge
commit authored by whoever pressed it, carrying the base branch's files. Judging
it would deny pull requests for paths their author never touched, so commits
with two or more parents are skipped.

---

# Part 2 — for maintainers

## `access.yaml`

Arbitrary group names, each with `folders` and `users`. A user may be in
several groups; a group may own several folders. `folders: ["*"]` is the whole
repository.

```yaml
administrators:
  folders: ["*"]
  users:
    - EnocMartinez

Balearic_Sea:
  folders: ["federation/Balearic_Sea"]
  users:
    - enoc-martinez-emso
    - luna-garcia
```

The file is **committed**, because CI cannot read a file that is not. It holds
GitHub usernames only — no email addresses. Usernames are already visible to
anyone reading the commit history, so there is nothing here that needs hiding;
an email address would be.

Note that the group owns the **facility directory**, not the service file. The
`federation/` layout mirrors the `emso-erddap` facility directories, and OBSEA
is the ERDDAP server inside `Balearic_Sea`, not a facility of its own.

### Loader details

`manage_access.py` refuses to guess about permissions. It raises `AccessError`
rather than defaulting when anything is ambiguous:

- Both `folder` and `folders` are accepted. Accepting one and silently ignoring
  the other would hand out access nobody granted.
- A group with no folders, or a `users` value that is not a list of strings, is
  an error, not an empty group.
- A leading `@` on a username is stripped, so both spellings work.

`owners_of(path)` matches three ways: exact, directory prefix (`folder + "/"`),
and `fnmatch` glob. The prefix rule is what makes `federation/Balearic_Sea`
cover everything beneath it.

## Where it is enforced

There is exactly one lock and one backstop.

### The lock: `authorize` in `pr-gate.yml`

`main` is protected by the **`require-pr`** ruleset, which targets the default
branch and carries four rules:

| Rule | Setting |
|---|---|
| `pull_request` | required, **0 approving reviews** |
| `required_status_checks` | `authorize`, `registry` |
| `non_fast_forward` | no force-pushes |
| `deletion` | the branch cannot be deleted |

Zero required approvals is what makes the hands-off path possible: the *checks*
are the review. Nothing reaches `main` except through a pull request whose
`authorize` and `registry` checks passed.

The job runs on `pull_request_target`, so GitHub always executes the copy of
the workflow **on the base branch** — a contributor cannot edit the rule in
their own branch to neuter the check that judges it. It also checks out
`github.base_ref` explicitly before reading `access.yaml` and
`manage_access.py`, for the same reason.

What it does, in order:

1. Lists every changed path via `repos/{repo}/pulls/{pr}/files`, **including
   `previous_filename`** — a rename is two paths, and moving a file *out* of a
   facility folder is a change to that folder.
2. Refuses a diff of 3000 or more paths. That is where the API stops
   enumerating, and authorization cannot be decided on a truncated list.
3. Refuses an empty diff, rather than reporting success for nothing.
4. Records `data_only` — whether every path matches
   `^federation/[^/]+/[^/]+\.yaml$`. This is what gates auto-merge; see
   [`.github/workflows/README.md`](../workflows/README.md).
5. Resolves every commit's author to a GitHub login, skipping merge commits and
   failing closed on an unresolvable address.
6. Requires **every author to own every changed path** — not merely their own
   commits.

That last point is deliberate. A facility pull request touches one folder that
all of that facility's members own, so it costs nothing in practice, and it
closes the case where two authors each own half a diff and neither is allowed
the whole of it.

### The backstop: `Enforce access rules` in `validate.yml`

Runs after the push has landed, on the self-hosted runner, only for `push`
events. It cannot reject anything — GitHub Actions never can — so it is a brake,
not a lock: the job fails there and `gate.py run` never executes, so an
unauthorised edit never reaches the central ERDDAP. The commit stays in the
branch for a human to revert.

It is kept because it is the only part of the access rules that is
version-controlled, diffable, testable offline and fails closed. Branch
protection lives in GitHub's settings, where nothing in this repository can
diff, test or roll it back; a ruleset that was disabled, mis-scoped or never
applied to a new branch pattern fails silently and open.

**Known false failure.** This step judges `github.actor` — the account that
pushed — which on a merge is whoever pressed the button rather than the author
of the work. A facility member merging an administrator's code pull request
therefore fails here, *after* `pr-gate.yml` already authorized it correctly.
Nothing is federated and nothing escapes; an administrator can re-run it with
`workflow_dispatch`. The fix is to judge the pushed commits' authors, as
`pr-gate.yml` does.

## Why there are no push rulesets

An earlier design used one push ruleset per facility, restricting that
facility's folder and bypassed by that facility's team, to refuse the push
server-side.

**That is not available here.** GitHub's push rulesets
[apply only to private and internal repositories](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
and their fork networks. `emso-eric/validation-gate` is public, so path-scoped
push rules cannot be created for it at all.

The enforcement is therefore the required check in `pr-gate.yml`, which refuses
the *merge* rather than the push. In practice this is stronger for this
repository than the push ruleset would have been, because the rule being
enforced is itself version-controlled rather than living in the web UI.

## Testing a change

`manage_access.py check` is offline and takes a path list, so a rule change can
be verified before it is committed:

```bash
printf 'federation/Balearic_Sea/OBSEA.yaml\n' > /tmp/changed.txt
python3 .github/access/manage_access.py check \
    --author luna-garcia --changed-files /tmp/changed.txt   # exit 0

printf 'gate.py\n' > /tmp/changed.txt
python3 .github/access/manage_access.py check \
    --author luna-garcia --changed-files /tmp/changed.txt   # exit 1
```

Paths can also be passed as positional arguments instead of `--changed-files`.
Exit codes: `0` authorized, `1` denied, `2` `access.yaml` could not be
interpreted.

## Adding a facility

1. Add the group to `access.yaml` with `folders: ["federation/<Facility>"]`.
2. Create the GitHub team and add its members.
3. Verify with `manage_access.py check` before merging.

Nothing reconciles `access.yaml` against GitHub team membership — they are two
separate manual steps, and a user removed from the team but left in
`access.yaml` still passes `authorize` while being unable to push. Keep them in
step by hand.
