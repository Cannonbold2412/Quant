"""Configuration precedence and correlation-ID logging."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from aqrl.config import PROJECT_ROOT, Settings
from aqrl.logging import JsonFormatter, TextFormatter, current_context, log_context


def test_relative_paths_resolve_against_the_project_root():
    settings = Settings(db_path=Path("some.db"))
    assert settings.db_path == PROJECT_ROOT / "some.db"


def test_absolute_paths_are_left_alone(tmp_path: Path):
    assert Settings(db_path=tmp_path / "x.db").db_path == tmp_path / "x.db"


def test_environment_overrides_defaults(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AQRL_LOG_LEVEL", "debug")
    monkeypatch.setenv("AQRL_DATA_ROOT", str(tmp_path / "elsewhere"))
    settings = Settings()
    assert settings.log_level == "DEBUG"  # normalised
    assert settings.data_root == tmp_path / "elsewhere"


def test_invalid_log_level_is_rejected(monkeypatch):
    monkeypatch.setenv("AQRL_LOG_LEVEL", "chatty")
    with pytest.raises(ValueError, match="log_level must be one of"):
        Settings()


def test_settings_are_frozen(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "x.db")
    with pytest.raises(ValidationError):
        settings.db_path = tmp_path / "y.db"  # type: ignore[misc]


def test_data_root_boundary_round_trips(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data")
    absolute = settings.data_root / "snapshots" / "a" / "b.parquet"
    relative = settings.to_data_relative(absolute)
    assert relative == "snapshots/a/b.parquet"
    assert settings.from_data_relative(relative) == absolute


def test_paths_outside_the_data_root_are_rejected(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data")
    with pytest.raises(ValueError, match="outside data_root"):
        settings.to_data_relative(tmp_path / "elsewhere" / "x.parquet")


def _record(message: str = "hello", **extra) -> logging.LogRecord:
    record = logging.LogRecord("aqrl.test", logging.INFO, __file__, 1, message, (), None)
    record.__dict__.update(extra)
    return record


def test_correlation_fields_reach_the_log_record():
    with log_context(strategy_id=7):
        payload = json.loads(JsonFormatter().format(_record()))
    assert payload["strategy_id"] == 7
    assert payload["correlation_id"]
    assert payload["level"] == "INFO"


def test_nested_contexts_merge_and_share_one_correlation_id():
    """The thread strategy -> experiment -> job that TRD §17 requires."""
    with log_context(strategy_id=7) as outer, log_context(experiment_id=41, job_id=3):
        payload = json.loads(JsonFormatter().format(_record()))
    assert payload["strategy_id"] == 7
    assert payload["experiment_id"] == 41
    assert payload["job_id"] == 3
    assert payload["correlation_id"] == outer["correlation_id"]


def test_context_is_restored_on_exit():
    with log_context(strategy_id=7):
        with log_context(experiment_id=41):
            pass
        assert "experiment_id" not in current_context()
    assert current_context() == {}


def test_context_is_restored_after_an_exception():
    with pytest.raises(RuntimeError), log_context(strategy_id=7):
        raise RuntimeError("boom")
    assert current_context() == {}


def test_none_valued_fields_are_not_bound():
    with log_context(strategy_id=7, experiment_id=None):
        assert "experiment_id" not in current_context()


def test_extra_fields_are_included():
    payload = json.loads(JsonFormatter().format(_record(iteration=3)))
    assert payload["iteration"] == 3


def test_text_formatter_renders_context_inline():
    with log_context(strategy_id=7):
        line = TextFormatter().format(_record("evaluating"))
    assert "evaluating" in line and "strategy_id=7" in line
