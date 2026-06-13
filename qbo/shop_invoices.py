"""Read-only QBO invoice access for the General Truck Service shop app.

Consumed by the mobile "Invoice History" view and (later) the "New Invoice"
auto-numbering. This module never writes to QBO - it only queries.

Per the project's separation of concerns, all QBO API access lives in the
``qbo`` layer; the Streamlit page calls these helpers and renders the result.
"""

from __future__ import annotations

import logging
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
        if doc.isdigit():
            highest = max(highest, int(doc))
    return highest + 1 if highest else None


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
