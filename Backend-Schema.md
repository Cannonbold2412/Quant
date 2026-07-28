# Backend Schema — AQRL

> **Status:** Design complete for v1. No implementation started.
> **Last updated:** 2026-07-28
> **Target:** SQLite for v1, PostgreSQL-compatible by design. No SQLite-only features.
> **Companion docs:** `TRD.md` (architecture) · `App-Flow.md` (who writes what, when)

---

## 0. Conventions

- Every table has `id INTEGER PRIMARY KEY` plus a `uid TEXT UNIQUE` (UUID) for cross-system references.
- Timestamps are `TEXT` ISO-8601 **UTC**. Local time is a display concern only.
- JSON blobs are `TEXT` with a documented shape. Anything queried or filtered gets a real column.
- Enums are `TEXT` with a `CHECK` constraint, listed in §13.
- **Nothing is hard-deleted.** Use status transitions and `archived_at`.
- Money is stored as integer minor units with an explicit `currency`. **Never float.**
- Foreign keys are enforced.

---

## 1. What to Build First ★

**This document describes the destination, not the starting point.** Building 15 tables before running one experiment is designing the archive before doing the science.

**v1 (nanoAQRL, TRD §2) uses three tables:**

| Table | Why on day one |
|---|---|
| `strategies` | The research thread, carrying `family` — required for trial counting |
| `experiments` | One row per attempt: provenance, `code_commit`, status |
| `evaluations` | The metrics produced by `evaluate.py` |

Add `jobs` when the scheduler arrives. Add the integrity tables (§11) alongside the integrity work, which precedes any real-data result. Everything else is added on **felt need** — when a question arrives that the existing tables cannot answer. The designs already exist here, so later addition is cheap.

### 1.1 Code lives in git, not in the database

Strategy code is **never** stored as a blob. git holds the code, diffs and history; SQLite holds metadata and metrics; `experiments.code_commit` links them. Storing code in the database loses diffs, blame, and the ability to check out and re-run a past experiment.

The reference project uses git as the *entire* experiment database. We diverge because the deflated Sharpe needs a **queryable trial count** — *"how many attempts in this family?"* cannot be answered from `git log` — and because deployment, paper trading and health state need real storage. See TRD §5.1.

---

## 2. Entity Overview

```
research_goals
     │
     ▼
strategies ────────────► strategy_specs ────► spec_operators ────► operators
     │                        │
     │                        ▼
     ├──────────────► experiments ──┬──► code_versions
     │                    │         ├──► evaluations ──┬──► evaluation_tests
     │                    │         │                  └──► regime_performance
     │                    │         ├──► research_plans
     │                    │         └──► lab_notebooks
     │                    ▼
     ├──────────────► promotions
     │                    │
     ├──────────────► deployments ──┬──► trades
     │                              ├──► health_checks
     │                              └──► lifecycle_events
     ▼
knowledge_entries ──► knowledge_edges                        internal, tested
research_questions
external_documents ──► document_chunks ──► external_knowledge   external, untested

vault_access_log · null_world_runs · acceptance_bars              integrity
jobs · budgets · audit_log · data_snapshots · market_profiles · timeframe_profiles
```

---

## 3. Research Direction

### `research_goals`
Top-level direction set by the human Research Director. Drives A1's hypothesis budget.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| title | TEXT | "Find robust swing alpha in Indian equities" |
| description | TEXT | |
| market | TEXT | Nullable for cross-market goals |
| timeframe | TEXT | Nullable |
| allocation_bucket | TEXT | `incremental` \| `cross_market` \| `exploratory` — the 70/20/10 split (PRD §4.5) |
| priority | INTEGER | |
| hypothesis_budget | INTEGER | Max specs A1 may generate for this goal |
| hypotheses_used | INTEGER | |
| status | TEXT | `active` \| `paused` \| `completed` \| `abandoned` |
| created_by | TEXT | `human` \| `agent5` |
| created_at, updated_at | | |

---

## 4. Strategy & Spec

