"""Configuration — environment plus file, no secrets in code (Implementation_Plan §3).

Precedence, highest first:

    explicit kwargs  >  AQRL_* environment  >  .env  >  aqrl.toml  >  defaults

Path settings may be written relative; they are resolved against the repository
root so that behaviour does not depend on the working directory.

`data_root` deserves a note. Backend-Schema §16 requires Parquet paths to be
stored in the database **relative to a configurable root**, so moving from a
local disk to object storage is a configuration change rather than a data
migration. `to_data_relative` / `from_data_relative` are the only sanctioned way
to cross that boundary — nothing else should join paths against `data_root` by
hand.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LogFormat = Literal["json", "text"]


class Settings(BaseSettings):
    """Resolved runtime configuration. Immutable once constructed."""

    model_config = SettingsConfigDict(
        env_prefix="AQRL_",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file=PROJECT_ROOT / "aqrl.toml",
        extra="ignore",
        frozen=True,
    )

    db_path: Path = Field(default=Path("aqrl.db"), description="SQLite metadata database.")
    data_root: Path = Field(default=Path("data"), description="Root of the Parquet snapshot store.")
    profiles_dir: Path = Field(default=Path("profiles"), description="Market/timeframe/cost YAML.")
    log_level: str = Field(default="INFO")
    log_format: LogFormat = Field(default="json")

    # -- Stage 5: A2 Quant Engineer (Implementation_Plan §8) ---------------------
    strategy_repo_path: Path = Field(
        default=Path("strategy_repo"),
        description="One repo, one branch per strategy (TRD §5.2). Never the framework repo.",
    )
    max_fix_attempts: int = Field(
        default=3, description="Bounded FIX_CODE retries on one experiment before quarantine (App-Flow §4.3)."
    )
    sandbox_timeout_seconds: int = Field(
        default=60, description="Wall-clock cap on the static-check/smoke-run subprocess."
    )
    sandbox_cpu_seconds: int = Field(default=30, description="RLIMIT_CPU for the sandboxed subprocess.")
    sandbox_memory_bytes: int = Field(
        default=3 * 1024 * 1024 * 1024, description="RLIMIT_AS for the sandboxed subprocess (3 GiB default)."
    )
    anthropic_model: str = Field(default="claude-opus-5", description="Model A2's session wrapper calls.")

    # -- Stage 4: the nervous system (Implementation_Plan §6) -------------------
    busy_timeout_ms: int = Field(
        default=5000, description="SQLite busy_timeout — how long a writer waits under contention."
    )
    scheduler_tick_seconds: int = Field(default=60, description="Scheduler tick interval.")
    default_lease_seconds: int = Field(default=300, description="Job lease duration before it is reclaimable.")
    max_concurrent_workers: int = Field(default=4, description="Scheduler dispatch concurrency cap.")
    quarantine_after_failures: int = Field(
        default=3, description="Consecutive job failures on one strategy before it is quarantined."
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @field_validator("db_path", "data_root", "profiles_dir")
    @classmethod
    def _resolve_against_root(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}, got {value!r}")
        return level

    # -- the data_root boundary ------------------------------------------------

    def to_data_relative(self, path: Path | str) -> str:
        """Absolute path -> the POSIX-relative form stored in the database."""
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.data_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"{resolved} is outside data_root {self.data_root}") from exc

    def from_data_relative(self, relative: str) -> Path:
        """The stored relative form -> an absolute path under the current root."""
        return self.data_root / relative


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached, so a run cannot silently change roots."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cache. For tests that manipulate the environment."""
    get_settings.cache_clear()
