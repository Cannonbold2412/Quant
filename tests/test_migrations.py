"""Migrations: applied in order, idempotent, immutable once applied.

An already-applied migration whose file has since been edited must raise. The
database would no longer match the source that claims to describe it, and every
provenance guarantee downstream assumes those agree.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from aqrl.db import connect, migrate
from aqrl.db.connection import render_sql, split_statements
from aqrl.db.migrate import MIGRATIONS_DIR, MigrationError, discover, status

# Every table Backend-Schema.md defines, plus the cost_models registry Stage 1
# adds so `experiments.cost_model_hash` has something to point at.
EXPECTED_TABLES = {
    # §3-6 core research
    "research_goals", "operators", "strategies", "strategy_specs", "spec_operators",
    "experiments", "research_plans", "code_versions", "evaluations", "evaluation_tests",
    "regime_performance",
    # §7-8 promotion and lifecycle
    "promotions", "deployments", "trades", "health_checks", "lifecycle_events",
    # §9-10 knowledge
    "knowledge_entries", "knowledge_edges", "lab_notebooks", "external_documents",
    "document_chunks", "external_knowledge", "research_questions",
    # §11 research integrity
    "vault_access_log", "null_world_runs", "acceptance_bars",
    # §12 data integrity
    "data_snapshots", "corporate_actions", "index_membership", "data_validation_flags",
    # §13 infrastructure
    "jobs", "market_profiles", "timeframe_profiles", "cost_models", "budgets", "audit_log",
}


def test_migrations_are_discovered_in_order():
    versions = [m.version for m in discover()]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1)), "versions must be contiguous from 1"


def test_migrate_creates_every_documented_table(conn):
    tables = set(status(conn)["tables"]) - {"schema_migrations"}
    assert EXPECTED_TABLES <= tables, f"missing: {sorted(EXPECTED_TABLES - tables)}"


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []
    assert status(conn)["pending"] == []


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO data_validation_flags (uid, snapshot_id, flag_type, created_at) "
            "VALUES ('x', 9999, 'gap', '2020-01-01')"
        )


def test_enum_check_constraints_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO corporate_actions (uid, instrument, market, action_type, ex_date, ratio, created_at) "
            "VALUES ('x', 'A', 'nse_equity', 'not_an_action', '2020-01-01', 0.5, '2020-01-01')"
        )


def test_failure_reason_rejects_free_text(conn):
    """Backend-Schema §14.5: free text is not acceptable in `failure_reason`."""
    conn.execute(
        "INSERT INTO strategies (uid, name, family, market, timeframe, status, created_at, updated_at) "
        "VALUES ('s', 'n', 'f', 'm', 't', 'draft', '2020-01-01', '2020-01-01')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO experiments (uid, strategy_id, iteration, status, failure_reason, created_at) "
            "VALUES ('e', 1, 1, 'failed', 'it did not work', '2020-01-01')"
        )


def test_checksum_drift_is_detected(tmp_path: Path):
    directory = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, directory)
    conn = connect(tmp_path / "drift.db")
    migrate(conn, directory)

    edited = directory / "0005_research_integrity.sql"
    edited.write_text(edited.read_text() + "\n-- edited after being applied\n")

    with pytest.raises(MigrationError, match="changed after being applied"):
        migrate(conn, directory)


def test_partial_migration_target(tmp_path: Path):
    conn = connect(tmp_path / "partial.db")
    migrate(conn, target=1)
    state = status(conn)
    assert state["applied"] == [1]
    assert state["pending"] == [2, 3, 4, 5, 6, 7]
    assert "data_snapshots" in state["tables"]
    assert "strategies" not in state["tables"]


def test_dialect_tokens_resolve_for_both_backends():
    for migration in discover():
        for dialect in ("sqlite", "postgresql"):
            rendered = render_sql(migration.sql, dialect)
            assert "{{" not in rendered, f"{migration.path.name} has an unresolved token"


def test_unknown_dialect_token_raises():
    with pytest.raises(ValueError, match="unresolved dialect token"):
        render_sql("CREATE TABLE t (id {{NOT_A_TOKEN}})")


def test_no_sqlite_specific_sql_in_migrations():
    """TRD §20: the PostgreSQL swap must be a backend swap, not a rewrite."""
    banned = ("AUTOINCREMENT", "datetime('now')", "INSERT OR REPLACE", "PRAGMA", "WITHOUT ROWID")
    for migration in discover():
        upper = migration.sql.upper()
        for token in banned:
            assert token.upper() not in upper, f"{migration.path.name} uses SQLite-specific {token}"


def test_statement_splitter_respects_strings_and_comments():
    sql = """
    CREATE TABLE t (
        a TEXT CHECK (a IN ('x;y', 'z')),  -- a comment with ; in it
        b TEXT
    );
    CREATE INDEX i ON t (a);
    """
    statements = split_statements(sql)
    assert len(statements) == 2
    assert "'x;y'" in statements[0]


def test_indexes_exist_for_the_hot_queries(conn):
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    # Backend-Schema §15: each of these must be an indexed query, not a scan.
    for required in (
        "idx_strategies_family",              # Q1 family trial count
        "idx_experiments_provenance",         # Q2 comparability
        "idx_index_membership_resolution",    # Q11 point-in-time universe
        "idx_validation_flags_snapshot",      # Q12 snapshot safety
        "idx_jobs_dispatch",                  # the scheduler's hot path
    ):
        assert required in names
