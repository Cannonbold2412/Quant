# Backend Schema — AQRL

> **Status:** Living document. Updated after every design session.
> **Last updated:** 2026-07-27
> **Target:** SQLite for v1, PostgreSQL-compatible by design. No SQLite-only features.

---

## 0. Conventions

- Every table has `id INTEGER PRIMARY KEY` plus a `uid TEXT UNIQUE` (UUID) for cross-system references.
- Timestamps are `TEXT` ISO-8601 UTC (`created_at`, `updated_at`). UTC always; local time is a display concern.
- JSON blobs are `TEXT` with a documented shape. Anything queried or filtered gets a real column.
- Enums are `TEXT` with a `CHECK` constraint, listed in §11.
- Nothing is hard-deleted. Use status transitions and `archived_at`.
- Money is stored as integer minor units with an explicit `currency`. Never float.
- Foreign keys are enforced.

---

## 0A. What to build first ★

**This document describes the destination, not the starting point.** Building 15 tables before running one experiment is designing the archive before doing the science.

**v1 (nanoAQRL, TRD §2A) uses three tables:**

| Table | Why on day one |
|---|---|
| `strategies` | The research thread, carrying `family` — required for trial counting |
| `experiments` | One row per attempt: provenance, `code_commit`, `status` |
| `evaluations` | The metrics produced by `evaluate.py` |

Add `jobs` when the scheduler arrives. Add `vault_access_log` and `null_world_runs` (§15) alongside the integrity work, which precedes any real-data result. Everything else is added on **felt need** — when a question arrives that the existing tables cannot answer. The designs already exist here, so later addition is cheap.

### 0A.1 Code lives in git, not in the database

Strategy code is **never** stored as a blob. git holds the code, diffs and history; SQLite holds metadata and metrics; `experiments.code_commit` links them. Storing code in the database loses diffs, blame, and the ability to check out and re-run a past experiment.

The reference project (PRD §14) uses git as the *entire* experiment database. We diverge because deflated Sharpe needs a queryable trial count — "how many attempts in this family?" cannot be answered from `git log` — and because deployment, paper trading and health state need real storage.

---

## 1. Entity Overview

```
research_goals
     │
     ▼
strategies ────────────► strategy_specs ────► spec_operators ────► operators
     │                        │
     │                        ▼
     ├──────────────► experiments ──┬──► code_versions
     │                    │         ├──► evaluations ──► evaluation_tests
     │                    │         ├──► research_plans
     │                    │         └──► lab_notebooks
     │                    ▼
     ├──────────────► promotions
     │                    │
     ├──────────────► deployments ──┬──► trades
     │                              ├──► health_checks
     │                              └──► lifecycle_events
     ▼
knowledge_entries ──► knowledge_edges
research_questions
external_documents ──► external_knowledge

jobs · budgets · audit_log · data_snapshots · market_profiles · timeframe_profiles
```

---

## 2. Research Direction

### `research_goals`
Top-level direction set by the human Research Director. Drives A1's hypothesis budget.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| title | TEXT | "Find robust swing alpha in Indian equities" |
| description | TEXT | |
| market | TEXT | FK-ish → `market_profiles.name`, nullable for cross-market goals |
| timeframe | TEXT | nullable |
| allocation_bucket | TEXT | `incremental` \| `cross_market` \| `exploratory` — the 70/20/10 split (PRD §4.4) |
| priority | INTEGER | |
| hypothesis_budget | INTEGER | Max specs A1 may generate for this goal |
| hypotheses_used | INTEGER | |
| status | TEXT | `active` \| `paused` \| `completed` \| `abandoned` |
| created_by | TEXT | `human` \| `agent5` |
| created_at, updated_at | | |

---

## 3. Strategy & Spec

