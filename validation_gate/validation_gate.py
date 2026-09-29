"""
The gate itself: validate declared datasets, record the verdicts, federate the
compliant ones.

Every check is performed inside emso-metadata-harmonizer. The gate does not
probe reachability itself - the engine already fails on an unreachable dataset,
and doing it twice meant every dataset was fetched twice.

Concurrency: init_emso_metadata() downloads the specifications and vocabularies
once, up front. After that metadata_report() only reads them, so the compliance
pass runs in a thread pool. The engine resolves its cache by the literal name
'.emso' relative to the working directory, so the gate chdirs there once before
starting the pool - every thread is then already in the right place and no
thread ever calls chdir.
"""

from __future__ import annotations

import concurrent.futures as futures
import contextlib
import io
import json
import logging
import os
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .db import GateDatabase
from .erddap import ErddapHelper
from .federation import Dataset, Federation

logger = logging.getLogger("gate")

VALIDATION_STATES = (
    "healthy",      # passed every enforced rule -> federated
    "blocked",      # reachable and scored, failed at least one rule
    "unreachable",  # HTTP error or timeout; no score exists
    "unhandled",    # the compliance engine itself broke; no score exists
)

# Exception names that mean "the server did not answer" rather than "the
# engine broke". Matched by name so the gate does not import requests, urllib3
# and httpx just to catch them.
_NETWORK_ERRORS = frozenset({
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
    "TimeoutError", "HTTPError", "SSLError", "TooManyRedirects",
    "ProxyError", "URLError", "HTTPStatusError", "ConnectError",
    "RemoteDisconnected", "IncompleteRead", "socket.timeout", "gaierror",
    "NewConnectionError", "MaxRetryError", "ProtocolError",
})


@dataclass
class ValidationReport:
    """One dataset's outcome, ready for the database."""

    dataset: Dataset
    status: str
    required_score: float | None = None
    optional_score: float | None = None
    operational_passed: bool | None = None
    keywords_passed: bool | None = None

    threshold: float | None = None
    operational_required: bool | None = None
    keywords_required: bool | None = None

    failures: list[str] = field(default_factory=list)
    error_message: str | None = None
    payload: dict | None = None

    institution: str = ""
    emso_facility: str = ""
    duration_s: float = 0.0

    @property
    def detail(self) -> str:
        """One line for the log, phrased for whoever has to fix it."""
        if self.status in ("unreachable", "unhandled"):
            message = (self.error_message or "").splitlines()
            return message[0] if message else ""
        return (
            f"required={self.required_score:.1f}% "
            f"oper={_word(self.operational_passed)} "
            f"kw={_word(self.keywords_passed)}"
        )


def shuffle_datasets(datasets: list[Dataset]) -> list[Dataset]:
    """
    Interleave datasets by server, so consecutive work hits different hosts.

    Registry order groups every dataset of a server together, which points the
    whole thread pool at one regional ERDDAP at a time - slow for the gate and
    rude to the facility. Round-robin across servers instead:

        srv1/a srv2/a srv3/a srv1/b srv2/b srv3/b ...
    """
    by_server: dict[str, list[Dataset]] = defaultdict(list)
    for dataset in datasets:
        by_server[dataset.server].append(dataset)

    queues = list(by_server.values())
    interleaved: list[Dataset] = []
    for index in range(max((len(q) for q in queues), default=0)):
        for queue in queues:
            if index < len(queue):
                interleaved.append(queue[index])
    return interleaved


