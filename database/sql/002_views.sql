-- ---------------------------------------------------------------------------
-- EMSO Validation Gate - views
--
-- Everything Grafana reads goes through a view, so dashboards hold panel
-- configuration rather than business logic.
--
-- v_dataset_current is the spine: the latest validation of every dataset git
-- still declares. There is no state table to keep in sync with it.
-- ---------------------------------------------------------------------------

-- Inline migration: rename run.status -> run.outcome for existing databases.
-- This must run before any view below references r.outcome.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name   = 'run'
          AND column_name  = 'status'
    ) THEN
        ALTER TABLE run RENAME COLUMN status TO outcome;
    END IF;
END $$;
-- Fold the old 'aborted' outcome into 'failed'; idempotent.
UPDATE run SET outcome = 'failed' WHERE outcome = 'aborted';
-- v_runs selected r.status; drop it so CREATE OR REPLACE can use r.outcome.
DROP VIEW IF EXISTS v_runs;
-- v_federation_timeline changed shape when the federation_* snapshot was
-- added; CREATE OR REPLACE cannot rename or drop a column.
DROP VIEW IF EXISTS v_federation_timeline;

-- ---------------------------------------------------------------------------
-- Backfill the federation_* snapshot for runs that finished before those
-- columns existed.
--
-- Reconstructible from the validation table alone: the state of a dataset at
-- time T is its newest validation at or before T, which is the same rule
-- v_dataset_current applies at now(). Datasets already dropped from the
-- registry by then are excluded, so an old point counts the federation as it
-- stood, not as it stands today.
--
-- 'pending' cannot be derived that way - a dataset never validated has no row
-- to find - so it comes from datasets_declared, which has always been the
-- federation-wide declared count.
--
-- Idempotent twice over: WHERE federation_healthy IS NULL, and finished runs
-- never change afterwards.
-- ---------------------------------------------------------------------------
UPDATE run r
SET federation_healthy     = s.healthy,
    federation_blocked     = s.blocked,
    federation_unreachable = s.unreachable,
    federation_unhandled   = s.unhandled,
    federation_pending     = GREATEST(
        r.datasets_declared - (s.healthy + s.blocked + s.unreachable + s.unhandled),
        0
    )
FROM (
    SELECT
        r2.id,
        COUNT(*) FILTER (WHERE latest.status = 'healthy')     AS healthy,
        COUNT(*) FILTER (WHERE latest.status = 'blocked')     AS blocked,
        COUNT(*) FILTER (WHERE latest.status = 'unreachable') AS unreachable,
        COUNT(*) FILTER (WHERE latest.status = 'unhandled')   AS unhandled
    FROM run r2
    LEFT JOIN LATERAL (
        SELECT DISTINCT ON (v.dataset_pk) v.dataset_pk, v.status
        FROM validation v
        JOIN dataset d ON d.id = v.dataset_pk
        WHERE v.checked_at <= COALESCE(r2.finished_at, r2.started_at)
          AND (d.removed_at IS NULL
               OR d.removed_at > COALESCE(r2.finished_at, r2.started_at))
        ORDER BY v.dataset_pk, v.checked_at DESC
    ) AS latest ON TRUE
    WHERE r2.outcome <> 'running'
    GROUP BY r2.id
) AS s
WHERE r.id = s.id
  AND r.federation_healthy IS NULL;

-- The current state of every declared dataset: its most recent validation.
CREATE OR REPLACE VIEW v_dataset_current AS
SELECT DISTINCT ON (d.id)
    d.id            AS dataset_pk,
    d.dataset_id,
    d.source_url,
    d.protocol,
    d.erddap_type,
    d.default_query,
    d.registry_path,
    f.slug          AS facility,
    s.name          AS service,
    s.url           AS service_url,
    COALESCE(v.status, 'pending')   AS status,
    (v.status = 'healthy')          AS federated,
    v.checked_at    AS last_checked_at,
    v.required_score,
    v.optional_score,
    v.operational_passed,
    v.keywords_passed,
    v.threshold,
    v.failures,
    v.error_message,
    v.institution,
    v.emso_facility,
    v.duration_s,
    v.run_id        AS last_run_id
