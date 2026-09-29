#!/usr/bin/env python3
"""
The EMSO Validation Gate command line.

    ./gate.py run new         validate newly declared datasets only (CI default)
    ./gate.py run pending     everything not currently healthy, new ones too
    ./gate.py run all         the whole federation (nightly / forced rebuild)

    ./gate.py check           validate the registry and print it. No database.
    ./gate.py summary         per-facility counts by status
    ./gate.py status          full report: summary, attention, regressions, runs
    ./gate.py list blocked    list datasets by status, grouped by facility
    ./gate.py init-db         create the schema and exit

    ./gate.py autodeploy      configure a fresh checkout: .env files + secrets
    ./gate.py password-rotate new secrets in .env, applied to the services

Exit codes, because CI reads them:

    0   the run completed and every checked dataset is healthy
    1   the run completed; at least one dataset is blocked or unreachable
    2   the gate could not run (config error, DB error, or another instance
        is already running)

author: Enoc Martinez
institution: Universitat Politecnica de Catalunya (UPC)
email: enoc.martinez@upc.edu
license: MIT
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime

from validation_gate import (
    Config, ConfigError, DatabaseError, ErddapError, ErddapHelper,
    RegistryError, ValidationGate, __version__,
)
from validation_gate.config import DATABASE_ENV_FILE, read_env_file

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ERROR = 2

LOG_LEVELS = ("debug", "info", "warning", "error")

logger = logging.getLogger("gate")

input("THIS SHOULD NOT HAPPEN")

def setup_logging(level: str, log_dir: str | None = None) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)

    # Logs go to stderr, data (the rich tables) to stdout. That split is also
    # what lets the compliance pass silence the engine's own prints without
    # silencing the gate's progress: see ValidationGate._validate_all.
    handlers: list[logging.Handler] = []
    try:
        from rich.console import Console
        from rich.logging import RichHandler
        handlers.append(RichHandler(
            console=Console(stderr=True),
            rich_tracebacks=True,
            show_path=numeric <= logging.DEBUG,
            show_time=True,
            omit_repeated_times=False,
            log_time_format="[%Y-%m-%d %H:%M:%S]",
        ))
    except ImportError:
        handlers.append(logging.StreamHandler(stream=sys.stderr))

    file_error = ""
    if log_dir:
        # A log file that cannot be opened must not stop the gate: the
        # directory may be read-only, or owned by the container that ran last.
        try:
            os.makedirs(log_dir, exist_ok=True)
            from logging.handlers import TimedRotatingFileHandler
            file_handler = TimedRotatingFileHandler(
                os.path.join(log_dir, "gate.log"),
                when="midnight", interval=1, backupCount=30,
            )
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            handlers.append(file_handler)
        except OSError as exc:
            file_error = f"file logging disabled: {exc}"

    # Root at WARNING so bare logging.info() calls from third-party code do not
    # reach the screen; the gate logger gets its own explicit level.
    logging.basicConfig(level=logging.WARNING, format="%(message)s",
                        datefmt="%H:%M:%S", handlers=handlers, force=True)
    logger.setLevel(numeric)

    # The engine's own output is captured in the JSON reports. Showing it too
    # would bury the gate's log on a 208-dataset run.
    logging.getLogger("emso_metadata_harmonizer").setLevel(logging.CRITICAL)
    for noisy in ("urllib3", "requests", "matplotlib", "rdflib", "erddapy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if file_error:
        logger.warning(file_error)


# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    with ValidationGate(cfg) as gate:
        return gate.run_validation(how=args.how, trigger=args.trigger)


def cmd_check(args: argparse.Namespace, cfg: Config) -> int:
    with ValidationGate(cfg) as gate:
        return gate.check()


def cmd_summary(args: argparse.Namespace, cfg: Config) -> int:
    with ValidationGate(cfg) as gate:
        return gate.summary()


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    with ValidationGate(cfg) as gate:
        return gate.status()


def cmd_init_db(args: argparse.Namespace, cfg: Config) -> int:
    with ValidationGate(cfg) as gate:
        gate.connect()
    print("schema is up to date")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Every secret in the root .env, and what has to happen for a new value to take
# effect. Editing the file is never enough on its own:
#
#   POSTGRES_PASSWORD           the postgres image reads this at initdb only, so
#                               an existing cluster keeps the old password until
#                               ALTER ROLE changes it. Done here.
#   GF_SECURITY_ADMIN_PASSWORD  Grafana copies this into its own database on
#                               first start and ignores it afterwards. Reset
#                               here with grafana-cli.
#   ERDDAP_flagKeyKey           read once at startup, so it applies on the next
#                               `docker compose up -d erddap`. Every flag URL
#                               issued under the old key stops working.
ROTATED_SECRETS = (
    "POSTGRES_PASSWORD",
    "GF_SECURITY_ADMIN_PASSWORD",
    "ERDDAP_flagKeyKey",
)

GRAFANA_CONTAINER = "emso-gate-grafana"


def _new_secret() -> str:
    # token_urlsafe: A-Za-z0-9-_ only, so it needs no quoting in a .env file, no
    # escaping in a URL and no special handling in a shell.
    return secrets.token_urlsafe(24)


def _rewrite_env(path: str, updates: dict[str, str]) -> list[str]:
    """
    Replace values in place, keeping every comment and the original order.

    Returns the names that were not found, so the caller can report rather than
    silently lose a rotated secret. Written via a temporary file and os.replace,
    so an interrupted rotation cannot leave a half-written .env.
    """
    lines: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line)
            if match and match.group(1) in updates:
                name = match.group(1)
                lines.append(f"{name}={updates[name]}\n")
                seen.add(name)
            else:
                lines.append(line)

    missing = [name for name in updates if name not in seen]
    if missing:
        lines.append("\n# Added by `gate.py password-rotate`.\n")
        lines.extend(f"{name}={updates[name]}\n" for name in missing)

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return missing


# ---------------------------------------------------------------------------
# Everything a deployment needs before `docker compose` will even parse its own
# file. Compose fails with "env file ... not found" on a missing env_file, so
# all four must exist - not just the one that carries secrets.
DEPLOYMENT_FILES = (
    (".env.template", ".env"),
    ("database/.env.template", "database/.env"),
    ("erddap/.env.template", "erddap/.env"),
    ("grafana/.env.template", "grafana/.env"),
)

COMPOSE_FILE = "docker-compose.yaml"


def _compose_bind_directories(root: str) -> tuple[list[str], list[str]]:
    """
    Directory bind-mount sources declared in docker-compose.yaml.

    Docker creates a missing bind-mount source itself, as root and as a
    *directory* - so a first `docker compose up` on a fresh checkout leaves
    root-owned directories in the working tree, and where a file was expected
    (datasets.xml) a directory that ERDDAP cannot read. Creating them here, from
    the process that already runs as the right user, avoids both.

    Returns (directories, warnings). A source that already exists is left alone.
    A source that looks like a file - anything with an extension - is not
    created: datasets.xml is written separately, and logo.png and favicon.ico
    are in the repository.
    """
    path = os.path.join(root, COMPOSE_FILE)
    if not os.path.isfile(path):
        return [], [f"no {COMPOSE_FILE}; no directories created"]

    import yaml
    with open(path, encoding="utf-8") as f:
        compose = yaml.safe_load(f) or {}

    found: list[str] = []
    warnings: list[str] = []
    mounts: list[tuple[str, str]] = []      # (host source, container target)

    for service in (compose.get("services") or {}).values():
        for volume in (service or {}).get("volumes") or []:
            if isinstance(volume, dict):
                if volume.get("type") not in (None, "bind"):
                    continue
                source = volume.get("source") or ""
                target = volume.get("target") or ""
            else:
                parts = str(volume).split(":")
                source = parts[0]
                target = parts[1] if len(parts) > 1 else ""

            # Named volumes are not paths, and '.' is the repository itself.
            if not source.startswith((".", "/")):
                continue
            if "$" in source:
                warnings.append(f"{source}: not created, it is interpolated")
                continue
            if target:
                mounts.append((source, target))
            if source in (".", "./") or os.path.splitext(source)[1]:
                continue

            absolute = os.path.normpath(os.path.join(root, source))
            if absolute not in found:
                found.append(absolute)

    # A mount whose target sits inside another mount's target needs its mount
    # point to exist first. Docker creates a missing one as root even when the
    # container runs as someone else, and because the outer source is on the
    # host, that root-owned directory appears in the working tree - where it
    # cannot be removed without sudo. Pre-creating it is the whole fix.
    for outer_source, outer_target in mounts:
        if os.path.splitext(outer_source)[1]:
            continue
        for inner_source, inner_target in mounts:
            if inner_target == outer_target:
                continue
            prefix = outer_target.rstrip("/") + "/"
            if not inner_target.startswith(prefix):
                continue
            inside = inner_target[len(prefix):]
            # Only directories: a nested *file* mount point is created as an
            # empty file, which is harmless and not ours to guess at.
            if os.path.splitext(inside)[1]:
                continue
            absolute = os.path.normpath(
                os.path.join(root, outer_source, inside)
            )
            if absolute not in found:
                found.append(absolute)

    return found, warnings


def cmd_autodeploy(args: argparse.Namespace, cfg: Config) -> int:
    """
    Turn a fresh checkout into a startable one.

    Three independent things, each skipped when already done, so this is safe to
    re-run after any of them has been removed by hand:

      1. the four .env files, copied from their templates
      2. every directory docker-compose.yaml bind-mounts
      3. an empty-but-valid erddap/datasets.xml

    Only files are touched: nothing is started, and nothing existing is ever
    overwritten. Secrets are generated *into* the root .env rather than applied
    to a running service, because on a first deployment there is no server yet -
    postgres reads POSTGRES_PASSWORD when it initialises its cluster. Rotating
    later, against running services, is `password-rotate`.
    """
    root = cfg.repo_root

    missing_files = [(t, live) for t, live in DEPLOYMENT_FILES
                     if not os.path.isfile(os.path.join(root, live))]
    present_files = [live for _, live in DEPLOYMENT_FILES
                     if os.path.isfile(os.path.join(root, live))]

    directories, warnings = _compose_bind_directories(root)
    missing_dirs = [d for d in directories if not os.path.isdir(d)]

    datasets_xml = cfg.path("datasets_xml")
    needs_datasets_xml = not os.path.isfile(datasets_xml)

    if not (missing_files or missing_dirs or needs_datasets_xml):
        print("This deployment is already complete:")
        for live in present_files:
            print(f"  {live}")
        for directory in directories:
            print(f"  {os.path.relpath(directory, root)}/")
        print(f"  {os.path.relpath(datasets_xml, root)}")
        print()
        print("Nothing to do - no existing file is ever overwritten. To replace")
        print("the secrets in .env on the running services, use:")
        print()
        print("    ./gate.py password-rotate")
        return EXIT_OK

    absent = [t for t, _ in missing_files
              if not os.path.isfile(os.path.join(root, t))]
    if absent:
        logger.error("missing template(s): %s", ", ".join(absent))
        logger.error("this does not look like a complete checkout")
        return EXIT_ERROR

    # Secrets are only generated when we create the root .env ourselves. If it
    # already exists it may hold the password of a live database, and silently
    # replacing that would lock the gate out of its own history.
    fresh_root = any(live == DATABASE_ENV_FILE for _, live in missing_files)

    print("This deployment is incomplete.")
    print()
    print("Will create:")
    for _, live in missing_files:
        print(f"  {live}                    (from its template)")
    for directory in missing_dirs:
        print(f"  {os.path.relpath(directory, root)}/")
    if needs_datasets_xml:
        print(f"  {os.path.relpath(datasets_xml, root)}   (valid, no datasets)")
    if fresh_root:
        print()
        print(f"and generate {len(ROTATED_SECRETS)} random secrets in "
              f"{DATABASE_ENV_FILE}.")
    elif missing_files:
        print()
        print(f"{DATABASE_ENV_FILE} already exists, so its secrets are left "
              f"alone.")

    if not args.yes:
        if not sys.stdin.isatty():
            logger.error(
                "refusing to write without confirmation: re-run with --yes"
            )
            return EXIT_ERROR
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return EXIT_OK

    print()
    for tmpl, live in missing_files:
        shutil.copyfile(os.path.join(root, tmpl), os.path.join(root, live))
        print(f"created {live}")

    if fresh_root:
        env_path = os.path.join(root, DATABASE_ENV_FILE)
        # Only this file holds secrets, so only this one is tightened.
        os.chmod(env_path, 0o600)

        updates = {name: _new_secret() for name in ROTATED_SECRETS}
        # The host user that owns the checkout. The template guesses 1000, which
        # is wrong often enough to be worth reading for real: every container
        # runs as this uid, so it is also what every bind mount must belong to.
        if hasattr(os, "getuid"):
            updates["UID"] = str(os.getuid())
            updates["GID"] = str(os.getgid())
        _rewrite_env(env_path, updates)
        print(f"generated {len(ROTATED_SECRETS)} secrets in {DATABASE_ENV_FILE} "
              f"(mode 600)")
        if "UID" in updates:
            print(f"set UID={updates['UID']} GID={updates['GID']} from this host")

    # Created here rather than by Docker, which would make them root-owned.
    for warning in warnings:
        logger.warning("%s", warning)
    for directory in missing_dirs:
        os.makedirs(directory, exist_ok=True)
        print(f"created {os.path.relpath(directory, root)}/ "
              f"(uid {os.stat(directory).st_uid})")

    if needs_datasets_xml:
        relative = os.path.relpath(datasets_xml, root)
        try:
            ErddapHelper(cfg).build_datasets([])
            print(f"created {relative} (valid, no datasets yet)")
        except ErddapError as exc:
            # The rest is already written, so this is a warning rather than a
            # failure: the deployment is configured, just not startable yet.
            logger.warning("could not write %s: %s", relative, exc)
            logger.warning(
                "create it before `docker compose up`, or Docker will create a "
                "directory at that path"
            )

    if fresh_root:
        pgdata = os.path.join(root, "database", "pgdata")
        # An existing cluster keeps its own password, so the one just generated
        # will not authenticate against it.
        if os.path.isdir(pgdata) and os.listdir(pgdata):
            print()
            logger.warning(
                "%s already contains a cluster, which keeps its existing "
                "password - the one just generated will not authenticate "
                "against it", pgdata
            )
            logger.warning(
                "restore the previous .env, or delete %s to start over "
                "(this destroys the validation history)", pgdata
            )

    print()
    print("Next, edit .env for this host - at minimum ERDDAP_baseUrl, which")
    print("ERDDAP writes into every link and metadata record - then:")
    print()
    print("    docker compose up -d")
    print("    ./gate.py init-db")
    print("    ./gate.py run new")
    return EXIT_OK


def cmd_password_rotate(args: argparse.Namespace, cfg: Config) -> int:
    env_path = os.path.join(cfg.repo_root, DATABASE_ENV_FILE)
    if not os.path.isfile(env_path):
        logger.error("no environment file at %s", env_path)
        return EXIT_ERROR

    current = read_env_file(env_path)
    role = current.get("POSTGRES_USER")
    if not role:
        logger.error("%s: POSTGRES_USER is not set", env_path)
        return EXIT_ERROR

    if not args.yes:
        if not sys.stdin.isatty():
            logger.error(
                "refusing to rotate without confirmation: re-run with --yes"
            )
            return EXIT_ERROR
        print(f"Rotate {', '.join(ROTATED_SECRETS)} in {env_path},")
        print(f"change the password of PostgreSQL role '{role}', and reset the")
        print("Grafana admin password. Existing ERDDAP flag URLs will stop")
        print("working once ERDDAP is recreated.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return EXIT_OK

    new = {name: _new_secret() for name in ROTATED_SECRETS}

    # PostgreSQL first, using the *old* credentials from cfg. If this fails the
    # deployment is still entirely consistent, because .env is untouched.
    import psycopg2
    from psycopg2 import sql
    try:
        with psycopg2.connect(cfg.database_url) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # psycopg2 interpolates client-side, so %s becomes a correctly
                # escaped literal: ALTER ROLE does not take bind parameters.
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(
                        sql.Identifier(role)
                    ),
                    (new["POSTGRES_PASSWORD"],),
                )
    except psycopg2.Error as exc:
        logger.error("could not change the password of role '%s': %s", role, exc)
        logger.error("nothing was written; %s is unchanged", env_path)
        return EXIT_ERROR
    logger.info("PostgreSQL role '%s': password changed", role)

    # The database now disagrees with .env, so write it immediately. If this
    # fails, the new password is printed rather than lost.
    backup = f"{env_path}.bckp.{datetime.now().strftime('%Y%m%d%H%M%S')}"
    try:
        shutil.copy2(env_path, backup)
        missing = _rewrite_env(env_path, new)
    except OSError as exc:
        logger.error("could not write %s: %s", env_path, exc)
        logger.error(
            "the database password IS now '%s' - set POSTGRES_PASSWORD to it "
            "by hand before running the gate again",
            new["POSTGRES_PASSWORD"],
        )
        return EXIT_ERROR
    logger.info("backed up %s", os.path.basename(backup))
    for name in missing:
        logger.warning("%s was not in the file and has been appended", name)

    # Grafana keeps its own copy, so the new value in .env only applies to a
    # fresh grafana/data. Non-fatal: the rotation above already succeeded.
    grafana = _reset_grafana_password(
        args.grafana_container, new["GF_SECURITY_ADMIN_PASSWORD"]
    )

    print()
    print(f"Rotated {len(ROTATED_SECRETS)} secrets in {env_path}")
    print(f"  previous values: {os.path.basename(backup)}")
    print(f"  PostgreSQL role '{role}': applied")
    print(f"  Grafana admin: {'applied' if grafana else 'NOT applied - see above'}")
    print("  ERDDAP flagKeyKey: applies on the next recreate:")
    print()
    print("      docker compose up -d erddap")
    print()
    if not grafana:
        print("  Grafana keeps the old password until you run:")
        print()
        print(f"      docker exec {args.grafana_container} grafana-cli "
              f"admin reset-admin-password '<the value now in .env>'")
        print()
    return EXIT_OK


def _reset_grafana_password(container: str, password: str) -> bool:
    if not shutil.which("docker"):
        logger.warning("docker not on PATH; Grafana admin password not reset")
        return False
    try:
        result = subprocess.run(
            ["docker", "exec", container,
             "grafana-cli", "admin", "reset-admin-password", password],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not reset the Grafana admin password: %s", exc)
        return False
    if result.returncode != 0:
        # Most often: the container is not running. Worth a warning, not a
        # failure - the value is in .env and applies to a fresh grafana/data.
        logger.warning(
            "grafana-cli failed (exit %d): %s",
            result.returncode,
            (result.stderr or result.stdout).strip().splitlines()[-1:] or "",
        )
        return False
    logger.info("Grafana admin password reset in %s", container)
    return True


# ---------------------------------------------------------------------------
_STATUS_COLOR = {
    "healthy": "green",
    "blocked": "red",
    "unreachable": "red",
    "unhandled": "yellow",
}


def _word(v: bool | None) -> str:
    if v is None:
        return "-"
    return "[green]pass[/]" if v else "[red]FAIL[/]"


def _score_str(score: float | None, threshold: float | None) -> str:
    if score is None:
        return "-"
    label = f"{score:.1f}%"
    if threshold is None:
        return label
    if score >= threshold:
        return f"[green]{label}[/]"
    if score >= 0.8 * threshold:
        return f"[orange1]{label}[/]"
    return f"[red]{label}[/]"


def cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    from rich.console import Console
    from rich.table import Table

    status_filter = None if args.status == "all" else args.status
    with ValidationGate(cfg) as gate:
        rows = gate.connect().list_by_status(status_filter, args.rf)

    if not rows:
        msg = f"no {args.status} datasets"
        if args.rf:
            msg += f" in facility '{args.rf}'"
        print(msg)
        return EXIT_OK

    color = _STATUS_COLOR.get(args.status, "white")
    title = f"[{color}]{args.status.capitalize()}[/] datasets"
    if args.rf:
        title += f"  —  {args.rf}"

    table = Table(title=title, show_lines=False)
    table.add_column("facility",   no_wrap=True)
    table.add_column("url",        no_wrap=True)
    table.add_column("required %", justify="right", no_wrap=True)
    table.add_column("kw",         justify="center", no_wrap=True)
    table.add_column("op",         justify="center", no_wrap=True)

    prev_facility = None
    for row in rows:
        if prev_facility is not None and row["facility"] != prev_facility:
            table.add_section()
        prev_facility = row["facility"]
        table.add_row(
            row["facility"],
            row["source_url"],
            _score_str(row["required_score"], row["threshold"]),
            _word(row["keywords_passed"]),
            _word(row["operational_passed"]),
        )

    Console().print(table)
    n_facilities = len({r["facility"] for r in rows})
    print(f"{len(rows)} dataset(s) across {n_facilities} facilit(y/ies)")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "datasetID", "facility", "url",
                "required_score", "keywords_passed", "operational_passed",
            ])
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "datasetID":           row["dataset_id"],
                    "facility":            row["facility"],
                    "url":                 row["source_url"],
                    "required_score":      row["required_score"],
                    "keywords_passed":     row["keywords_passed"],
                    "operational_passed":  row["operational_passed"],
                })
        logger.info("wrote %s (%d rows)", args.out, len(rows))

    return EXIT_OK


COMMANDS = {
    "run": cmd_run,
    "check": cmd_check,
    "summary": cmd_summary,
    "status": cmd_status,
    "list": cmd_list,
    "init-db": cmd_init_db,
    "password-rotate": cmd_password_rotate,
    "autodeploy": cmd_autodeploy,
}


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    # Shared parent so the global flags are also accepted *after* the
    # subcommand: `gate.py run all -l debug` is what people actually type.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=".", help="repository root")
    common.add_argument("--config", default="validation-gate.yaml",
                        help="policy file, relative to --repo")
    common.add_argument("-l", "--log-level", default="info", choices=LOG_LEVELS,
                        metavar="LEVEL",
                        help="debug, info, warning or error (default: info)")
    common.add_argument("--log-dir", default="logs", metavar="DIR",
                        help="where gate.log is written; '' to disable")

    parser = argparse.ArgumentParser(
        prog="gate.py", parents=[common],
        description="EMSO Validation Gate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"EMSO Validation Gate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common],
                         help="validate, record and federate")
    run.add_argument("how", nargs="?", default="new",
                     choices=("new", "pending", "all"),
                     help="new (default): never validated. pending: everything "
                          "not healthy. all: the whole federation.")
    run.add_argument("--trigger", default="manual",
                     choices=("manual", "push", "schedule"),
                     help="recorded against the run, for the audit trail")

    deploy = sub.add_parser(
        "autodeploy", parents=[common],
        help="create the missing .env files from their templates and generate "
             "secrets")
    deploy.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation")

    rotate = sub.add_parser(
        "password-rotate", parents=[common],
        help="generate new secrets in .env and apply them to the services")
    rotate.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation")
    rotate.add_argument("--grafana-container", default=GRAFANA_CONTAINER,
                        metavar="NAME",
                        help=f"container to reset the Grafana admin password "
                             f"in (default: {GRAFANA_CONTAINER})")

    sub.add_parser("check", parents=[common],
                   help="validate the registry and print it (no database)")
    sub.add_parser("summary", parents=[common],
                   help="per-facility counts by status")
    sub.add_parser("status", parents=[common],
                   help="full federation report")
    lst = sub.add_parser("list", parents=[common],
                         help="list datasets by status, grouped by facility")
    lst.add_argument(
        "status",
        choices=("healthy", "blocked", "unreachable", "unhandled", "all"),
        help="which status bucket to show (all = every dataset)",
    )
    lst.add_argument(
        "--rf", metavar="FACILITY", default=None,
        help="restrict output to this regional facility",
    )
    lst.add_argument(
        "--out", metavar="FILE", default=None,
        help="also dump results to a CSV file",
    )

    sub.add_parser("init-db", parents=[common],
                   help="create the database schema and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = os.path.abspath(args.repo)

    log_dir = os.path.join(repo_root, args.log_dir) if args.log_dir else None
    setup_logging(args.log_level, log_dir)

    try:
        cfg = Config.load(args.config, repo_root)
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return EXIT_ERROR

    try:
        return COMMANDS[args.command](args, cfg)
    except (RegistryError, DatabaseError, ErddapError) as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return EXIT_ERROR
    except Exception as exc:
        logger.exception("the gate could not complete: %s", exc)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
