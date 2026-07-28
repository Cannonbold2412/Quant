-- Minimum viable schema (TRD §2.5, Implementation_Plan §2.5): three tables to
-- start, trimmed to columns Stage 0 can actually populate. The full
-- Backend-Schema.md tables (spec_id, research_plan_id, operator_library_version,
-- ...) arrive with A1/A2/the operator library in later stages.
--
-- Plain, parameterized SQL only — no SQLite-specific syntax beyond
-- `INTEGER PRIMARY KEY` — so the eventual Postgres swap (Stage 13) is a
-- backend swap, not a rewrite (TRD §19).

CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    family TEXT NOT NULL,          -- critical for trial counting (Backend-Schema §4)
    market TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    git_branch TEXT,
    code_path TEXT,
    current_experiment_id INTEGER,
    best_experiment_id INTEGER,
    iteration_count INTEGER NOT NULL DEFAULT 0,
    total_trials INTEGER NOT NULL DEFAULT 0,
    best_score REAL,
    plateau_counter INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE NOT NULL,
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
    iteration INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('keep', 'discard', 'crash')),
    phase_reached TEXT NOT NULL,           -- 'bar' | 'P0' | 'P1' | 'P2' | 'P3'
    outcome TEXT,                          -- 'passed' | 'failed' | 'error'
    failure_reason TEXT,
    eval_engine_version TEXT,
    wf_config_hash TEXT,                   -- hash of {scheme, train_years, test_years} (TRD §8.2)
    code_commit TEXT NOT NULL,             -- the git commit evaluate.py actually scored
    random_seed INTEGER,
    comparable INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE NOT NULL,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    phase TEXT NOT NULL,                   -- 'bar' | 'P0' | 'P1' | 'P2' | 'P3'
    result TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'warn', 'error')),
    bar_result TEXT,                       -- 'pass' | 'fail' (TRD §7.5)
    bar_failed_on TEXT,                    -- 'min_trades' | 'max_drawdown' | 'breadth' | 'cost_stress' | 'complexity'
    honest_score REAL,                     -- max across the three train windows (TRD §8.2)
    sr_oos REAL,
    se_sr REAL,
    z_multiplier REAL,
    trials_haircut REAL,
    n_trials INTEGER,
    winning_train_years INTEGER,
    score_1yr REAL,
    score_2yr REAL,
    score_3yr REAL,
    n_trades INTEGER,
    max_drawdown REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_experiments_strategy_iteration ON experiments (strategy_id, iteration);

-- Supports the vault (TRD §15.2): logged opens, decremented against a
-- lifetime budget per strategy family.
CREATE TABLE IF NOT EXISTS vault_access_log (
    id INTEGER PRIMARY KEY,
    family TEXT NOT NULL,
    reason TEXT,
    opened_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Supports null-world calibration (TRD §15.3, Milestone 0): every calibration
-- run and its measured false-discovery count, so "has FDR been measured and
-- is it low" is an answerable query rather than a claim.
CREATE TABLE IF NOT EXISTS null_world_runs (
    id INTEGER PRIMARY KEY,
    uid TEXT UNIQUE NOT NULL,
    generator TEXT NOT NULL,               -- 'permuted' | 'block_bootstrap' | 'synthetic_path'
    n_replications INTEGER NOT NULL,
    n_discoveries INTEGER NOT NULL,        -- how many replications cleared the bar
    max_score_observed REAL NOT NULL,
    eval_engine_version TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
