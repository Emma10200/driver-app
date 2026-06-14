"""Fast Supabase reads for cached QBO shop customers."""

from __future__ import annotations

from typing import Any

from services.qbo_supabase import SupabaseRestClient

_TABLE = "shop_customer_cache"
_STATE_TABLE = "shop_customer_sync_state"


def search_customers(realm_id: str, term: str = "", *, limit: int = 25) -> list[dict[str, Any]]:
    if not realm_id:
        return []
    supabase = SupabaseRestClient()
    payload = supabase.rpc(
        "shop_customer_search",
        {
            "p_realm_id": realm_id,
            "p_term": term or "",
            "p_limit": limit,
            "p_active_only": True,
        },
    )
    return payload if isinstance(payload, list) else []


def customer_names(realm_id: str, term: str = "", *, limit: int = 25) -> list[str]:
    """Return cached customer names.

    For blank term, return a large alphabetized list so Streamlit's selectbox can
    do client-side type-to-filter like QuickBooks. For nonblank term, use the
    SQL search RPC.
    """
    names: list[str] = []
    if not realm_id:
        return names

    if not str(term or "").strip():
        supabase = SupabaseRestClient()
        rows = supabase.select_all(
            _TABLE,
            select="display_name,fully_qualified_name,company_name",
            filters={"realm_id": f"eq.{realm_id}", "active": "eq.true"},
            order="display_name.asc",
        )
    else:
        rows = search_customers(realm_id, term, limit=limit)

    for row in rows:
        name = str(row.get("display_name") or row.get("fully_qualified_name") or row.get("company_name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def last_customer_sync(realm_id: str) -> str:
    if not realm_id:
        return ""
    supabase = SupabaseRestClient()
    rows = supabase.select(
        _STATE_TABLE,
        select="last_run_at,last_run_status,last_run_message,customers_upserted",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    return str((rows[0] if rows else {}).get("last_run_at") or "")
