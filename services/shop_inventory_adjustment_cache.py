"""Fast Supabase reads for cached shop inventory adjustments."""

from __future__ import annotations

from typing import Any

from services.qbo_supabase import SupabaseRestClient

_TABLE = "shop_inventory_adjustment_cache"
_STATE_TABLE = "shop_inventory_adjustment_sync_state"


def list_cached_inventory_adjustments(realm_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    if not realm_id:
        return []
    supabase = SupabaseRestClient()
    rows = supabase.select_all(
        _TABLE,
        select="realm_id,qbo_adjustment_id,doc_number,txn_date,adjust_account_id,adjust_account_name,reason,private_note,line_items,qbo_last_updated_at,last_synced,raw",
        filters={"realm_id": f"eq.{realm_id}"},
        order="txn_date.desc,doc_number.desc",
        page_size=1000,
        hard_cap=max(1000, int(limit or 500)),
    )
    rows.sort(key=_adjustment_sort_key)
    return rows[: max(1, int(limit or 500))]


def last_inventory_adjustment_sync(realm_id: str) -> str:
    if not realm_id:
        return ""
    supabase = SupabaseRestClient()
    rows = supabase.select(
        _STATE_TABLE,
        select="last_run_at,last_run_status,last_run_message,adjustments_upserted",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    return str((rows[0] if rows else {}).get("last_run_at") or "")


def _adjustment_sort_key(row: dict[str, Any]) -> tuple:
    date = str(row.get("txn_date") or "")
    doc = str(row.get("doc_number") or row.get("qbo_adjustment_id") or "")
    return (-_yyyymmdd(date), doc.lower())


def _yyyymmdd(value: str) -> int:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())[:8]
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0
