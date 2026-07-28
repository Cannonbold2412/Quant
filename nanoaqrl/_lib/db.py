"""Thin repository layer over SQLite (TRD §5.1, §19). Parameterized SQL only —
no ORM, no SQLite-specific syntax beyond what `schema.sql` already documents.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA_PATH.read_text())
    conn.commit()
    return conn


def get_or_create_strategy(
    conn: sqlite3.Connection,
    name: str,
    family: str,
    market: str,
    timeframe: str,
    git_branch: str | None = None,
    code_path: str | None = None,
) -> int:
    row = conn.execute("SELECT id FROM strategies WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        """INSERT INTO strategies (uid, name, family, market, timeframe, git_branch, code_path)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), name, family, market, timeframe, git_branch, code_path),
    )
    conn.commit()
    return cur.lastrowid


def next_iteration(conn: sqlite3.Connection, strategy_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(iteration), 0) AS max_iter FROM experiments WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()
    return row["max_iter"] + 1


def get_family_trial_count(conn: sqlite3.Connection, family: str) -> int:
    """Prior iterations across every strategy in this family (Backend-Schema
    §4: "the same idea tried in 3 markets is 3 trials in one family, not 3
    independent results"). Returns the *raw* count — the caller adds this
    run's own attempt and applies the best-of-three x3 exactly once
    (`walk_forward.run_best_of_three`), so the multiplier is never applied
    twice."""
    row = conn.execute(
        "SELECT COALESCE(SUM(iteration_count), 0) AS total FROM strategies WHERE family = ?",
        (family,),
    ).fetchone()
    return int(row["total"])


def insert_experiment(
    conn: sqlite3.Connection,
    strategy_id: int,
    iteration: int,
    code_commit: str,
    wf_config_hash: str,
    eval_engine_version: str,
    random_seed: int,
) -> int:
    cur = conn.execute(
        """INSERT INTO experiments
           (uid, strategy_id, iteration, status, phase_reached, code_commit,
            wf_config_hash, eval_engine_version, random_seed)
           VALUES (?, ?, ?, 'crash', 'bar', ?, ?, ?, ?)""",
        (str(uuid.uuid4()), strategy_id, iteration, code_commit, wf_config_hash, eval_engine_version, random_seed),
    )
    conn.commit()
    return cur.lastrowid


def complete_experiment(
    conn: sqlite3.Connection,
    experiment_id: int,
    status: str,
    phase_reached: str,
    outcome: str,
    failure_reason: str | None = None,
) -> None:
    conn.execute(
        """UPDATE experiments
           SET status = ?, phase_reached = ?, outcome = ?, failure_reason = ?, completed_at = datetime('now')
           WHERE id = ?""",
        (status, phase_reached, outcome, failure_reason, experiment_id),
    )
    conn.execute(
        "UPDATE strategies SET iteration_count = iteration_count + 1, updated_at = datetime('now') WHERE id = (SELECT strategy_id FROM experiments WHERE id = ?)",
        (experiment_id,),
    )
    conn.commit()


def record_bar_clear(conn: sqlite3.Connection, strategy_id: int, experiment_id: int, score: float) -> None:
    conn.execute(
        "UPDATE strategies SET best_experiment_id = ?, best_score = ?, updated_at = datetime('now') WHERE id = ?",
        (experiment_id, score, strategy_id),
    )
    conn.commit()


def record_plateau_step(conn: sqlite3.Connection, strategy_id: int, cleared: bool) -> int:
    if cleared:
        conn.execute("UPDATE strategies SET plateau_counter = 0 WHERE id = ?", (strategy_id,))
    else:
        conn.execute("UPDATE strategies SET plateau_counter = plateau_counter + 1 WHERE id = ?", (strategy_id,))
    conn.commit()
    row = conn.execute("SELECT plateau_counter FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return row["plateau_counter"]


def insert_evaluation(conn: sqlite3.Connection, experiment_id: int, phase: str, result: str, **kwargs) -> int:
    fields = ["experiment_id", "phase", "result"] + list(kwargs.keys())
    placeholders = ", ".join(["?"] * (len(fields) + 1))
    values = [str(uuid.uuid4()), experiment_id, phase, result] + list(kwargs.values())
    cur = conn.execute(
        f"INSERT INTO evaluations (uid, {', '.join(fields)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return cur.lastrowid


def insert_null_world_run(
    conn: sqlite3.Connection,
    generator: str,
    n_replications: int,
    n_discoveries: int,
    max_score_observed: float,
    eval_engine_version: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO null_world_runs
           (uid, generator, n_replications, n_discoveries, max_score_observed, eval_engine_version)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), generator, n_replications, n_discoveries, max_score_observed, eval_engine_version),
    )
    conn.commit()
    return cur.lastrowid


def append_results_tsv(path: str | Path, commit: str, score, n_trades: int, status: str, description: str) -> None:
    path = Path(path)
    is_new = not path.exists()
    with path.open("a") as f:
        if is_new:
            f.write("commit\tscore\tn_trades\tstatus\tdescription\n")
        score_str = "" if score is None else f"{score:.6f}"
        f.write(f"{commit}\t{score_str}\t{n_trades}\t{status}\t{description}\n")
