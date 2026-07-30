"""Fixtures for the Stage 4 orchestration suite.

Builds a full strategy -> spec -> experiment -> `EVALUATE` job chain against
a real (small, synthetic) snapshot, so the crash/queue tests exercise the
actual `aqrl.orchestration.handlers.evaluate` path rather than a stand-in.
"""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from aqrl.config import PROJECT_ROOT, Settings, reset_settings_cache
from aqrl.data import SnapshotManager
from aqrl.db.repositories import ExperimentRepository, JobRepository, SpecRepository, StrategyRepository
from aqrl.operators import Node, StrategySpec

from ..eval.conftest import random_walk_bars

MARKET = "nse_equity"
TIMEFRAME = "daily"
ASSET_CLASS = "cash_equity"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Overrides the root fixture to add `strategy_repo_path` — Stage 5's
    `IMPLEMENT`/`FIX_CODE` handler tests need their own scratch git repo, and
    every other test in this suite ignores the field entirely."""
    return Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path / "data",
        profiles_dir=PROJECT_ROOT / "profiles",
        strategy_repo_path=tmp_path / "strategy_repo",
        log_level="WARNING",
    )


@pytest.fixture(autouse=True)
def _env_settings(settings, monkeypatch):
    """`aqrl.orchestration.worker.run_job` and the real worker subprocess both
    resolve `Settings` through `get_settings()` (the environment), not
    through the `settings` fixture object — a real subprocess only inherits
    `os.environ`. Point the environment at the same tmp database and data
    root the `conn` fixture already uses, for every test in this suite.
    """
    monkeypatch.setenv("AQRL_DB_PATH", str(settings.db_path))
    monkeypatch.setenv("AQRL_DATA_ROOT", str(settings.data_root))
    monkeypatch.setenv("AQRL_STRATEGY_REPO_PATH", str(settings.strategy_repo_path))
    monkeypatch.setenv("AQRL_LOG_LEVEL", "WARNING")
    reset_settings_cache()
    yield
    reset_settings_cache()


def crossover_spec(fast: int = 10, slow: int = 40) -> StrategySpec:
    return StrategySpec(
        entry_logic=[
            Node(id="fast", operator="ema", inputs={"series": "price.close"}, params={"span": fast}),
            Node(id="slow", operator="ema", inputs={"series": "price.close"}, params={"span": slow}),
            Node(id="e1", operator="crossover", inputs={"fast": "fast", "slow": "slow"}),
        ],
        hypothesis="orchestration fixture: a plain dual-EMA crossover.",
    )


@pytest.fixture
def snapshot_id(conn, settings, loader, tmp_path: Path) -> int:
    manager = SnapshotManager(conn, settings=settings, loader=loader)
    frame = random_walk_bars(504, instruments=2, seed=42, drift=0.0003, volatility=0.012)
    path = tmp_path / "src" / "bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return manager.ingest(path, MARKET, TIMEFRAME, ASSET_CLASS)


@pytest.fixture
def strategy_id(conn) -> int:
    return StrategyRepository(conn).insert(
        name="orch-fixture", family="orch-fixture-family", market=MARKET, timeframe=TIMEFRAME, status="evaluating"
    )


@pytest.fixture
def spec_id(conn, strategy_id: int) -> int:
    return SpecRepository(conn).insert_spec(crossover_spec(), strategy_id)


@pytest.fixture
def experiment_id(conn, strategy_id: int, spec_id: int, snapshot_id: int) -> int:
    experiments = ExperimentRepository(conn)
    iteration = experiments.next_iteration(strategy_id)
    return experiments.start(
        strategy_id, iteration, spec_id=spec_id, data_snapshot_id=snapshot_id, code_commit="deadbeef"
    )


@pytest.fixture
def evaluate_payload() -> dict:
    return {"asset_class": ASSET_CLASS, "cost_multiplier": 2.0}


@pytest.fixture
def evaluate_job_id(conn, strategy_id: int, experiment_id: int, evaluate_payload: dict) -> int:
    return JobRepository(conn).enqueue(
        "EVALUATE", evaluate_payload, strategy_id=strategy_id, experiment_id=experiment_id
    )


@pytest.fixture
def evaluate_job_uid(conn, evaluate_job_id: int) -> str:
    return JobRepository(conn).get(evaluate_job_id)["uid"]


@pytest.fixture
def loose_bar(conn) -> None:
    """A locked, generous acceptance bar so a plumbing test can reach — and
    clear — the funnel's final phase without needing hundreds of trades from
    a small synthetic dataset."""
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.execute(
        """INSERT INTO acceptance_bars
            (uid, campaign_label, min_score, max_drawdown, min_trades, cost_stress_multiple,
             z_multiplier, plateau_patience, hard_iteration_cap, locked_at, locked_by)
           VALUES (?, 'orchestration-tests', -10.0, 0.99, 1, 1.5, 1.65, 5, 25, ?, 'test-fixture')""",
        (str(uuid.uuid4()), now),
    )
