"""QBO -> Supabase delta sync for shop customer cache."""

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
_UPSERT_CHUNK = 500
_MAX_RATE_LIMIT_RETRIES = 3
_CACHE_TABLE = "shop_customer_cache"
_STATE_TABLE = "shop_customer_sync_state"


@dataclass(slots=True)
class CustomerSyncResult:
    realm_id: str = ""
    mode: str = ""
    customers_fetched: int = 0
    customers_upserted: int = 0
    new_cursor: str = ""
    status: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)


def sync_shop_customers(realm_id: str | None = None, *, force_full: bool = False) -> CustomerSyncResult:
    result = CustomerSyncResult()
    try:
        qbo_client, token_repo, supabase = build_services()
        result.realm_id = realm_id or resolve_shop_realm_id(token_repo)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Service initialisation failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Customer sync could not initialise")
        return result

    state = _load_state(supabase, result.realm_id)
    cursor = "" if force_full else str(state.get("last_qbo_updated_at") or "")
    result.mode = "full" if not cursor else "delta"

    try:
        customers = _fetch_customers(qbo_client, result.realm_id, cursor=cursor)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"QBO customer query failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Customer sync query failed")
        _save_state(supabase, result.realm_id, cursor, "failed", result.message, 0)
        return result

    result.customers_fetched = len(customers)
    rows = [_map_customer(result.realm_id, row) for row in customers if row.get("Id")]
    new_cursor = _max_cursor(cursor, rows)
    try:
        upserted = _upsert_rows(supabase, rows)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Supabase upsert failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Customer cache upsert failed")
        _save_state(supabase, result.realm_id, cursor, "failed", result.message, 0)
        return result

    result.customers_upserted = upserted
    result.new_cursor = new_cursor
    result.status = "success"
    result.message = f"{result.mode} sync: fetched {result.customers_fetched}, upserted {upserted}."
    _save_state(supabase, result.realm_id, new_cursor, "success", result.message, upserted, full_completed=(result.mode == "full"))
    return result


def _fetch_customers(qbo_client: QboClient, realm_id: str, *, cursor: str) -> list[dict[str, Any]]:
    if not cursor:
        return _paginated_query(qbo_client, realm_id, "Active = true")
    safe_cursor = _escape_literal(cursor)
    rows = _paginated_query(qbo_client, realm_id, f"Metadata.LastUpdatedTime > '{safe_cursor}'")
    inactive = _paginated_query(qbo_client, realm_id, f"Active = false AND Metadata.LastUpdatedTime > '{safe_cursor}'")
    by_id: dict[str, dict[str, Any]] = {}
    for row in (*rows, *inactive):
        row_id = str(row.get("Id") or "")
        if row_id:
            by_id[row_id] = row
    return list(by_id.values())


def _paginated_query(qbo_client: QboClient, realm_id: str, where: str) -> list[dict[str, Any]]:
    start = 1
    out: list[dict[str, Any]] = []
    while True:
        clause = f" WHERE {where}" if where else ""
        sql = f"SELECT * FROM Customer{clause} STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
        response = _query_with_retry(qbo_client, sql, realm_id)
        rows = (response.get("QueryResponse") or {}).get("Customer") or []
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


def _map_customer(realm_id: str, row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("MetaData") or {}
    return {
        "realm_id": realm_id,
        "qbo_customer_id": str(row.get("Id") or ""),
        "display_name": str(row.get("DisplayName") or row.get("FullyQualifiedName") or row.get("CompanyName") or ""),
        "fully_qualified_name": str(row.get("FullyQualifiedName") or ""),
        "company_name": str(row.get("CompanyName") or ""),
        "active": bool(row.get("Active", True)),
        "qbo_last_updated_at": str(metadata.get("LastUpdatedTime") or "") or None,
        "qbo_created_at": str(metadata.get("CreateTime") or "") or None,
        "last_synced": datetime.now(UTC).isoformat(),
        "raw": row,
    }


def _load_state(supabase: SupabaseRestClient, realm_id: str) -> dict[str, Any]:
    rows = supabase.select(
        _STATE_TABLE,
        select="realm_id,last_qbo_updated_at,last_run_at,last_run_status,customers_upserted",
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
    customers_upserted: int,
    *,
    full_completed: bool = False,
) -> None:
    row: dict[str, Any] = {
        "realm_id": realm_id,
        "last_qbo_updated_at": cursor or None,
        "last_run_at": datetime.now(UTC).isoformat(),
        "last_run_status": status,
        "last_run_message": message[:1000],
        "customers_upserted": int(customers_upserted),
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
        supabase.upsert(_CACHE_TABLE, chunk, on_conflict="realm_id,qbo_customer_id")
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
    return str(value).replace("'", "\\'")