### `strategies`
The durable identity of a research thread. One strategy has many experiments (iterations).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| name | TEXT | Human-readable |
| family | TEXT | e.g. `jma_atr_trend` — **critical for trial-counting** (TRD §5.2) |
| goal_id | FK → research_goals | |
| market | TEXT | |
| timeframe | TEXT | |
| status | TEXT | See §11.1 |
| current_experiment_id | FK → experiments | Latest iteration |
| best_experiment_id | FK → experiments | Best by primary score |
| iteration_count | INTEGER | **Feeds the multiple-testing correction** |
| total_trials | INTEGER | Iterations + parameter combinations swept |
| best_score | REAL | Primary composite score |
| plateau_counter | INTEGER | Consecutive iterations with no meaningful gain |
| tokens_spent, compute_seconds | INTEGER | Cost accounting |
| quarantined | INTEGER (bool) | Poison-pill protection (TRD §3.3) |
| quarantine_reason | TEXT | |
| created_at, updated_at, archived_at | | |

### `strategy_specs`
A1's output. Immutable once created; a revised spec is a new row.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id | FK | |
| version | INTEGER | |
| hypothesis | TEXT | The falsifiable claim, in plain language |
| rationale | TEXT | Why A1 believes this — the "why did you do this?" record |
| entry_logic, exit_logic, filter_logic, risk_logic | TEXT (JSON) | Operator compositions |
| universe | TEXT (JSON) | Instrument selection rules |
| parameters | TEXT (JSON) | Parameter names, defaults, and **allowed ranges** |
| spec_hash | TEXT | **Canonical hash of the operator DAG — duplicate detection (TRD §6.1)** |
| source_knowledge_ids | TEXT (JSON) | Which knowledge entries inspired this — traceability |
| source_question_id | FK → research_questions | If curiosity-driven |
| expected_behavior | TEXT | What A1 predicts, so we can score A1's calibration |
| prompt_version | TEXT | |
| created_at | | |

> `spec_hash` has a UNIQUE index. An exact re-run is rejected at insert time.

### `operators`
The vetted building-block library (TRD §6).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| name | TEXT | `jma`, `atr_stop`, `vol_expansion` |
| category | TEXT | `transformation` \| `signal` \| `risk` \| `portfolio` |
| version | TEXT | |
| implementation_ref | TEXT | Module path |
| parameters | TEXT (JSON) | Name, type, default, valid range |
| valid_markets, valid_timeframes | TEXT (JSON) | Applicability declarations |
| description, references | TEXT | |
| test_status | TEXT | `tested` \| `untested` \| `deprecated` |
| approved_by | TEXT | `human` \| null — library is not self-modifying |
| created_at | | |

### `spec_operators`
Join table making operator usage queryable — "which experiments ever used a Kalman filter?"

| Column | Type |
|---|---|
| spec_id | FK → strategy_specs |
| operator_id | FK → operators |
| role | TEXT (`entry`/`exit`/`filter`/`risk`) |
| parameters_used | TEXT (JSON) |

---

## 4. Experiments & Evaluation

### `experiments`
**The central table.** One row per iteration of the A2↔A3 loop.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id | FK | |
| spec_id | FK | |
| iteration | INTEGER | 1-based within the strategy |
| parent_experiment_id | FK → experiments | Lineage across iterations |
| research_plan_id | FK → research_plans | The plan that produced this iteration (null for iteration 1) |
| code_version_id | FK → code_versions | |
| status | TEXT | See §11.2 |
| phase_reached | TEXT | `P0`…`P4` |
| outcome | TEXT | `passed` \| `failed` \| `error` \| `plateaued` |
| failure_reason | TEXT | Structured category, see §11.5 |
| primary_score | REAL | Composite used for ranking |
| **Provenance (TRD §4.7)** | | |
| eval_engine_version | TEXT | |
| market_profile_hash | TEXT | |
| timeframe_profile_hash | TEXT | |
| operator_library_version | TEXT | |
| data_snapshot_id | FK → data_snapshots | |
| random_seed | INTEGER | |
| comparable | INTEGER (bool) | Set false when engine/profile changes invalidate comparison |
| tokens_spent, compute_seconds | INTEGER | |
| created_at, completed_at | | |

> **Index on `(strategy_id, iteration)`, `(eval_engine_version, market_profile_hash, timeframe_profile_hash)`.**
> The second index answers "which stored results are still comparable?" instantly.