### `strategies`
The durable identity of a research thread. One strategy has many experiments (iterations).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| name | TEXT | Human-readable |
| **family** | TEXT | e.g. `jma_atr_trend` — **critical for trial counting** (TRD §10.2). The same idea tried in 3 markets is 3 trials in one family, not 3 independent results |
| goal_id | FK → research_goals | |
| market, timeframe | TEXT | |
| status | TEXT | See §13.1 |
| **git_branch** | TEXT | `strategy/<strategy_id>`. Created on the first `IMPLEMENT` job (TRD §5.2). **Never deleted**, including on rejection — git's GC only protects commits reachable from a branch |
| **code_path** | TEXT | `strategies/<strategy_id>/strategy.py` — its own path, so many strategies merge into one deploy branch conflict-free |
| current_experiment_id | FK → experiments | Latest iteration |
| best_experiment_id | FK → experiments | The bar-clearing one, if any |
| iteration_count | INTEGER | **Feeds the multiple-testing correction** |
| total_trials | INTEGER | Iterations + parameter combinations swept |
| best_score | REAL | `honest_score` of the passing experiment; null if never cleared |
| plateau_counter | INTEGER | Consecutive **bar failures** (App-Flow §6.3). Only ever increments — clearing the bar stops the loop immediately, so there is no "improvement" case to reset against |
| tokens_spent, compute_seconds | INTEGER | Cost accounting |
| quarantined | INTEGER (bool) | Poison-pill protection (TRD §4.3) |
| quarantine_reason | TEXT | |
| created_at, updated_at, archived_at | | |

### `strategy_specs`
A1's output. **Immutable** once created; a revised spec is a new row.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id | FK | |
| version | INTEGER | |
| hypothesis | TEXT | The falsifiable claim, in plain language |
| rationale | TEXT | Why A1 believes this — the "why did you do this?" record |
| entry_logic, exit_logic, filter_logic, risk_logic | TEXT (JSON) | Operator compositions |
| universe | TEXT (JSON) | Instrument selection rules |
| parameters | TEXT (JSON) | Names, defaults, and **allowed ranges** |
| **spec_hash** | TEXT UNIQUE | Canonical hash of the operator DAG — duplicate detection (TRD §11.1). An exact re-run is rejected at insert |
| **source_external_knowledge_ids** | TEXT (JSON) | Which `external_knowledge` rows (candidate, untested) inspired this |
| **source_internal_knowledge_ids** | TEXT (JSON) | Which `knowledge_entries` (tested, trusted) this respects or deliberately overrides |
| source_question_id | FK → research_questions | If curiosity-driven |
| expected_behavior | TEXT | A1's prediction, so A1's calibration can be scored later |
| prompt_version | TEXT | |
| created_at | | |

### `operators`
The vetted building-block library (TRD §11).

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
| approved_by | TEXT | `human` or null — **the library is not self-modifying** |
| created_at | | |

### `spec_operators`
Join table making operator usage queryable — *"which experiments ever used a Kalman filter?"*

| Column | Type |
|---|---|
| spec_id | FK → strategy_specs |
| operator_id | FK → operators |
| role | TEXT (`entry` / `exit` / `filter` / `risk`) |
| parameters_used | TEXT (JSON) |

---

## 5. Experiments & Evaluation

### `experiments`
**The central table.** One row per iteration of the A2↔A3 loop.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id, spec_id | FK | |
| iteration | INTEGER | 1-based within the strategy |
| parent_experiment_id | FK → experiments | Lineage across iterations |
| research_plan_id | FK → research_plans | The plan that produced this iteration (null for iteration 1) |
| code_version_id | FK → code_versions | |
| status | TEXT | See §13.2 |
| phase_reached | TEXT | `bar` \| `P0` … `P4` |
| outcome | TEXT | `passed` \| `failed` \| `error` \| `plateaued` |
| failure_reason | TEXT | Structured category, §13.5 |
| **Provenance (TRD §6.6)** | | |
| eval_engine_version | TEXT | |
| market_profile_hash | TEXT | |
| timeframe_profile_hash | TEXT | |
| **wf_config_hash** | TEXT | Hash of `{scheme, train_years, test_years}` (TRD §8.2). Train length carries the same hidden-multiple-testing risk as scheme choice, so it is hashed for the same reason |
| operator_library_version | TEXT | |
| **code_commit** | TEXT | git commit — the link to the code (§1.1) |
| data_snapshot_id | FK → data_snapshots | |
| random_seed | INTEGER | |
| comparable | INTEGER (bool) | Set false when engine / profile / wf-config changes invalidate comparison |
| tokens_spent, compute_seconds | INTEGER | |
| created_at, completed_at | | |

> **Indexes:** `(strategy_id, iteration)` and
> `(eval_engine_version, market_profile_hash, timeframe_profile_hash, wf_config_hash)`.
> The second answers *"which stored results are still comparable?"* instantly.

