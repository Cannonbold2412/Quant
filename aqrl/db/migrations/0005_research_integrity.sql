-- Research integrity (Backend-Schema §11, TRD §15).
--
-- 0001 keeps the *inputs* honest. These keep the *research process* honest.
-- They are separate defences because null-world calibration cannot detect a
-- corrupted input, and no amount of clean data detects a moved goalpost.

-- Every opening of the locked holdout. The vault is the one defence that does
-- not depend on honestly counting trials, so ITS OWN BOOKKEEPING MUST BE EXACT.
CREATE TABLE vault_access_log (
    id            {{PK}},
    uid           TEXT NOT NULL UNIQUE,
    strategy_id   INTEGER REFERENCES strategies(id),
    -- Budget is consumed PER FAMILY, not per strategy. A family whose budget
    -- reaches zero cannot be promoted again until genuinely new data exists.
    family        TEXT NOT NULL,
    vault_segment TEXT,
    opened_at     TEXT NOT NULL,
    -- NEVER the research loop.
    opened_by     TEXT NOT NULL CHECK (opened_by IN ('human', 'promotion_gate')),
    reason        TEXT,
    promotion_id  INTEGER REFERENCES promotions(id),
    budget_before INTEGER,
    budget_after  INTEGER,
    result_score  REAL,
    outcome       TEXT CHECK (outcome IN ('confirmed', 'contradicted'))
);

-- The headline integrity metric (PRD §4.2, Milestone 0). Re-run after ANY
-- change to evaluate.py, the scoring rule, or a profile.
--
-- Zero discoveries means the pipeline is honest. Twelve means it invents twelve
-- findings from nothing, and every real result it has ever produced is suspect.
CREATE TABLE null_world_runs (
    id                   {{PK}},
    uid                  TEXT NOT NULL UNIQUE,
    run_label            TEXT,
    null_model           TEXT NOT NULL CHECK (null_model IN (
                             'permuted_returns', 'block_bootstrap', 'synthetic_gbm', 'synthetic_fat_tail')),
    replications         INTEGER NOT NULL,
    -- What was being calibrated.
    eval_engine_version  TEXT,
    scoring_rule_version TEXT,
    experiments_run      INTEGER,
    -- The number that matters.
    discoveries_reported INTEGER NOT NULL,
    false_discovery_rate REAL NOT NULL,
    -- The best "strategy" found in pure noise — a useful bar for real results.
    max_score_observed   REAL,
    verdict              TEXT CHECK (verdict IN ('pipeline_trusted', 'pipeline_suspect')),
    notes                TEXT,
    created_at           TEXT NOT NULL
);

-- The satisficing bar (PRD §10.2), recorded BEFORE a campaign begins so it
-- cannot be adjusted after seeing results. Changing a bar mid-campaign creates
-- a new row and marks the campaign's prior results incomparable.
CREATE TABLE acceptance_bars (
    id                  {{PK}},
    uid                 TEXT NOT NULL UNIQUE,
    campaign_label      TEXT NOT NULL,
    min_score           REAL NOT NULL,
    -- 0.15 default; 0.20 for crypto (per-market override).
    max_drawdown        REAL NOT NULL,
    min_trades          INTEGER NOT NULL,
    -- Definitions still open (TRD §21) — how breadth and complexity are
    -- measured is a human-owned question.
    min_breadth         REAL,
    max_complexity      INTEGER,
    cost_stress_multiple REAL NOT NULL,
    z_multiplier        REAL NOT NULL,
    -- Consecutive BAR FAILURES before a PLATEAU verdict — never a score
    -- comparison, since clearing the bar stops the loop immediately.
    plateau_patience    INTEGER NOT NULL DEFAULT 5,
    -- Outer backstop independent of plateau detection.
    hard_iteration_cap  INTEGER NOT NULL DEFAULT 25,
    -- Fixed per campaign, never searched over (TRD §8.2).
    wf_scheme           TEXT CHECK (wf_scheme IN ('rolling', 'anchored', 'holdout', 'cpcv')),
    wf_train_years      {{JSON}},
    wf_test_years       INTEGER,
    locked_at           TEXT NOT NULL,
    locked_by           TEXT,
    superseded_by       INTEGER REFERENCES acceptance_bars(id)
);
