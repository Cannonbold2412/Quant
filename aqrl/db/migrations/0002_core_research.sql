-- Core research tables (Backend-Schema §3-6).
--
-- `experiments` is the central table: one row per iteration, carrying the full
-- provenance stamp of TRD §6.6 so that the day cost assumptions change we can
-- answer "which stored results are still comparable?" from an index rather than
-- a scan.
--
-- A note on the five INTEGER columns below that look like they should be
-- foreign keys but are not: strategies <-> experiments, experiments <->
-- research_plans, experiments <-> code_versions and strategy_specs ->
-- research_questions are genuine reference cycles. SQLite cannot add a
-- constraint after the fact and PostgreSQL rejects a forward reference at
-- CREATE time, so the back-edge of each cycle is a plain INTEGER enforced by
-- the repository layer. Each one is marked below.

CREATE TABLE research_goals (
    id                {{PK}},
    uid               TEXT NOT NULL UNIQUE,
    title             TEXT NOT NULL,
    description       TEXT,
    market            TEXT,                    -- nullable for cross-market goals
    timeframe         TEXT,
    -- The 70/20/10 split (PRD §4.5).
    allocation_bucket TEXT CHECK (allocation_bucket IN ('incremental', 'cross_market', 'exploratory')),
    priority          INTEGER NOT NULL DEFAULT 0,
    hypothesis_budget INTEGER,
    hypotheses_used   INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'paused', 'completed', 'abandoned')),
    created_by        TEXT CHECK (created_by IN ('human', 'agent5')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- The vetted building-block library (TRD §11). Not self-modifying: `approved_by`
-- is a human or the operator does not enter the library.
CREATE TABLE operators (
    id                 {{PK}},
    uid                TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    category           TEXT NOT NULL CHECK (category IN ('transformation', 'signal', 'risk', 'portfolio')),
    version            TEXT NOT NULL,
    implementation_ref TEXT,
    parameters         {{JSON}},
    valid_markets      {{JSON}},
    valid_timeframes   {{JSON}},
    description        TEXT,
    reference_notes    TEXT,
    test_status        TEXT NOT NULL DEFAULT 'untested'
                           CHECK (test_status IN ('tested', 'untested', 'deprecated')),
    approved_by        TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (name, version)
);

-- The durable identity of a research thread. One strategy has many experiments.
CREATE TABLE strategies (
    id                    {{PK}},
    uid                   TEXT NOT NULL UNIQUE,
    name                  TEXT NOT NULL,
    -- CRITICAL for trial counting (TRD §10.2). The same idea tried in 3 markets
    -- is 3 trials in one family, not 3 independent results.
    family                TEXT NOT NULL,
    goal_id               INTEGER REFERENCES research_goals(id),
    market                TEXT NOT NULL,
    timeframe             TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                              'draft', 'spec_ready', 'coding', 'evaluating', 'evaluated', 'iterating',
                              'plateaued', 'rejected', 'pending_promotion', 'awaiting_human_review',
                              'paper_trading', 'pending_live_review', 'live_small', 'live_scaled',
                              'retired', 'quarantined')),
    -- `strategy/<strategy_id>`, created on the first IMPLEMENT job (TRD §5.2).
    -- NEVER deleted, including on rejection: git's GC only protects commits
    -- reachable from a branch, so the branch is what keeps `code_commit` valid.
    git_branch            TEXT,
    code_path             TEXT,
    current_experiment_id INTEGER,             -- cycle back-edge -> experiments(id)
    best_experiment_id    INTEGER,             -- cycle back-edge -> experiments(id)
    iteration_count       INTEGER NOT NULL DEFAULT 0,
    total_trials          INTEGER NOT NULL DEFAULT 0,
    best_score            REAL,
    -- Consecutive BAR FAILURES. Only ever increments — clearing the bar stops
    -- the loop immediately, so there is no "improvement" case to reset against.
    plateau_counter       INTEGER NOT NULL DEFAULT 0,
    tokens_spent          INTEGER NOT NULL DEFAULT 0,
    compute_seconds       INTEGER NOT NULL DEFAULT 0,
    quarantined           {{BOOL}} NOT NULL DEFAULT 0,
    quarantine_reason     TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    archived_at           TEXT
);

-- A1's output. Immutable once created; a revised spec is a new row.
CREATE TABLE strategy_specs (
    id                            {{PK}},
    uid                           TEXT NOT NULL UNIQUE,
    strategy_id                   INTEGER NOT NULL REFERENCES strategies(id),
    version                       INTEGER NOT NULL,
    hypothesis                    TEXT NOT NULL,
    rationale                     TEXT,
    entry_logic                   {{JSON}},
    exit_logic                    {{JSON}},
    filter_logic                  {{JSON}},
    risk_logic                    {{JSON}},
    universe                      {{JSON}},
    parameters                    {{JSON}},
    -- Canonical hash of the operator DAG. An exact re-run is rejected at insert.
    spec_hash                     TEXT NOT NULL UNIQUE,
    -- Which external_knowledge rows (candidate, untested) inspired this.
    source_external_knowledge_ids {{JSON}},
    -- Which knowledge_entries (tested, trusted) this respects or overrides.
    source_internal_knowledge_ids {{JSON}},
    source_question_id            INTEGER,     -- cycle back-edge -> research_questions(id)
    expected_behavior             TEXT,
    prompt_version                TEXT,
    created_at                    TEXT NOT NULL,
    UNIQUE (strategy_id, version)
);

