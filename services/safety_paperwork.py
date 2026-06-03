"""Safety Paperwork Portal — pure ingest, join, and review logic.

This module is intentionally framework-free (no Streamlit imports) so it
stays easy to unit test. It loads the four ProTransport exports the staff
upload, joins them, classifies each warning, and produces an
``ImportPreview`` containing one ``RecipientBundle`` per person who needs
to be contacted plus a list of ``ReviewIssue`` rows that should NOT be
auto-emailed (bad data, missing contact, etc.).

Phase 1 deliberately stops at the preview stage — no DB writes, no emails,
no token issuance. Those land in later phases.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd

# ---------------------------------------------------------------------------
# Document scope (header-driven only; no free-text parsing in v1)
# ---------------------------------------------------------------------------

DRIVER_DOC_TYPES: tuple[str, ...] = ("CDL", "MEDICAL_CARD")
TRUCK_DOC_TYPES: tuple[str, ...] = ("DOT_INSPECTION", "INSURANCE", "PLATES", "IFTA")

EXCLUDED_FROM_OUTBOUND: tuple[str, ...] = (
    "MVR Review",
    "Pre-Employment Drug Test",
    "Annual Clearinghouse",
    "Maintenance Overdue",
    "Driver Birthday",
    "HAZMAT (Custom Warning)",
    "Custom Warning (free text)",
)

DOC_TYPE_LABELS: dict[str, str] = {
    "CDL": "Commercial Driver's License (CDL)",
    "MEDICAL_CARD": "DOT Medical Card",
    "DOT_INSPECTION": "Annual DOT Inspection",
    "INSURANCE": "Insurance Certificate",
    "PLATES": "Plates / Registration",
    "IFTA": "IFTA Sticker",
}

# Status classification thresholds
EXPIRING_SOON_DAYS = 60

# Date sanity bounds — used to reject 12/31/9999 and similar
_MIN_PLAUSIBLE_YEAR = 2000
_MAX_PLAUSIBLE_FUTURE_YEARS = 50


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DriverDetail:
    display_name: str
    full_name: str
    division: str
    email: str
    phone: str
    driver_personal_id: str
    active: bool
    status: str


@dataclass(frozen=True)
class TruckOwnerDetail:
    unit_no: str
    owner_company: str
    owner_email: str
    owner_first: str
    owner_last: str
    division: str
    active: bool


@dataclass(frozen=True)
class WarningItem:
    doc_type: str  # one of DRIVER_DOC_TYPES or TRUCK_DOC_TYPES
    expiration_date: date | None
    status: str  # ok | expiring_soon | expired | missing
    raw_date_text: str
    unit_no: str | None  # set for truck-scoped items, None for driver-scoped


@dataclass
class ReviewIssue:
    severity: str  # "blocker" (will not be sent) | "warning" (sent but flagged)
    category: str
    message: str
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecipientBundle:
    kind: str  # "driver" | "owner" | "driver_owner"
    recipient_key: str
    display_name: str
    division: str
    email: str
    phone: str
    units: list[str] = field(default_factory=list)
    items: list[WarningItem] = field(default_factory=list)


@dataclass
class ImportPreview:
    driver_warning_rows: int
    truck_warning_rows: int
    driver_detail_rows: int
    truck_detail_rows: int
    recipients: list[RecipientBundle]
    review: list[ReviewIssue]
    excluded_doc_types: tuple[str, ...] = EXCLUDED_FROM_OUTBOUND


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_OLD_FILE_RE = re.compile(r"\bOLD\s+FILE\b", re.IGNORECASE)
_NON_ALPHA_RE = re.compile(r"[^A-Z\s]")


def _strip(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.lower() == "nan":
        return ""
    return text.strip()


def _is_old_file(name: str) -> bool:
    return bool(_OLD_FILE_RE.search(name or ""))


def normalize_person_name(raw: str) -> str:
    """Build a stable lookup key for a person's name.

    Handles:
      - "SUPPICICH, JOHN D"   → "john suppicich"
      - "JOHN SUPPICICH"      → "john suppicich"
      - "matthew krezel"      → "matthew krezel"
      - "KIATPHONG  TEERATHAJARUPONG OLD FILE" → "kiatphong teerathajarupong"
      - middle initials dropped so "JOHN D" matches "JOHN"
    """
    text = _strip(raw)
    if not text:
        return ""
    text = _OLD_FILE_RE.sub(" ", text)
    text = text.upper()
    if "," in text:
        last, _, rest = text.partition(",")
        text = f"{rest.strip()} {last.strip()}"
    text = _NON_ALPHA_RE.sub(" ", text)
    tokens = [t for t in text.split() if len(t) > 1]  # drop middle initials
    tokens = sorted(tokens)
    return " ".join(tokens).lower()


def normalize_unit_no(raw: Any) -> str:
    text = _strip(raw)
    if not text:
        return ""
    # ProTransport exports units as floats sometimes ("802.0")
    if text.endswith(".0"):
        text = text[:-2]
    return text.lstrip("0") or text


def parse_checkbox(raw: Any) -> bool:
    return _strip(raw).lower() == "checked"


def parse_warning_date(raw: Any, *, today: date | None = None) -> tuple[date | None, str | None]:
    """Parse an expiration date safely.

    Returns ``(parsed_or_none, error_message_or_none)``. An error is
    returned for unparseable input or implausible dates (e.g. 12/31/9999,
    something more than 50 years in the future, or before 2000).
    """
    today = today or date.today()
    text = _strip(raw)
    if not text:
        return None, None  # absent is not an error here; "missing" handled by caller
    try:
        parsed = pd.to_datetime(text, errors="raise").date()
    except Exception:
        return None, f"Unparseable date: {text!r}"
    if parsed.year < _MIN_PLAUSIBLE_YEAR:
        return None, f"Implausible past date: {parsed.isoformat()}"
    if parsed.year > today.year + _MAX_PLAUSIBLE_FUTURE_YEARS:
        return None, f"Implausible future date: {parsed.isoformat()}"
    return parsed, None


def classify_status(
    expiration: date | None,
    *,
    today: date | None = None,
    expired_flag: bool = False,
    expiring_flag: bool = False,
) -> str:
    today = today or date.today()
    if expiration is None:
        return "missing"
    if expired_flag or expiration < today:
        return "expired"
    if expiring_flag or (expiration - today).days <= EXPIRING_SOON_DAYS:
        return "expiring_soon"
    return "ok"


def is_actionable(status: str) -> bool:
    return status in {"expired", "expiring_soon", "missing"}


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------

def _read_csv(buffer: bytes | str) -> pd.DataFrame:
    if isinstance(buffer, (bytes, bytearray)):
        df = pd.read_csv(io.BytesIO(buffer), dtype=str, keep_default_na=False)
    else:
        df = pd.read_csv(io.StringIO(buffer), dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _read_excel(buffer: bytes | str) -> pd.DataFrame:
    if isinstance(buffer, (bytes, bytearray)):
        df = pd.read_excel(io.BytesIO(buffer))
    else:
        df = pd.read_excel(buffer)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_driver_details(buffer: bytes | str) -> list[DriverDetail]:
    df = _read_excel(buffer)
    out: list[DriverDetail] = []
    for _, row in df.iterrows():
        display = _strip(row.get("DisplayName"))
        if not display:
            continue
        out.append(
            DriverDetail(
                display_name=display,
                full_name=_strip(row.get("Full Name")),
                division=_strip(row.get("Division")),
                email=_strip(row.get("Email")).lower(),
                phone=_strip(row.get("Cell-Phone")),
                driver_personal_id=_strip(row.get("Driver Personal Id")),
                active=_strip(row.get("Active")) in {"1", "1.0", "True", "true"},
                status=_strip(row.get("Status")),
            )
        )
    return out


def load_truck_owner_details(buffer: bytes | str) -> list[TruckOwnerDetail]:
    df = _read_excel(buffer)
    out: list[TruckOwnerDetail] = []
    for _, row in df.iterrows():
        unit = normalize_unit_no(row.get("Unit #"))
        if not unit:
            continue
        out.append(
            TruckOwnerDetail(
                unit_no=unit,
                owner_company=_strip(row.get("Owner Company")),
                owner_email=_strip(row.get("Owner eMail Address")).lower(),
                owner_first=_strip(row.get("Owner First Name")),
                owner_last=_strip(row.get("Owner Last Name")),
                division=_strip(row.get("Division")),
                active=_strip(row.get("Active")) in {"1", "1.0", "True", "true"},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Build preview
# ---------------------------------------------------------------------------

def _driver_warning_items(
    row: pd.Series, *, today: date
) -> tuple[list[WarningItem], list[str]]:
    """Return (items, parse_errors) for one driver warning row."""
    items: list[WarningItem] = []
    errors: list[str] = []
    spec = (
        ("CDL", "CDL Expiration Date", "CDL Expired", "CDL Expiring Soon"),
        (
            "MEDICAL_CARD",
            "Medical Card Expiration Date",
            "Medical Card Expired",
            "Medical Card Expiring Soon",
        ),
    )
    for doc_type, date_col, expired_col, soon_col in spec:
        raw_date = row.get(date_col, "")
        parsed, err = parse_warning_date(raw_date, today=today)
        if err:
            errors.append(f"{doc_type}: {err}")
        status = classify_status(
            parsed,
            today=today,
            expired_flag=parse_checkbox(row.get(expired_col, "")),
            expiring_flag=parse_checkbox(row.get(soon_col, "")),
        )
        if is_actionable(status):
            items.append(
                WarningItem(
                    doc_type=doc_type,
                    expiration_date=parsed,
                    status=status,
                    raw_date_text=_strip(raw_date),
                    unit_no=None,
                )
            )
    return items, errors


def _truck_warning_items(
    row: pd.Series, *, today: date, unit_no: str
) -> tuple[list[WarningItem], list[str]]:
    items: list[WarningItem] = []
    errors: list[str] = []
    spec = (
        (
            "DOT_INSPECTION",
            "DOT Inspection Expiration Date",
            "DOT Expired",
            "DOT Expiring Soon",
        ),
        (
            "INSURANCE",
            "Insurance Expiration Date",
            "Insurance Expired",
            "Insurance Expiring Soon",
        ),
        ("PLATES", "Plates Expiration Date", "Plates Expired", "Plates Expiring Soon"),
        ("IFTA", "IFTA Expiration Date", None, None),
    )
    for doc_type, date_col, expired_col, soon_col in spec:
        raw_date = row.get(date_col, "")
        parsed, err = parse_warning_date(raw_date, today=today)
        if err:
            errors.append(f"{doc_type}: {err}")
        expired_flag = parse_checkbox(row.get(expired_col, "")) if expired_col else False
        soon_flag = parse_checkbox(row.get(soon_col, "")) if soon_col else False
        status = classify_status(
            parsed,
            today=today,
            expired_flag=expired_flag,
            expiring_flag=soon_flag,
        )
        # IFTA has no flags; if date missing we don't ask (too noisy). Only ask
        # if there's a real date and it's expired/expiring soon.
        if doc_type == "IFTA" and parsed is None:
            continue
        if is_actionable(status):
            items.append(
                WarningItem(
                    doc_type=doc_type,
                    expiration_date=parsed,
                    status=status,
                    raw_date_text=_strip(raw_date),
                    unit_no=unit_no,
                )
            )
    return items, errors


def _index_drivers(details: Iterable[DriverDetail]) -> dict[str, DriverDetail]:
    index: dict[str, DriverDetail] = {}
    for d in details:
        for candidate in {
            normalize_person_name(d.display_name),
            normalize_person_name(d.full_name),
        }:
            if candidate and candidate not in index:
                index[candidate] = d
    return index


def _index_units(details: Iterable[TruckOwnerDetail]) -> dict[str, TruckOwnerDetail]:
    return {d.unit_no: d for d in details if d.unit_no}


def build_preview(
    *,
    driver_warnings_csv: bytes | str,
    truck_warnings_csv: bytes | str,
    driver_details_xls: bytes | str | None = None,
    truck_owner_xls: bytes | str | None = None,
    driver_details: Iterable[DriverDetail] | None = None,
    truck_details: Iterable[TruckOwnerDetail] | None = None,
    today: date | None = None,
) -> ImportPreview:
    """Build an ImportPreview.

    Either pass raw export bytes (``driver_details_xls`` / ``truck_owner_xls``)
    or already-loaded detail lists from the reference DB
    (``driver_details`` / ``truck_details``). At least one source must be
    provided per side.
    """
    today = today or date.today()

    if driver_details is None:
        if driver_details_xls is None:
            raise ValueError("driver_details_xls or driver_details must be provided")
        driver_details = load_driver_details(driver_details_xls)
    else:
        driver_details = list(driver_details)

    if truck_details is None:
        if truck_owner_xls is None:
            raise ValueError("truck_owner_xls or truck_details must be provided")
        truck_details = load_truck_owner_details(truck_owner_xls)
    else:
        truck_details = list(truck_details)

    driver_warnings = _read_csv(driver_warnings_csv)
    truck_warnings = _read_csv(truck_warnings_csv)

    driver_index = _index_drivers(driver_details)
    unit_index = _index_units(truck_details)

    review: list[ReviewIssue] = []
    # recipient_key -> RecipientBundle
    bundles: dict[str, RecipientBundle] = {}

    # ---- Driver warnings ----
    for _, row in driver_warnings.iterrows():
        raw_name = _strip(row.get("Driver"))
        if not raw_name:
            review.append(
                ReviewIssue(
                    severity="blocker",
                    category="driver_missing_name",
                    message="Driver warning row has no Driver name.",
                    source=row.to_dict(),
                )
            )
            continue
        if _is_old_file(raw_name):
            review.append(
                ReviewIssue(
                    severity="blocker",
                    category="driver_old_file",
                    message=f"Skipped 'OLD FILE' driver record: {raw_name}",
                    source={"Driver": raw_name},
                )
            )
            continue

        items, errors = _driver_warning_items(row, today=today)
        for err in errors:
            review.append(
                ReviewIssue(
                    severity="warning",
                    category="driver_bad_date",
                    message=f"{raw_name}: {err}",
                    source={"Driver": raw_name},
                )
            )
        if not items:
            continue  # nothing actionable; not a problem

        key = normalize_person_name(raw_name)
        detail = driver_index.get(key)
        if detail is None:
            review.append(
                ReviewIssue(
                    severity="blocker",
                    category="driver_not_in_details",
                    message=f"No driver detail row matched '{raw_name}'.",
                    source={"Driver": raw_name},
                )
            )
            continue
        if not detail.email:
            review.append(
                ReviewIssue(
                    severity="blocker",
                    category="driver_no_email",
                    message=f"Driver {detail.display_name} has no email in details list.",
                    source={"Driver": raw_name, "DisplayName": detail.display_name},
                )
            )
            continue

        bundle_key = f"driver::{detail.email}"
        bundle = bundles.get(bundle_key)
        if bundle is None:
            bundle = RecipientBundle(
                kind="driver",
                recipient_key=bundle_key,
                display_name=detail.display_name,
                division=detail.division,
                email=detail.email,
                phone=detail.phone,
            )
            bundles[bundle_key] = bundle
        bundle.items.extend(items)

    # ---- Truck warnings ----
    for _, row in truck_warnings.iterrows():
        unit = normalize_unit_no(row.get("Unit #"))
        if not unit:
            review.append(
                ReviewIssue(
                    severity="blocker",
                    category="truck_missing_unit",
                    message="Truck warning row has no Unit #.",
                    source=row.to_dict(),
                )
            )
            continue
        items, errors = _truck_warning_items(row, today=today, unit_no=unit)
        for err in errors:
            review.append(
                ReviewIssue(
                    severity="warning",
                    category="truck_bad_date",
                    message=f"Unit {unit}: {err}",
                    source={"Unit #": unit},
                )
            )
        if not items:
            continue

        detail = unit_index.get(unit)
        if detail is None:
            review.append(
                ReviewIssue(
                    severity="blocker",
                    category="unit_not_in_details",
                    message=f"No truck owner detail row matched Unit {unit}.",
                    source={"Unit #": unit, "Driver 1": _strip(row.get("Driver 1"))},
                )
            )
            continue
        if not detail.owner_email:
            review.append(
                ReviewIssue(
                    severity="blocker",
                    category="unit_no_owner_email",
                    message=f"Unit {unit} ({detail.owner_first} {detail.owner_last}) has no owner email.",
                    source={"Unit #": unit},
                )
            )
            continue

        owner_display = f"{detail.owner_first} {detail.owner_last}".strip() or detail.owner_company
        bundle_key = f"owner::{detail.owner_email}"
        bundle = bundles.get(bundle_key)
        if bundle is None:
            bundle = RecipientBundle(
                kind="owner",
                recipient_key=bundle_key,
                display_name=owner_display,
                division=detail.division,
                email=detail.owner_email,
                phone="",
            )
            bundles[bundle_key] = bundle
        if unit not in bundle.units:
            bundle.units.append(unit)
        bundle.items.extend(items)

    # ---- Merge owner-operators (same email on both sides) ----
    merged: dict[str, RecipientBundle] = {}
    seen_emails: dict[str, str] = {}  # email -> merged key
    for key, bundle in bundles.items():
        email = bundle.email.lower()
        existing_key = seen_emails.get(email)
        if existing_key is None:
            merged[key] = bundle
            seen_emails[email] = key
            continue
        # Merge into existing bundle
        target = merged[existing_key]
        target.kind = "driver_owner"
        target.items.extend(bundle.items)
        for u in bundle.units:
            if u not in target.units:
                target.units.append(u)
        # Prefer driver display name (more personal) when merging
        if bundle.kind == "driver":
            target.display_name = bundle.display_name
            target.phone = bundle.phone or target.phone

    # Sort items inside each bundle for stable display
    for bundle in merged.values():
        bundle.items.sort(key=lambda i: (i.unit_no or "", i.doc_type))
        bundle.units.sort()

    return ImportPreview(
        driver_warning_rows=len(driver_warnings),
        truck_warning_rows=len(truck_warnings),
        driver_detail_rows=len(driver_details),
        truck_detail_rows=len(truck_details),
        recipients=sorted(
            merged.values(),
            key=lambda b: (b.division.lower(), b.display_name.lower()),
        ),
        review=review,
    )
