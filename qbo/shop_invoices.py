"""Read-only QBO invoice access for the General Truck Service shop app.

Consumed by the mobile "Invoice History" view and (later) the "New Invoice"
auto-numbering. This module never writes to QBO - it only queries.

Per the project's separation of concerns, all QBO API access lives in the
``qbo`` layer; the Streamlit page calls these helpers and renders the result.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from qbo.api_client import QboClient

logger = logging.getLogger(__name__)

_MAX_INVOICE_FETCH = 100


def fetch_recent_invoices(
    qbo_client: QboClient, realm_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Return the most recent invoices for ``realm_id``, newest first.

    Tries QBO's ``ORDERBY TxnDate DESC`` first; if that query shape is rejected,
    falls back to an unordered page and sorts client-side. Returns an empty list
    on any failure so the UI can show a friendly empty state.
    """
    capped = max(1, min(int(limit), _MAX_INVOICE_FETCH))
    rows = _query_invoices(
        qbo_client,
        realm_id,
        f"SELECT * FROM Invoice ORDERBY TxnDate DESC MAXRESULTS {capped}",
    )
    if rows is None:
        # Fallback: unordered fetch, then sort newest-first in Python.
        rows = _query_invoices(
            qbo_client,
            realm_id,
            f"SELECT * FROM Invoice MAXRESULTS {capped}",
        ) or []
        rows.sort(key=lambda inv: str(inv.get("TxnDate") or ""), reverse=True)
    return rows or []


def next_invoice_number(qbo_client: QboClient, realm_id: str) -> int | None:
    """Best-effort next sequential invoice number.

    Scans recent invoices, takes the highest purely numeric ``DocNumber`` and
    adds one. Returns ``None`` when no numeric document numbers are found. This
    is a teaser/helper for now; the authoritative next-number logic will live in
    the New Invoice flow when it is built.
    """
    rows = fetch_recent_invoices(qbo_client, realm_id, limit=_MAX_INVOICE_FETCH)
    highest = 0
    for inv in rows:
        doc = str(inv.get("DocNumber") or "").strip()
        match = re.search(r"\d+", doc)
        if match:
            highest = max(highest, int(match.group(0)))
    return highest + 1 if highest else None


def fetch_invoice_by_id(
    qbo_client: QboClient, realm_id: str, invoice_id: str
) -> dict[str, Any] | None:
    """Return a single full invoice (with line items + custom fields) by Id.

    Returns ``None`` if not found or on error.
    """
    invoice_id = str(invoice_id or "").strip()
    if not invoice_id:
        return None
    try:
        response = qbo_client.get(f"/invoice/{invoice_id}", realm_id=realm_id).json()
    except Exception as exc:  # noqa: BLE001 - caller shows a friendly error
        logger.warning("Invoice fetch by id failed (%s): %s", invoice_id, exc)
        return None
    return response.get("Invoice")


def custom_field_map(inv: dict[str, Any]) -> dict[str, str]:
    """Return a {lowercased field name: value} map of an invoice's CustomFields.

    QBO custom fields arrive as a ``CustomField`` list of
    ``{"Name": "Unit", "StringValue": "457", ...}``. We key by lowercased name so
    callers can pull "unit" / "vin" / "miles" regardless of QBO casing.
    """
    out: dict[str, str] = {}
    for field in inv.get("CustomField") or []:
        if not isinstance(field, dict):
            continue
        raw_name = str(field.get("Name") or "").strip()
        name = _canonical_custom_field_name(raw_name)
        value = str(
            field.get("StringValue")
            or field.get("NumberValue")
            or field.get("DateValue")
            or ""
        ).strip()
        if name:
            out[name] = value
    return out


def custom_field_items(inv: dict[str, Any]) -> list[tuple[str, str]]:
    """Return all invoice custom fields as (display label, value) pairs.

    This is intentionally name-agnostic for the shop UI. General Truck Service's
    invoice custom fields are the only ones in the company file, so rendering all
    non-empty custom fields is more reliable than exact character-for-character
    matching on "Unit" / "VIN" / "Miles".
    """
    out: list[tuple[str, str]] = []
    for field in inv.get("CustomField") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("Name") or "").strip()
        value = str(
            field.get("StringValue")
            or field.get("NumberValue")
            or field.get("DateValue")
            or ""
        ).strip()
        if name and value:
            out.append((name, value))
    return out


def _canonical_custom_field_name(raw_name: str) -> str:
    """Map QBO custom field labels to canonical keys used by the shop UI."""
    normalized = re.sub(r"[^a-z0-9]+", "", str(raw_name or "").lower())
    if normalized in {"unit", "unitno", "unitnumber", "truck", "truckunit"}:
        return "unit"
    if normalized in {"vin", "vinnumber"}:
        return "vin"
    if normalized in {"mile", "miles", "mileage", "odometer", "odom"}:
        return "miles"
    return normalized


def _query_invoices(
    qbo_client: QboClient, realm_id: str, sql: str
) -> list[dict[str, Any]] | None:
    """Run an invoice query. Returns rows, or ``None`` if the query was rejected."""
    try:
        response = qbo_client.query(sql, realm_id=realm_id)
    except Exception as exc:  # noqa: BLE001 - caller decides on fallback / empty UI
        logger.warning("Invoice query failed for realm %s: %s", realm_id, exc)
        return None
    return (response.get("QueryResponse") or {}).get("Invoice") or []
