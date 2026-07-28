-- Infrastructure (Backend-Schema §13).
--
-- `jobs` ships empty. The scheduler that drives it is Stage 4; the table exists
-- now because the lease/heartbeat shape must be right from day one — it is what
-- makes the eventual Redis swap a backend change rather than an agent-logic
-- rewrite (TRD §3.2).

CREATE TABLE jobs (
    id                {{PK}},
    uid               TEXT NOT NULL UNIQUE,
    job_type          TEXT NOT NULL CHECK (job_type IN (
                          'GENERATE_SPEC', 'IMPLEMENT', 'FIX_CODE', 'EVALUATE', 'REVIEW', 'PROMOTE',
                          'ARCHIVE', 'MINE_PATTERNS', 'EXTRACT_KNOWLEDGE', 'COLLECT_PAPERS',
                          'COLLECT_GITHUB', 'COLLECT_MARKET_DATA', 'MONITOR_DEPLOYMENT',
                          'NULL_WORLD_RUN', 'GENERATE_REPORT')),
    payload           {{JSON}},
    strategy_id       INTEGER REFERENCES strategies(id),
    experiment_id     INTEGER REFERENCES experiments(id),
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                          'pending', 'claimed', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled')),
    priority          INTEGER NOT NULL DEFAULT 0,
    -- Lease-based claiming: dead-worker recovery without a distributed lock.
    -- Lease expiry returns orphaned jobs, so `kill -9` loses nothing.
    claimed_by        TEXT,
    lease_expires_at  TEXT,
    heartbeat_at      TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    -- Determines retry behaviour: transient retries, deterministic routes to a fix.
    failure_class     TEXT CHECK (failure_class IN ('transient', 'deterministic')),
    depends_on_job_id INTEGER REFERENCES jobs(id),
    scheduled_for     TEXT,
    error_message     TEXT,
    error_trace       TEXT,
    tokens_spent      INTEGER NOT NULL DEFAULT 0,
    duration_seconds  INTEGER,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    completed_at      TEXT
);

-- Registry of resolved profiles (TRD §6). Content-hashed so experiments can pin
-- them: `experiments.market_profile_hash` refers to `config_hash` here.
CREATE TABLE market_profiles (
    id            {{PK}},
    uid           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    config        {{JSON}} NOT NULL,
    config_hash   TEXT NOT NULL UNIQUE,
    active        {{BOOL}} NOT NULL DEFAULT 1,
    superseded_by INTEGER REFERENCES market_profiles(id),
    created_at    TEXT NOT NULL,
    UNIQUE (name, version)
);

CREATE TABLE timeframe_profiles (
    id            {{PK}},
    uid           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    config        {{JSON}} NOT NULL,
    config_hash   TEXT NOT NULL UNIQUE,
    active        {{BOOL}} NOT NULL DEFAULT 1,
    superseded_by INTEGER REFERENCES timeframe_profiles(id),
    created_at    TEXT NOT NULL,
    UNIQUE (name, version)
);

-- Cost models are keyed on (market, asset_class), not market alone (TRD §6.3).
-- Registered here alongside the profiles so `experiments.cost_model_hash` pins
-- the exact rates a result was scored under.
CREATE TABLE cost_models (
    id            {{PK}},
    uid           TEXT NOT NULL UNIQUE,
    market        TEXT NOT NULL,
    asset_class   TEXT NOT NULL CHECK (asset_class IN (
                      'cash_equity', 'etf', 'future', 'cfd', 'spot_crypto', 'perpetual')),
    version       TEXT NOT NULL,
    config        {{JSON}} NOT NULL,
    config_hash   TEXT NOT NULL UNIQUE,
    active        {{BOOL}} NOT NULL DEFAULT 1,
    superseded_by INTEGER REFERENCES cost_models(id),
    created_at    TEXT NOT NULL,
    UNIQUE (market, asset_class, version)
);

-- Scheduler back-pressure (TRD §4.4).
CREATE TABLE budgets (
    id           {{PK}},
    uid          TEXT NOT NULL UNIQUE,
    scope        TEXT NOT NULL CHECK (scope IN ('global', 'strategy', 'goal')),
    scope_id     INTEGER,
    budget_type  TEXT NOT NULL CHECK (budget_type IN (
                     'tokens', 'experiments', 'iterations', 'compute_seconds', 'usd')),
    period       TEXT NOT NULL CHECK (period IN ('day', 'week', 'lifetime')),
    limit_value  INTEGER NOT NULL,
    used_value   INTEGER NOT NULL DEFAULT 0,
    period_start TEXT,
    exhausted    {{BOOL}} NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Answers "why did you do this?" for every system action (PRD §11.1).
CREATE TABLE audit_log (
    id            {{PK}},
    uid           TEXT NOT NULL UNIQUE,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    entity_type   TEXT,
    entity_id     INTEGER,
    -- Every agent decision stores its reasoning and the evidence it cited.
    reasoning     TEXT,
    evidence      {{JSON}},
    prompt_version TEXT,
    model_version TEXT,
    created_at    TEXT NOT NULL
);