### `code_versions`
Every implementation A2 produces.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| experiment_id | FK | |
| code_path | TEXT | Path to the generated strategy module |
| code_hash | TEXT | Content hash |
| git_commit | TEXT | If committed |
| diff_from_parent | TEXT | What changed vs the previous iteration |
| change_summary | TEXT | A2's plain-language description |
| implements_plan_id | FK → research_plans | |
| compile_ok | INTEGER (bool) | |
| static_check_results | TEXT (JSON) | Look-ahead/leakage scan output (TRD §5.1) |
| prompt_version | TEXT | |
| created_at | | |

### `evaluations`
One row per phase run of `evaluate.py`.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| experiment_id | FK | |
| phase | TEXT | `P0` \| `P1` \| `P2` \| `P3` \| `P4` |
| result | TEXT | `pass` \| `fail` \| `warn` \| `error` |
| **★ The honest score (TRD §4A)** | | |
| bar_result | TEXT | `pass` \| `fail` — the pre-registered gate. On `fail`, no score is computed |
| bar_failed_on | TEXT | Which bar item failed: `min_trades` \| `max_drawdown` \| `breadth` \| `cost_stress` \| `complexity` |
| **honest_score** | REAL | `sr_oos − z·se_sr − trials_haircut`. **The single float that drives keep/discard** |
| sr_oos | REAL | Sharpe on concatenated purged/embargoed walk-forward test windows, at 2× costs |
| se_sr | REAL | Standard error incl. skew and kurtosis terms |
| z_multiplier | REAL | 2.0 (~97.5% one-sided) or 1.65 (~95%) — recorded, since changing it changes comparability |
| trials_haircut | REAL | `SR*(N_trials)` — expected best-under-null for this family |
| n_trials_used | INTEGER | The family trial count fed into the haircut |
| oos_skew, oos_kurtosis, oos_n_obs | REAL/INT | Inputs to `se_sr`, stored for audit |
| embargo_bars, holding_period_bars | INTEGER | Embargo must be ≥ holding period or trades leak across the split |
| **wf_scheme** | TEXT | `rolling` \| `anchored` \| `holdout` \| `cpcv`. **Hashed into provenance** — changing it invalidates comparability (TRD §4A.2g) |
| wf_train_bars, wf_test_bars | INTEGER | Window sizes |
| n_folds | INTEGER | |
| folds_profitable | INTEGER | How many test windows made money — the consistency diagnostic concatenation hides |
| fold_metrics | TEXT (JSON) | Per-fold score, trades, drawdown. **Stored for diagnosis; does not drive keep/discard** |
| params_refit_per_fold | INTEGER (bool) | Whether tuning re-ran on each training window. Determines what walk-forward actually tested (TRD §4A.2i) |
| autocorr_adjusted | INTEGER (bool) | Whether Lo's correction was applied |
| complexity_count | INTEGER | Rules / free parameters — the tertiary criterion |
| **Core metrics** | | |
| sharpe, sortino, calmar | REAL | |
| cagr, total_return | REAL | |
| max_drawdown, avg_drawdown, dd_duration_days | REAL | |
| profit_factor, win_rate, expectancy | REAL | |
| trade_count | INTEGER | |
| avg_trade_return, turnover | REAL | |
| exposure_pct | REAL | |
| **Robustness metrics (P3)** | | |
| deflated_sharpe | REAL | |
| trials_used_in_deflation | INTEGER | **Must reflect true trial count (TRD §5.2)** |
| pbo | REAL | CSCV probability of backtest overfitting |
| white_rc_pvalue | REAL | |
| mc_p5_return, mc_p50_return, mc_p95_return | REAL | Monte Carlo percentiles |
| mc_ruin_probability | REAL | |
| wf_efficiency, wf_windows_passed, wf_windows_total | REAL/INT | Walk-forward |
| param_sensitivity_score | REAL | |
| cost_breakeven_multiplier | REAL | At what cost multiple does the edge vanish |
| regime_consistency_score | REAL | |
| **Artifacts** | | |
| equity_curve_path, tradebook_path | TEXT | Parquet references |
| report_path | TEXT | Full evaluation report |
| metrics_json | TEXT (JSON) | Everything not promoted to a column |
| duration_seconds | INTEGER | |
| created_at | | |