### `code_versions`
Every implementation A2 produces.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| experiment_id | FK | |
| code_path | TEXT | Path to the generated strategy module |
| code_hash | TEXT | Content hash |
| git_commit | TEXT | The commit on `strategies.git_branch` |
| diff_from_parent | TEXT | What changed vs the previous iteration |
| change_summary | TEXT | A2's plain-language description — including any objection to the plan it implemented anyway |
| implements_plan_id | FK → research_plans | |
| compile_ok | INTEGER (bool) | |
| static_check_results | TEXT (JSON) | Look-ahead / leakage scan output (TRD §10.1) |
| prompt_version | TEXT | |
| created_at | | |

### `evaluations`
One row per phase run of `evaluate.py`.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| experiment_id | FK | |
| phase | TEXT | `bar` \| `P0` \| `P1` \| `P2` \| `P3` \| `P4` |
| result | TEXT | `pass` \| `fail` \| `warn` \| `error` |
| **★ The hard bar (TRD §7.5)** | | |
| bar_result | TEXT | `pass` \| `fail`. **On `fail`, no score is computed at all** |
| bar_failed_on | TEXT | `min_trades` \| `max_drawdown` \| `breadth` \| `cost_stress` \| `complexity` |
| **★ The honest score (TRD §7)** | | |
| **honest_score** | REAL | `sr_oos − z·se_sr − trials_haircut`. **The single float that drives keep/discard** |
| sr_oos | REAL | Sharpe on the concatenated purged/embargoed walk-forward series, at 2× costs |
| se_sr | REAL | Standard error including skew and kurtosis terms |
| z_multiplier | REAL | 2.0 (~97.5% one-sided) or 1.65 (~95%) — recorded, since changing it changes comparability |
| trials_haircut | REAL | `SR*(N_trials)` — expected best-under-null for this family |
| n_trials_used | INTEGER | The family trial count fed into the haircut |
| oos_skew, oos_kurtosis, oos_n_obs | REAL/INT | Inputs to `se_sr`, stored for audit |
| autocorr_adjusted | INTEGER (bool) | Whether Lo's correction was applied |
| complexity_count | INTEGER | Rules / free parameters — the tertiary criterion |
| **Walk-forward configuration (TRD §8)** | | |
| wf_scheme | TEXT | `rolling` \| `anchored` \| `holdout` \| `cpcv`. Part of `wf_config_hash` |
| wf_train_years | INTEGER | **1, 2, or 3.** Chosen once per family before the campaign, never swept for a better score |
| wf_test_years | INTEGER | **Always 1.** Fixed regardless of train length — it represents re-fit cadence, not a search parameter |
| wf_train_bars, wf_test_bars | INTEGER | Bar-count equivalents, resolved per timeframe profile |
| embargo_bars, holding_period_bars | INTEGER | **Embargo must be ≥ holding period** or trades leak across the split |
| n_folds | INTEGER | |
| folds_profitable | INTEGER | How many test windows made money — the consistency diagnostic concatenation hides |
| fold_metrics | TEXT (JSON) | Per-fold score, trades, drawdown. **Stored for diagnosis; does not drive keep/discard** |
| wf_efficiency | REAL | OOS ÷ IS performance — if far below 1, each fold overfits internally |
| params_refit_per_fold | INTEGER (bool) | Whether tuning re-ran on each training window (TRD §8.5) |
| **Core metrics** | | |
| sharpe, sortino, calmar | REAL | |
| cagr, total_return | REAL | |
| max_drawdown, avg_drawdown, dd_duration_days | REAL | |
| profit_factor, win_rate, expectancy | REAL | |
| trade_count | INTEGER | |
| avg_trade_return, turnover, exposure_pct | REAL | |
| **Robustness metrics (P3)** | | |
| deflated_sharpe | REAL | |
| pbo | REAL | CSCV probability of backtest overfitting |
| white_rc_pvalue | REAL | |
| mc_p5_return, mc_p50_return, mc_p95_return | REAL | Monte Carlo percentiles |
| mc_ruin_probability | REAL | |
| param_sensitivity_score | REAL | |
| cost_breakeven_multiplier | REAL | At what cost multiple the edge vanishes |
| regime_consistency_score | REAL | |
| **Artifacts & cost** | | |
| equity_curve_path, tradebook_path, report_path | TEXT | Parquet / report references |
| metrics_json | TEXT (JSON) | Everything not promoted to a column |
| duration_seconds | REAL | Wall clock, tracked against the per-experiment budget (TRD §9.6) |
| cpu_seconds | REAL | Total across workers — reveals parallel efficiency |
| n_workers | INTEGER | |
| timed_out | INTEGER (bool) | Exceeded the budget → recorded as `crash`, not `discard` |
| created_at | | |

