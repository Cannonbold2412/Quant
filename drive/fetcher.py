from __future__ import annotations

import logging
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from config import AppConfig
from drive.auth import DriveAccount
from schemas import DriveFileMetadata

logger = logging.getLogger(__name__)

GOOGLE_EXPORT_MIME_MAP: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
}

GOOGLE_DIRECT_DOWNLOAD_MIME_MAP: dict[str, str] = {
    "application/vnd.google.colaboratory": ".ipynb",
}


@dataclass(slots=True)
class DriveDownloadResult:
    path: Path | None
    reason: str = ""
    transfer_mode: str = ""


def _is_file_not_exportable_error(exc: HttpError) -> bool:
    message = str(exc)
    return "fileNotExportable" in message or "Export only supports Docs Editors files." in message


class GoogleDriveFetcher:
    def __init__(self, config: AppConfig, accounts: list[DriveAccount]) -> None:
        self.config = config
        self.accounts = accounts

    def iter_files(self) -> Iterator[DriveFileMetadata]:
        seen_ids: set[str] = set()
        for account in self.accounts:
            for file_metadata in self._iter_account_files(account):
                if file_metadata.file_id in seen_ids:
                    continue
                seen_ids.add(file_metadata.file_id)
                yield file_metadata

    def _iter_account_files(self, account: DriveAccount) -> Iterator[DriveFileMetadata]:
        for drive_scope in [None, *self.config.shared_drive_ids]:
            page_token: str | None = None
            while True:
                request_kwargs = {
                    "q": "trashed = false and mimeType != 'application/vnd.google-apps.folder'",
                    "pageSize": self.config.drive_page_size,
                    "pageToken": page_token,
                    "fields": (
                        "nextPageToken, files("
                        "id, name, mimeType, fileExtension, size, modifiedTime, md5Checksum, "
                        "parents, driveId, webViewLink)"
                    ),
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                }
                if drive_scope:
                    request_kwargs["corpora"] = "drive"
                    request_kwargs["driveId"] = drive_scope
                else:
                    request_kwargs["corpora"] = "user"

                try:
                    response = account.service.files().list(**request_kwargs).execute()
                except HttpError as exc:
                    logger.exception("Failed listing files for account=%s drive=%s", account.name, drive_scope or "user")
                    raise RuntimeError("Google Drive listing failed.") from exc

                for item in response.get("files", []):
                    extension = item.get("fileExtension") or Path(item["name"]).suffix.lstrip(".")
                    yield DriveFileMetadata(
                        file_id=item["id"],
                        name=item["name"],
                        mime_type=item.get("mimeType", ""),
                        file_extension=extension.lower(),
                        size=int(item["size"]) if item.get("size") is not None else None,
                        modified_time=item.get("modifiedTime"),
                        parents=item.get("parents", []),
                        web_view_link=item.get("webViewLink"),
                        drive_id=item.get("driveId"),
                        account_name=account.name,
                        md5_checksum=item.get("md5Checksum"),
                    )

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

    def download_to_tempfile(self, metadata: DriveFileMetadata) -> DriveDownloadResult:
        account = next(account for account in self.accounts if account.name == metadata.account_name)
        request, suffix, transfer_mode = self._build_download_request(account, metadata)
        logger.debug(
            "Drive fetch decision file_id=%s name=%s mime_type=%s extension=%s transfer_mode=%s",
            metadata.file_id,
            metadata.name,
            metadata.mime_type,
            metadata.file_extension,
            transfer_mode,
        )
        if request is None:
            logger.info(
                "Skipping Google-native file file_id=%s name=%s mime_type=%s because no export rule exists.",
                metadata.file_id,
                metadata.name,
                metadata.mime_type,
            )
            return DriveDownloadResult(path=None, reason="unsupported_google_native", transfer_mode=transfer_mode)

        if metadata.size and metadata.size > self.config.max_download_mb * 1024 * 1024:
            logger.warning(
                "Skipping oversized file file_id=%s name=%s (%.2f MB > limit %d MB)",
                metadata.file_id,
                metadata.name,
                metadata.size / (1024 * 1024),
                self.config.max_download_mb,
            )
            return DriveDownloadResult(path=None, reason="oversized", transfer_mode=transfer_mode)

        temp_handle = tempfile.NamedTemporaryFile(delete=False, dir=self.config.temp_dir, suffix=suffix)
        temp_path = Path(temp_handle.name)
        downloader = MediaIoBaseDownload(temp_handle, request)
        done = False
        try:
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as exc:
            temp_handle.close()
            temp_path.unlink(missing_ok=True)
            if _is_file_not_exportable_error(exc):
                logger.warning(
                    "Skipping file file_id=%s name=%s because Drive cannot export mime_type=%s extension=%s transfer_mode=%s web_view_link=%s.",
                    metadata.file_id,
                    metadata.name,
                    metadata.mime_type,
                    metadata.file_extension,
                    transfer_mode,
                    metadata.web_view_link,
                )
                return DriveDownloadResult(path=None, reason="file_not_exportable", transfer_mode=transfer_mode)
            logger.exception(
                "Drive fetch failed file_id=%s name=%s mime_type=%s extension=%s transfer_mode=%s",
                metadata.file_id,
                metadata.name,
                metadata.mime_type,
                metadata.file_extension,
                transfer_mode,
            )
            raise
        except Exception:
            temp_handle.close()
            temp_path.unlink(missing_ok=True)
            raise
        finally:
            if not temp_handle.closed:
                temp_handle.close()
        logger.debug(
            "Drive fetch succeeded file_id=%s name=%s transfer_mode=%s temp_path=%s",
            metadata.file_id,
            metadata.name,
            transfer_mode,
            temp_path,
        )
        return DriveDownloadResult(path=temp_path, transfer_mode=transfer_mode)

    def _build_download_request(self, account: DriveAccount, metadata: DriveFileMetadata) -> tuple[Any | None, str, str]:
        suffix = Path(metadata.name).suffix
        if metadata.mime_type in GOOGLE_DIRECT_DOWNLOAD_MIME_MAP:
            request = account.service.files().get_media(fileId=metadata.file_id)
            download_suffix = GOOGLE_DIRECT_DOWNLOAD_MIME_MAP[metadata.mime_type]
            return request, suffix or download_suffix, "download:get_media_google_native"

        if metadata.mime_type in GOOGLE_EXPORT_MIME_MAP:
            export_mime, export_suffix = GOOGLE_EXPORT_MIME_MAP[metadata.mime_type]
            request = account.service.files().export_media(fileId=metadata.file_id, mimeType=export_mime)
            return request, export_suffix, f"export:{export_mime}"

        if metadata.mime_type.startswith("application/vnd.google-apps"):
            return None, suffix or "", "skip:unsupported_google_native"

        request = account.service.files().get_media(fileId=metadata.file_id)
        if suffix:
            return request, suffix, "download:get_media"
        if metadata.file_extension:
            return request, f".{metadata.file_extension}", "download:get_media"
        return request, "", "download:get_media"
