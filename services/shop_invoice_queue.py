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
    status: str = "pending",
    customer_is_new: bool = False,
) -> dict[str, Any]:
    """Insert a shop invoice draft for accounting review.

    Returns the inserted row. Raises on failure so the caller can surface it.
    """
    supabase = SupabaseRestClient()
    normalized_status = status if status in {"draft", "pending"} else "pending"
    now = datetime.now(UTC).isoformat()
    row = {
        "realm_id": realm_id,
        "proposed_doc_number": proposed_doc_number or "",
        "status": normalized_status,
        "customer_name": customer_name or "",
        "customer_is_new": bool(customer_is_new),
        "truck_unit": truck_unit or "",
        "vin": vin or "",
        "miles": miles or "",
        "notes": notes or "",
        "line_items": line_items,
        "total": round(float(total or 0), 2),
        "submitted_by": submitted_by or "",
        "shop_locked_at": now if normalized_status == "pending" else None,
        "last_shop_edit_at": now,
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


def list_submitted_invoices(realm_id: str, *, limit: int = 25) -> list[dict[str, Any]]:
    """Return finished invoices the shop submitted but accounting has NOT yet posted.

    These are still editable by the shop (status pending/approved/rejected). Once
    accounting posts to QuickBooks the row becomes ``imported`` and is excluded
    here (locked) because it now lives in the real QuickBooks invoice list.
    """
    try:
        supabase = SupabaseRestClient()
        return supabase.select(
            _TABLE,
            select="id,proposed_doc_number,status,customer_name,truck_unit,vin,miles,total,line_items,created_at,updated_at",
            filters={"realm_id": f"eq.{realm_id}", "status": "in.(pending,approved,rejected)"},
            order="created_at.desc",
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - read-only convenience; never crash UI
        logger.warning("Could not list submitted shop invoices: %s", exc)
        return []


def save_invoice_draft(
    *,
    draft_id: str | None,
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
    customer_is_new: bool = False,
) -> dict[str, Any]:
    """Create or update a ``draft`` row and return it (with its id).

    Used for perpetual autosave: the first call inserts a draft, later calls
    patch the same row by id. Drafts never touch QuickBooks.
    """
    supabase = SupabaseRestClient()
    now = datetime.now(UTC).isoformat()
    payload = {
        "realm_id": realm_id,
        "proposed_doc_number": proposed_doc_number or "",
        "status": "draft",
        "customer_name": customer_name or "",
        "customer_is_new": bool(customer_is_new),
        "truck_unit": truck_unit or "",
        "vin": vin or "",
        "miles": miles or "",
        "notes": notes or "",
        "line_items": line_items,
        "total": round(float(total or 0), 2),
        "submitted_by": submitted_by or "",
        "last_shop_edit_at": now,
    }
    if draft_id:
        updated = supabase.patch(_TABLE, payload, filters={"id": f"eq.{draft_id}"})
        if updated:
            return updated[0]
    inserted = supabase.insert(_TABLE, payload)
    return inserted[0] if inserted else {**payload, "id": draft_id or ""}


def finalize_invoice_draft(draft_id: str) -> dict[str, Any]:
    """Mark an existing draft as ``pending`` so accounting will review/import it."""
    supabase = SupabaseRestClient()
    now = datetime.now(UTC).isoformat()
    updated = supabase.patch(
        _TABLE,
        {"status": "pending", "shop_locked_at": now, "last_shop_edit_at": now},
        filters={"id": f"eq.{draft_id}"},
    )
    return updated[0] if updated else {"id": draft_id, "status": "pending"}


def list_drafts(realm_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return open shop drafts (status='draft') for editing/deleting."""
    try:
        supabase = SupabaseRestClient()
        return supabase.select(
            _TABLE,
            select="id,proposed_doc_number,status,customer_name,truck_unit,vin,miles,notes,line_items,total,customer_is_new,updated_at",
            filters={"realm_id": f"eq.{realm_id}", "status": "eq.draft"},
            order="updated_at.desc",
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list shop drafts: %s", exc)
        return []


def get_draft(draft_id: str) -> dict[str, Any] | None:
    if not draft_id:
        return None
    try:
        supabase = SupabaseRestClient()
        rows = supabase.select(
            _TABLE,
            select="*",
            filters={"id": f"eq.{draft_id}"},
            limit=1,
        )
        return rows[0] if rows else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load shop draft %s: %s", draft_id, exc)
        return None


def delete_draft(draft_id: str) -> None:
    if not draft_id:
        return
    try:
        supabase = SupabaseRestClient()
        supabase.delete(_TABLE, filters={"id": f"eq.{draft_id}", "status": "eq.draft"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not delete shop draft %s: %s", draft_id, exc)