### `evaluation_tests`
Individual test outcomes within a phase — needed to answer "which specific gate failed?"

| Column | Type | Notes |
|---|---|---|
| id | | |
| evaluation_id | FK | |
| test_name | TEXT | `look_ahead_scan`, `deflated_sharpe`, `pbo`, `regime_2008` |
| category | TEXT | `correctness` \| `performance` \| `robustness` \| `cost` \| `regime` |
| result | TEXT | `pass` \| `fail` \| `warn` |
| gating | INTEGER (bool) | Hard fail vs advisory |
| value, threshold | REAL | |
| detail | TEXT | |

### `regime_performance`
Per-regime breakdown. Feeds both promotion checks and the knowledge graph.

| Column | Type |
|---|---|
| evaluation_id | FK |
| regime | TEXT (`trending`/`sideways`/`high_vol`/`low_vol`/`crisis`) |
| sharpe, cagr, max_drawdown, trade_count | REAL/INT |
| period_start, period_end | TEXT |

---

## 5. The Iteration Loop

### `research_plans`
A3's output. **Never contains code** — it is a research instruction (PRD §6.1).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| experiment_id | FK | The experiment being reviewed |
| strategy_id | FK | |
| verdict | TEXT | `iterate` \| `reject` \| `promote` \| `plateau` |
| diagnosis | TEXT | What A3 concluded from the evidence |
| evidence_cited | TEXT (JSON) | Which metrics/tests drove the verdict — the "why" record |
| proposed_changes | TEXT (JSON) | Ordered list, e.g. `[{target: "exit", change: "replace fixed stop with ATR trailing", reason: "..."}]` |
| expected_effect | TEXT | Prediction, so A3's calibration can be scored |
| confidence | REAL | 0–1 |
| improvement_vs_parent | REAL | |
| prompt_version | TEXT | |
| created_at | | |

---

## 6. Promotion & Deployment

### `promotions`
A4's decision. Sees the **entire** research history, not just the final result.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id | FK | |
| best_experiment_id | FK | |
| stage_from, stage_to | TEXT | `research`→`human_review`→`paper`→`live_small`→`live_scaled` |
| decision | TEXT | `approve` \| `reject` \| `defer` |
| rationale | TEXT | |
| evidence_summary | TEXT (JSON) | |
| iterations_considered | INTEGER | **Overfitting signal** |
| overfitting_risk | TEXT | `low` \| `medium` \| `high` |
| confidence | REAL | |
| correlation_with_live | REAL | vs existing portfolio |
| recommended_allocation_pct | REAL | |
| requires_human_approval | INTEGER (bool) | Always 1 for paper and live gates |
| human_decision | TEXT | `approved` \| `rejected` \| `pending` |
| human_decided_by, human_decided_at, human_notes | TEXT | |
| prompt_version | TEXT | |
| created_at | | |

### `deployments`
A strategy running in paper or live mode.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id, experiment_id | FK | The exact validated version deployed |
| mode | TEXT | `paper` \| `live` |
| status | TEXT | `active` \| `paused` \| `stopped` \| `retired` |
| allocation_pct | REAL | |
| capital_minor_units, currency | INTEGER/TEXT | |
| broker_ref | TEXT | |
| started_at, ended_at | TEXT | |
| **Promotion gate tracking (PRD §9.3)** | | |
| trades_required, trades_completed | INTEGER | |
| regimes_required, regimes_observed | TEXT (JSON) | |
| **Expected behavior baseline** | | |
| expected_sharpe, expected_max_dd, expected_win_rate, expected_avg_trade | REAL | Copied from validation — the yardstick for health checks |
| current_health | TEXT | `green` \| `yellow` \| `orange` \| `red` |
| retirement_reason | TEXT | |

### `trades`
Individual paper/live executions. Parquet mirror for analytics; SQLite row for state.