### `evaluation_tests`
Individual test outcomes within a phase — needed to answer *"which specific gate failed, and by how much?"*

| Column | Type | Notes |
|---|---|---|
| id | | |
| evaluation_id | FK | |
| test_name | TEXT | `look_ahead_scan`, `deflated_sharpe`, `pbo`, `regime_2008` |
| category | TEXT | `correctness` \| `performance` \| `robustness` \| `cost` \| `regime` |
| result | TEXT | `pass` \| `fail` \| `warn` |
| gating | INTEGER (bool) | Hard fail vs advisory |
| value, threshold | REAL | **Both stored** — a passing test with an invisible threshold is not evidence |
| detail | TEXT | |

### `regime_performance`
Per-regime breakdown. Feeds promotion checks, health monitoring, and the knowledge graph.

| Column | Type |
|---|---|
| evaluation_id | FK |
| regime | TEXT (§13.4) |
| sharpe, cagr, max_drawdown, trade_count | REAL/INT |
| period_start, period_end | TEXT |

---

## 6. The Iteration Loop

### `research_plans`
A3's output. **Never contains code** — it is a research instruction (App-Flow §6.4).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| experiment_id | FK | The experiment being reviewed — always a **bar failure** (App-Flow §6.1) |
| strategy_id | FK | |
| verdict | TEXT | `iterate` \| `plateau` \| `reject`. **No `promote`** — clearing the bar bypasses A3 entirely |
| diagnosis | TEXT | What A3 concluded from the evidence |
| evidence_cited | TEXT (JSON) | Which metrics / tests drove the verdict — the "why" record |
| proposed_changes | TEXT (JSON) | Ordered list, e.g. `[{target: "exit", change: "replace fixed stop with ATR trailing", reason: "..."}]` |
| expected_effect | TEXT | Prediction, so A3's calibration can be scored |
| confidence | REAL | 0–1 |
| prompt_version | TEXT | |
| created_at | | |

---

## 7. Promotion & Deployment

### `promotions`
A4's decision. Sees the **entire** research history, not just the winner.

**No portfolio-correlation column, by design** (App-Flow §7.2) — multi-strategy portfolio construction is out of scope for v1 (PRD §3), and A4 judges each strategy on its own merits.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id, best_experiment_id | FK | |
| stage_from, stage_to | TEXT | `research`→`human_review`→`paper`→`live_small`→`live_scaled` |
| decision | TEXT | `approve` \| `reject` \| `defer` |
| rationale | TEXT | |
| evidence_summary | TEXT (JSON) | |
| iterations_considered | INTEGER | **Overfitting signal** — attempts spent before clearing the bar |
| overfitting_risk | TEXT | `low` \| `medium` \| `high` |
| confidence | REAL | |
| capacity_liquidity_ok | INTEGER (bool) | Can *this* strategy alone trade at real size — a single-strategy property |
| recommended_allocation_pct | REAL | Sized from this strategy's own robustness only |
| requires_human_approval | INTEGER (bool) | **Always 1** for paper and live gates |
| human_decision | TEXT | `approved` \| `rejected` \| `pending` |
| human_decided_by, human_decided_at | TEXT | |
| human_notes | TEXT | **Mandatory on approval** — friction on purpose |
| **merge_commit** | TEXT | Set only on `approve`: the commit where `strategy/<id>` merged into `deploy/paper` or `deploy/live` (TRD §5.3). The merge message references this row's `uid`, so git and this table cross-reference |
| prompt_version | TEXT | |
| created_at | | |

> **Portfolio correlation is a dashboard display value, not a column here.** Computed on demand from stored return series and shown to the human at review time (App-Flow §9) — deliberately more than A4 used, never fed back into A4's decision.

### `deployments`
A strategy running in paper or live mode.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id, experiment_id | FK | The exact validated version deployed |
| mode | TEXT | `paper` \| `live` |
| status | TEXT | `active` \| `paused` \| `stopped` \| `retired` |
| deploy_branch | TEXT | `deploy/paper` or `deploy/live` — whichever currently contains this code (TRD §5.3) |
| allocation_pct | REAL | |
| capital_minor_units, currency | INTEGER/TEXT | |
| broker_ref | TEXT | |
| started_at, ended_at | TEXT | |
| **Promotion gate tracking (PRD §9.3)** | | |
| trades_required, trades_completed | INTEGER | |
| regimes_required, regimes_observed | TEXT (JSON) | |
| **Expected-behaviour baseline** | | |
| expected_sharpe, expected_max_dd, expected_win_rate, expected_avg_trade | REAL | Copied from validation — **the yardstick for every health check** |
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
| execution_quality | TEXT (`good` / `degraded` / `failed`) |
| regime_at_entry | TEXT |
| signal_reference | TEXT |

