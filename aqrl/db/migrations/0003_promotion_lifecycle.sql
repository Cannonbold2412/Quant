-- Promotion, deployment, health and lifecycle (Backend-Schema §7-8).
--
-- Two mandatory human gates live here: research->paper and paper->live. No code
-- path may bypass them (TRD §18). A4 outputs a recommendation record and holds
-- no execution authority; `requires_human_approval` is always 1 for both gates.

CREATE TABLE promotions (
    id                        {{PK}},
    uid                       TEXT NOT NULL UNIQUE,
    strategy_id               INTEGER NOT NULL REFERENCES strategies(id),
    best_experiment_id        INTEGER REFERENCES experiments(id),
    stage_from                TEXT NOT NULL,
    stage_to                  TEXT NOT NULL,
    decision                  TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'defer')),
    rationale                 TEXT,
    evidence_summary          {{JSON}},
    -- OVERFITTING SIGNAL — attempts spent before clearing the bar.
    iterations_considered     INTEGER,
    overfitting_risk          TEXT CHECK (overfitting_risk IN ('low', 'medium', 'high')),
    confidence                REAL,
    -- Can THIS strategy alone trade at real size — a single-strategy property.
    -- There is deliberately no portfolio-correlation column: multi-strategy
    -- construction is out of scope for v1 (PRD §3), and correlation is a
    -- dashboard-computed value the human sees, never part of A4's brief.
    capacity_liquidity_ok     {{BOOL}},
    recommended_allocation_pct REAL,
    requires_human_approval   {{BOOL}} NOT NULL DEFAULT 1,
    human_decision            TEXT CHECK (human_decision IN ('approved', 'rejected', 'pending')),
    human_decided_by          TEXT,
    human_decided_at          TEXT,
    -- MANDATORY on approval — friction on purpose.
    human_notes               TEXT,
    -- Set only on approve: the commit where strategy/<id> merged into
    -- deploy/paper or deploy/live. The merge message references this row's uid,
    -- so git and this table cross-reference (TRD §5.3).
    merge_commit              TEXT,
    prompt_version            TEXT,
    created_at                TEXT NOT NULL
);

CREATE TABLE deployments (
    id                    {{PK}},
    uid                   TEXT NOT NULL UNIQUE,
    strategy_id           INTEGER NOT NULL REFERENCES strategies(id),
    -- The exact validated version deployed.
    experiment_id         INTEGER REFERENCES experiments(id),
    mode                  TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    status                TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'paused', 'stopped', 'retired')),
    deploy_branch         TEXT,
    allocation_pct        REAL,
    -- Money is integer minor units with an explicit currency. Never float.
    capital_minor_units   INTEGER,
    currency              TEXT,
    broker_ref            TEXT,
    started_at            TEXT,
    ended_at              TEXT,
    -- Promotion gate tracking (PRD §9.3) — trade count AND deviation AND regime
    -- coverage AND execution AND health are ALL required.
    trades_required       INTEGER,
    trades_completed      INTEGER NOT NULL DEFAULT 0,
    regimes_required      {{JSON}},
    regimes_observed      {{JSON}},
    -- Expected-behaviour baseline, copied from validation — the yardstick for
    -- every health check.
    expected_sharpe       REAL,
    expected_max_dd       REAL,
    expected_win_rate     REAL,
    expected_avg_trade    REAL,
    current_health        TEXT CHECK (current_health IN ('green', 'yellow', 'orange', 'red')),
    retirement_reason     TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE trades (
    id                     {{PK}},
    uid                    TEXT NOT NULL UNIQUE,
    deployment_id          INTEGER NOT NULL REFERENCES deployments(id),
    instrument             TEXT NOT NULL,
    side                   TEXT NOT NULL CHECK (side IN ('long', 'short')),
    entry_time             TEXT,
    exit_time              TEXT,
    entry_price            REAL,
    exit_price             REAL,
    quantity               REAL,
    pnl_minor_units        INTEGER,
    currency               TEXT,
    fees_minor_units       INTEGER,
    -- Expected vs actual is the point: it is how execution quality becomes
    -- measurable rather than assumed.
    expected_slippage_bps  REAL,
    actual_slippage_bps    REAL,
    execution_quality      TEXT CHECK (execution_quality IN ('good', 'degraded', 'failed')),
    regime_at_entry        TEXT CHECK (regime_at_entry IN (
                               'trending', 'sideways', 'high_vol', 'low_vol', 'crisis')),
    signal_reference       TEXT,
    created_at             TEXT NOT NULL
);

-- The periodic verdict on "is this still behaving like what we validated?"
CREATE TABLE health_checks (
    id                       {{PK}},
    uid                      TEXT NOT NULL UNIQUE,
    deployment_id            INTEGER NOT NULL REFERENCES deployments(id),
    check_time               TEXT NOT NULL,
    level                    TEXT NOT NULL CHECK (level IN ('green', 'yellow', 'orange', 'red')),
    live_sharpe              REAL,
    live_max_dd              REAL,
    live_win_rate            REAL,
    live_profit_factor       REAL,
    -- Z-scores, because "1.2 vs 1.6" means nothing without the expected spread.
    sharpe_zscore            REAL,
    win_rate_zscore          REAL,
    avg_trade_zscore         REAL,
    loss_distribution_pvalue REAL,
    current_regime           TEXT CHECK (current_regime IN (
                                 'trending', 'sideways', 'high_vol', 'low_vol', 'crisis')),
    -- A drawdown in a known-weak regime is EXPECTED, not evidence of death.
    -- This single field prevents the most common bad decision.
    regime_historically_weak {{BOOL}},
    slippage_deviation       REAL,
    missed_fill_rate         REAL,
    liquidity_change         REAL,
    verdict_reason           TEXT,
    recommended_action       TEXT CHECK (recommended_action IN ('continue', 'reduce', 'pause', 'stop')),
    created_at               TEXT NOT NULL
);

-- Immutable audit trail of everything that happened to a deployment.
CREATE TABLE lifecycle_events (
    id            {{PK}},
    uid           TEXT NOT NULL UNIQUE,
    deployment_id INTEGER REFERENCES deployments(id),
    strategy_id   INTEGER REFERENCES strategies(id),
    event_type    TEXT NOT NULL CHECK (event_type IN (
                      'deployed', 'scaled_up', 'scaled_down', 'paused', 'resumed', 'retired',
                      'health_change', 'kill_switch')),
    from_state    TEXT,
    to_state      TEXT,
    triggered_by  TEXT CHECK (triggered_by IN ('agent', 'human', 'automatic_rule')),
    reason        TEXT,
    -- On 'retired', includes the commit that removed this strategy from its
    -- deploy branch (App-Flow §11.2).
    evidence      {{JSON}},
    created_at    TEXT NOT NULL
);