| Column | Type |
|---|---|
| id, uid | |
| deployment_id | FK |
| instrument, side | TEXT |
| entry_time, exit_time | TEXT |
| entry_price, exit_price, quantity | REAL |
| pnl_minor_units, currency | INTEGER/TEXT |
| fees_minor_units | INTEGER |
| **expected_slippage_bps, actual_slippage_bps** | REAL |
| execution_quality | TEXT (`good`/`degraded`/`failed`) |
| regime_at_entry | TEXT |
| signal_reference | TEXT |

---

## 7. Health & Lifecycle

### `health_checks`
The Strategy Lifecycle Manager's periodic verdict (PRD §9.4).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| deployment_id | FK | |
| check_time | TEXT | |
| level | TEXT | `green` \| `yellow` \| `orange` \| `red` |
| **Performance** | | |
| live_sharpe, live_max_dd, live_win_rate, live_profit_factor | REAL | |
| **Deviation from validated baseline** | | |
| sharpe_zscore, win_rate_zscore, avg_trade_zscore | REAL | |
| loss_distribution_pvalue | REAL | Are losses outside the historical distribution? |
| **Context** | | |
| current_regime | TEXT | |
| regime_historically_weak | INTEGER (bool) | A drawdown in a known-weak regime is *expected*, not evidence of death |
| **Execution** | | |
| slippage_deviation, missed_fill_rate, liquidity_change | REAL | |
| verdict_reason | TEXT | |
| recommended_action | TEXT | `continue` \| `reduce` \| `pause` \| `stop` |

### `lifecycle_events`
Immutable audit trail of everything that happened to a deployment.

| Column | Type |
|---|---|
| id, uid | |
| deployment_id, strategy_id | FK |
| event_type | TEXT (`deployed`/`scaled_up`/`scaled_down`/`paused`/`resumed`/`retired`/`health_change`/`kill_switch`) |
| from_state, to_state | TEXT |
| triggered_by | TEXT (`agent`/`human`/`automatic_rule`) |
| reason | TEXT |
| evidence | TEXT (JSON) |
| created_at | TEXT |

---

## 8. Knowledge

### `knowledge_entries`
A5's output. The permanent scientific record.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| experiment_id, strategy_id | FK | Nullable — cross-experiment lessons have neither |
| entry_type | TEXT | `experiment_record` \| `lesson` \| `global_rule` \| `pattern` |
| scope | TEXT | `experiment` \| `family` \| `market` \| `global` |
| title | TEXT | |
| statement | TEXT | "ATR multipliers above 3.0 consistently overfit in trend families" |
| evidence | TEXT (JSON) | Supporting experiment IDs |
| evidence_count | INTEGER | How many experiments back this |
| counter_evidence_count | INTEGER | Honest bookkeeping |
| confidence | REAL | |
| applicable_markets, applicable_timeframes, applicable_regimes | TEXT (JSON) | |
| future_ideas | TEXT (JSON) | **Mandatory — this is what self-propels the lab** |
| embedding_id | TEXT | Vector index reference |
| superseded_by | FK → knowledge_entries | Knowledge is revised, never deleted |
| created_at | | |

### `knowledge_edges`
The knowledge graph (TRD §7.4). **An edge with no experiment backing must not exist.**

| Column | Type | Notes |
|---|---|---|
| id | | |
| subject | TEXT | `momentum`, `jma`, `atr_stop` |
| predicate | TEXT | `works_in` \| `fails_in` \| `pairs_well_with` \| `pairs_poorly_with` \| `requires` \| `degrades_with` |
| object | TEXT | `high_volatility`, `commodities`, `rsi` |
| confidence | REAL | |
| evidence_count, counter_evidence_count | INTEGER | |
| supporting_experiments | TEXT (JSON) | |
| first_observed_at, last_updated_at | TEXT | |

> UNIQUE on `(subject, predicate, object)`. Repeated observation updates counts, not duplicate rows.

### `lab_notebooks`
The human-readable record per experiment (TRD §10).

| Column | Type |
|---|---|
| id, uid | |
| experiment_id | FK |
| hypothesis, result, reason, evidence | TEXT |
| confidence | REAL |
| next_questions | TEXT (JSON) — **mandatory, non-empty** |
| rendered_markdown | TEXT |
| created_at | TEXT |

