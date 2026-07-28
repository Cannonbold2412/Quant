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
