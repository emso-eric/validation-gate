-- ---------------------------------------------------------------------------
-- EMSO Validation Gate - schema
--
-- Three things are recorded:
--
--   facility / service / dataset   what git declares, mirrored from
--                                  federation/ on every run.
--   run                            one execution of the gate.
--   validation                     one (run, dataset) result. Immutable:
--                                  nothing here is updated after the run that
--                                  wrote it.
--
-- There is no current-state table. The current state of a dataset is its
-- latest validation row, exposed as v_dataset_current. That is also why
-- 'degraded' does not exist as a status: "was healthy, now is not" is a
-- question about history, and history is what the validation table already is.
--
-- A dataset is federated when its latest status is 'healthy'. Nothing else.
--
-- Idempotent: safe to re-run against an existing database.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS facility (
    id         SERIAL PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,        -- the federation/ directory name
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS service (
    id            SERIAL PRIMARY KEY,
    facility_id   INTEGER NOT NULL REFERENCES facility(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,            -- the registry file stem
    url           TEXT NOT NULL,            -- normalised, ends in /erddap
    registry_path TEXT NOT NULL,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (facility_id, name)
);

CREATE TABLE IF NOT EXISTS dataset (
    id            SERIAL PRIMARY KEY,
    service_id    INTEGER NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    -- ERDDAP keys the whole federation on the datasetID, so it is unique here
    -- too and the registry loader enforces it before we get this far.
    dataset_id    TEXT NOT NULL UNIQUE,
    source_url    TEXT NOT NULL,
    protocol      TEXT NOT NULL DEFAULT 'tabledap',
    erddap_type   TEXT NOT NULL DEFAULT 'EDDTableFromErddap',
    default_query TEXT,
    registry_path TEXT NOT NULL,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when the dataset disappears from the registry. The row is kept: its
    -- validation history is still the answer to "why was this dropped?".
    removed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS dataset_service_idx ON dataset (service_id);


-- --------------------------------------------------------------------------
-- Runs
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run (
    id                 SERIAL PRIMARY KEY,
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    outcome            TEXT NOT NULL DEFAULT 'running',
                       -- running | success | failed
    mode               TEXT,      -- all | new | pending
    trigger            TEXT,      -- push | schedule | manual

    -- the engine that produced every score in this run
    harmonizer_version TEXT,
    specs_version      TEXT,

    -- the policy this run enforced
    threshold            DOUBLE PRECISION,
    operational_required BOOLEAN,
    keywords_required    BOOLEAN,

    -- What THIS run looked at. An incremental run checks a handful of
    -- datasets, so these are small on purpose and say nothing about the
    -- federation as a whole - see the federation_* snapshot below.
    datasets_declared    INTEGER NOT NULL DEFAULT 0,
    datasets_checked     INTEGER NOT NULL DEFAULT 0,
    datasets_healthy     INTEGER NOT NULL DEFAULT 0,
    datasets_blocked     INTEGER NOT NULL DEFAULT 0,
    datasets_unreachable INTEGER NOT NULL DEFAULT 0,
    datasets_unhandled   INTEGER NOT NULL DEFAULT 0,
    datasets_federated   INTEGER NOT NULL DEFAULT 0,

    -- The state of the WHOLE federation when this run finished, counted from
    -- v_dataset_current rather than from what the run happened to check.
    --
    -- Without this a timeseries is unreadable: `run new` checks two datasets
    -- and reports datasets_blocked = 0, `run all` checks everything and
    -- reports 140, and the chart swings between them according to which kind
    -- of run happened - not according to anything about the federation.
    --
    -- NULL means "not recorded", which is true for runs that predate these
    -- columns and were not reconstructible. Never 0 in that case: the same
    -- rule as required_score.
    federation_healthy     INTEGER,
    federation_blocked     INTEGER,
    federation_unreachable INTEGER,
    federation_unhandled   INTEGER,
    federation_pending     INTEGER,

    error_message      TEXT
);

CREATE INDEX IF NOT EXISTS run_started_idx ON run (started_at DESC);

-- CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
-- new columns need their own idempotent statement for existing deployments.
ALTER TABLE run ADD COLUMN IF NOT EXISTS federation_healthy     INTEGER;
ALTER TABLE run ADD COLUMN IF NOT EXISTS federation_blocked     INTEGER;
ALTER TABLE run ADD COLUMN IF NOT EXISTS federation_unreachable INTEGER;
ALTER TABLE run ADD COLUMN IF NOT EXISTS federation_unhandled   INTEGER;
ALTER TABLE run ADD COLUMN IF NOT EXISTS federation_pending     INTEGER;


-- --------------------------------------------------------------------------
-- Validations - immutable history, and the only source of current state
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS validation (
    id         SERIAL PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    dataset_pk INTEGER NOT NULL REFERENCES dataset(id) ON DELETE CASCADE,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- healthy      passed every enforced rule -> federated
    -- blocked      reachable and scored, failed at least one rule
    -- unreachable  HTTP error or timeout; no score exists
    -- unhandled    the compliance engine itself broke; no score exists
    status     TEXT NOT NULL,
    CONSTRAINT validation_status_known CHECK (
        status IN ('healthy', 'blocked', 'unreachable', 'unhandled')
    ),

    -- NULL when the engine never scored the dataset. Never 0 in that case:
    -- "could not be evaluated" and "evaluated badly" are different outcomes,
    -- and only the second one is the facility's to fix.
    required_score     DOUBLE PRECISION,
    optional_score     DOUBLE PRECISION,
    operational_passed BOOLEAN,
    keywords_passed    BOOLEAN,

    -- the policy in force for THIS verdict, so raising the bar later does not
    -- rewrite the history of why past datasets were admitted
    threshold            DOUBLE PRECISION,
    operational_required BOOLEAN,
    keywords_required    BOOLEAN,

    -- one line per rule that failed, phrased for the data manager
    failures      TEXT[],
    error_message TEXT,

    -- the engine's full JSON report, so a future policy change can be replayed
    -- against past runs without re-validating the federation
    report_json JSONB,

    -- read from the dataset's own metadata; drives the mismatch view
    institution   TEXT,
    emso_facility TEXT,

    duration_s DOUBLE PRECISION,

    UNIQUE (run_id, dataset_pk)
);

CREATE INDEX IF NOT EXISTS validation_dataset_idx
    ON validation (dataset_pk, checked_at DESC);
CREATE INDEX IF NOT EXISTS validation_run_idx ON validation (run_id);
CREATE INDEX IF NOT EXISTS validation_status_idx ON validation (status);