---

## 8. Health & Lifecycle

### `health_checks`
The periodic verdict on *"is this still behaving like what we validated?"* (PRD §9.4).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| deployment_id | FK | |
| check_time | TEXT | |
| level | TEXT | `green` \| `yellow` \| `orange` \| `red` |
| **Performance** | | |
| live_sharpe, live_max_dd, live_win_rate, live_profit_factor | REAL | |
| **Deviation from the validated baseline** | | |
| sharpe_zscore, win_rate_zscore, avg_trade_zscore | REAL | Z-scores, because "1.2 vs 1.6" means nothing without the expected spread |
| loss_distribution_pvalue | REAL | Are losses outside the historical distribution? |
| **Context** | | |
| current_regime | TEXT | |
| **regime_historically_weak** | INTEGER (bool) | A drawdown in a known-weak regime is *expected*, not evidence of death. This single field prevents the most common bad decision |
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
| event_type | TEXT (`deployed` / `scaled_up` / `scaled_down` / `paused` / `resumed` / `retired` / `health_change` / `kill_switch`) |
| from_state, to_state | TEXT |
| triggered_by | TEXT (`agent` / `human` / `automatic_rule`) |
| reason | TEXT |
| evidence | TEXT (JSON) — on `retired`, includes the commit that removed this strategy from its deploy branch (App-Flow §11.2) |
| created_at | TEXT |

---

## 9. Internal Knowledge — tested, ground truth

Two layers (TRD §12.1): the **raw record** above (`experiments` + `evaluations`, written automatically for every attempt), and the **synthesized lessons** below, written by A5 once per strategy.

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
| **counter_evidence_count** | INTEGER | Honest bookkeeping — a lesson with contradictions must look less certain |
| confidence | REAL | |
| applicable_markets, applicable_timeframes, applicable_regimes | TEXT (JSON) | |
| future_ideas | TEXT (JSON) | **Mandatory — this is what self-propels the lab** |
| embedding_id | TEXT | Vector index reference |
| superseded_by | FK → knowledge_entries | **Knowledge is revised, never deleted** — the lab's memory only grows, and you can always see what it used to believe |
| created_at | | |

### `knowledge_edges`
The knowledge graph (TRD §12.5). **An edge with no experiment backing must not exist.**

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

> UNIQUE on `(subject, predicate, object)`. Repeated observation updates counts, never duplicates rows.

### `lab_notebooks`
The human-readable record per strategy (TRD §16).

| Column | Type |
|---|---|
| id, uid | |
| experiment_id, strategy_id | FK |
| hypothesis, result, reason, evidence | TEXT |
| confidence | REAL |
| **next_questions** | TEXT (JSON) — **mandatory, non-empty** |
| rendered_markdown | TEXT |
| created_at | TEXT |

---

## 10. External Knowledge — the Librarian's output, untested

### `external_documents`
Raw ingested artifacts. **Read once, ever.**

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| source | TEXT | `arxiv` \| `ssrn` \| `github` \| `blog` \| `journal` \| `book` |
| source_id, url, title, authors, published_at | TEXT | |
| content_hash | TEXT UNIQUE | Deduplication |
| raw_path | TEXT | Archived original |
| ingested_at, extracted_at | TEXT | |
| extraction_status | TEXT | `pending` \| `chunked` \| `done` \| `failed` \| `irrelevant` |
| relevance_score | REAL | Cheap filter **before** spending LLM tokens |
| chunk_count | INTEGER | How many pieces this was split into. `1` for short documents |

### `document_chunks`
One row per piece a large document was split into. Exists so a synthesized idea points at an **exact passage** rather than "somewhere in this paper" (TRD §12.2).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| document_id | FK → external_documents | |
| chunk_index | INTEGER | 0-based position |
| section_title | TEXT | Populated when the source has structural headings |
| char_start, char_end | INTEGER | Offsets into the raw document, for exact re-location |
| chunk_extraction | TEXT (JSON) | Pass-1 output: candidate claims in *this chunk alone*, before synthesis |
| processed_at | TEXT | |

> Chunks are never re-read once synthesis has run. They exist for audit and traceability.