class ValidationGate:
    """One gate run, end to end."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.db: GateDatabase | None = None
        self._engine_info: dict = {}
        # Scratch space for the engine's JSON reports, removed on close() so an
        # aborted run cannot leave stray files in the repository.
        self._workdir = tempfile.TemporaryDirectory(prefix="emso-gate-")

    def close(self) -> None:
        self._workdir.cleanup()
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self) -> "ValidationGate":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> GateDatabase:
        """Open the database and apply the schema. Idempotent."""
        if self.db is None:
            self.db = GateDatabase(self.cfg.database_url, self.cfg.repo_root)
            self.db.init_db()
        return self.db

    # ------------------------------------------------------------------
    # gate.py run
    # ------------------------------------------------------------------
    def run_validation(self, how: str = "all", trigger: str = "manual") -> int:
        """
        Validate, record, and rebuild the federation.

        Exit codes:
            0  the run completed and every checked dataset is healthy
            1  the run completed; at least one dataset is blocked/unreachable
            2  the gate could not start (another instance holds the lock)
        """
        db = self.connect()

        if not db.try_acquire_run_lock():
            logger.error(
                "another gate instance is already running "
                "(PostgreSQL advisory lock 0x454D534F is held); "
                "only one instance may run at a time"
            )
            return 2

        db.cleanup_stale_runs()

        federation = Federation(self.cfg.path("federation_dir"), database=db)
        all_datasets = federation.build_federation()
        pks = db.sync_federation(all_datasets)

        datasets = shuffle_datasets(federation.get_datasets(how=how))
        logger.info(
            "%s: %d of %d declared dataset(s) to validate",
            how, len(datasets), len(all_datasets),
        )

        engine_info = self._prepare_engine()
        run_id = db.start_run(how, trigger, self.cfg, engine_info,
                              len(all_datasets))

        counts: Counter = Counter()
        durations: list[tuple[str, float]] = []
        build_s = 0.0
        try:
            run_start = time.time()
            counts, durations = self._validate_all(db, run_id, datasets, pks)
            federated, flagged, build_s = self._deploy(db)
            db.finish_run(run_id, "success", counts, federated)
        except Exception as exc:
            db.finish_run(run_id, "failed", counts, 0, str(exc))
            raise

        logger.info(
            "run %d finished: %s", run_id,
            ", ".join(f"{k}={counts[k]}" for k in VALIDATION_STATES if counts[k])
            or "nothing checked",
        )
        logger.info("%d dataset(s) federated, %d flagged for reload",
                    federated, flagged)

        total_s = time.time() - run_start
        all_dur = [d for _, d in durations]
        excl_dur = [d for st, d in durations if st != "unreachable"]
        avg_all = sum(all_dur) / len(all_dur) if all_dur else 0.0
        avg_excl = sum(excl_dur) / len(excl_dur) if excl_dur else 0.0
        logger.info(
            "run %d performance: total=%.1fs  avg/dataset=%.2fs  "
            "datasets.xml build=%.1fs  avg/dataset (excl. unreachable)=%.2fs",
            run_id, total_s, avg_all, build_s, avg_excl,
        )

        return 0 if counts["blocked"] + counts["unreachable"] + counts["unhandled"] == 0 else 1

    def _validate_all(self, db, run_id: int, datasets: list[Dataset],
                      pks: dict[str, int]) -> tuple[Counter, list[tuple[str, float]]]:
        counts: Counter = Counter()
        durations: list[tuple[str, float]] = []
        if not datasets:
            logger.info("nothing to validate")
            return counts, durations

        total = len(datasets)
        workers = max(1, min(self.cfg.workers, total))
        logger.info("validating %d dataset(s) with %d worker(s)", total, workers)

        # Every thread needs the working directory to be the engine's cache
        # parent. Setting it once here means no thread ever calls chdir, which
        # is process-global and would race.
        os.chdir(os.path.dirname(self.cfg.path("cache_dir")))

        done = 0
        # The engine prints pandas frames and progress tables straight to
        # stdout, and quiet=True does not cover all of it - on 208 datasets that
        # is thousands of lines around the gate's own output. Redirecting for
        # the whole pass rather than per call because sys.stdout is process-wide
        # and swapping it per thread would race. The gate's logging is on
        # stderr, so progress still appears.
        with contextlib.redirect_stdout(io.StringIO()):
            with futures.ThreadPoolExecutor(max_workers=workers) as pool:
                pending = {pool.submit(self.validate, d): d for d in datasets}
                for future in futures.as_completed(pending):
                    report = future.result()
                    db.insert_validation(run_id, pks[report.dataset.id], report)
                    counts[report.status] += 1
                    durations.append((report.status, report.duration_s))
                    done += 1
                    logger.info(
                        "(%d/%d) %s status=%s %s",
                        done, total, report.dataset, report.status,
                        report.detail,
                    )
        return counts, durations

    def _deploy(self, db) -> tuple[int, int, float]:
        """Rebuild datasets.xml from the database and reload what changed."""
        erddap = ErddapHelper(self.cfg)
        healthy = db.get_healthy_datasets()

        # Built from the database, not from this run: an incremental run must
        # not drop the datasets it never looked at.
        previous = erddap.existing_dataset_ids()
        build_start = time.time()
        erddap.build_datasets(healthy)
        build_s = time.time() - build_start

        current = {row["dataset_id"] for row in healthy}
        dropped = previous - current
        if dropped:
            logger.info("%d dataset(s) removed from datasets.xml: %s",
                        len(dropped), ", ".join(sorted(dropped)))

        # Only what is new to the file needs loading; the rest keeps serving.
        flagged = sum(
            erddap.reload_dataset(dataset_id)
            for dataset_id in sorted(current - previous)
        )
        return len(current), flagged, build_s

    # ------------------------------------------------------------------
    # One dataset
    # ------------------------------------------------------------------
    def validate(self, dataset: Dataset) -> ValidationReport:
        """
        Score one dataset. Never raises: every failure is a status.

        Called from the thread pool, so it touches nothing shared except the
        engine's read-only caches and its own JSON file.
        """
        report = ValidationReport(
            dataset=dataset,
            status="unhandled",
            threshold=self.cfg.threshold,
            operational_required=self.cfg.operational_required,
            keywords_required=self.cfg.keywords_required,
        )
        logger.debug("validate start  %s", dataset)
        started = time.time()
        try:
            payload = self._run_engine(dataset)
            report.payload = payload
            self._score(report, payload)
        except _Unreachable as exc:
            report.status = "unreachable"
            report.error_message = str(exc)
        except Exception as exc:
            # The engine broke on this dataset. Not the facility's fault, and
            # deliberately distinct from a bad score: nobody should be asked to
            # fix metadata over a KeyError in the tester.
            report.status = "unhandled"
            report.error_message = _safe_error(exc)
            # The full text goes to the log file, which is gitignored and local
            # to the deployment host, and never into the report that reaches the
            # database, Grafana and the public step summary.
            logger.error("%s: the compliance engine broke: %s", dataset, exc)
        finally:
            report.duration_s = time.time() - started
        logger.debug("validate finish %s status=%s elapsed=%.2fs",
                     dataset, report.status, report.duration_s)
        return report

    def verdict(self, report: ValidationReport) -> bool:
        """
        Apply the policy. A dataset is federated when all enforced rules hold.

        Every rule is evaluated even once one has failed: a data manager needs
        every reason at once, not the first one the gate happened to check.
        """
        if report.required_score is None:
            return False

        failures: list[str] = []
        if report.required_score < self.cfg.threshold:
            short = self.cfg.threshold - report.required_score
            failures.append(
                f"required metadata {report.required_score:.1f}% is {short:.1f} "
                f"points below the {self.cfg.threshold:.1f}% threshold"
            )
        if self.cfg.operational_required and not report.operational_passed:
            failures.append("operational tests failed")
        if self.cfg.keywords_required and not report.keywords_passed:
            failures.append("keyword tests failed")

        report.failures = failures
        return not failures

    # ------------------------------------------------------------------
    def _prepare_engine(self) -> dict:
        """
        Download the specifications and vocabularies once, before the pool.

        Everything metadata_report() needs is cached by this call, which is
        what makes the compliance pass safe to run in threads.
        """
        if self._engine_info:
            return self._engine_info

        try:
            from emso_metadata_harmonizer.metadata import (
                EmsoMetadata, init_emso_metadata,
            )
        except ImportError as exc:
            raise RuntimeError(
                f"emso-metadata-harmonizer is not installed: {exc}. "
                f"Install it with `pip install -r requirements.txt`."
            ) from exc

        cache_dir = self.cfg.path("cache_dir")
        work_root = os.path.dirname(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)

        warm = any(os.scandir(cache_dir))
        logger.info("preparing the compliance engine, specifications %s "
                    "(%s cache at %s)", self.cfg.specs_version,
                    "warm" if warm else "cold", cache_dir)
        started = time.time()
        previous = os.getcwd()
        try:
            os.chdir(work_root)
            # Downloading ~50 resources prints a progress table per resource.
            with contextlib.redirect_stdout(io.StringIO()):
                EmsoMetadata.set_version(self.cfg.specs_version)
                metadata = init_emso_metadata()
        finally:
            os.chdir(previous)

        resolved = str(getattr(metadata, "specs_version", "")
                       or self.cfg.specs_version)
        self._engine_info = {
            "harmonizer_version": _harmonizer_version(),
            "specs_version": resolved,
        }
        logger.info("compliance engine ready in %.1f s: harmonizer %s, "
                    "specifications %s", time.time() - started,
                    self._engine_info["harmonizer_version"], resolved)
        return self._engine_info

    def _run_engine(self, dataset: Dataset) -> dict:
        """
        Hand one dataset to the engine and return its JSON report.

        quiet=True on purpose: the engine's printed tables carry every check
        that passed as well as every one that failed, which on 208 datasets
        would bury the log. The JSON is the gate's only channel into it.
        """
        from emso_metadata_harmonizer import metadata_report

        json_path = os.path.join(self._workdir.name, f"{dataset.id}.json")
        try:
            metadata_report(
                dataset.info_url,
                json_out=json_path,
                specs_version=self.cfg.specs_version,
                keywords=True,
                quiet=True,
                verbose=False,
                summary=False,
            )
        except Exception as exc:
            if _is_network_error(exc):
                # The type and status only. `exc` here is the transport failure
                # from the fetch, so its text carries the URL and whatever the
                # far end said - see _safe_error.
                raise _Unreachable(_safe_error(exc)) from exc
            raise

        if not os.path.isfile(json_path):
            # The engine returns None whatever happens, and writes no JSON when
            # it could not download the dataset. Reachable metadata with
            # undownloadable data is still the server not answering.
            raise _Unreachable(
                "the compliance engine wrote no report: the dataset's data "
                "could not be downloaded"
            )

        with open(json_path, encoding="utf-8") as f:
            return json.load(f)

    def _score(self, report: ValidationReport, payload: dict) -> None:
        """Read the four numbers the verdict needs out of the engine's JSON."""
        if not isinstance(payload, dict):
            raise ValueError("the engine's report is not a JSON object")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("the engine's report has no 'metadata' section")

        report.required_score = _percentage(metadata.get("required"))
        report.optional_score = _percentage(metadata.get("optional"))
        report.operational_passed = _passed(payload.get("operational"))
        report.keywords_passed = _passed(payload.get("keywords"))
        report.institution = str(payload.get("institution") or "")
        report.emso_facility = str(payload.get("emso_facility") or "")

        report.status = "healthy" if self.verdict(report) else "blocked"

    # ------------------------------------------------------------------
    # gate.py check / summary / status
    # ------------------------------------------------------------------
    def check(self) -> int:
        """Validate the registry and print it by facility. No database."""
        from rich.console import Console
        from rich.table import Table

        federation = Federation(self.cfg.path("federation_dir"))
        datasets = federation.build_federation()

        table = Table(title="Federation registry")
        table.add_column("facility")
        table.add_column("service")
        table.add_column("ERDDAP")
        table.add_column("datasets", justify="right")

        services: dict[tuple[str, str], list] = defaultdict(list)
        for dataset in datasets:
            services[(dataset.facility, dataset.service)].append(dataset)
        for (facility, service), group in sorted(services.items()):
            table.add_row(facility, service, group[0].host, str(len(group)))

        table.add_section()
        table.add_row("[bold]TOTAL", f"[bold]{len(services)} service(s)", "",
                      f"[bold]{len(datasets)}")
        Console().print(table)
        print(f"registry is valid: {len(datasets)} dataset(s) in "
              f"{len({d.facility for d in datasets})} facilit(y/ies)")
        print(f"policy: {self.cfg.describe()}")
        return 0

    def summary(self) -> int:
        """Per-facility counts by status."""
        from rich.console import Console
        from rich.table import Table

        rows = self.connect().facility_summary()
        table = Table(title="Federation summary")
        table.add_column("facility")
        for column in ("declared", "healthy", "blocked", "unreachable",
                       "unhandled", "pending", "mean required"):
            table.add_column(column, justify="right")

        totals: Counter = Counter()
        for row in rows:
            for key in ("declared", "healthy", "blocked", "unreachable",
                        "unhandled", "pending"):
                totals[key] += row[key]
            table.add_row(
                row["facility"], str(row["declared"]),
                _green(row["healthy"]), _red(row["blocked"]),
                _red(row["unreachable"]), _yellow(row["unhandled"]),
                str(row["pending"]),
                "-" if row["mean_required_score"] is None
                else f"{row['mean_required_score']:.1f}%",
            )

        if rows:
            table.add_section()
            table.add_row(
                "[bold]TOTAL", f"[bold]{totals['declared']}",
                f"[bold][green]{totals['healthy']}",
                f"[bold][red]{totals['blocked']}",
                f"[bold][red]{totals['unreachable']}",
                f"[bold][yellow]{totals['unhandled']}",
                f"[bold]{totals['pending']}", "",
            )
        Console().print(table)
        return 0

    def status(self) -> int:
        """The full report: summary, then everything needing attention."""
        from rich.console import Console
        from rich.table import Table

        console = Console()
        db = self.connect()
        self.summary()

        attention = db.attention(limit=100)
        if not attention:
            console.print("[green]nothing needs attention")
        else:
            table = Table(title=f"Needs attention ({len(attention)})")
            for column in ("facility", "dataset", "status", "required",
                           "short", "detail"):
                table.add_column(column)
            for row in attention:
                table.add_row(
                    row["facility"], row["dataset_id"], row["status"],
                    "-" if row["required_score"] is None
                    else f"{row['required_score']:.1f}%",
                    "-" if row["points_short"] is None
                    else str(row["points_short"]),
                    (row["detail"] or "")[:70],
                )
            console.print(table)

        regressions = db.regressions(limit=20)
        if regressions:
            table = Table(title=f"Regressed since last healthy ({len(regressions)})")
            for column in ("facility", "dataset", "status", "last healthy"):
                table.add_column(column)
            for row in regressions:
                table.add_row(
                    row["facility"], row["dataset_id"], row["current_status"],
                    str(row["last_healthy_at"])[:19],
                )
            console.print(table)

        runs = db.recent_runs(limit=5)
        if runs:
            table = Table(title="Recent runs")
            for column in ("run", "started", "outcome", "mode", "checked",
                           "healthy", "blocked", "unreachable", "unhandled"):
                table.add_column(column, justify="right")
            for row in runs:
                table.add_row(
                    str(row["run_id"]), str(row["started_at"])[:19],
                    row["outcome"] or "", row["mode"] or "",
                    str(row["datasets_checked"]),
                    _green(row["datasets_healthy"]),
                    _red(row["datasets_blocked"]),
                    _red(row["datasets_unreachable"]),
                    _yellow(row["datasets_unhandled"]),
                )
            console.print(table)
        return 0


