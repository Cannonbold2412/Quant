from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha1
from pathlib import Path
from typing import Any

from config import AppConfig

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_google_auth_components() -> tuple[Any, Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google.oauth2.service_account import Credentials as ServiceAccountCredentials
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Google Drive dependencies are missing. Install them with `pip install -r requirements.txt`."
        ) from exc

    return Request, Credentials, ServiceAccountCredentials, build


@lru_cache(maxsize=1)
def _load_installed_app_flow() -> Any:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "google-auth-oauthlib is required for OAuth client credentials. "
            "Install it with `pip install -r requirements.txt`."
        ) from exc

    return InstalledAppFlow


@dataclass(slots=True)
class DriveAccount:
    name: str
    service: Any


def _token_cache_name(payload: dict, fallback: str) -> str:
    client_id = payload.get("client_id")
    if client_id:
        return str(client_id)

    installed_payload = payload.get("installed")
    if isinstance(installed_payload, dict) and installed_payload.get("client_id"):
        return str(installed_payload["client_id"])

    web_payload = payload.get("web")
    if isinstance(web_payload, dict) and web_payload.get("client_id"):
        return str(web_payload["client_id"])

    digest = sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"{fallback}_{digest}"


def _load_credentials(credentials_path: Path, token_dir: Path, scopes: tuple[str, ...]) -> Any:
    with credentials_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    return _load_credentials_from_payload(payload, token_dir, scopes, credentials_path.stem)


def _load_credentials_from_payload(
    payload: dict,
    token_dir: Path,
    scopes: tuple[str, ...],
    source_name: str,
) -> Any:
    Request, Credentials, ServiceAccountCredentials, _ = _load_google_auth_components()
    credential_type = payload.get("type")
    if credential_type is None:
        if isinstance(payload.get("installed"), dict):
            credential_type = "installed"
        elif isinstance(payload.get("web"), dict):
            credential_type = "web"

    if credential_type == "service_account":
        return ServiceAccountCredentials.from_service_account_info(payload, scopes=scopes)

    if credential_type == "authorized_user":
        creds = Credentials.from_authorized_user_info(payload, scopes=scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds

    if credential_type in {"installed", "web"}:
        InstalledAppFlow = _load_installed_app_flow()
        token_path = token_dir / f"{_token_cache_name(payload, source_name)}_token.json"
        creds = None
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes=scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_config(payload, scopes=scopes)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    raise ValueError(f"Unsupported Google credential type in {source_name}.")


def authenticate_accounts(config: AppConfig) -> list[DriveAccount]:
    _, _, _, build = _load_google_auth_components()
    inline_payloads = config.google_credential_jsons or ([config.google_credential_json] if config.google_credential_json else [])

    if not config.google_credential_files and not inline_payloads:
        raise RuntimeError(
            f"No Google Drive credentials configured. Add JSON files to {config.google_credential_dir}, set GOOGLE_CREDENTIAL_DIR, or use GOOGLE_CREDENTIAL_JSON / GOOGLE_CREDENTIAL_FILES."
        )

    accounts: list[DriveAccount] = []
    for index, raw_json in enumerate(inline_payloads, start=1):
        payload = json.loads(raw_json)
        source_name = "GOOGLE_CREDENTIAL_JSON" if index == 1 and config.google_credential_json == raw_json else f"GOOGLE_CREDENTIAL_JSON_{index}"
        creds = _load_credentials_from_payload(payload, config.google_token_dir, config.google_scopes, source_name)
        account_name = _token_cache_name(payload, f"env_google_credential_{index}")
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        accounts.append(DriveAccount(name=account_name, service=service))
        logger.info("Authenticated Drive account from .env: %s", account_name)

    for credential_file in config.google_credential_files:
        creds = _load_credentials(credential_file, config.google_token_dir, config.google_scopes)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        account_name = credential_file.stem
        accounts.append(DriveAccount(name=account_name, service=service))
        logger.info("Authenticated Drive account: %s", account_name)
    return accounts
