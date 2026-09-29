# Per-folder write access

GitHub has no per-folder write permission. Write access is repository-wide: an
account that may edit `federation/Balearic_Sea/` can, as far as the API is
concerned, also edit `gate.py` and `.github/workflows/`. Two mechanisms get us
the rest of the way, and they are not equivalent.

**Access is controlled by filtering pushes.** There is no `CODEOWNERS`, no
pull-request requirement and no review step: a facility pushes to `main` and is
either allowed to or is not. Nothing here waits for a human.

`access.yaml` is the single source of truth for both mechanisms below - but
only the second one reads it. The ruleset is configured in the GitHub UI and
nothing reconciles the two automatically, so the table in §1 and `access.yaml`
have to be edited together.

## 1. Push ruleset - refuses the push

The only thing that stops an unauthorised commit from ever entering the
repository. Configure it in the web UI:

**Repo → Settings → Rules → Rulesets → New ruleset → New push ruleset**

Rules from multiple rulesets are aggregated with the most restrictive winning,
so the obvious layout does *not* compose: one ruleset restricting `**/*` with
`federation/Balearic_Sea/**` as an allowed exception cannot be joined by a
second one for Azores. The second one's exception does not re-open the first
one's restriction, so both facilities end up blocked.

Invert it. Bypass permission is granted **per ruleset** and can name a team, so
restrict each facility's own folder and let that facility bypass its own rule:

| ruleset | restricted file paths | bypass list |
| --- | --- | --- |
| `core` | `**/*`, allowed exceptions = one `federation/<facility>/**` per facility | Repository admin |
| `facility-Balearic_Sea` | `federation/Balearic_Sea/**` | Repository admin, team `emso-balearic-sea` |
| `facility-Azores` | `federation/Azores/**` | Repository admin, team `emso-azores` |
| … one per facility | | |

All with **Enforcement status: Active**. A Balearic Sea member pushing into
their own folder clears `core` through the exceptions and bypasses
`facility-Balearic_Sea`; pushing into `federation/Azores/` they are stopped by
`facility-Azores`; pushing `gate.py` they are stopped by `core`.

Five things to know before relying on it:

- **The organization must be on GitHub Team or Enterprise.** This is the
  binding constraint today: `emso-eric` is on GitHub Free, where private
  repositories get a limited feature set and rulesets are *not enforced*. The
  UI still lets you create them and shows "Your rulesets won't be enforced on
  this private repository until you upgrade this organization account to
  GitHub Team", so a ruleset on Free is decoration. Until the plan changes,
  §2 is the only mechanism actually doing anything.
- **Private or internal repositories only.** Push rulesets are not available on
  public repositories. Note the trap: going public to escape the plan limit
  does not work, because it removes push rulesets altogether. Public plus Free
  gets you *branch* rulesets and no path filtering at all.
- **Bypass entries are roles, teams and apps, not individual users.** A
  one-person facility still needs a one-person team, and teams mean an
  organization repository.
- **75 rulesets per repository.** Fourteen is comfortable.
- **List the facility folders explicitly in `core`** rather than writing
  `federation/**`. Otherwise anyone with write access can create
  `federation/NewPlace/` unopposed, because no ruleset restricts it.

Keep this table and `access.yaml` in step: the ruleset is what enforces, the
YAML is what CI reads, and nothing reconciles them automatically. A facility
that gets a ruleset but no group in `access.yaml` is blocked by CI; one that
gets a group but no ruleset is not blocked at push time at all.

## 2. The access step in `validate.yml` - refuses to federate

Runs on every push, after it has landed. Compares `github.actor` against
`access.yaml` for every path in `github.event.before..github.sha`, and fails the
job if any path is not owned. `gate.py run` is a later step in the same job, so
a failure here means nothing is validated and nothing reaches the central
ERDDAP. The commit stays in the branch for a human to revert.

It is a brake, not a lock, and the difference matters:

- The commit is in the branch. Anyone cloning gets it.
- It trusts the workflow in the commit being pushed. An account with write
  access can, in the same push, edit `.github/workflows/validate.yml` to remove
  the check. `access.yaml` denies `.github/**` to non-admins, so that push is
  reported - but it is reported by the very file the push may have changed.
  Only (1) closes this.

So why keep it, when (1) is strictly stronger? Because (1) lives in the GitHub
UI and nothing in this repository can see it. The rulesets cannot be diffed,
reviewed, tested or rolled back with the code they protect, and a ruleset that
was disabled, mis-scoped, or simply never created for a new facility fails
silently and open. This step is the version-controlled half: it is testable
offline (`manage_access.py check`), it fails closed, and it turns that silent
gap into a red CI run.

## Commands

```bash
python3 .github/access/manage_access.py show          # effective access map
python3 .github/access/manage_access.py check --author <user> <paths...>
python3 .github/access/manage_access.py check --author <user> --changed-files <file>
```

Two subcommands, and that is the whole script. It reads `access.yaml` and
writes nothing - no generated files, no GitHub API calls, no token. A tool that
can grant access is a tool a commit can use to grant itself access, so
membership is changed in the GitHub UI and mirrored here by hand.

`check` takes paths as arguments or, as CI does, one per line in the file named
by `--changed-files`. An unknown user is denied, so it fails closed.

`show` prints the effective map, which is the quickest way to confirm a new
facility group does what you meant before you push it.