### `external_knowledge`
**One row is one idea, never one row per document.** A single paper's synthesis pass typically yields 2–3 of these.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| document_id | FK | |
| **source_chunk_ids** | TEXT (JSON) | Which chunks this idea was synthesized from — the traceability link |
| layer | TEXT | `research` \| `market` \| `software` \| `infrastructure` (PRD §8.2) |
| core_idea | TEXT | **One clear sentence.** If it needs a paragraph, synthesis didn't finish its job |
| category | TEXT | `signal`, `risk_management`, `portfolio_construction`, `validation_technique` |
| applicable_markets, applicable_timeframes | TEXT (JSON) | |
| strengths, weaknesses | TEXT | |
| implementation_difficulty | TEXT | `low` \| `medium` \| `high` |
| required_operators | TEXT (JSON) | Operators that would need to exist to implement it |
| proposed_experiments | TEXT (JSON) | Directly consumable by A1 — this is what makes the record *actionable* |
| novelty_score | REAL | How much this differs from what's already known. Above a threshold, triggers a `GENERATE_SPEC` job immediately (App-Flow §3.1) |
| **extraction_confidence** | REAL | **The Librarian's confidence that it read the source correctly.** Explicitly *not* a claim the idea is true |
| **evidence_tier** | TEXT | Always `external_claim`. Contrasts with `knowledge_entries` (internal, tested) — never conflate (TRD §12.3) |
| extracted_by | TEXT | Agent identifier, e.g. `librarian` |
| extraction_prompt_version | TEXT | |
| extracted_at | TEXT | |
| related_knowledge_ids | TEXT (JSON) | |
| embedding_id | TEXT | |
| used_in_specs | TEXT (JSON) | Did this ever produce a hypothesis? |

> **Trust tier, stated plainly:** an `external_knowledge` row is a *candidate worth testing*. It earns the weight of "confirmed" only when an experiment tests it and A5 writes the result into `knowledge_entries`. **No promotion decision may cite `extraction_confidence` as evidence** — that field describes reading accuracy, not truth.

### `research_questions`
The curiosity queue (TRD §12.4).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| question | TEXT | "How do practitioners make momentum volatility-adaptive?" |
| motivation | TEXT | |
| origin_type | TEXT | `experiment_failure` \| `pattern_detection` \| `human` \| `live_degradation` |
| origin_experiment_id, origin_knowledge_id | FK | |
| priority | INTEGER | |
| status | TEXT | `open` \| `searching` \| `answered` \| `abandoned` |
| search_terms | TEXT (JSON) | Handed to the collectors |
| answer_knowledge_ids | TEXT (JSON) | |
| **produced_spec_ids** | TEXT (JSON) | **Did asking this ever pay off?** |
| created_at, resolved_at | | |

---

## 11. Integrity Tables ★

Built alongside the integrity work (TRD §14), which precedes any real-data result.

### `vault_access_log`
Every opening of the locked holdout. The vault is the one defence that does not depend on honestly counting trials, so **its own bookkeeping must be exact.**

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| strategy_id, family | FK / TEXT | Budget is consumed **per family**, not per strategy |
| vault_segment | TEXT | Which locked span / instruments / market was opened |
| opened_at | TEXT | |
| opened_by | TEXT | `human` \| `promotion_gate` — **never the research loop** |
| reason | TEXT | |
| promotion_id | FK → promotions | The decision this unlock served |
| budget_before, budget_after | INTEGER | Remaining lifetime opens for this family |
| result_score | REAL | What the vault said |
| outcome | TEXT | `confirmed` \| `contradicted` |

> A family whose budget reaches zero **cannot be promoted again** until genuinely new data exists.

### `null_world_runs`
The headline integrity metric (PRD §4.2). Re-run after any change to `evaluate.py`, the scoring rule, or a profile.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| run_label | TEXT | |
| null_model | TEXT | `permuted_returns` \| `block_bootstrap` \| `synthetic_gbm` \| `synthetic_fat_tail` |
| replications | INTEGER | |
| eval_engine_version, scoring_rule_version | TEXT | What was being calibrated |
| experiments_run | INTEGER | |
| **discoveries_reported** | INTEGER | The number that matters |
| **false_discovery_rate** | REAL | discoveries ÷ replications |
| max_score_observed | REAL | The best "strategy" found in pure noise — a useful bar for real results |
| verdict | TEXT | `pipeline_trusted` \| `pipeline_suspect` |
| notes | TEXT | |
| created_at | | |

