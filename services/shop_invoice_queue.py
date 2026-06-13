"""Supabase access for the shop invoice review queue.

The shop app does not post invoices to QuickBooks. Instead, "Finish invoice"
writes a pending draft here for accounting to review (and later import). This
keeps financial writes under accounting control while letting the shop manager
assemble invoices quickly.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from services.qbo_supabase import SupabaseRestClient

logger = logging.getLogger(__name__)

_TABLE = "shop_invoice_queue"


def submit_invoice_draft(
    *,
    realm_id: str,
    proposed_doc_number: str,
    customer_name: str,
    truck_unit: str,
    vin: str,
    miles: str,
    notes: str,
    line_items: list[dict[str, Any]],
    total: float,
    submitted_by: str,
) -> dict[str, Any]:
    """Insert a pending invoice draft for accounting review.

    Returns the inserted row. Raises on failure so the caller can surface it.
    """
    supabase = SupabaseRestClient()
    row = {
        "realm_id": realm_id,
        "proposed_doc_number": proposed_doc_number or "",
        "status": "pending",
        "customer_name": customer_name or "",
        "truck_unit": truck_unit or "",
        "vin": vin or "",
        "miles": miles or "",
        "notes": notes or "",
        "line_items": line_items,
        "total": round(float(total or 0), 2),
        "submitted_by": submitted_by or "",
        "shop_locked_at": datetime.now(UTC).isoformat(),
        "last_shop_edit_at": datetime.now(UTC).isoformat(),
    }
    inserted = supabase.insert(_TABLE, row)
    return inserted[0] if inserted else row


def list_recent_drafts(realm_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Return recent queued drafts for this realm, newest first."""
    try:
        supabase = SupabaseRestClient()
        return supabase.select(
            _TABLE,
            select="id,proposed_doc_number,status,customer_name,truck_unit,total,submitted_by,created_at",
            filters={"realm_id": f"eq.{realm_id}"},
            order="created_at.desc",
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - read-only convenience; never crash UI
        logger.warning("Could not list shop invoice drafts: %s", exc)
        return []
