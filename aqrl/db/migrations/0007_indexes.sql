-- Indexes for the twelve queries Backend-Schema §15 says must be fast.
--
-- "Each must be a simple indexed query, not a scan. These drove the design."
-- UNIQUE constraints already cover queries 3 (spec_hash) and 9 (uid lookups).

-- Q1: "How many trials have been run in this strategy FAMILY?"
--     Deflated-Sharpe correctness depends on this being exact and cheap.
CREATE INDEX idx_strategies_family ON strategies (family);
CREATE INDEX idx_strategies_status ON strategies (status);

-- Q2: "Which stored results are still comparable to the current engine version?"
--     The provenance index — the whole reason TRD §6.6 exists.
CREATE INDEX idx_experiments_provenance
    ON experiments (eval_engine_version, market_profile_hash, timeframe_profile_hash, wf_config_hash);

-- Q4: "Why did every experiment using ATR > 3.0 fail?"
CREATE INDEX idx_experiments_failure_reason ON experiments (failure_reason);
CREATE INDEX idx_spec_operators_operator ON spec_operators (operator_id);
CREATE INDEX idx_spec_operators_spec ON spec_operators (spec_id);

CREATE INDEX idx_evaluations_experiment ON evaluations (experiment_id, phase);
CREATE INDEX idx_evaluation_tests_evaluation ON evaluation_tests (evaluation_id);
CREATE INDEX idx_regime_performance_evaluation ON regime_performance (evaluation_id);
CREATE INDEX idx_code_versions_experiment ON code_versions (experiment_id);
CREATE INDEX idx_research_plans_experiment ON research_plans (experiment_id);

-- Q5: "Which knowledge entries have contradicting evidence?"
CREATE INDEX idx_knowledge_entries_counter_evidence ON knowledge_entries (counter_evidence_count);
CREATE INDEX idx_knowledge_entries_scope ON knowledge_entries (scope, entry_type);

-- Q6: "Is this live strategy behaving like the validated version?"
CREATE INDEX idx_health_checks_deployment ON health_checks (deployment_id, check_time);
CREATE INDEX idx_trades_deployment ON trades (deployment_id, entry_time);
CREATE INDEX idx_lifecycle_events_deployment ON lifecycle_events (deployment_id, created_at);

-- Q7: "Which research questions ever produced a usable hypothesis?"
CREATE INDEX idx_research_questions_status ON research_questions (status, priority);

-- Q8: "What is the cost per credible discovery?"
CREATE INDEX idx_promotions_strategy ON promotions (strategy_id, created_at);

-- Q10: "What is trading right now?"
CREATE INDEX idx_deployments_status ON deployments (status, mode);

-- Q11: "Which stocks were in NIFTY-50 on 2014-03-11?" — the point-in-time
--      universe. Resolution is the half-open interval, so both bounds index.
CREATE INDEX idx_index_membership_resolution
    ON index_membership (index_name, effective_from, effective_to);
CREATE INDEX idx_index_membership_instrument ON index_membership (instrument);

-- Q12: "Is this snapshot safe to run experiments against?"
CREATE INDEX idx_data_snapshots_validation ON data_snapshots (validation_status);
CREATE INDEX idx_data_snapshots_market ON data_snapshots (market, timeframe, asset_class);
CREATE INDEX idx_validation_flags_snapshot ON data_validation_flags (snapshot_id, resolution);

-- Corporate-action lookup during load-time adjustment: hot on every load.
CREATE INDEX idx_corporate_actions_instrument ON corporate_actions (instrument, market, ex_date);

-- The scheduler's hot path (Backend-Schema §13). Stage 4 leans on this.
CREATE INDEX idx_jobs_dispatch ON jobs (status, priority, scheduled_for);
CREATE INDEX idx_jobs_lease ON jobs (status, lease_expires_at);

-- The Librarian's pipeline.
CREATE INDEX idx_external_documents_status ON external_documents (extraction_status);
CREATE INDEX idx_document_chunks_document ON document_chunks (document_id, chunk_index);
CREATE INDEX idx_external_knowledge_document ON external_knowledge (document_id);

CREATE INDEX idx_audit_log_entity ON audit_log (entity_type, entity_id);
CREATE INDEX idx_vault_access_family ON vault_access_log (family, opened_at);