### `acceptance_bars`
The satisficing bar (PRD §10.2), recorded **before** a campaign begins so it cannot be adjusted after seeing results.

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| campaign_label | TEXT | |
| min_score, max_drawdown, min_trades, min_breadth, max_complexity | REAL/INTEGER | |
| cost_stress_multiple | REAL | Default 2.0 |
| z_multiplier | REAL | Default 2.0 |
| **plateau_patience** | INTEGER | Consecutive **bar failures** before a PLATEAU verdict — never a score comparison, since clearing the bar stops the loop immediately. **Default 5** |
| **hard_iteration_cap** | INTEGER | Outer backstop independent of plateau detection. **Default ~20–25** |
| wf_scheme, wf_train_years, wf_test_years | TEXT/INTEGER | Fixed per campaign (TRD §8.2) |
| locked_at, locked_by | TEXT | |
| superseded_by | FK | |

> Written at campaign start. Changing a bar mid-campaign creates a new row and marks the campaign's prior results incomparable.

---

## 12. Infrastructure

### `jobs`
The queue. **Lease-based claiming**, so the v1→v3 migration is a backend swap (TRD §3.2).

| Column | Type | Notes |
|---|---|---|
| id, uid | | |
| job_type | TEXT | §13.3 |
| payload | TEXT (JSON) | |
| strategy_id, experiment_id | FK | Nullable |
| status | TEXT | `pending` \| `claimed` \| `running` \| `succeeded` \| `failed` \| `timed_out` \| `cancelled` |
| priority | INTEGER | |
| **claimed_by, lease_expires_at, heartbeat_at** | TEXT | Dead-worker recovery |
| attempts, max_attempts | INTEGER | |
| failure_class | TEXT | `transient` \| `deterministic` — determines retry behaviour |
| depends_on_job_id | FK → jobs | |
| scheduled_for | TEXT | Delayed execution |
| error_message, error_trace | TEXT | |
| tokens_spent, duration_seconds | INTEGER | |
| created_at, started_at, completed_at | | |

> Index on `(status, priority, scheduled_for)` — the scheduler's hot path.
> This table also backs the live activity feed (UI-UX-Brief §8.1).

### `data_snapshots`
Immutable dataset versions. An experiment references a snapshot, never "the files on disk."

| Column | Type |
|---|---|
| id, uid | |
| market, timeframe | TEXT |
| period_start, period_end | TEXT |
| instrument_count, bar_count | INTEGER |
| storage_path, content_hash | TEXT |
| adjustment_method | TEXT |
| survivorship_handled | INTEGER (bool) |
| in_vault | INTEGER (bool) — if true, the research loop has no read path (TRD §14.2) |
| validation_status, validation_report | TEXT |
| created_at | TEXT |

### `market_profiles` / `timeframe_profiles`
Registry of resolved profiles (TRD §6). Content-hashed so experiments can pin them.

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
Scheduler back-pressure (TRD §4.4).

| Column | Type |
|---|---|
| id | |
| scope | TEXT (`global` / `strategy` / `goal`) |
| scope_id | INTEGER |
| budget_type | TEXT (`tokens` / `experiments` / `iterations` / `compute_seconds` / `usd`) |
| period | TEXT (`day` / `week` / `lifetime`) |
| limit_value, used_value | INTEGER |
| period_start | TEXT |
| exhausted | INTEGER (bool) |

### `audit_log`
Answers *"why did you do this?"* for every system action (PRD §11.1).

| Column | Type |
|---|---|
| id, uid | |
| actor | TEXT (`agent1`…`agent5` / `librarian` / `scheduler` / `human` / `evaluate`) |
| action | TEXT |
| entity_type, entity_id | TEXT/INTEGER |
| reasoning | TEXT |
| evidence | TEXT (JSON) |
| prompt_version, model_version | TEXT |
| created_at | TEXT |

---

## 13. Enumerations

### 13.1 `strategies.status`
```
draft → spec_ready → coding → evaluating → evaluated
      → iterating (loops back to coding, below the bar only)
      → plateaued | rejected                         (never cleared the bar)
      → pending_promotion → awaiting_human_review     (cleared the bar)
      → paper_trading → pending_live_review
      → live_small → live_scaled
      → retired | quarantined
```

### 13.2 `experiments.status`
```
created → code_pending → code_ready → evaluating → evaluated → reviewed → archived
        → failed | error
```

### 13.3 `jobs.job_type`
```
GENERATE_SPEC        (A1)
IMPLEMENT            (A2)
FIX_CODE             (A2, from a static-check or P0 failure)
EVALUATE             (evaluate.py — no LLM)
REVIEW               (A3 — only ever on a bar failure)
PROMOTE              (A4)
ARCHIVE              (A5)
MINE_PATTERNS        (A5, cross-experiment, weekly)
EXTRACT_KNOWLEDGE    (Librarian — once per document, ever)
COLLECT_PAPERS · COLLECT_GITHUB · COLLECT_MARKET_DATA    (Python collectors)
MONITOR_DEPLOYMENT   (health checks)
NULL_WORLD_RUN       (integrity calibration)
GENERATE_REPORT
```

