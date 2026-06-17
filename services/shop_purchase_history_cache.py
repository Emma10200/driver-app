"""Fast Supabase reads for cached shop purchase history."""

from __future__ import annotations

import json
from typing import Any

from services.qbo_supabase import SupabaseRestClient

_TABLE = "shop_purchase_history_cache"
_STATE_TABLE = "shop_purchase_history_sync_state"
_PURCHASE_SELECT = "realm_id,qbo_txn_type,qbo_txn_id,doc_number,txn_date,vendor_name,payment_type,total,line_items,qbo_last_updated_at,last_synced,raw"


def _item_contains_filter(item_id: str) -> str:
    """PostgREST jsonb containment value matching a flattened line item_id."""
    return "cs." + json.dumps([{"item_id": str(item_id)}], separators=(",", ":"))


def list_purchases_with_item(realm_id: str, item_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
    """Cached purchases/bills whose line_items contain ``item_id`` (server filtered).

    Uses the GIN index on line_items so part Bought history fetches only the few
    relevant purchase documents instead of the whole history.
    """
    if not realm_id or not str(item_id or "").strip():
        return []
    supabase = SupabaseRestClient()
    rows = supabase.select_all(
        _TABLE,
        select=_PURCHASE_SELECT,
        filters={
            "realm_id": f"eq.{realm_id}",
            "line_items": _item_contains_filter(item_id),
        },
        order="txn_date.desc,doc_number.desc",
        page_size=1000,
        hard_cap=max(1000, int(limit or 2000)),
    )
    rows.sort(key=_purchase_sort_key)
    return rows[: max(1, int(limit or 2000))]


def list_cached_purchases(realm_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    if not realm_id:
        return []
    supabase = SupabaseRestClient()
    rows = supabase.select_all(
        _TABLE,
        select="realm_id,qbo_txn_type,qbo_txn_id,doc_number,txn_date,vendor_name,payment_type,total,line_items,qbo_last_updated_at,last_synced,raw",
        filters={"realm_id": f"eq.{realm_id}"},
        order="txn_date.desc,doc_number.desc",
        page_size=1000,
        hard_cap=max(1000, int(limit or 500)),
    )
    rows.sort(key=_purchase_sort_key)
    return rows[: max(1, int(limit or 500))]


def get_cached_purchase(realm_id: str, txn_id: str) -> dict[str, Any] | None:
    """Fetch a single cached purchase/bill document by its QBO transaction id."""
    if not realm_id or not txn_id:
        return None
    supabase = SupabaseRestClient()
    rows = supabase.select(
        _TABLE,
        select="*",
        filters={"realm_id": f"eq.{realm_id}", "qbo_txn_id": f"eq.{txn_id}"},
        limit=1,
    )
    return rows[0] if rows else None


def last_purchase_history_sync(realm_id: str) -> str:
    if not realm_id:
        return ""
    supabase = SupabaseRestClient()
    rows = supabase.select(
        _STATE_TABLE,
        select="last_run_at,last_run_status,last_run_message,purchases_upserted",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    return str((rows[0] if rows else {}).get("last_run_at") or "")


def _purchase_sort_key(row: dict[str, Any]) -> tuple:
    date = str(row.get("txn_date") or "")
    doc = str(row.get("doc_number") or row.get("qbo_txn_id") or "")
    return (-_yyyymmdd(date), doc.lower())


def _yyyymmdd(value: str) -> int:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[:8]
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0
