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
import sys

from validation_gate import (
    Config, ConfigError, DatabaseError, ErddapError, RegistryError,
    ValidationGate, __version__,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ERROR = 2

LOG_LEVELS = ("debug", "info", "warning", "error")

logger = logging.getLogger("gate")


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
