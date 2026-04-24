"""Append each submission as a row in the shared 'Applicants' Google Sheet.

The sheet has one tab per company. The slug-to-tab mapping intentionally keeps
the URL slug "side-xpress" so existing application links keep working, while
the visible tab is just "Xpress" per the safety team's preference.

The tabs receive a frozen header row on first write. New submissions are
inserted at row 2 so the newest applicant is always at the top -- no manual
sorting required when the sheet is opened.

Failure mode: every public function in this module catches its own exceptions
and returns a structured status dict. The submission flow must never break
because Sheets is unreachable, the credentials are missing, or the API quota
is exhausted.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from config import COMPANY_PROFILES, DEFAULT_COMPANY_SLUG
from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)


# Visible Google Sheets tab name per company slug. Slugs themselves are
# unchanged so existing ?company=<slug> links keep working.
COMPANY_TAB_NAMES: dict[str, str] = {
    "prestige": "Prestige Transportation",
    "side-xpress": "Xpress",
}

# Column order written into the header row on first run. Adding a new column
# later is safe -- the code only writes the header if the existing first row
# is empty, so existing sheets keep their existing column layout.
SHEET_COLUMNS: list[str] = [
    "Submitted At",
    "Apply Date",
    "Applicant Name",
    "Email",
    "Primary Phone",
    "Cell Phone",
    "Date of Birth",
    "Address",
    "City",
    "State",
    "Zip",
    "Position Applied",
    "Has CDL",
    "License Number",
    "License State",
    "License Class",
    "Years Driving Experience",
    "Submission ID",
    "Storage Location",
    "Test Mode",
]

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _tab_name_for_company(company_slug: str | None) -> str:
    slug = (company_slug or DEFAULT_COMPANY_SLUG).strip().lower()
    if slug in COMPANY_TAB_NAMES:
        return COMPANY_TAB_NAMES[slug]
    profile = COMPANY_PROFILES.get(slug) or COMPANY_PROFILES.get(DEFAULT_COMPANY_SLUG)
    return profile.name if profile else slug


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_stringify(v)}" for k, v in value.items())
    return str(value)


def _full_name(form_data: dict[str, Any]) -> str:
    parts = [
        str(form_data.get("first_name", "") or "").strip(),
        str(form_data.get("middle_name", "") or "").strip(),
        str(form_data.get("last_name", "") or "").strip(),
    ]
    return " ".join(part for part in parts if part)


def _primary_license(licenses: list[dict[str, Any]] | None) -> dict[str, Any]:
    for entry in licenses or []:
        if isinstance(entry, dict):
            return entry
    return {}


def build_submission_row(
    *,
    form_data: dict[str, Any] | None,
    licenses: list[dict[str, Any]] | None = None,
    submission_id: str | None = None,
    storage_location: str | None = None,
    test_mode: bool = False,
) -> list[str]:
    """Compose the cell values for a single submission row.

    Pure function -- no Google API calls. Easy to unit test.
    """

    form_data = form_data or {}
    primary = _primary_license(licenses)
    submitted_at = (
        form_data.get("final_submission_timestamp")
        or datetime.now().isoformat(timespec="seconds")
    )
    apply_date = (
        form_data.get("apply_date")
        or form_data.get("application_date")
        or (
            submitted_at[:10]
            if isinstance(submitted_at, str) and len(submitted_at) >= 10
            else ""
        )
    )

    values = {
        "Submitted At": submitted_at,
        "Apply Date": apply_date,
        "Applicant Name": _full_name(form_data),
        "Email": form_data.get("email"),
        "Primary Phone": form_data.get("primary_phone"),
        "Cell Phone": form_data.get("cell_phone"),
        "Date of Birth": form_data.get("dob"),
        "Address": form_data.get("address"),
        "City": form_data.get("city"),
        "State": form_data.get("state"),
        "Zip": form_data.get("zip_code"),
        "Position Applied": form_data.get("position_applied")
        or form_data.get("position"),
        "Has CDL": form_data.get("has_cdl"),
        "License Number": primary.get("license_number")
        or primary.get("number"),
        "License State": primary.get("license_state")
        or primary.get("state"),
        "License Class": primary.get("license_class")
        or primary.get("class"),
        "Years Driving Experience": form_data.get("years_experience")
        or form_data.get("years_driving"),
        "Submission ID": submission_id,
        "Storage Location": storage_location,
        "Test Mode": test_mode,
    }
    return [_stringify(values.get(column)) for column in SHEET_COLUMNS]


def _sheets_settings() -> dict[str, str]:
    return {
        "service_account_json": (
            get_runtime_secret("GOOGLE_SERVICE_ACCOUNT_JSON", "") or ""
        ).strip(),
        "spreadsheet_id": (
            get_runtime_secret("APPLICANTS_SHEET_ID", "") or ""
        ).strip(),
    }


def _open_worksheet(spreadsheet_id: str, tab_name: str, credentials_info: dict[str, Any]):
    """Authenticate and return a gspread worksheet for ``tab_name``.

    Imports gspread / google-auth lazily so the rest of the app doesn't pay the
    import cost (or break) when Sheets sync isn't configured.
    """

    import gspread  # type: ignore[import-not-found]
    from google.oauth2.service_account import Credentials  # type: ignore[import-not-found]

    credentials = Credentials.from_service_account_info(
        credentials_info, scopes=GOOGLE_SCOPES
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        worksheet = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=tab_name, rows=1000, cols=max(len(SHEET_COLUMNS), 26)
        )
    return worksheet


def _ensure_header(worksheet) -> None:
    """Write the header row if the sheet is empty; otherwise leave it alone."""

    existing = worksheet.row_values(1)
    if existing:
        return
    worksheet.update("A1", [SHEET_COLUMNS])
    try:
        worksheet.freeze(rows=1)
    except Exception:  # noqa: BLE001 - freeze is cosmetic, never fatal
        logger.debug("Could not freeze header row", exc_info=True)


def append_submission_row(
    *,
    company_slug: str | None,
    form_data: dict[str, Any] | None,
    licenses: list[dict[str, Any]] | None = None,
    submission_id: str | None = None,
    storage_location: str | None = None,
    test_mode: bool = False,
) -> dict[str, Any]:
    """Insert one row for a submission. Always returns a status dict."""

    settings = _sheets_settings()
    if not settings["service_account_json"] or not settings["spreadsheet_id"]:
        return {
            "status": "disabled",
            "message": "Sheets export not configured (missing GOOGLE_SERVICE_ACCOUNT_JSON or APPLICANTS_SHEET_ID).",
        }

    try:
        credentials_info = json.loads(settings["service_account_json"])
    except json.JSONDecodeError as exc:
        logger.warning("Invalid GOOGLE_SERVICE_ACCOUNT_JSON: %s", exc)
        return {
            "status": "error",
            "message": f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}",
        }

    tab_name = _tab_name_for_company(company_slug)
    row = build_submission_row(
        form_data=form_data,
        licenses=licenses,
        submission_id=submission_id,
        storage_location=storage_location,
        test_mode=test_mode,
    )

    try:
        worksheet = _open_worksheet(
            settings["spreadsheet_id"], tab_name, credentials_info
        )
        _ensure_header(worksheet)
        # Insert at row 2 so the newest submission is always at the top,
        # immediately under the frozen header.
        worksheet.insert_row(row, index=2, value_input_option="USER_ENTERED")
        return {
            "status": "appended",
            "message": f"Row added to '{tab_name}' tab.",
            "tab": tab_name,
        }
    except Exception as exc:  # noqa: BLE001 - never let Sheets break submissions
        logger.warning("Sheets append failed for tab '%s': %s", tab_name, exc)
        return {
            "status": "error",
            "message": f"Sheets append failed: {exc}",
            "tab": tab_name,
        }
