from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from utils.file_utils import ensure_directories


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].lstrip()

    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()

    return key, value


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        assignment = _parse_env_assignment(raw_line)
        if assignment is None:
            continue
        key, value = assignment
        os.environ.setdefault(key, value)


def _split_env_list(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [item.strip() for item in value.replace(";", ",").split(",")]
    return [item for item in parts if item]


def _first_non_empty_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _load_llm_api_keys() -> list[str]:
    multi_value = _first_non_empty_env("LLM_API_KEYS", "OPENAI_API_KEYS", "GROQ_API_KEYS")
    if multi_value:
        return _split_env_list(multi_value)

    single_value = _first_non_empty_env("LLM_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY")
    return [single_value] if single_value else []


def _load_llm_base_url() -> str | None:
    explicit_base_url = _first_non_empty_env("LLM_BASE_URL", "OPENAI_BASE_URL", "GROQ_BASE_URL")
    if explicit_base_url:
        return explicit_base_url

    if _first_non_empty_env("GROQ_API_KEYS", "GROQ_API_KEY"):
        return "https://api.groq.com/openai/v1"
    return None


def _load_google_credential_jsons() -> list[str]:
    values: list[str] = []

    single_value = _first_non_empty_env("GOOGLE_CREDENTIAL_JSON")
    if single_value:
        values.append(single_value)

    indexed_entries: list[tuple[int, str]] = []
    prefix = "GOOGLE_CREDENTIAL_JSON_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if not suffix.isdigit() or not value or not value.strip():
            continue
        indexed_entries.append((int(suffix), value.strip()))

    for _, value in sorted(indexed_entries):
        values.append(value)

    return values


def _load_google_credential_file_list() -> list[Path]:
    explicit_list_file = _first_non_empty_env("GOOGLE_CREDENTIAL_LIST_FILE")
    if not explicit_list_file:
        return []

    list_file = Path(explicit_list_file).expanduser().resolve()
    if not list_file.exists():
        return []

    credential_paths: list[Path] = []
    for raw_line in list_file.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        credential_paths.append(Path(stripped).expanduser().resolve())
    return credential_paths


def _resolve_path(value: str | Path, project_root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def _load_google_credential_directory_files(credential_dir: Path) -> list[Path]:
    if not credential_dir.exists():
        return []
    return [path.resolve() for path in sorted(credential_dir.glob("*.json")) if path.is_file()]


@dataclass(slots=True)
class AppConfig:
    project_root: Path
    storage_dir: Path
    output_dir: Path
    temp_dir: Path
    data_dir: Path
    market_data_dir: Path
    google_token_dir: Path
    tradebooks_dir: Path
    notebook_summaries_path: Path
    indicator_library_path: Path
    strategies_json_path: Path
    market_data_catalog_path: Path
    backtest_jobs_path: Path
    pipeline_manifest_path: Path
    google_credential_dir: Path
    google_credential_files: list[Path]
    google_credential_json: str | None
    google_credential_jsons: list[str]
    shared_drive_ids: list[str]
    market_data_files: list[Path]
    market_data_dirs: list[Path]
    llm_api_keys: list[str]
    llm_base_url: str | None
    log_level: str = "INFO"
    drive_page_size: int = 1000
    text_excerpt_chars: int = 12000
    llm_model: str = "gpt-4.1-mini"
    llm_retries: int = 3
    llm_timeout_seconds: float = 30.0
    llm_chunk_chars: int = 7000
    max_download_mb: int = 256
    no_entry_after: str = "15:15"
    eod_exit_time: str = "15:25"
    brokerage_rate: float = 0.001
    slippage_rate: float = 0.0001
    capital_per_trade: float = 100000.0
    default_stop_loss_pct: float = 0.005
    default_take_profit_pct: float = 0.015
    strategy_target_min: int = 50
    strategy_target_max: int = 100
    google_scopes: tuple[str, ...] = field(
        default_factory=lambda: ("https://www.googleapis.com/auth/drive.readonly",)
    )

    @classmethod
    def from_env(cls, project_root: str | Path | None = None) -> "AppConfig":
        root = Path(project_root or Path.cwd()).resolve()
        _load_dotenv_file(root / ".env")

        storage_dir = root / "storage"
        output_dir = root / "output"
        temp_dir = storage_dir / "tmp"
        data_dir = root / "data"
        market_data_dir = storage_dir / "market_data"
        google_token_dir = storage_dir / "tokens"
        google_credential_dir = _resolve_path(
            _first_non_empty_env("GOOGLE_CREDENTIAL_DIR") or "credentials/google_drive",
            root,
        )

        llm_api_keys = _load_llm_api_keys()
        credential_files_from_env = [
            Path(item).expanduser().resolve()
            for item in _split_env_list(os.getenv("GOOGLE_CREDENTIAL_FILES"))
        ]
        credential_files = list(
            dict.fromkeys(
                credential_files_from_env
                + _load_google_credential_file_list()
                + _load_google_credential_directory_files(google_credential_dir)
            )
        )

        config = cls(
            project_root=root,
            storage_dir=storage_dir,
            output_dir=output_dir,
            temp_dir=temp_dir,
            data_dir=data_dir,
            market_data_dir=market_data_dir,
            google_token_dir=google_token_dir,
            tradebooks_dir=output_dir / "tradebooks",
            notebook_summaries_path=storage_dir / "notebook_summaries.json",
            indicator_library_path=storage_dir / "indicator_library.json",
            strategies_json_path=storage_dir / "strategies.json",
            market_data_catalog_path=storage_dir / "market_data_catalog.json",
            backtest_jobs_path=_resolve_path(
                _first_non_empty_env("BACKTEST_JOBS_FILE") or (storage_dir / "backtest_jobs.json"),
                root,
            ),
            pipeline_manifest_path=storage_dir / "pipeline_manifest.json",
            google_credential_dir=google_credential_dir,
            google_credential_files=credential_files,
            google_credential_json=_first_non_empty_env("GOOGLE_CREDENTIAL_JSON"),
            google_credential_jsons=_load_google_credential_jsons(),
            shared_drive_ids=_split_env_list(os.getenv("GOOGLE_SHARED_DRIVE_IDS")),
            market_data_files=[
                Path(item).expanduser().resolve()
                for item in _split_env_list(os.getenv("MARKET_DATA_FILES"))
            ],
            market_data_dirs=[
                Path(item).expanduser().resolve()
                for item in _split_env_list(os.getenv("MARKET_DATA_DIRS"))
            ]
            or [data_dir, market_data_dir],
            llm_api_keys=llm_api_keys,
            llm_base_url=_load_llm_base_url(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            drive_page_size=int(os.getenv("DRIVE_PAGE_SIZE", "1000")),
            text_excerpt_chars=int(os.getenv("TEXT_EXCERPT_CHARS", "12000")),
            llm_model=_first_non_empty_env("LLM_MODEL", "OPENAI_MODEL") or "gpt-4.1-mini",
            llm_retries=int(_first_non_empty_env("LLM_RETRIES", "OPENAI_RETRIES") or "3"),
            llm_timeout_seconds=float(
                _first_non_empty_env("LLM_TIMEOUT_SECONDS", "OPENAI_TIMEOUT_SECONDS") or "30"
            ),
            llm_chunk_chars=int(_first_non_empty_env("LLM_CHUNK_CHARS", "OPENAI_CHUNK_CHARS") or "7000"),
            max_download_mb=int(os.getenv("MAX_DOWNLOAD_MB", "256")),
            no_entry_after=os.getenv("NO_ENTRY_AFTER", "15:15"),
            eod_exit_time=os.getenv("EOD_EXIT_TIME", "15:25"),
            brokerage_rate=float(os.getenv("BROKERAGE_RATE", "0.001")),
            slippage_rate=float(os.getenv("SLIPPAGE_RATE", "0.0001")),
            capital_per_trade=float(os.getenv("CAPITAL_PER_TRADE", "100000")),
            default_stop_loss_pct=float(os.getenv("DEFAULT_STOP_LOSS_PCT", "0.005")),
            default_take_profit_pct=float(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "0.015")),
            strategy_target_min=int(os.getenv("STRATEGY_TARGET_MIN", "50")),
            strategy_target_max=int(os.getenv("STRATEGY_TARGET_MAX", "100")),
        )
        ensure_directories(
            [
                config.storage_dir,
                config.output_dir,
                config.temp_dir,
                config.data_dir,
                config.market_data_dir,
                config.google_token_dir,
                config.google_credential_dir,
                config.tradebooks_dir,
            ]
        )
        return config


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
