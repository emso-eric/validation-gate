#!/usr/bin/env python3
"""
Check a push against ``access.yaml``.

    python3 .github/access/manage_access.py check --author X --changed-files F
    python3 .github/access/manage_access.py show

Why this exists
---------------
GitHub has no per-folder *write* permission. Write access is repository-wide,
so a data manager who may edit ``federation/Azores/`` can also, as far as the
API is concerned, edit ``gate.py``. Two mechanisms close that, and they are
layered on purpose:

  push rulesets   refuse the push itself, server-side, on a private or
                  internal repository. This is the enforcement. One ruleset
                  per facility, restricting that facility's folder and
                  bypassed by that facility's team - see README.md. Configured
                  in the web UI; nothing here writes them.
  this script     runs in CI after the push has landed and fails the job, so
                  the gate never runs and nothing reaches the central ERDDAP.
                  A brake rather than a lock, and deliberately kept: it is the
                  only part of the access rules that is version-controlled,
                  diffable and testable, so it catches a ruleset that was
                  disabled, mis-scoped or never created for a new facility.

There is no CODEOWNERS and no pull-request path: access is controlled by
filtering pushes, not by review.

access.yaml
-----------
Arbitrary group names, each with ``folders`` and ``users``. A user may be in
several groups; a group may own several folders::

    administrators:
      folders: ["*"]
      users: [EnocMartinez]

    Balearic_Sea:
      folders: ["federation/Balearic_Sea"]
      users: [enoc-martinez-emso]

``users`` are GitHub usernames. ``folders`` are repository-relative paths, or
``"*"`` for everything.

author: Enoc Martinez
institution: Universitat Politecnica de Catalunya (UPC)
email: enoc.martinez@upc.edu
license: MIT
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ACCESS_FILE = os.path.join(HERE, "access.yaml")


class AccessError(Exception):
    """access.yaml cannot be interpreted. Never guess about permissions."""


# ---------------------------------------------------------------------------
def load(path: str = ACCESS_FILE) -> dict[str, dict]:
    if not os.path.isfile(path):
        raise AccessError(f"no access file at {path}")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise AccessError(f"{path}: expected a mapping of group name to group")

    groups: dict[str, dict] = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise AccessError(f"{path}: group '{name}' must be a mapping")
        # Both 'folder' and 'folders' appear in the wild. Accepting one and
        # silently ignoring the other would hand out access nobody granted.
        folders = body.get("folders", body.get("folder"))
        if isinstance(folders, str):
            folders = [folders]
        users = body.get("users") or []
        if not folders:
            raise AccessError(f"{path}: group '{name}' has no folders")
        if not isinstance(users, list) or not all(isinstance(u, str) for u in users):
            raise AccessError(f"{path}: group '{name}' users must be a list of names")
        folders = [str(f) for f in folders]
        groups[str(name)] = {
            "folders": [f.strip("/") or "*" for f in folders],
            "users": [str(u).lstrip("@") for u in users],
            "admin": "*" in folders,
        }
    if not groups:
        raise AccessError(f"{path}: no groups defined")
    return groups


def owners_of(path: str, groups: dict[str, dict]) -> set[str]:
    """Every user allowed to change *path*."""
    allowed: set[str] = set()
    normalised = path.strip("/")
    for group in groups.values():
        for folder in group["folders"]:
            if folder == "*":
                allowed.update(group["users"])
            elif normalised == folder or normalised.startswith(folder + "/"):
                allowed.update(group["users"])
            elif fnmatch.fnmatch(normalised, folder):
                allowed.update(group["users"])
    return allowed


# ---------------------------------------------------------------------------
def cmd_check(args: argparse.Namespace) -> int:
    """Fail when the author does not own every changed path."""
    groups = load(args.access_file)

    if args.changed_files:
        with open(args.changed_files, encoding="utf-8") as f:
            changed = [line.strip() for line in f if line.strip()]
    else:
        changed = list(args.paths)

    if not changed:
        print("no changed files, nothing to check")
        return 0

    author = args.author.lstrip("@")
    denied = [p for p in changed if author not in owners_of(p, groups)]

    for path in changed:
        print(f"  {'DENIED ' if path in denied else 'ok     '} {path}")

    if denied:
        print(
            f"\n@{author} is not listed for {len(denied)} changed path(s) in "
            f"access.yaml.\nNothing was federated: the commit is in the branch "
            f"but the gate did not run.\nEither revert it, or have an "
            f"administrator add @{author} to the owning group.",
            file=sys.stderr,
        )
        return 1
    print(f"\n@{author} owns every changed path")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    groups = load(args.access_file)
    for name, group in groups.items():
        scope = "everything" if group["admin"] else ", ".join(group["folders"])
        print(f"{name}: {scope}")
        for user in sorted(group["users"]):
            print(f"    @{user}")
    return 0


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", default=ACCESS_FILE)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="fail if the author does not own a path")
    check.add_argument("--author", required=True)
    check.add_argument("--changed-files", default=None,
                       help="file with one changed path per line")
    check.add_argument("paths", nargs="*", default=[])

    sub.add_parser("show", help="print the effective access map")

    args = parser.parse_args(argv)
    commands = {"check": cmd_check, "show": cmd_show}
    try:
        return commands[args.command](args)
    except AccessError as exc:
        print(f"access configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
