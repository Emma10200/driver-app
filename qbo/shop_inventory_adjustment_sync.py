"""QBO -> Supabase delta sync for shop inventory adjustment history.

Caches QBO InventoryAdjustment transactions so each part detail page can show
quantity corrections (shrinkage, recounts, damage, etc.) alongside bought and
sold history without querying QuickBooks during normal page views.

This module is read-only: it never creates or edits QuickBooks transactions.
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
_CACHE_TABLE = "shop_inventory_adjustment_cache"
_STATE_TABLE = "shop_inventory_adjustment_sync_state"
_ENTITY = "InventoryAdjustment"


@dataclass(slots=True)
class InventoryAdjustmentSyncResult:
    realm_id: str = ""
    mode: str = ""  # full | delta
    adjustments_fetched: int = 0
    adjustments_upserted: int = 0
    new_cursor: str = ""
    status: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)


def sync_shop_inventory_adjustments(
    realm_id: str | None = None,
    *,
    force_full: bool = False,
) -> InventoryAdjustmentSyncResult:
    """Sync QBO InventoryAdjustment entities into Supabase cache."""
    result = InventoryAdjustmentSyncResult()
    try:
        qbo_client, token_repo, supabase = build_services()
        result.realm_id = realm_id or resolve_shop_realm_id(token_repo)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Service initialisation failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Inventory adjustment sync could not initialise")
        return result

    try:
        state = _load_state(supabase, result.realm_id)
    except Exception as exc:  # noqa: BLE001
        result.status = "skipped"
        result.message = f"Inventory adjustment cache is not ready. Run migration 0011 first: {exc}"
        result.errors.append(str(exc))
        logger.warning("Inventory adjustment sync skipped: %s", exc)
        return result
    cursor = "" if force_full else str(state.get("last_qbo_updated_at") or "")
    result.mode = "full" if not cursor else "delta"

    try:
        docs = _fetch_adjustments(qbo_client, result.realm_id, cursor=cursor)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"QBO inventory adjustment query failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Inventory adjustment sync query failed")
        _save_state(supabase, result.realm_id, cursor, "failed", result.message, 0)
        return result

    result.adjustments_fetched = len(docs)
    rows = [_map_adjustment(result.realm_id, doc) for doc in docs if doc.get("Id")]
    new_cursor = _max_cursor(cursor, rows)

    try:
        upserted = _upsert_rows(supabase, rows)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Supabase upsert failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Inventory adjustment upsert failed")
        _save_state(supabase, result.realm_id, cursor, "failed", result.message, 0)
        return result

    result.adjustments_upserted = upserted
    result.new_cursor = new_cursor
    result.status = "success"
    result.message = f"{result.mode} sync: fetched {result.adjustments_fetched}, upserted {upserted}."
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


def _fetch_adjustments(qbo_client: QboClient, realm_id: str, *, cursor: str) -> list[dict[str, Any]]:
    where = f"Metadata.LastUpdatedTime > '{_escape_literal(cursor)}'" if cursor else ""
    return _paginated_query(qbo_client, realm_id, where)


def _paginated_query(qbo_client: QboClient, realm_id: str, where: str) -> list[dict[str, Any]]:
    start = 1
    out: list[dict[str, Any]] = []
    while True:
        clause = f" WHERE {where}" if where else ""
        sql = f"SELECT * FROM {_ENTITY}{clause} STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
        response = _query_with_retry(qbo_client, sql, realm_id)
        rows = (response.get("QueryResponse") or {}).get(_ENTITY) or []
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


def _map_adjustment(realm_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    metadata = doc.get("MetaData") or {}
    return {
        "realm_id": realm_id,
        "qbo_adjustment_id": str(doc.get("Id") or ""),
        "doc_number": str(doc.get("DocNumber") or doc.get("ReferenceNumber") or ""),
        "txn_date": str(doc.get("TxnDate") or doc.get("AdjustmentDate") or "") or None,
        "adjust_account_id": _ref_value(doc.get("AdjustAccountRef") or doc.get("AdjustmentAccountRef")),
        "adjust_account_name": _ref_name(doc.get("AdjustAccountRef") or doc.get("AdjustmentAccountRef")),
        "reason": str(doc.get("Reason") or ""),
        "private_note": str(doc.get("PrivateNote") or ""),
        "line_items": _line_items(doc),
        "qbo_last_updated_at": str(metadata.get("LastUpdatedTime") or "") or None,
        "qbo_created_at": str(metadata.get("CreateTime") or "") or None,
        "last_synced": datetime.now(UTC).isoformat(),
        "raw": doc,
    }


def _line_items(doc: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in doc.get("Line") or []:
        detail = line.get("ItemAdjustmentLineDetail") or {}
        item_ref = detail.get("ItemRef") if isinstance(detail, dict) else None
        out.append(
            {
                "line_id": str(line.get("Id") or ""),
                "detail_type": str(line.get("DetailType") or ""),
                "item_id": _ref_value(item_ref),
                "item_name": _ref_name(item_ref),
                "qty_diff": _as_number(detail.get("QtyDiff")) if isinstance(detail, dict) else None,
                "description": str(line.get("Description") or ""),
            }
        )
    return out


def _ref_name(ref: Any) -> str:
    return str(ref.get("name") or "").strip() if isinstance(ref, dict) else ""


def _ref_value(ref: Any) -> str:
    return str(ref.get("value") or "").strip() if isinstance(ref, dict) else ""


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
        select="realm_id,last_qbo_updated_at,last_run_at,last_run_status,adjustments_upserted",
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
    adjustments_upserted: int,
    *,
    full_completed: bool = False,
) -> None:
    row: dict[str, Any] = {
        "realm_id": realm_id,
        "last_qbo_updated_at": cursor or None,
        "last_run_at": datetime.now(UTC).isoformat(),
        "last_run_status": status,
        "last_run_message": message[:1000],
        "adjustments_upserted": int(adjustments_upserted),
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
        supabase.upsert(_CACHE_TABLE, chunk, on_conflict="realm_id,qbo_adjustment_id")
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