---

## 9. External Knowledge

### `external_documents`
Raw ingested artifacts. Read once, ever.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| source | TEXT | `arxiv` \| `ssrn` \| `github` \| `blog` \| `journal` |
| source_id, url, title, authors, published_at | TEXT | |
| content_hash | TEXT | UNIQUE — deduplication |
| raw_path | TEXT | Archived original |
| ingested_at, extracted_at | TEXT | |
| extraction_status | TEXT | `pending` \| `done` \| `failed` \| `irrelevant` |
| relevance_score | REAL | Cheap filter before spending LLM tokens |

### `external_knowledge`
The structured extraction. **Store knowledge, not documents** (PRD §8.2).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| document_id | FK | |
| layer | TEXT | `research` \| `market` \| `software` \| `infrastructure` |
| core_idea | TEXT | |
| category | TEXT | |
| applicable_markets, applicable_timeframes | TEXT (JSON) | |
| strengths, weaknesses | TEXT | |
| implementation_difficulty | TEXT | `low` \| `medium` \| `high` |
| proposed_experiments | TEXT (JSON) | Directly consumable by A1 |
| required_operators | TEXT (JSON) | Operators that would need to exist |
| novelty_score, confidence | REAL | |
| related_knowledge_ids | TEXT (JSON) | |
| embedding_id | TEXT | |
| used_in_specs | TEXT (JSON) | Did this ever produce a hypothesis? |

### `research_questions`
The curiosity queue (TRD §7.3).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| question | TEXT | "How do practitioners make momentum volatility-adaptive?" |
| motivation | TEXT | |
| origin_type | TEXT | `experiment_failure` \| `pattern_detection` \| `human` \| `live_degradation` |
| origin_experiment_id, origin_knowledge_id | FK | |
| priority | INTEGER | |
| status | TEXT | `open` \| `searching` \| `answered` \| `abandoned` |
| search_terms | TEXT (JSON) | Handed to collectors |
| answer_knowledge_ids | TEXT (JSON) | |
| produced_spec_ids | TEXT (JSON) | **Did asking this ever pay off?** |
| created_at, resolved_at | | |

---

## 10. Infrastructure Tables

### `jobs`
The queue. Lease-based claiming so v1→v3 migration is a backend swap (TRD §2.2).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| job_type | TEXT | See §11.3 |
| payload | TEXT (JSON) | |
| strategy_id, experiment_id | FK | Nullable |
| status | TEXT | `pending` \| `claimed` \| `running` \| `succeeded` \| `failed` \| `timed_out` \| `cancelled` |
| priority | INTEGER | |
| **claimed_by, lease_expires_at, heartbeat_at** | TEXT | Dead-worker recovery |
| attempts, max_attempts | INTEGER | |
| failure_class | TEXT | `transient` \| `deterministic` — determines retry behavior |
| depends_on_job_id | FK → jobs | |
| scheduled_for | TEXT | Delayed execution |
| error_message, error_trace | TEXT | |
| tokens_spent, duration_seconds | INTEGER | |
| created_at, started_at, completed_at | | |

> Index on `(status, priority, scheduled_for)` — the scheduler's hot path.

### `data_snapshots`
Immutable dataset versions. An experiment references a snapshot, never "the files on disk."

| Column | Type |
|---|---|
| id, uid | |
| market, timeframe | TEXT |
| period_start, period_end | TEXT |
| instrument_count, bar_count | INTEGER |
| storage_path | TEXT |
| content_hash | TEXT |
| adjustment_method | TEXT |
| survivorship_handled | INTEGER (bool) |
| validation_status, validation_report | TEXT |
| created_at | TEXT |

### `market_profiles` / `timeframe_profiles`
Registry of resolved profiles (TRD §4). Content-hashed so experiments can pin them.

| Column | Type |
|---|---|
| id, uid | |
| name | TEXT UNIQUE |
| version | TEXT |
| config | TEXT (JSON) |
| config_hash | TEXT UNIQUE |
| active | INTEGER (bool) |
| superseded_by | FK |
| created_at | TEXT |

