"""Fixtures for the Stage 5 (A2) suite.

Overrides the root `settings` fixture to add `strategy_repo_path`, pointed at
a directory under `tmp_path` — every test gets its own scratch git repo, the
same isolation principle the root fixture already applies to the database and
data root.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from aqrl.config import PROJECT_ROOT, Settings

PROFILES_DIR = PROJECT_ROOT / "profiles"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        data_root=tmp_path / "data",
        profiles_dir=PROFILES_DIR,
        strategy_repo_path=tmp_path / "strategy_repo",
        log_level="WARNING",
    )


def crossover_spec_json(fast: int = 10, slow: int = 40, hypothesis: str = "dual EMA crossover"):
    return {
        "entry_logic": [
            {"id": "fast", "operator": "ema", "params": {"span": fast}, "inputs": {"series": "price.close"}},
            {"id": "slow", "operator": "ema", "params": {"span": slow}, "inputs": {"series": "price.close"}},
            {"id": "cross", "operator": "crossover", "inputs": {"fast": "fast", "slow": "slow"}},
        ],
        "hypothesis": hypothesis,
    }


def absolute_level_spec_json(offset: float = 0.0):
    """A deliberately bad spec: `threshold` compared directly against a raw
    price column — the exact TRD §14.2c violation `no_absolute_price_levels`
    exists to catch."""
    return {
        "entry_logic": [
            {
                "id": "e1",
                "operator": "threshold",
                "params": {"upper": 500.0 + offset, "lower": -500.0},
                "inputs": {"series": "price.close"},
            }
        ],
        "hypothesis": "deliberately bad: absolute price level",
    }


@pytest.fixture
def crossover_spec():
    from aqrl.operators.spec import StrategySpec

    return StrategySpec(**crossover_spec_json())
