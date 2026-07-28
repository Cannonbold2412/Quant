-- Data integrity (Backend-Schema §12, TRD §14).
--
-- These tables come first because `experiments.data_snapshot_id` points here:
-- an experiment references a snapshot, never "the files on disk that day".
--
-- They defend against a different threat from the research-integrity tables in
-- 0005. Those stop the *process* fooling us; these stop the *data* fooling us —
-- and null-world calibration structurally cannot catch the latter, because the
-- null generator inherits the same corrupted assumptions (TRD §14.1).

CREATE TABLE data_snapshots (
    id                        {{PK}},
    uid                       TEXT NOT NULL UNIQUE,
    market                    TEXT NOT NULL,
    timeframe                 TEXT NOT NULL,
    -- Keys the cost model together with `market` (TRD §6.3). NSE charges a
    -- delivery equity trade, an intraday trade and an index future differently.
    asset_class               TEXT NOT NULL CHECK (asset_class IN (
                                  'cash_equity', 'etf', 'future', 'cfd', 'spot_crypto', 'perpetual')),
    period_start              TEXT NOT NULL,
    period_end                TEXT NOT NULL,
    instrument_count          INTEGER,
    bar_count                 INTEGER,
    -- Path to the RAW, unadjusted OHLCV, relative to the configured data root
    -- (Backend-Schema §16). Never rewritten (TRD §14.2a).
    storage_path              TEXT NOT NULL,
    raw_content_hash          TEXT NOT NULL,
    -- Together with `raw_content_hash` this is the snapshot's true identity: a
    -- new split bumps THIS, not the price hash, so one corporate action does
    -- not invalidate the entire archive.
    corporate_actions_version TEXT NOT NULL,
    adjustment_method         TEXT NOT NULL CHECK (adjustment_method IN (
                                  'back_ratio_price', 'back_ratio_total_return', 'none')),
    -- Both flags are REJECTED AT P0 when false, not merely warned about.
    adjusted                  {{BOOL}} NOT NULL DEFAULT 0,
    point_in_time_membership  {{BOOL}} NOT NULL DEFAULT 0,
    -- Currently false for Indian equities — blocked on collecting price history
    -- for all ~100-150 ever-members of NIFTY-50 (TRD §14.5).
    survivorship_handled      {{BOOL}} NOT NULL DEFAULT 0,
    -- If true, the research loop has NO read path to this data (TRD §15.2).
    in_vault                  {{BOOL}} NOT NULL DEFAULT 0,
    -- Cannot be 'valid' while any data_validation_flags row is 'pending'.
    validation_status         TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (validation_status IN ('pending', 'valid', 'invalid')),
    validation_report         {{JSON}},
    created_at                TEXT NOT NULL,
    UNIQUE (raw_content_hash, corporate_actions_version)
);

-- Splits, bonuses and dividends. Append-only and versioned — this table exists
-- so raw price history never has to be rewritten (TRD §14.2a).
CREATE TABLE corporate_actions (
    id          {{PK}},
    uid         TEXT NOT NULL UNIQUE,
    instrument  TEXT NOT NULL,
    market      TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('split', 'bonus', 'dividend', 'consolidation')),
    -- The date from which the adjustment applies BACKWARDS.
    ex_date     TEXT NOT NULL,
    -- Split 1:N -> 1/N. Bonus a:b -> b/(a+b). Dividend D at price P -> (P-D)/P.
    ratio       REAL NOT NULL CHECK (ratio > 0),
    -- As published, e.g. '1:2' — kept so `ratio` stays auditable.
    raw_terms   TEXT,
    source      TEXT,
    -- NULL means unverified. Unverified actions must not silently affect prices,
    -- so the adjustment pipeline refuses to apply them.
    verified_by TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (instrument, market, action_type, ex_date, ratio)
);

-- Point-in-time index constituents (TRD §14.3). Answers "which stocks were
-- actually in NIFTY-50 on this date?" — without it every equity backtest
-- carries survivorship bias.
--
-- The union of all rows is ~100-150 tickers over 2000-2025, NOT 50. Price
-- history is required for every one: the companies that left are exactly the
-- ones whose losses are currently invisible.
CREATE TABLE index_membership (
    id                   {{PK}},
    uid                  TEXT NOT NULL UNIQUE,
    index_name           TEXT NOT NULL,
    instrument           TEXT NOT NULL,
    effective_from       TEXT NOT NULL,
    -- NULL = still a member. Resolution is the half-open interval
    -- [effective_from, effective_to).
    effective_to         TEXT,
    reason_added         TEXT CHECK (reason_added IN ('periodic_review', 'ipo_inclusion', 'replacement')),
    reason_removed       TEXT CHECK (reason_removed IN (
                             'periodic_review', 'merger', 'demerger', 'delisting')),
    -- For mergers — where the entity went, so the transition stays traceable
    -- rather than the ticker simply vanishing.
    successor_instrument TEXT,
    source               TEXT,
    verified_by          TEXT,
    created_at           TEXT NOT NULL,
    UNIQUE (index_name, instrument, effective_from)
);

-- The safety net for what adjustment CANNOT catch: a missing corporate action
-- is invisible to the adjustment pipeline itself (TRD §14.4).
CREATE TABLE data_validation_flags (
    id             {{PK}},
    uid            TEXT NOT NULL UNIQUE,
    snapshot_id    INTEGER NOT NULL REFERENCES data_snapshots(id),
    instrument     TEXT,
    bar_date       TEXT,
    flag_type      TEXT NOT NULL CHECK (flag_type IN (
                       'unexplained_jump', 'universe_too_narrow', 'zero_volume', 'stale_price', 'gap')),
    observed_value REAL,
    threshold      REAL,
    -- A snapshot with 'pending' flags cannot be marked valid, and the scheduler
    -- will not dispatch experiments against it. Every flag is either a real
    -- market event or a data error, and a human must say which.
    resolution     TEXT NOT NULL DEFAULT 'pending' CHECK (resolution IN (
                       'pending', 'genuine_move', 'missing_action_added', 'data_error')),
    detail         TEXT,
    resolved_by    TEXT,
    resolved_at    TEXT,
    created_at     TEXT NOT NULL
);
