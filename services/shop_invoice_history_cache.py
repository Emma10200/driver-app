"""Fast Supabase reads for cached shop invoice history."""

from __future__ import annotations

import re
from typing import Any

from services.qbo_supabase import SupabaseRestClient

_TABLE = "shop_invoice_history_cache"
_STATE_TABLE = "shop_invoice_history_sync_state"


def list_cached_invoices(realm_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    if not realm_id:
        return []
    supabase = SupabaseRestClient()
    # Fetch a generous window and sort in Python so invoice numbers like
    # 6498 / 6498a / 6498.0 stay grouped together. PostgREST's simple text sort
    # cannot do this natural-number grouping without an extra SQL function.
    fetch_limit = max(int(limit or 100), 1000)
    rows = supabase.select(
        _TABLE,
        select="realm_id,qbo_invoice_id,doc_number,txn_date,customer_name,total,balance,unit,vin,miles,line_items,qbo_last_updated_at,last_synced,raw",
        filters={"realm_id": f"eq.{realm_id}"},
        order="doc_number.desc,txn_date.desc",
        limit=fetch_limit,
    )
    rows = [row for row in rows if _is_display_invoice_doc(row.get("doc_number"))]
    rows.sort(key=_invoice_sort_key)
    return rows[: max(1, int(limit or 100))]


def _is_display_invoice_doc(value: Any) -> bool:
    """True for normal shop invoice numbers shown to the shop user.

    The QBO file contains some non-invoice accounting artifacts in DocNumber,
    e.g. ``refund check 57804`` or ``check# 55788 & 55868``. Those should stay in
    the cache for audit/history purposes but not clutter the shop-facing history.

    Expected shop invoice shapes:
    - 6483
    - 6498a
    - 6498.0

    This intentionally rejects long/freeform text and suffixes like
    ``6706-REFUND CHECK``.
    """
    doc = str(value or "").strip()
    if not doc or len(doc) > 8:
        return False
    return bool(re.fullmatch(r"\d{4,6}(?:[A-Za-z]|\.\d{1,2})?", doc))


def _invoice_sort_key(row: dict[str, Any]) -> tuple:
    """Natural descending invoice sort with date as secondary.

    Examples that group together:
    - 6498
    - 6498.0
    - 6498a

    The primary key is the first numeric run in the DocNumber, descending. The
    suffix is then sorted alphabetically within that invoice number group, and
    date is only a secondary/tertiary tie-breaker.
    """
    doc = str(row.get("doc_number") or "").strip()
    match = re.search(r"\d+", doc)
    base_number = int(match.group(0)) if match else -1
    suffix = doc[match.end():].lower() if match else doc.lower()
    # Keep the plain number first within the group, then dotted/lettered variants.
    suffix_rank = 0 if suffix == "" else 1
    txn_date = str(row.get("txn_date") or "")
    return (-base_number, suffix_rank, suffix, -_yyyymmdd(txn_date), doc.lower())


def _yyyymmdd(value: str) -> int:
    digits = re.sub(r"\D+", "", str(value or ""))[:8]
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


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