### `budgets`
Scheduler back-pressure (TRD §3.4).

| Column | Type |
|---|---|
| id | |
| scope | TEXT (`global`/`strategy`/`goal`) |
| scope_id | INTEGER |
| budget_type | TEXT (`tokens`/`experiments`/`iterations`/`compute_seconds`/`usd`) |
| period | TEXT (`day`/`week`/`lifetime`) |
| limit_value, used_value | INTEGER |
| period_start | TEXT |
| exhausted | INTEGER (bool) |

### `audit_log`
Answers "why did you do this?" for every system action (PRD §10.1).

| Column | Type |
|---|---|
| id, uid | |
| actor | TEXT (`agent1`…`agent5`/`scheduler`/`human`/`evaluate`) |
| action | TEXT |
| entity_type, entity_id | TEXT/INTEGER |
| reasoning | TEXT |
| evidence | TEXT (JSON) |
| prompt_version, model_version | TEXT |
| created_at | TEXT |

---

## 11. Enumerations

### 11.1 `strategies.status`
```
draft → spec_ready → coding → evaluating → evaluated → in_review
      → iterating (loops back to coding)
      → plateaued | rejected
      → pending_promotion → awaiting_human_review
      → paper_trading → pending_live_review
      → live_small → live_scaled
      → retired | quarantined
```

### 11.2 `experiments.status`
```
created → code_pending → code_ready → evaluating → evaluated → reviewed → archived
        → failed | error
```

### 11.3 `jobs.job_type`
```
GENERATE_SPEC      (A1)
IMPLEMENT          (A2)
FIX_CODE           (A2, from a P0 failure)
EVALUATE           (evaluate.py, no LLM)
REVIEW             (A3)
PROMOTE            (A4)
ARCHIVE            (A5)
MINE_PATTERNS      (A5, cross-experiment, weekly)
COLLECT_PAPERS · COLLECT_GITHUB · COLLECT_MARKET_DATA   (Python collectors)
EXTRACT_KNOWLEDGE  (LLM, once per document)
MONITOR_DEPLOYMENT (health checks)
GENERATE_REPORT
```

### 11.4 Regimes
`trending` · `sideways` · `high_vol` · `low_vol` · `crisis`

### 11.5 `experiments.failure_reason`
Structured so A5 can aggregate. Free text is not acceptable here.
```
no_signal · negative_expectancy · costs_exceed_edge · overfit_in_sample
walk_forward_unstable · regime_dependent · pbo_too_high · deflated_sharpe_insufficient
insufficient_trades · monte_carlo_ruin_risk · parameter_sensitive
correlated_with_existing · capacity_constrained
code_error · look_ahead_detected · data_leakage_detected
```

> The last three are **bugs, not findings** (TRD §5.1). They route back to A2 and must not be recorded as research conclusions.

---

## 12. Key Queries the Schema Must Answer Fast

These drove the design. Each must be a simple indexed query, not a scan.

1. *"How many trials have been run in this strategy family?"* → deflated Sharpe correctness
2. *"Which stored results are still comparable to the current engine version?"* → `comparable` + provenance index
3. *"Has this exact operator composition been tried before?"* → `spec_hash` unique index
4. *"Why did every experiment using ATR > 3.0 fail?"* → `spec_operators` + `failure_reason`
5. *"Which knowledge entries have contradicting evidence?"* → `counter_evidence_count > 0`
6. *"Is this live strategy behaving like the validated version?"* → `health_checks` vs `deployments.expected_*`
7. *"Which research questions ever produced a usable hypothesis?"* → `research_questions.produced_spec_ids`
8. *"What is the cost per credible discovery?"* → `tokens_spent` aggregated against promoted strategies
9. *"Reproduce experiment #12,483 exactly."* → spec + code_version + data_snapshot + profiles + seed

---

## 13. Migration Notes

