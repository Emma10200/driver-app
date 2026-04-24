"""Append each submission as a row in the shared 'Applicants' Google Sheet.

The sheet has one tab per company. The visible tab name is just "Xpress" per
the safety team's preference.

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


# Visible Google Sheets tab name per company slug.
COMPANY_TAB_NAMES: dict[str, str] = {
    "prestige": "Prestige Transportation",
    "xpress": "Xpress",
}

# Tab names for the cross-company decision log. Approved + declined live in
# the same spreadsheet as the per-company applicant tabs so safety/owner only
# has one URL to bookmark.
DECISION_TAB_NAMES: dict[str, str] = {
    "approved": "Approved",
    "declined": "Declined",
}

# Division label written into the General > Division column. Mirrors what the
# ProTransport ERP shows so safety can copy/paste straight across.
COMPANY_DIVISION_LABEL: dict[str, str] = {
    "prestige": "Prestig Inc",
    "xpress": "Xpress Inc",
}

# Column order written into the header row. The layout mirrors the
# ProTransport ERP "Driver Info" tab so the safety team can copy a row from
# this sheet straight into the matching ERP fields. Columns we do not collect
# in the application (e.g. bank account, hire date) are still included as
# empty cells so the column position stays aligned with ProTransport.
SHEET_COLUMNS: list[str] = [
    # Application metadata
    "Submitted At",
    "Apply Date",
    # Personal Info
    "First Name",
    "Middle Name",
    "Last Name",
    "Display Name",
    "Date Of Birth",
    "SSN",
    "FEIN",
    "Bank Account #",
    "Bank Routing #",
    "Active",
    # Address
    "Street 1",
    "Street 2",
    "City",
    "State",
    "Zip",
    "Country",
    # Contact
    "Home Phone",
    "Cell Phone",
    "Email",
    "Send Text Messages",
    "Send Emails",
    "Emergency Contact",
    "Emergency Phone",
    "Emergency Relationship",
    # Safety
    "Medical Card Expiration",
    "TWIC Expiration",
    "Pre-Employment Drug Test",
    "Pre-Employment MVR Date",
    "Pre-Employment Clearinghouse Date",
    # General
    "Hire Date",
    "Termination Date",
    "Status",
    "Driver Personal ID",
    "Dispatch Category",
    "Division",
    # CDL Info
    "CDL Number",
    "CDL State",
    "CDL Expiration Date",
    "CDL Class",
    "CDL Endorsement",
    "Years Of Experience",
    "States Operated",
    "Safe Driving Awards",
    "Special Training",
    # Application context
    "Position Applied",
    "Has CDL",
    "Submission ID",
    "Storage Location",
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


def _display_name(form_data: dict[str, Any]) -> str:
    """Return 'Last, First M.' to match ProTransport's Display Name field."""
    last = str(form_data.get("last_name", "") or "").strip()
    first = str(form_data.get("first_name", "") or "").strip()
    middle = str(form_data.get("middle_name", "") or "").strip()
    if not last and not first:
        return ""
    middle_initial = f" {middle[0]}." if middle else ""
    if last and first:
        return f"{last}, {first}{middle_initial}".strip()
    return last or first


def _cdl_endorsement_label(form_data: dict[str, Any], primary_license: dict[str, Any]) -> str:
    """Combine endorsement-style flags into one comma-separated label.

    ProTransport stores endorsements as a free-text field; the safety team
    cares about the abbreviations (H, X, T) plus TWIC, so we surface those.
    """

    parts: list[str] = []
    explicit = primary_license.get("endorsements") or primary_license.get("endorsement")
    if explicit:
        parts.append(_stringify(explicit))

    hazmat = str(form_data.get("hazmat_endorsement", "") or "").strip().lower()
    if hazmat in {"yes", "true", "1"}:
        parts.append("Hazmat")

    twic = str(form_data.get("twic_card", "") or "").strip().lower()
    if twic in {"yes", "true", "1"}:
        parts.append("TWIC")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for raw in parts:
        for token in [t.strip() for t in raw.split(",") if t.strip()]:
            key = token.lower()
            if key not in seen:
                seen.add(key)
                unique.append(token)
    return ", ".join(unique)


