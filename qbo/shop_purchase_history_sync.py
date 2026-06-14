"""QBO -> Supabase delta sync for shop purchase history.

Caches purchase-side item transactions for General Truck Service so part detail
and Purchase History views can read Supabase instead of calling QBO live.

QBO entities covered:
- Purchase: expenses/checks/credit-card purchases
- Bill: vendor bills
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from qbo.api_client import QboClient, QboRateLimitError
from qbo.shop_inventory_sync import build_services, resolve_shop_realm_id
from services.qbo_supabase import SupabaseRestClient

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000
_UPSERT_CHUNK = 250
_MAX_RATE_LIMIT_RETRIES = 3
_CACHE_TABLE = "shop_purchase_history_cache"
_STATE_TABLE = "shop_purchase_history_sync_state"
_ENTITIES = ("Purchase", "Bill")


@dataclass(slots=True)
class PurchaseHistorySyncResult:
    realm_id: str = ""
    mode: str = ""  # full | delta
    purchases_fetched: int = 0
    purchases_upserted: int = 0
    new_cursor: str = ""
    status: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)


def sync_shop_purchase_history(
    realm_id: str | None = None,
    *,
    force_full: bool = False,
) -> PurchaseHistorySyncResult:
    """Sync QBO Purchase + Bill entities into Supabase cache."""
    result = PurchaseHistorySyncResult()
    try:
        qbo_client, token_repo, supabase = build_services()
        result.realm_id = realm_id or resolve_shop_realm_id(token_repo)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Service initialisation failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Purchase history sync could not initialise")
        return result

    state = _load_state(supabase, result.realm_id)
    cursor = "" if force_full else str(state.get("last_qbo_updated_at") or "")
    result.mode = "full" if not cursor else "delta"

    try:
        docs = _fetch_purchase_docs(qbo_client, result.realm_id, cursor=cursor)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"QBO purchase query failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Purchase history sync query failed")
        _save_state(supabase, result.realm_id, cursor, "failed", result.message, 0)
        return result

    result.purchases_fetched = len(docs)
    rows = [
        _map_purchase_doc(result.realm_id, entity, doc)
        for entity, doc in docs
        if doc.get("Id")
    ]
    new_cursor = _max_cursor(cursor, rows)

    try:
        upserted = _upsert_rows(supabase, rows)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Supabase upsert failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Purchase history upsert failed")
        _save_state(supabase, result.realm_id, cursor, "failed", result.message, 0)
        return result

    result.purchases_upserted = upserted
    result.new_cursor = new_cursor
    result.status = "success"
    result.message = f"{result.mode} sync: fetched {result.purchases_fetched}, upserted {upserted}."
    _save_state(
        supabase,
        result.realm_id,
        new_cursor,
        "success",
        result.message,
        upserted,
        full_completed=(result.mode == "full"),
    )
    return result


def _fetch_purchase_docs(qbo_client: QboClient, realm_id: str, *, cursor: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    where = f"Metadata.LastUpdatedTime > '{_escape_literal(cursor)}'" if cursor else ""
    for entity in _ENTITIES:
        for row in _paginated_query(qbo_client, realm_id, entity, where):
            out.append((entity, row))
    return out


def _paginated_query(qbo_client: QboClient, realm_id: str, entity: str, where: str) -> list[dict[str, Any]]:
    start = 1
    out: list[dict[str, Any]] = []
    while True:
        clause = f" WHERE {where}" if where else ""
        sql = f"SELECT * FROM {entity}{clause} STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
        response = _query_with_retry(qbo_client, sql, realm_id)
        rows = (response.get("QueryResponse") or {}).get(entity) or []
        if not rows:
            break
        out.extend(rows)
        if len(rows) < _PAGE_SIZE:
            break
        start += _PAGE_SIZE
    return out


def _query_with_retry(qbo_client: QboClient, sql: str, realm_id: str) -> dict[str, Any]:
    attempt = 0
    while True:
        try:
            return qbo_client.query(sql, realm_id=realm_id)
        except QboRateLimitError as exc:
            attempt += 1
            if attempt > _MAX_RATE_LIMIT_RETRIES:
                raise
            time.sleep(exc.retry_after_seconds or 60)


def _map_purchase_doc(realm_id: str, entity: str, doc: dict[str, Any]) -> dict[str, Any]:
    metadata = doc.get("MetaData") or {}
    return {
        "realm_id": realm_id,
        "qbo_txn_type": entity,
        "qbo_txn_id": str(doc.get("Id") or ""),
        "doc_number": str(doc.get("DocNumber") or ""),
        "txn_date": str(doc.get("TxnDate") or "") or None,
        "vendor_name": _vendor_name(doc),
        "payment_type": str(doc.get("PaymentType") or ""),
        "total": _as_number(doc.get("TotalAmt")) or 0,
        "line_items": _line_items(doc),
        "qbo_last_updated_at": str(metadata.get("LastUpdatedTime") or "") or None,
        "qbo_created_at": str(metadata.get("CreateTime") or "") or None,
        "last_synced": datetime.now(UTC).isoformat(),
        "raw": doc,
    }


def _line_items(doc: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in doc.get("Line") or []:
        detail_type = str(line.get("DetailType") or "")
        item_detail = line.get("ItemBasedExpenseLineDetail") or {}
        account_detail = line.get("AccountBasedExpenseLineDetail") or {}
        item_ref = item_detail.get("ItemRef") if isinstance(item_detail, dict) else None
        account_ref = account_detail.get("AccountRef") if isinstance(account_detail, dict) else None
        out.append(
            {
                "line_id": str(line.get("Id") or ""),
                "detail_type": detail_type,
                "item_id": str(item_ref.get("value") or "") if isinstance(item_ref, dict) else "",
                "item_name": str(item_ref.get("name") or "") if isinstance(item_ref, dict) else "",
                "account_id": str(account_ref.get("value") or "") if isinstance(account_ref, dict) else "",
                "account_name": str(account_ref.get("name") or "") if isinstance(account_ref, dict) else "",
                "description": str(line.get("Description") or ""),
                "qty": _as_number(item_detail.get("Qty")) if isinstance(item_detail, dict) else None,
                "unit_price": _as_number(item_detail.get("UnitPrice")) if isinstance(item_detail, dict) else None,
                "amount": _as_number(line.get("Amount")),
            }
        )
    return out


def _vendor_name(doc: dict[str, Any]) -> str:
    for key in ("EntityRef", "VendorRef"):
        ref = doc.get(key)
        if isinstance(ref, dict) and ref.get("name"):
            return str(ref.get("name") or "")
    return ""


def _as_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_state(supabase: SupabaseRestClient, realm_id: str) -> dict[str, Any]:
    rows = supabase.select(
        _STATE_TABLE,
        select="realm_id,last_qbo_updated_at,last_run_at,last_run_status,purchases_upserted",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    return rows[0] if rows else {}


def _save_state(
    supabase: SupabaseRestClient,
    realm_id: str,
    cursor: str,
    status: str,
    message: str,
    purchases_upserted: int,
    *,
    full_completed: bool = False,
) -> None:
    row: dict[str, Any] = {
        "realm_id": realm_id,
        "last_qbo_updated_at": cursor or None,
        "last_run_at": datetime.now(UTC).isoformat(),
        "last_run_status": status,
        "last_run_message": message[:1000],
        "purchases_upserted": int(purchases_upserted),
    }
    if full_completed:
        row["full_sync_completed_at"] = datetime.now(UTC).isoformat()
    supabase.upsert(_STATE_TABLE, row, on_conflict="realm_id")


def _upsert_rows(supabase: SupabaseRestClient, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    count = 0
    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        supabase.upsert(_CACHE_TABLE, chunk, on_conflict="realm_id,qbo_txn_type,qbo_txn_id")
        count += len(chunk)
    return count


def _max_cursor(current: str, rows: list[dict[str, Any]]) -> str:
    best = current
    best_dt = _parse_dt(current)
    for row in rows:
        candidate = str(row.get("qbo_last_updated_at") or "")
        candidate_dt = _parse_dt(candidate)
        if candidate_dt is not None and (best_dt is None or candidate_dt > best_dt):
            best, best_dt = candidate, candidate_dt
    return best


def _parse_dt(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _escape_literal(value: str) -> str:
    return str(value or "").replace("'", "\\'")