-- Makes operator usage queryable: "which experiments ever used a Kalman filter?"
CREATE TABLE spec_operators (
    id              {{PK}},
    spec_id         INTEGER NOT NULL REFERENCES strategy_specs(id),
    operator_id     INTEGER NOT NULL REFERENCES operators(id),
    role            TEXT NOT NULL CHECK (role IN ('entry', 'exit', 'filter', 'risk')),
    parameters_used {{JSON}}
);

CREATE TABLE experiments (
    id                     {{PK}},
    uid                    TEXT NOT NULL UNIQUE,
    strategy_id            INTEGER NOT NULL REFERENCES strategies(id),
    spec_id                INTEGER REFERENCES strategy_specs(id),
    iteration              INTEGER NOT NULL,
    parent_experiment_id   INTEGER REFERENCES experiments(id),
    research_plan_id       INTEGER,            -- cycle back-edge -> research_plans(id)
    code_version_id        INTEGER,            -- cycle back-edge -> code_versions(id)
    status                 TEXT NOT NULL CHECK (status IN (
                               'created', 'code_pending', 'code_ready', 'evaluating', 'evaluated',
                               'reviewed', 'archived', 'failed', 'error')),
    phase_reached          TEXT CHECK (phase_reached IN ('bar', 'P0', 'P1', 'P2', 'P3', 'P4')),
    outcome                TEXT CHECK (outcome IN ('passed', 'failed', 'error', 'plateaued')),
    -- Structured so A5 can aggregate. Free text is not acceptable here. The
    -- three bug categories route back to A2 and must NEVER be recorded as
    -- research conclusions or pollute the knowledge base (TRD §10.1).
    failure_reason         TEXT CHECK (failure_reason IN (
                               'no_signal', 'negative_expectancy', 'costs_exceed_edge', 'overfit_in_sample',
                               'walk_forward_unstable', 'regime_dependent', 'pbo_too_high',
                               'deflated_sharpe_insufficient', 'insufficient_trades', 'monte_carlo_ruin_risk',
                               'parameter_sensitive', 'capacity_constrained', 'plateaued_below_bar',
                               'code_error', 'look_ahead_detected', 'data_leakage_detected')),
    -- Provenance (TRD §6.6) — mandatory on every experiment.
    eval_engine_version    TEXT,
    market_profile_hash    TEXT,
    timeframe_profile_hash TEXT,
    cost_model_hash        TEXT,
    -- Hash of {scheme, train_years, test_years}. Train length carries the same
    -- hidden-multiple-testing risk as scheme choice, so it is hashed too.
    wf_config_hash         TEXT,
    operator_library_version TEXT,
    code_commit            TEXT,               -- the git commit — the link to the code
    data_snapshot_id       INTEGER REFERENCES data_snapshots(id),
    random_seed            INTEGER,
    -- Set false when engine / profile / wf-config changes invalidate comparison.
    comparable             {{BOOL}} NOT NULL DEFAULT 1,
    tokens_spent           INTEGER NOT NULL DEFAULT 0,
    compute_seconds        INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL,
    completed_at           TEXT,
    UNIQUE (strategy_id, iteration)
);

