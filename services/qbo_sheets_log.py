"""Best-effort writer/reader for the legacy Apps Script `ImportLog` Google Sheet.

The Apps Script version of the QBO importer wrote one summary row per import
to an `ImportLog` tab in a shared Google Sheet. ~300 historical rows already
exist there. To preserve that history (and keep one bookmarkable history URL
for the team), the Streamlit importer also writes a matching summary row to
that same tab after every successful post, and can read recent rows back as
"legacy history" in the UI.

Failure mode: every public method catches its own errors. A Sheets outage,
missing credential, or quota exhaustion must never block a QBO import, since
the authoritative audit trail lives in Supabase (`qbo_audit_log`).

Legacy column schema (do not change order without coordinating with the
Apps Script project):
    Timestamp | User | Action | Template | Company | RealmId |
    SourceSheet | SourceCount | Success | Failed | Skipped |
    DurationMs | ExecutionId | ErrorsPreview
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)

DEFAULT_SPREADSHEET_ID = "1tpzTBuMgyWYItyJ0E4rWHQ6VVBvXvspWpJi6cepM5wo"
DEFAULT_WORKSHEET = "ImportLog"
LEGACY_COLUMNS: list[str] = [
    "Timestamp",
    "User",
    "Action",
    "Template",
    "Company",
    "RealmId",
    "SourceSheet",
    "SourceCount",
    "Success",
    "Failed",
    "Skipped",
    "DurationMs",
    "ExecutionId",
    "ErrorsPreview",
]
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _setting(name: str, default: str = "") -> str:
    value = get_runtime_secret(name, default)
    return (value or "").strip()


def _resolved_spreadsheet_id() -> str:
    return _setting("QBO_IMPORT_LOG_SHEET_ID", DEFAULT_SPREADSHEET_ID) or DEFAULT_SPREADSHEET_ID


def _resolved_worksheet() -> str:
    return _setting("QBO_IMPORT_LOG_WORKSHEET", DEFAULT_WORKSHEET) or DEFAULT_WORKSHEET


class GoogleSheetsImportLog:
    """Append/read summary rows to the legacy `ImportLog` Google Sheet."""

    def __init__(
        self,
        *,
        spreadsheet_id: str | None = None,
        worksheet_name: str | None = None,
    ) -> None:
        self._spreadsheet_id = (spreadsheet_id or _resolved_spreadsheet_id()).strip()
        self._worksheet_name = (worksheet_name or _resolved_worksheet()).strip()
        self._worksheet: Any | None = None
        self._unavailable_reason: str | None = None

    # ------------------------------------------------------------------ writes
    def append_summary(
        self,
        *,
        user_email: str,
        action: str,
        template: str,
        company: str,
        realm_id: str,
        source_sheet: str,
        source_count: int,
        success: int,
        failed: int,
        skipped: int,
        duration_ms: int = 0,
        execution_id: str = "",
        errors: Iterable[str] | None = None,
    ) -> bool:
        worksheet = self._open_worksheet()
        if worksheet is None:
            return False

        errors_preview = " | ".join(str(e) for e in (errors or []) if e)[:500]
        row = [
            datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            user_email or "",
            action or "",
            template or "",
            company or "",
            realm_id or "",
            source_sheet or "",
            int(source_count or 0),
            int(success or 0),
            int(failed or 0),
            int(skipped or 0),
            int(duration_ms or 0),
            execution_id or "",
            errors_preview,
        ]
        try:
            worksheet.append_row(row, value_input_option="USER_ENTERED")
            return True
        except Exception as exc:  # noqa: BLE001 - never break the import flow
            logger.warning("Failed to append to ImportLog sheet: %s", exc)
            self._unavailable_reason = f"append failed: {exc}"
            return False

    # ------------------------------------------------------------------- reads
    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        worksheet = self._open_worksheet()
        if worksheet is None:
            return []
        try:
            values = worksheet.get_all_values()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read ImportLog sheet: %s", exc)
            self._unavailable_reason = f"read failed: {exc}"
            return []
        if not values:
            return []

        header = [str(cell).strip() for cell in values[0]]
        body = values[1:]
        if not body:
            return []

        recent_rows = list(reversed(body[-max(1, int(limit or 1)) :]))
        out: list[dict[str, Any]] = []
        for raw in recent_rows:
            row: dict[str, Any] = {}
            for index, column in enumerate(header):
                row[column or f"col_{index + 1}"] = raw[index] if index < len(raw) else ""
            out.append(row)
        return out

    # --------------------------------------------------------------- diagnostics
    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @property
    def worksheet_name(self) -> str:
        return self._worksheet_name

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def is_configured(self) -> bool:
        return bool(self._spreadsheet_id and self._worksheet_name and _service_account_info())

    # ----------------------------------------------------------------- internals
    def _open_worksheet(self) -> Any | None:
        if self._worksheet is not None:
            return self._worksheet
        if not self._spreadsheet_id:
            self._unavailable_reason = "QBO_IMPORT_LOG_SHEET_ID is not configured."
            return None

        creds_info = _service_account_info()
        if not creds_info:
            self._unavailable_reason = (
                "GOOGLE_SERVICE_ACCOUNT_JSON is not configured for this Streamlit app, "
                "so the ImportLog sheet cannot be reached."
            )
            return None

        try:
            import gspread  # type: ignore
            from google.oauth2.service_account import Credentials  # type: ignore
        except ImportError as exc:
            self._unavailable_reason = f"gspread/google-auth not installed: {exc}"
            return None

        try:
            credentials = Credentials.from_service_account_info(creds_info, scopes=GOOGLE_SCOPES)
            client = gspread.authorize(credentials)
            spreadsheet = client.open_by_key(self._spreadsheet_id)
        except Exception as exc:  # noqa: BLE001
            self._unavailable_reason = f"Could not open spreadsheet {self._spreadsheet_id}: {exc}"
            logger.warning(self._unavailable_reason)
            return None

        try:
            worksheet = spreadsheet.worksheet(self._worksheet_name)
        except Exception:
            try:
                worksheet = spreadsheet.add_worksheet(
                    title=self._worksheet_name, rows=1000, cols=len(LEGACY_COLUMNS)
                )
                worksheet.append_row(LEGACY_COLUMNS, value_input_option="USER_ENTERED")
            except Exception as exc:  # noqa: BLE001
                self._unavailable_reason = (
                    f"Worksheet {self._worksheet_name!r} not found and could not be created: {exc}"
                )
                logger.warning(self._unavailable_reason)
                return None

        self._worksheet = worksheet
        return worksheet


def _service_account_info() -> dict[str, Any] | None:
    raw = _setting("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: %s", exc)
        return None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON does not look like a service account key.")
        return None
    return info