def _send_emails_label(form_data: dict[str, Any]) -> str:
    """Default to Yes if an email is present (ProTransport defaults to checked)."""
    email = str(form_data.get("email", "") or "").strip()
    return "Yes" if email else ""


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
    company_slug: str | None = None,
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

    primary_phone = form_data.get("primary_phone")
    cell_phone = form_data.get("cell_phone") or primary_phone

    division = ""
    slug = (company_slug or form_data.get("company_slug") or "").strip().lower()
    if slug:
        division = COMPANY_DIVISION_LABEL.get(slug, "")

    values = {
        # Application metadata
        "Submitted At": submitted_at,
        "Apply Date": apply_date,
        # Personal Info
        "First Name": form_data.get("first_name"),
        "Middle Name": form_data.get("middle_name"),
        "Last Name": form_data.get("last_name"),
        "Display Name": _display_name(form_data),
        "Date Of Birth": form_data.get("dob"),
        "SSN": form_data.get("ssn"),
        "FEIN": "",
        "Bank Account #": "",
        "Bank Routing #": "",
        "Active": "",
        # Address
        "Street 1": form_data.get("address"),
        "Street 2": "",
        "City": form_data.get("city"),
        "State": form_data.get("state"),
        "Zip": form_data.get("zip_code"),
        "Country": form_data.get("country"),
        # Contact
        "Home Phone": primary_phone,
        "Cell Phone": cell_phone,
        "Email": form_data.get("email"),
        "Send Text Messages": form_data.get("text_consent"),
        "Send Emails": _send_emails_label(form_data),
        "Emergency Contact": form_data.get("emergency_name"),
        "Emergency Phone": form_data.get("emergency_phone"),
        "Emergency Relationship": form_data.get("emergency_relationship"),
        # Safety
        "Medical Card Expiration": form_data.get("medical_card_expiration"),
        "TWIC Expiration": form_data.get("twic_expiration"),
        "Pre-Employment Drug Test": "",
        "Pre-Employment MVR Date": "",
        "Pre-Employment Clearinghouse Date": "",
        # General
        "Hire Date": "",
        "Termination Date": "",
        "Status": "",
        "Driver Personal ID": "",
        "Dispatch Category": "",
        "Division": division,
        # CDL Info
        "CDL Number": primary.get("license_number") or primary.get("number"),
        "CDL State": primary.get("license_state") or primary.get("state"),
        "CDL Expiration Date": primary.get("expiration_date")
        or primary.get("expiration"),
        "CDL Class": primary.get("license_class") or primary.get("class"),
        "CDL Endorsement": _cdl_endorsement_label(form_data, primary),
        "Years Of Experience": form_data.get("years_experience")
        or form_data.get("years_driving"),
        "States Operated": "",
        "Safe Driving Awards": "",
        "Special Training": "",
        # Application context
        "Position Applied": form_data.get("position_applied")
        or form_data.get("position"),
        "Has CDL": form_data.get("has_cdl"),
        "Submission ID": submission_id,
        "Storage Location": storage_location,
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


def _ensure_header(worksheet, columns: list[str] | None = None) -> None:
    """Make sure the header row matches the given column list.

    Defaults to SHEET_COLUMNS so existing callers stay backward-compatible.
    If the sheet is empty, write the header. If the existing header doesn't
    match (e.g. we added new columns in a release), rewrite row 1 in place so
    new columns appear above any existing data. Only the header row is
    touched -- data rows below it are never modified here.
    """

    target_columns = columns if columns is not None else SHEET_COLUMNS
    existing = worksheet.row_values(1)
    if existing == target_columns:
        return

    # Make sure the worksheet has enough columns to fit the header.
    needed_cols = len(target_columns)
    try:
        current_cols = int(getattr(worksheet, "col_count", 0) or 0)
    except (TypeError, ValueError):
        current_cols = 0
    if current_cols and current_cols < needed_cols:
        try:
            worksheet.resize(cols=needed_cols)
        except Exception:  # noqa: BLE001 - resize is best-effort
            logger.debug("Could not resize worksheet columns", exc_info=True)

    worksheet.update("A1", [target_columns])
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
        company_slug=company_slug,
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


def append_from_payload(
    payload: dict[str, Any],
    *,
    storage_location: str | None = None,
    company_slug_override: str | None = None,
) -> dict[str, Any]:
    """Append a row from a previously-saved ``submission.json`` payload.

    Used by the admin dashboard to re-export an existing submission to the
    shared sheet (e.g. for backfilling submissions that pre-date the Sheets
    integration).
    """

    payload = payload or {}
    form_data = payload.get("form_data") or {}
    company_slug = (
        company_slug_override
        or form_data.get("company_slug")
        or DEFAULT_COMPANY_SLUG
    )
    submission_id = payload.get("submission_key") or payload.get("submission_id")
    test_mode = bool(form_data.get("test_mode"))

    return append_submission_row(
        company_slug=company_slug,
        form_data=form_data,
        licenses=payload.get("licenses") or [],
        submission_id=submission_id,
        storage_location=storage_location,
        test_mode=test_mode,
    )


# ---------------------------------------------------------------------------
# Decision log (Approved / Declined tabs)
# ---------------------------------------------------------------------------

# Columns prepended to every decision row. The remainder of the row mirrors
# SHEET_COLUMNS so safety/owner can see the full applicant snapshot alongside
# the decision metadata in one place.
DECISION_PREFIX_COLUMNS: list[str] = [
    "Decision",
    "Decided At",
    "Decided By",
    "Company",
    "Notes",
]
DECISION_SHEET_COLUMNS: list[str] = DECISION_PREFIX_COLUMNS + SHEET_COLUMNS


def _decision_tab_name(decision: str) -> str:
    key = (decision or "").strip().lower()
    if key not in DECISION_TAB_NAMES:
        raise ValueError(
            f"Unknown decision '{decision}'. Expected one of: "
            f"{sorted(DECISION_TAB_NAMES)}."
        )
    return DECISION_TAB_NAMES[key]


def append_decision_row(
    *,
    decision: str,
    decided_by: str | None,
    notes: str | None,
    company_slug: str | None,
    form_data: dict[str, Any] | None,
    licenses: list[dict[str, Any]] | None = None,
    submission_id: str | None = None,
    storage_location: str | None = None,
    test_mode: bool = False,
) -> dict[str, Any]:
    """Insert one row into the Approved or Declined tab. Always returns a status dict."""

    settings = _sheets_settings()
    if not settings["service_account_json"] or not settings["spreadsheet_id"]:
        return {
            "status": "disabled",
            "message": "Sheets export not configured (missing GOOGLE_SERVICE_ACCOUNT_JSON or APPLICANTS_SHEET_ID).",
        }

    try:
        tab_name = _decision_tab_name(decision)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    try:
        credentials_info = json.loads(settings["service_account_json"])
    except json.JSONDecodeError as exc:
        logger.warning("Invalid GOOGLE_SERVICE_ACCOUNT_JSON: %s", exc)
        return {
            "status": "error",
            "message": f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}",
        }

    applicant_row = build_submission_row(
        form_data=form_data,
        licenses=licenses,
        submission_id=submission_id,
        storage_location=storage_location,
        test_mode=test_mode,
        company_slug=company_slug,
    )
    profile = (
        COMPANY_PROFILES.get((company_slug or "").strip().lower())
        or COMPANY_PROFILES.get(DEFAULT_COMPANY_SLUG)
    )
    company_label = profile.name if profile else (company_slug or "")
    prefix_values = [
        tab_name,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        (decided_by or "").strip(),
        company_label,
        (notes or "").strip(),
    ]
    row = prefix_values + applicant_row

    try:
        worksheet = _open_worksheet(
            settings["spreadsheet_id"], tab_name, credentials_info
        )
        _ensure_header(worksheet, DECISION_SHEET_COLUMNS)
        worksheet.insert_row(row, index=2, value_input_option="USER_ENTERED")
        return {
            "status": "appended",
            "message": f"Row added to '{tab_name}' tab.",
            "tab": tab_name,
        }
    except Exception as exc:  # noqa: BLE001 - never let Sheets break the dashboard
        logger.warning("Sheets decision append failed for '%s': %s", tab_name, exc)
        return {
            "status": "error",
            "message": f"Sheets append failed: {exc}",
            "tab": tab_name,
        }


def append_decision_from_payload(
    payload: dict[str, Any],
    *,
    decision: str,
    decided_by: str | None = None,
    notes: str | None = None,
    storage_location: str | None = None,
    company_slug_override: str | None = None,
) -> dict[str, Any]:
    """Wrapper that pulls company/licenses out of a saved submission payload."""

    payload = payload or {}
    form_data = payload.get("form_data") or {}
    company_slug = (
        company_slug_override
        or form_data.get("company_slug")
        or DEFAULT_COMPANY_SLUG
    )
    submission_id = payload.get("submission_key") or payload.get("submission_id")
    test_mode = bool(form_data.get("test_mode"))

    return append_decision_row(
        decision=decision,
        decided_by=decided_by,
        notes=notes,
        company_slug=company_slug,
        form_data=form_data,
        licenses=payload.get("licenses") or [],
        submission_id=submission_id,
        storage_location=storage_location,
        test_mode=test_mode,
    )
