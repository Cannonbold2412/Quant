from __future__ import annotations

import json

from config import AppConfig, _resolve_path
from schemas import BacktestJobDefinition
from utils.file_utils import safe_filename


def _as_string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(f"Expected a string or list, received {type(value).__name__}.")


def _coerce_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def _default_job(config: AppConfig) -> BacktestJobDefinition:
    return BacktestJobDefinition(
        job_id="default",
        description="Legacy fallback job generated from AppConfig defaults.",
        stop_loss_pct=config.default_stop_loss_pct,
        take_profit_pct=config.default_take_profit_pct,
        no_entry_after=config.no_entry_after,
        eod_exit_time=config.eod_exit_time,
        brokerage_rate=config.brokerage_rate,
        slippage_rate=config.slippage_rate,
        capital_per_trade=config.capital_per_trade,
    )


def load_backtest_jobs(config: AppConfig) -> list[BacktestJobDefinition]:
    path = config.backtest_jobs_path
    if not path.exists():
        return [_default_job(config)]

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        raw_jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        raw_jobs = payload
    else:
        raise ValueError("Backtest jobs file must contain a top-level list or an object with a 'jobs' list.")

    jobs: list[BacktestJobDefinition] = []
    for index, raw_job in enumerate(raw_jobs, start=1):
        if not isinstance(raw_job, dict):
            raise ValueError(f"Backtest job #{index} must be a JSON object.")

        source_id = raw_job.get("job_id") or raw_job.get("name") or f"job_{index}"
        job_id = safe_filename(str(source_id))
        market_data_files = [
            _resolve_path(item, config.project_root) for item in _as_string_list(raw_job.get("market_data_files"))
        ]
        market_data_dirs = [
            _resolve_path(item, config.project_root) for item in _as_string_list(raw_job.get("market_data_dirs"))
        ]
        jobs.append(
            BacktestJobDefinition(
                job_id=job_id,
                description=str(raw_job.get("description", "")).strip(),
                enabled=_coerce_bool(raw_job.get("enabled"), default=True),
                engine_names=[name.strip() for name in _as_string_list(raw_job.get("engine_names")) if name.strip()],
                market_categories=[name.strip().lower() for name in _as_string_list(raw_job.get("market_categories"))],
                ticker_patterns=[item.strip() for item in _as_string_list(raw_job.get("ticker_patterns")) if item.strip()],
                market_data_files=market_data_files,
                market_data_dirs=market_data_dirs,
                stop_loss_pct=float(raw_job.get("stop_loss_pct", config.default_stop_loss_pct)),
                take_profit_pct=float(raw_job.get("take_profit_pct", config.default_take_profit_pct)),
                no_entry_after=str(raw_job.get("no_entry_after", config.no_entry_after)),
                eod_exit_time=str(raw_job.get("eod_exit_time", config.eod_exit_time)),
                brokerage_rate=float(raw_job.get("brokerage_rate", config.brokerage_rate)),
                slippage_rate=float(raw_job.get("slippage_rate", config.slippage_rate)),
                capital_per_trade=float(raw_job.get("capital_per_trade", config.capital_per_trade)),
            )
        )

    enabled_jobs = [job for job in jobs if job.enabled]
    return enabled_jobs or [_default_job(config)]