# ---------------------------------------------------------------------------
class _Unreachable(Exception):
    """The server did not answer. Distinct from the engine breaking."""


def _safe_error(exc: BaseException) -> str:
    """
    The shape of a failure, never its content.

    A facility chooses the URL the gate fetches, and exception text is where
    content from that URL crosses into somewhere a person reads: a server
    banner, a redirect target, a fragment of a body, or the URL itself with
    whatever was appended to it. `gate.py summary` writes this string into the
    GitHub Actions step summary, which on a public repository is world-readable,
    and the Grafana `detail` column is built from the same field.

    So: the exception type, plus the numeric HTTP status when there is one.
    Both say what went wrong without quoting what answered, and an integer
    cannot carry a response. A facility debugging its own server reads the same
    two facts it would have taken from the full text; a facility pointing the
    gate at something it should not learns nothing it did not already know.

    `requests` is deliberately not imported to identify an HTTP error. The
    engine and its dependencies are imported lazily so that `gate.py check`
    stays offline with only PyYAML, psycopg2 and rich installed - see the
    dependency list in pr-gate.yml. Reading `.response.status_code` off the
    exception needs no import and no isinstance.
    """
    name = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return f"{name} (HTTP {status})"
    return name


def _is_network_error(exc: BaseException) -> bool:
    """True when *exc* or anything it was raised from is a transport failure."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if type(exc).__name__ in _NETWORK_ERRORS:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _harmonizer_version() -> str:
    try:
        from importlib.metadata import version
        return version("emso-metadata-harmonizer")
    except Exception:
        return "unknown"


def _percentage(value) -> float:
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError(f"{number} is not a percentage")
    return number


def _passed(section) -> bool:
    if not isinstance(section, dict) or "passed" not in section:
        raise ValueError("the engine's report has no 'passed' verdict")
    return bool(section["passed"])


def _word(passed: bool | None) -> str:
    return "-" if passed is None else ("pass" if passed else "FAIL")


def _green(n) -> str:
    return f"[green]{n}" if n else str(n)


def _yellow(n) -> str:
    return f"[yellow]{n}" if n else str(n)


def _red(n) -> str:
    return f"[red]{n}" if n else str(n)