-- A3's output. NEVER contains code — it is a research instruction (App-Flow §6.4).
CREATE TABLE research_plans (
    id               {{PK}},
    uid              TEXT NOT NULL UNIQUE,
    -- The experiment being reviewed — always a BAR FAILURE (App-Flow §6.1).
    experiment_id    INTEGER NOT NULL REFERENCES experiments(id),
    strategy_id      INTEGER NOT NULL REFERENCES strategies(id),
    -- No 'promote' verdict: clearing the bar bypasses A3 entirely.
    verdict          TEXT NOT NULL CHECK (verdict IN ('iterate', 'plateau', 'reject')),
    diagnosis        TEXT,
    evidence_cited   {{JSON}},
    proposed_changes {{JSON}},
    expected_effect  TEXT,
    confidence       REAL,
    prompt_version   TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE code_versions (
    id                   {{PK}},
    uid                  TEXT NOT NULL UNIQUE,
    experiment_id        INTEGER NOT NULL REFERENCES experiments(id),
    code_path            TEXT,
    code_hash            TEXT,
    git_commit           TEXT,
    diff_from_parent     TEXT,
    -- A2's plain-language description, including any objection to the plan it
    -- implemented anyway.
    change_summary       TEXT,
    implements_plan_id   INTEGER REFERENCES research_plans(id),
    compile_ok           {{BOOL}},
    static_check_results {{JSON}},
    prompt_version       TEXT,
    created_at           TEXT NOT NULL
);

-- One row per phase run of evaluate.py.
CREATE TABLE evaluations (
    id                     {{PK}},
    uid                    TEXT NOT NULL UNIQUE,
    experiment_id          INTEGER NOT NULL REFERENCES experiments(id),
    phase                  TEXT NOT NULL CHECK (phase IN ('bar', 'P0', 'P1', 'P2', 'P3', 'P4')),
    result                 TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'warn', 'error')),
    -- The hard bar (TRD §7.5). On 'fail', NO SCORE IS COMPUTED AT ALL.
    bar_result             TEXT CHECK (bar_result IN ('pass', 'fail')),
    bar_failed_on          TEXT CHECK (bar_failed_on IN (
                               'min_trades', 'max_drawdown', 'breadth', 'cost_stress', 'complexity')),
    -- The honest score (TRD §7): sr_oos - z*se_sr - trials_haircut, taken as the
    -- MAX across the three train windows. The single float that drives keep/discard.
    honest_score           REAL,
    sr_oos                 REAL,
    se_sr                  REAL,
    z_multiplier           REAL,
    trials_haircut         REAL,
    -- The family trial count fed into the haircut. INCLUDES the x3 from
    -- best-of-three-windows (TRD §8.6).
    n_trials_used          INTEGER,
    -- Best-of-three train windows (TRD §8.2) — all three stored, always.
    score_train_1y         REAL,
    score_train_2y         REAL,
    score_train_3y         REAL,
    winning_train_years    INTEGER,
    -- max - min across the three. A DIAGNOSTIC, NOT A GATE: 0.61/0.58/0.60 is
    -- robust to history length; 0.62/0.11/0.09 is not.
    train_window_spread    REAL,
    oos_skew               REAL,
    oos_kurtosis           REAL,
    oos_n_obs              INTEGER,
    autocorr_adjusted      {{BOOL}},
    complexity_count       INTEGER,
    -- Walk-forward configuration (TRD §8).
    wf_scheme              TEXT CHECK (wf_scheme IN ('rolling', 'anchored', 'holdout', 'cpcv')),
    wf_train_years_evaluated {{JSON}},
    wf_test_years          INTEGER,
    wf_train_bars          INTEGER,
    wf_test_bars           INTEGER,
    -- Does NOT inflate n_trials_used (tuning selects on train only), but a large
    -- grid raises internal fold overfitting, visible in wf_efficiency.
    params_grid_size       INTEGER,
    tuned_params_per_fold  {{JSON}},
    -- Embargo must be >= holding period or trades leak across the split.
    embargo_bars           INTEGER,
    holding_period_bars    INTEGER,
    n_folds                INTEGER,
    -- The consistency diagnostic that concatenation hides.
    folds_profitable       INTEGER,
    fold_metrics           {{JSON}},
    wf_efficiency          REAL,
    params_refit_per_fold  {{BOOL}},
    -- Core metrics.
    sharpe                 REAL,
    sortino                REAL,
    calmar                 REAL,
    cagr                   REAL,
    total_return           REAL,
    max_drawdown           REAL,
    avg_drawdown           REAL,
    dd_duration_days       REAL,
    profit_factor          REAL,
    win_rate               REAL,
    expectancy             REAL,
    trade_count            INTEGER,
    avg_trade_return       REAL,
    turnover               REAL,
    exposure_pct           REAL,
    -- Robustness metrics (P3).
    deflated_sharpe        REAL,
    pbo                    REAL,
    white_rc_pvalue        REAL,
    mc_p5_return           REAL,
    mc_p50_return          REAL,
    mc_p95_return          REAL,
    mc_ruin_probability    REAL,
    param_sensitivity_score REAL,
    cost_breakeven_multiplier REAL,
    regime_consistency_score REAL,
    -- Artifacts and cost.
    equity_curve_path      TEXT,
    tradebook_path         TEXT,
    report_path            TEXT,
    metrics_json           {{JSON}},
    duration_seconds       REAL,
    cpu_seconds            REAL,
    n_workers              INTEGER,
    -- Exceeded the budget -> recorded as `crash`, not `discard`.
    timed_out              {{BOOL}} NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL
);

-- Individual test outcomes within a phase: "which specific gate failed, and by
-- how much?"
CREATE TABLE evaluation_tests (
    id            {{PK}},
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    test_name     TEXT NOT NULL,
    category      TEXT CHECK (category IN ('correctness', 'performance', 'robustness', 'cost', 'regime')),
    result        TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'warn')),
    gating        {{BOOL}} NOT NULL DEFAULT 0,
    -- BOTH stored — a passing test with an invisible threshold is not evidence.
    value         REAL,
    threshold     REAL,
    detail        TEXT
);

CREATE TABLE regime_performance (
    id            {{PK}},
    evaluation_id INTEGER NOT NULL REFERENCES evaluations(id),
    regime        TEXT NOT NULL CHECK (regime IN ('trending', 'sideways', 'high_vol', 'low_vol', 'crisis')),
    sharpe        REAL,
    cagr          REAL,
    max_drawdown  REAL,
    trade_count   INTEGER,
    period_start  TEXT,
    period_end    TEXT
);