- SQLite first, but **no SQLite-specific SQL.** No `AUTOINCREMENT` reliance, no dynamic typing tricks.
- JSON columns become `JSONB` in PostgreSQL.
- The `jobs` table moves to Redis in v2; the lease/heartbeat model already matches Redis semantics, so agent code does not change.
- Parquet paths are relative to a configurable root so local → object storage is a config change.

---

## 14. Open Schema Questions

- [ ] Do parameter sweeps get one `experiment` row each, or one row with a child `sweep_runs` table? (Affects trial counting.)
- [ ] Should `trades` live in SQLite at all, or Parquet-only with SQLite holding aggregates?
- [ ] Versioning strategy for `knowledge_entries` when A5 revises a lesson — supersede chain vs in-place with history table
- [ ] Portfolio-level tables (multi-strategy allocation, correlation matrix) — deferred until A4 handles portfolios
- [ ] Retention policy for `evaluations.metrics_json` at millions of rows

---

## 15. Integrity Tables

Added alongside the integrity work (TRD §8A), which precedes any real-data result.

### `vault_access_log`
Every opening of the locked holdout. The vault is the one defence that does not depend on honestly counting trials, so its own bookkeeping must be exact.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id, family | FK / TEXT | Budget is consumed **per family**, not per strategy |
| vault_segment | TEXT | Which locked span/instruments/market was opened |
| opened_at | TEXT | |
| opened_by | TEXT | `human` \| `promotion_gate` — never the research loop |
| reason | TEXT | |
| promotion_id | FK → promotions | The decision this unlock served |
| budget_before, budget_after | INTEGER | Remaining lifetime opens for this family |
| result_score | REAL | What the vault said |
| outcome | TEXT | `confirmed` \| `contradicted` |

> A family whose budget reaches zero cannot be promoted again until genuinely new data exists.

### `null_world_runs`
The headline integrity metric (PRD §4.5). Re-run after any change to `evaluate.py`, the scoring rule, or a profile.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| run_label | TEXT | |
| null_model | TEXT | `permuted_returns` \| `block_bootstrap` \| `synthetic_gbm` \| `synthetic_fat_tail` |
| replications | INTEGER | |
| eval_engine_version | TEXT | What was being calibrated |
| scoring_rule_version | TEXT | |
| experiments_run | INTEGER | |
| **discoveries_reported** | INTEGER | The number that matters |
| **false_discovery_rate** | REAL | discoveries / replications |
| max_score_observed | REAL | The best "strategy" found in pure noise — a useful bar for real results |
| verdict | TEXT | `pipeline_trusted` \| `pipeline_suspect` |
| notes | TEXT | |
| created_at | | |

### `acceptance_bars`
The satisficing bar (PRD §13.2), recorded **before** a campaign begins so it cannot be adjusted after seeing results.

| Column | Type |
|---|---|
| id, uid | |
| campaign_label | TEXT |
| min_score, max_drawdown, min_trades, min_breadth, max_complexity | REAL/INTEGER |
| cost_stress_multiple | REAL |
| z_multiplier | REAL |
| locked_at | TEXT |
| locked_by | TEXT |
| superseded_by | FK |

> Written at campaign start. Changing a bar mid-campaign creates a new row and marks the campaign's prior results incomparable.

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial schema. Experiments as the central table with full provenance columns, trial-count support for deflated Sharpe, spec hashing for duplicate detection, knowledge graph edges with evidence counts, lease-based job queue. |
| 2026-07-27 | Added §0A (build 3 tables first, not 15; code stays in git with `code_commit` linking) and §15 integrity tables — `vault_access_log`, `null_world_runs`, `acceptance_bars`. |
| 2026-07-27 | Added walk-forward columns to `evaluations` — `wf_scheme` (hashed into provenance), window sizes, `n_folds`, `folds_profitable`, per-fold `fold_metrics` stored but non-gating, and `params_refit_per_fold`. |
| 2026-07-27 | Added the honest-score column group to `evaluations` — `honest_score` plus every input to it (`sr_oos`, `se_sr`, `z_multiplier`, `trials_haircut`, skew/kurtosis/n, embargo vs holding period) and the `bar_result` gate columns. Added `min_breadth` and `z_multiplier` to `acceptance_bars`. |
