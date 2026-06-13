"""Fast Supabase reads for cached shop invoice history."""

from __future__ import annotations

from typing import Any

from services.qbo_supabase import SupabaseRestClient

_TABLE = "shop_invoice_history_cache"
_STATE_TABLE = "shop_invoice_history_sync_state"


def list_cached_invoices(realm_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    if not realm_id:
        return []
    supabase = SupabaseRestClient()
    return supabase.select(
        _TABLE,
        select="realm_id,qbo_invoice_id,doc_number,txn_date,customer_name,total,balance,unit,vin,miles,line_items,qbo_last_updated_at,last_synced,raw",
        filters={"realm_id": f"eq.{realm_id}"},
        order="txn_date.desc,doc_number.desc",
        limit=limit,
    )


def get_cached_invoice(realm_id: str, invoice_id: str) -> dict[str, Any] | None:
    if not realm_id or not invoice_id:
        return None
    supabase = SupabaseRestClient()
    rows = supabase.select(
        _TABLE,
        select="*",
        filters={"realm_id": f"eq.{realm_id}", "qbo_invoice_id": f"eq.{invoice_id}"},
        limit=1,
    )
    return rows[0] if rows else None


def last_invoice_history_sync(realm_id: str) -> str:
    if not realm_id:
        return ""
    supabase = SupabaseRestClient()
    rows = supabase.select(
        _STATE_TABLE,
        select="last_run_at,last_run_status,last_run_message,invoices_upserted",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    return str((rows[0] if rows else {}).get("last_run_at") or "")