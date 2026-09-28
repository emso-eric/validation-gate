"""
PostgreSQL wrapper. Every SQL statement the gate runs lives here.

The schema is in database/sql/; this module applies it and then only ever
inserts. Nothing in `validation` is updated after the run that wrote it, so the
current state of a dataset is simply its newest row - which is what the
v_dataset_current view returns.
"""

from __future__ import annotations

import json
import logging
import os

import psycopg2
import psycopg2.extras

logger = logging.getLogger("gate")

SQL_DIR = os.path.join("database", "sql")

# Stable key for the PostgreSQL session-level advisory lock that ensures only
# one gate instance runs at a time.  0x454D534F = "EMSO" in ASCII.
_GATE_LOCK_KEY = 0x454D534F


class DatabaseError(Exception):
    """The database is unusable and the run must not continue blind."""


class GateDatabase:
    """A connection to the gate database, usable as a context manager."""

    def __init__(self, url: str, repo_root: str = ".") -> None:
        if not url:
            raise DatabaseError(
                "no database configured: set POSTGRES_USER, POSTGRES_PASSWORD, "
                "POSTGRES_HOST, POSTGRES_PORT and POSTGRES_DB in the "
                "repository-root .env (`./gate.py autodeploy` writes it), or "
                "set $GATE_DATABASE_URL"
            )
        self.url = url
        self.repo_root = os.path.abspath(repo_root)
        try:
            self.conn = psycopg2.connect(url)
        except psycopg2.Error as exc:
            raise DatabaseError(f"could not connect: {exc}") from exc
        self.conn.autocommit = True

    def close(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "GateDatabase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _cursor(self):
        return self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def init_db(self) -> None:
        """Apply database/sql/*.sql in order. Idempotent."""
        directory = os.path.join(self.repo_root, SQL_DIR)
        if not os.path.isdir(directory):
            raise DatabaseError(f"no SQL directory at {directory}")
        files = sorted(f for f in os.listdir(directory) if f.endswith(".sql"))
        if not files:
            raise DatabaseError(f"no .sql files in {directory}")
        with self._cursor() as cur:
            for name in files:
                with open(os.path.join(directory, name), encoding="utf-8") as f:
                    cur.execute(f.read())
                logger.debug("applied %s", name)
        logger.debug("schema up to date (%d file(s))", len(files))

    # ------------------------------------------------------------------
    # Registry mirror
    # ------------------------------------------------------------------
    def sync_federation(self, datasets: list) -> dict[str, int]:
        """
        Mirror the registry into the database and return datasetID -> pk.

        Datasets no longer declared are marked removed, never deleted: their
        history is the answer to "why was this dropped?".
        """
        pks: dict[str, int] = {}
        with self._cursor() as cur:
            for dataset in datasets:
                cur.execute(
                    """
                    INSERT INTO facility (slug) VALUES (%s)
                    ON CONFLICT (slug) DO UPDATE SET last_seen = now()
                    RETURNING id;
                    """,
                    (dataset.facility,),
                )
                facility_id = cur.fetchone()["id"]

                cur.execute(
                    """
                    INSERT INTO service (facility_id, name, url, registry_path)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (facility_id, name) DO UPDATE
                        SET url           = EXCLUDED.url,
                            registry_path = EXCLUDED.registry_path,
                            last_seen     = now()
                    RETURNING id;
                    """,
                    (facility_id, dataset.service, dataset.host,
                     dataset.registry_path),
                )
                service_id = cur.fetchone()["id"]

                cur.execute(
                    """
                    INSERT INTO dataset (
                        service_id, dataset_id, source_url, protocol,
                        erddap_type, default_query, registry_path
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (dataset_id) DO UPDATE
                        SET service_id    = EXCLUDED.service_id,
                            source_url    = EXCLUDED.source_url,
                            protocol      = EXCLUDED.protocol,
                            erddap_type   = EXCLUDED.erddap_type,
                            default_query = EXCLUDED.default_query,
                            registry_path = EXCLUDED.registry_path,
                            removed_at    = NULL,
                            last_seen     = now()
                    RETURNING id;
                    """,
                    (service_id, dataset.id, dataset.url, dataset.protocol,
                     dataset.erddap_type, dataset.default_query or None,
                     dataset.registry_path),
                )
                pks[dataset.id] = cur.fetchone()["id"]

            cur.execute(
                """
                UPDATE dataset SET removed_at = now()
                WHERE removed_at IS NULL AND NOT (dataset_id = ANY(%s))
                RETURNING dataset_id;
                """,
                (list(pks) or [""],),
            )
            removed = [r["dataset_id"] for r in cur.fetchall()]

        if removed:
            logger.info(
                "%d dataset(s) no longer declared: %s",
                len(removed), ", ".join(sorted(removed)),
            )
        return pks

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_status(self) -> dict[str, str]:
        """datasetID -> latest status, for the incremental selections."""
        with self._cursor() as cur:
            cur.execute("SELECT dataset_id, status FROM v_dataset_current;")
            return {r["dataset_id"]: r["status"] for r in cur.fetchall()}

    def list_by_status(self, status: str | None,
                       facility: str | None = None) -> list[dict]:
        """Datasets optionally filtered by *status* and/or *facility*.

        Pass status=None to return all datasets regardless of status.
        """
        with self._cursor() as cur:
            if status is not None and facility:
                cur.execute(
                    """
                    SELECT dataset_id, facility, source_url,
                           required_score, keywords_passed, operational_passed,
                           threshold
                    FROM v_dataset_current
                    WHERE status = %s AND facility = %s
                    ORDER BY facility, dataset_id;
                    """,
                    (status, facility),
                )
            elif status is not None:
                cur.execute(
                    """
                    SELECT dataset_id, facility, source_url,
                           required_score, keywords_passed, operational_passed,
                           threshold
                    FROM v_dataset_current
                    WHERE status = %s
                    ORDER BY facility, dataset_id;
                    """,
                    (status,),
                )
            elif facility:
                cur.execute(
                    """
                    SELECT dataset_id, facility, source_url,
                           required_score, keywords_passed, operational_passed,
                           threshold
                    FROM v_dataset_current
                    WHERE facility = %s
                    ORDER BY facility, dataset_id;
                    """,
                    (facility,),
                )
            else:
                cur.execute(
                    """
                    SELECT dataset_id, facility, source_url,
                           required_score, keywords_passed, operational_passed,
                           threshold
                    FROM v_dataset_current
                    ORDER BY facility, dataset_id;
                    """
                )
            return [dict(r) for r in cur.fetchall()]

    def get_healthy_datasets(self) -> list[dict]:
        """
        Everything currently federated, in datasets.xml order.

        This is what the file is built from - not the results of the current
        run, so an incremental run cannot drop the datasets it never looked at.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT dataset_id, source_url, erddap_type, default_query
                FROM v_dataset_current
                WHERE status = 'healthy'
                ORDER BY dataset_id;
                """
            )
            return [dict(r) for r in cur.fetchall()]

    def facility_summary(self) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM v_facility_summary;")
            return [dict(r) for r in cur.fetchall()]

    def attention(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM v_attention LIMIT %s;", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def regressions(self, limit: int = 50) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM v_regressions LIMIT %s;", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM v_runs LIMIT %s;", (limit,))
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------
    def start_run(self, mode: str, trigger: str, cfg, engine_info: dict,
                  declared: int) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO run (
                    mode, trigger, harmonizer_version, specs_version,
                    threshold, operational_required, keywords_required,
                    datasets_declared
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (mode, trigger, engine_info.get("harmonizer_version"),
                 engine_info.get("specs_version"), cfg.threshold,
                 cfg.operational_required, cfg.keywords_required, declared),
            )
            return cur.fetchone()["id"]

    def finish_run(self, run_id: int, outcome: str, counts: dict,
                   federated: int, error_message: str | None = None) -> None:
        """
        Close the run row: what it checked, and what the federation now is.

        The two are different numbers and both are recorded. `counts` is this
        run's own tally, which for an incremental run covers a handful of
        datasets; the federation_* columns are counted from v_dataset_current,
        so they describe every declared dataset whether this run looked at it
        or not.

        Storing only the former is what made the Grafana timeline swing
        between 0 and 140 according to the run mode. The snapshot is taken in
        the same UPDATE as the outcome, so a run can never carry counts from a
        moment other than the one it finished at.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE run SET
                    finished_at          = now(),
                    outcome              = %s,
                    datasets_checked     = %s,
                    datasets_healthy     = %s,
                    datasets_blocked     = %s,
                    datasets_unreachable = %s,
                    datasets_unhandled   = %s,
                    datasets_federated   = %s,
                    error_message        = %s,

                    federation_healthy     = fs.healthy,
                    federation_blocked     = fs.blocked,
                    federation_unreachable = fs.unreachable,
                    federation_unhandled   = fs.unhandled,
                    federation_pending     = fs.pending
                FROM (
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'healthy')     AS healthy,
                        COUNT(*) FILTER (WHERE status = 'blocked')     AS blocked,
                        COUNT(*) FILTER (WHERE status = 'unreachable') AS unreachable,
                        COUNT(*) FILTER (WHERE status = 'unhandled')   AS unhandled,
                        COUNT(*) FILTER (WHERE status = 'pending')     AS pending
                    FROM v_dataset_current
                ) AS fs
                WHERE run.id = %s;
                """,
                (outcome, sum(counts.values()), counts.get("healthy", 0),
                 counts.get("blocked", 0), counts.get("unreachable", 0),
                 counts.get("unhandled", 0), federated, error_message, run_id),
            )

    def cleanup_stale_runs(self) -> int:
        """
        Mark any runs still in 'running' state as 'failed'.

        A 'running' row that survives a restart is a ghost: the process that
        created it is gone and will never call finish_run.  Leaving it around
        would make v_runs misleading and block future lock checks if we ever
        use the outcome column for that.  This is called once per run, right
        after acquiring the advisory lock, so no live process can be racing us.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE run
                SET outcome       = 'failed',
                    finished_at   = now(),
                    error_message = 'interrupted: gate was killed before this run could finish'
                WHERE outcome = 'running'
                RETURNING id;
                """
            )
            stale = [r["id"] for r in cur.fetchall()]
        if stale:
            logger.warning(
                "%d stale run(s) found and marked failed: %s",
                len(stale), stale,
            )
        return len(stale)

    def try_acquire_run_lock(self) -> bool:
        """
        Try to acquire a PostgreSQL session-level advisory lock.

        Returns True if this session is now the sole gate instance.  Returns
        False if another session already holds the lock (another gate is
        running).  The lock is released automatically when the connection
        closes - even on a hard crash - so no explicit release is needed and
        stale locks never accumulate.

        The key (0x454D534F = "EMSO") is arbitrary but stable; any other app
        on the same database must simply avoid the same integer.
        """
        with self._cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired;",
                        (_GATE_LOCK_KEY,))
            return bool(cur.fetchone()["acquired"])

    # ------------------------------------------------------------------
    # Validations
    # ------------------------------------------------------------------
    def insert_validation(self, run_id: int, dataset_pk: int, report) -> int:
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO validation (
                    run_id, dataset_pk, status,
                    required_score, optional_score,
                    operational_passed, keywords_passed,
                    threshold, operational_required, keywords_required,
                    failures, error_message, report_json,
                    institution, emso_facility, duration_s
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (run_id, dataset_pk) DO NOTHING
                RETURNING id;
                """,
                (run_id, dataset_pk, report.status,
                 report.required_score, report.optional_score,
                 report.operational_passed, report.keywords_passed,
                 report.threshold, report.operational_required,
                 report.keywords_required,
                 report.failures or None, report.error_message,
                 json.dumps(report.payload) if report.payload else None,
                 report.institution or None, report.emso_facility or None,
                 report.duration_s),
            )
            row = cur.fetchone()
            return row["id"] if row else 0