FROM dataset d
JOIN service  s ON s.id = d.service_id
JOIN facility f ON f.id = s.facility_id
LEFT JOIN validation v ON v.dataset_pk = d.id
WHERE d.removed_at IS NULL
ORDER BY d.id, v.checked_at DESC NULLS LAST;


-- Per-facility counts, for the overview tiles and `gate summary`.
CREATE OR REPLACE VIEW v_facility_summary AS
SELECT
    facility,
    COUNT(*)                                        AS declared,
    COUNT(*) FILTER (WHERE status = 'healthy')      AS healthy,
    COUNT(*) FILTER (WHERE status = 'blocked')      AS blocked,
    COUNT(*) FILTER (WHERE status = 'unreachable')  AS unreachable,
    COUNT(*) FILTER (WHERE status = 'unhandled')    AS unhandled,
    COUNT(*) FILTER (WHERE status = 'pending')      AS pending,
    ROUND(AVG(required_score)::numeric, 1)          AS mean_required_score,
    MIN(last_checked_at)                            AS oldest_check
FROM v_dataset_current
GROUP BY facility
ORDER BY facility;


-- Federation-wide totals. One row, for the stat tiles.
CREATE OR REPLACE VIEW v_federation_summary AS
SELECT
    COUNT(*)                                        AS declared,
    COUNT(*) FILTER (WHERE status = 'healthy')      AS healthy,
    COUNT(*) FILTER (WHERE status = 'blocked')      AS blocked,
    COUNT(*) FILTER (WHERE status = 'unreachable')  AS unreachable,
    COUNT(*) FILTER (WHERE status = 'unhandled')    AS unhandled,
    COUNT(*) FILTER (WHERE status = 'pending')      AS pending,
    ROUND(AVG(required_score)::numeric, 1)          AS mean_required_score
FROM v_dataset_current;


-- What a data manager opens first: everything not federated, worst first,
-- with how far short it is. Datasets that were never scored sort last - they
-- need an ERDDAP fixed, not metadata.
CREATE OR REPLACE VIEW v_attention AS
SELECT
    facility,
    dataset_id,
    status,
    required_score,
    threshold,
    CASE
        WHEN required_score IS NULL THEN NULL
        ELSE ROUND((threshold - required_score)::numeric, 1)
    END AS points_short,
    operational_passed,
    keywords_passed,
    COALESCE(array_to_string(failures, '; '), error_message) AS detail,
    last_checked_at
FROM v_dataset_current
WHERE status <> 'healthy'
ORDER BY
    (required_score IS NULL),
    (threshold - required_score) ASC,
    facility, dataset_id;


-- Datasets whose self-declared EMSO facility disagrees with the directory they
-- are filed under. Almost always a metadata error at the facility, and
-- invisible without joining the two sources.
CREATE OR REPLACE VIEW v_facility_mismatch AS
SELECT
    facility      AS registry_facility,
    emso_facility AS declared_facility,
    dataset_id,
    last_checked_at
FROM v_dataset_current
WHERE emso_facility IS NOT NULL
  AND emso_facility <> ''
  AND lower(replace(emso_facility, ' ', '_')) <> lower(facility)
ORDER BY facility, dataset_id;


-- Run history.
CREATE OR REPLACE VIEW v_runs AS
SELECT
    r.id        AS run_id,
    r.started_at,
    r.finished_at,
    EXTRACT(EPOCH FROM (r.finished_at - r.started_at)) AS duration_s,
    r.outcome,
    r.mode,
    r.trigger,
    r.harmonizer_version,
    r.specs_version,
    r.threshold,
    r.operational_required,
    r.keywords_required,
    r.datasets_declared,
    r.datasets_checked,
    r.datasets_healthy,
    r.datasets_blocked,
    r.datasets_unreachable,
    r.datasets_unhandled,
    r.datasets_federated,
    -- The whole federation as this run left it, not just what it checked.
    r.federation_healthy,
    r.federation_blocked,
    r.federation_unreachable,
    r.federation_unhandled,
    r.federation_pending,
    r.error_message