### 13.4 Regimes
`trending` · `sideways` · `high_vol` · `low_vol` · `crisis`

### 13.5 `experiments.failure_reason`
Structured so A5 can aggregate. **Free text is not acceptable here.**

```
Research findings:
  no_signal · negative_expectancy · costs_exceed_edge · overfit_in_sample
  walk_forward_unstable · regime_dependent · pbo_too_high
  deflated_sharpe_insufficient · insufficient_trades · monte_carlo_ruin_risk
  parameter_sensitive · capacity_constrained · plateaued_below_bar

Bugs — NOT research findings:
  code_error · look_ahead_detected · data_leakage_detected
```

> The three bug categories route back to A2 (TRD §10.1) and **must never be recorded as research conclusions** or pollute the knowledge base.

---

## 14. Key Queries the Schema Must Answer Fast

Each must be a simple indexed query, not a scan. These drove the design.

1. *"How many trials have been run in this strategy **family**?"* → deflated Sharpe correctness (§4, `family`)
2. *"Which stored results are still comparable to the current engine version?"* → the provenance index on `experiments`
3. *"Has this exact operator composition been tried before?"* → `spec_hash` unique index
4. *"Why did every experiment using ATR > 3.0 fail?"* → `spec_operators` + `failure_reason`
5. *"Which knowledge entries have contradicting evidence?"* → `counter_evidence_count > 0`
6. *"Is this live strategy behaving like the validated version?"* → `health_checks` vs `deployments.expected_*`
7. *"Which research questions ever produced a usable hypothesis?"* → `research_questions.produced_spec_ids`
8. *"What is the cost per credible discovery?"* → `tokens_spent` aggregated against promoted strategies
9. *"Reproduce experiment #12,483 exactly."* → spec + `code_commit` + `data_snapshot_id` + profile hashes + `wf_config_hash` + seed
10. *"What is trading right now?"* → `deployments` where `status = active`, cross-checked against `git show deploy/live`

---

## 15. Migration Notes

- SQLite first, but **no SQLite-specific SQL.** No `AUTOINCREMENT` reliance, no dynamic-typing tricks.
- JSON columns become `JSONB` in PostgreSQL.
- The `jobs` table moves to Redis in v2; the lease/heartbeat model already matches Redis semantics, so **agent code does not change.**
- Parquet paths are relative to a configurable root, so local → object storage is a config change.

---

## 16. Open Schema Questions

- [ ] Do parameter sweeps get one `experiment` row each, or one row with a child `sweep_runs` table? (Affects trial counting.)
- [ ] Should `trades` live in SQLite at all, or Parquet-only with SQLite holding aggregates?
- [ ] Versioning strategy for `knowledge_entries` when A5 revises a lesson — supersede chain vs in-place with a history table
- [ ] Retention policy for `evaluations.metrics_json` and `fold_metrics` at millions of rows
- [ ] Portfolio-level tables (multi-strategy allocation, correlation matrix) — deferred to a future portfolio-construction capability, explicitly **not** A4 (PRD §3)

---

## Changelog

| Date | Change |
|---|---|
| 2026-07-27 | Initial schema — experiments as the central table with full provenance, trial-count support for the deflated Sharpe, spec hashing for duplicate detection, evidence-backed knowledge graph edges, lease-based job queue. |
| 2026-07-27 | Added the build-three-tables-first guidance, the honest-score column group, walk-forward columns, timing columns, and the integrity tables. |
| 2026-07-28 | Added `wf_config_hash` to provenance and the comparability index. Added the Librarian's output schema — `document_chunks`, and `external_knowledge` as one row per idea with `evidence_tier` and traceability back to exact passages. Added the git branch/merge columns. |
| 2026-07-28 | Removed `promotions.correlation_with_live` (portfolio fit is out of scope for A4) and `acceptance_bars.plateau_margin_factor` (clearing the bar is an immediate stop, so score-to-score comparison no longer exists). |
| 2026-07-28 | **Full rewrite for clarity and consistency.** Sequential numbering (§0–§16) replacing the patched §0A/§15 scheme; internal and external knowledge separated into clearly-labelled trust tiers; all cross-references updated to the renumbered TRD, PRD and App-Flow; `in_vault` added to `data_snapshots`; changelog consolidated. No schema decisions changed in this pass. |