FROM run r
ORDER BY r.started_at DESC;


-- Every score a dataset has ever had, for the per-dataset drill-down. This is
-- how a facility proves a fix worked, and how "was healthy, now is not" is
-- answered without storing a 'degraded' status.
CREATE OR REPLACE VIEW v_score_history AS
SELECT
    v.checked_at AS time,
    d.dataset_id,
    f.slug       AS facility,
    v.status,
    v.required_score,
    v.optional_score,
    v.operational_passed,
    v.keywords_passed,
    v.threshold,
    r.specs_version,
    r.harmonizer_version,
    v.run_id
FROM validation v
JOIN dataset  d ON d.id = v.dataset_pk
JOIN service  s ON s.id = d.service_id
JOIN facility f ON f.id = s.facility_id
JOIN run      r ON r.id = v.run_id
ORDER BY v.checked_at;


-- The federation over time. Every finished run contributes a point, so a flat
-- federation is a flat line rather than a gap in the data.
--
-- The unprefixed columns are the state of the WHOLE federation at that moment,
-- not the run's own tally. Plotting the run's tally is what made this chart
-- swing between 0 and 140 depending on whether the run was `new` or `all`; the
-- run's tally is still here, under checked_*, because "what did run 37 do" is
-- a different and also useful question.
--
-- healthy + blocked + unreachable + unhandled + pending = declared, for any
-- run where the snapshot exists. Runs from before the snapshot that could not
-- be reconstructed report NULL, which Grafana leaves as a gap - correct, since
-- the alternative is drawing a zero that was never true.
CREATE OR REPLACE VIEW v_federation_timeline AS
SELECT
    r.started_at AS time,
    r.datasets_declared        AS declared,
    r.federation_healthy       AS healthy,
    r.federation_blocked       AS blocked,
    r.federation_unreachable   AS unreachable,
    r.federation_unhandled     AS unhandled,
    r.federation_pending       AS pending,
    -- Federated is the deployed consequence of healthy: the number of dataset
    -- blocks actually written into datasets.xml by this run. Kept separate so
    -- a divergence between "should be federated" and "is federated" stays
    -- visible instead of being assumed away.
    r.datasets_federated       AS federated,
    -- What this run itself looked at.
    r.mode,
    r.trigger,
    r.datasets_checked         AS checked,
    r.datasets_healthy         AS checked_healthy,
    r.datasets_blocked         AS checked_blocked,
    r.datasets_unreachable     AS checked_unreachable,
    r.datasets_unhandled       AS checked_unhandled,
    r.specs_version,
    r.threshold
FROM run r
WHERE r.outcome <> 'running'
ORDER BY r.started_at;


-- Datasets that regressed: healthy at some point in the past, not healthy now.
-- This is what 'degraded' used to be, derived instead of stored.
CREATE OR REPLACE VIEW v_regressions AS
SELECT
    c.facility,
    c.dataset_id,
    c.status                AS current_status,
    c.required_score,
    c.threshold,
    COALESCE(array_to_string(c.failures, '; '), c.error_message) AS detail,
    last_ok.checked_at      AS last_healthy_at,
    c.last_checked_at
FROM v_dataset_current c
JOIN LATERAL (
    SELECT v.checked_at
    FROM validation v
    WHERE v.dataset_pk = c.dataset_pk
      AND v.status = 'healthy'
    ORDER BY v.checked_at DESC
    LIMIT 1
) AS last_ok ON TRUE
WHERE c.status <> 'healthy'
ORDER BY last_ok.checked_at DESC;
