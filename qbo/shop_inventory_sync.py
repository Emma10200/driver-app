"""QBO -> Supabase delta sync for the General Truck Service shop inventory.

Producer side of the mobile "Inventory List". Pulls QBO ``Item`` records for the
shop realm and upserts them into ``public.shop_inventory`` (migration 0004).

Design notes
------------
* **Delta sync** keys off ``Item.MetaData.LastUpdatedTime``. The high-water mark
  is stored in ``shop_inventory_sync_state``; subsequent runs only fetch items
  changed since that cursor, so routine syncs are tiny.
* **Full sync** (first run, or forced) pulls only ``Active = true`` items to stay
  lean (~<2k), per the project directive.
* **Deactivations** are captured on delta runs via a second pass restricted to
  ``Active = false`` so parts that get archived in QBO flip to ``active = false``
  in Supabase and drop out of the shop list.
* Runs headless (GitHub Actions cron) or from a manual call. Secrets resolve from
  env vars via ``get_runtime_secret`` -> ``os.getenv`` fallback.

This module never blocks on business hours; the cron runner owns that gate so
manual/admin runs always work.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from qbo.api_client import QboClient, QboRateLimitError
from qbo.company_directory import CompanyDirectory
from services.qbo_auth import QboAuthService, QboTokenRepository
from services.qbo_supabase import SupabaseRestClient
from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)

_PAGE_SIZE = 1000
_UPSERT_CHUNK = 500
_MAX_RATE_LIMIT_RETRIES = 3
_DEFAULT_SHOP_COMPANY_NAME = "General Truck Service"
_INVENTORY_TABLE = "shop_inventory"
_SYNC_STATE_TABLE = "shop_inventory_sync_state"


@dataclass(slots=True)
class ShopSyncResult:
    realm_id: str = ""
    company_name: str = ""
    mode: str = ""  # 'full' | 'delta'
    items_fetched: int = 0
    items_upserted: int = 0
    new_cursor: str = ""
    status: str = ""  # 'success' | 'partial' | 'failed' | 'skipped'
    message: str = ""
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- wiring
def build_services() -> tuple[QboClient, QboTokenRepository, SupabaseRestClient]:
    """Construct the QBO + Supabase clients used by the sync (headless-safe)."""
    supabase = SupabaseRestClient()
    token_repo = QboTokenRepository(supabase)
    auth_service = QboAuthService(token_repo)
    qbo_client = QboClient(auth_service)
    return qbo_client, token_repo, supabase


def resolve_shop_realm_id(token_repo: QboTokenRepository) -> str:
    """Resolve the shop realm id.

    Priority: explicit ``SHOP_REALM_ID`` env/secret, then a loose name match
    against the connected realms (default company name "General Truck Service").
    """
    explicit = (get_runtime_secret("SHOP_REALM_ID", "") or "").strip()
    if explicit:
        return explicit

    company_name = (
        get_runtime_secret("SHOP_COMPANY_NAME", _DEFAULT_SHOP_COMPANY_NAME)
        or _DEFAULT_SHOP_COMPANY_NAME
    ).strip()
    realms = token_repo.list_realms()
    realm_id = CompanyDirectory(realms).resolve_realm_id_by_name_loose(company_name)
    if realm_id:
        return realm_id

    raise RuntimeError(
        "Could not resolve the shop QBO realm. Connect the "
        f"'{company_name}' company via the QBO importer (?qbo=1), or set the "
        "SHOP_REALM_ID secret."
    )


# --------------------------------------------------------------------------- sync
def sync_shop_inventory(
    realm_id: str | None = None,
    *,
    force_full: bool = False,
) -> ShopSyncResult:
    """Pull QBO items for the shop realm and upsert into Supabase.

    Returns a :class:`ShopSyncResult`. Never raises for ordinary QBO/Supabase
    failures; the failure is captured on the result so the cron can log it and
    exit cleanly.
    """
    result = ShopSyncResult()
    try:
        qbo_client, token_repo, supabase = build_services()
    except Exception as exc:  # noqa: BLE001 - surface wiring errors on the result
        result.status = "failed"
        result.message = f"Service initialisation failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Shop inventory sync could not initialise services")
        return result

    try:
        result.realm_id = realm_id or resolve_shop_realm_id(token_repo)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = str(exc)
        result.errors.append(str(exc))
        logger.error("Shop inventory sync realm resolution failed: %s", exc)
        return result

    realm = token_repo.get_realm(result.realm_id)
    result.company_name = realm.company_name if realm else result.realm_id

    state = _load_sync_state(supabase, result.realm_id)
    cursor = "" if force_full else str(state.get("last_qbo_updated_at") or "")
    result.mode = "full" if not cursor else "delta"

    logger.info(
        "Shop inventory sync starting: realm=%s mode=%s cursor=%s",
        result.realm_id,
        result.mode,
        cursor or "(none)",
    )

    try:
        items = _fetch_items(qbo_client, result.realm_id, cursor=cursor)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"QBO item query failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Shop inventory sync QBO query failed")
        _save_sync_state(
            supabase,
            result.realm_id,
            last_cursor=cursor,
            status="failed",
            message=result.message,
            items_upserted=0,
        )
        return result

    result.items_fetched = len(items)
    rows = [_map_item_to_row(result.realm_id, item) for item in items]
    new_cursor = _max_cursor(cursor, rows)

    try:
        upserted = _upsert_rows(supabase, rows)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Supabase upsert failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Shop inventory sync upsert failed")
        _save_sync_state(
            supabase,
            result.realm_id,
            last_cursor=cursor,
            status="failed",
            message=result.message,
            items_upserted=0,
        )
        return result

    result.items_upserted = upserted
    result.new_cursor = new_cursor
    result.status = "success"
    result.message = (
        f"{result.mode} sync: fetched {result.items_fetched}, upserted {upserted}."
    )

    _save_sync_state(
        supabase,
        result.realm_id,
        last_cursor=new_cursor,
        status="success",
        message=result.message,
        items_upserted=upserted,
        full_completed=(result.mode == "full"),
    )
    logger.info("Shop inventory sync complete: %s", result.message)
    return result


# --------------------------------------------------------------------------- QBO read
def _fetch_items(qbo_client: QboClient, realm_id: str, *, cursor: str) -> list[dict[str, Any]]:
    """Return QBO items to sync.

    Full mode (no cursor): active items only.
    Delta mode (cursor): a) active items changed since cursor, plus
                         b) inactive items changed since cursor (captures
                            deactivations so they drop out of the shop list).
    """
    if not cursor:
        where = "Active = true"
        return _paginated_item_query(qbo_client, realm_id, where)

    safe_cursor = _escape_literal(cursor)
    changed_active = _paginated_item_query(
        qbo_client, realm_id, f"Metadata.LastUpdatedTime > '{safe_cursor}'"
    )
    changed_inactive = _paginated_item_query(
        qbo_client,
        realm_id,
        f"Active = false AND Metadata.LastUpdatedTime > '{safe_cursor}'",
    )

    # De-dupe by Id (an item cannot appear in both passes, but be defensive).
    by_id: dict[str, dict[str, Any]] = {}
    for item in (*changed_active, *changed_inactive):
        item_id = str(item.get("Id") or "")
        if item_id:
            by_id[item_id] = item
    return list(by_id.values())


def _paginated_item_query(
    qbo_client: QboClient, realm_id: str, where: str
) -> list[dict[str, Any]]:
    start = 1
    out: list[dict[str, Any]] = []
    while True:
        clause = f" WHERE {where}" if where else ""
        sql = (
            f"SELECT * FROM Item{clause} "
            f"STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
        )
        response = _query_with_retry(qbo_client, sql, realm_id)
        rows = (response.get("QueryResponse") or {}).get("Item") or []
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
            wait_seconds = exc.retry_after_seconds or 60
            logger.warning(
                "QBO rate limited (429); waiting %ss before retry %s/%s",
                wait_seconds,
                attempt,
                _MAX_RATE_LIMIT_RETRIES,
            )
            time.sleep(wait_seconds)


# --------------------------------------------------------------------------- mapping
def _map_item_to_row(realm_id: str, item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("MetaData") or {}
    return {
        "realm_id": realm_id,
        "qbo_item_id": str(item.get("Id") or ""),
        "name": str(item.get("Name") or ""),
        "fully_qualified_name": str(item.get("FullyQualifiedName") or ""),
        "sku": str(item.get("Sku") or ""),
        "sales_description": str(item.get("Description") or ""),
        "purchase_description": str(item.get("PurchaseDesc") or ""),
        "item_type": str(item.get("Type") or ""),
        "qty_on_hand": _as_number(item.get("QtyOnHand")),
        "reorder_point": _as_number(item.get("ReorderPoint")),
        "sales_price": _as_number(item.get("UnitPrice")),
        "purchase_cost": _as_number(item.get("PurchaseCost")),
        "income_account_name": _ref_name(item.get("IncomeAccountRef")),
        "expense_account_name": _ref_name(item.get("ExpenseAccountRef")),
        "asset_account_name": _ref_name(item.get("AssetAccountRef")),
        "taxable": item.get("Taxable") if isinstance(item.get("Taxable"), bool) else None,
        "active": bool(item.get("Active", True)),
        "qbo_last_updated_at": str(metadata.get("LastUpdatedTime") or "") or None,
        "qbo_created_at": str(metadata.get("CreateTime") or "") or None,
        "last_synced": datetime.now(UTC).isoformat(),
        "raw": item,
    }


def _ref_name(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("name") or "")
    return ""


def _as_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- Supabase write
def _upsert_rows(supabase: SupabaseRestClient, rows: list[dict[str, Any]]) -> int:
    cleaned = [row for row in rows if row.get("qbo_item_id")]
    if not cleaned:
        return 0
    upserted = 0
    for start in range(0, len(cleaned), _UPSERT_CHUNK):
        chunk = cleaned[start : start + _UPSERT_CHUNK]
        supabase.upsert(_INVENTORY_TABLE, chunk, on_conflict="realm_id,qbo_item_id")
        upserted += len(chunk)
    return upserted


def _load_sync_state(supabase: SupabaseRestClient, realm_id: str) -> dict[str, Any]:
    rows = supabase.select(
        _SYNC_STATE_TABLE,
        select="realm_id,last_qbo_updated_at,last_run_at,last_run_status,items_upserted",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    return rows[0] if rows else {}


def _save_sync_state(
    supabase: SupabaseRestClient,
    realm_id: str,
    *,
    last_cursor: str,
    status: str,
    message: str,
    items_upserted: int,
    full_completed: bool = False,
) -> None:
    row: dict[str, Any] = {
        "realm_id": realm_id,
        "last_qbo_updated_at": last_cursor or None,
        "last_run_at": datetime.now(UTC).isoformat(),
        "last_run_status": status,
        "last_run_message": message[:1000],
        "items_upserted": int(items_upserted),
    }
    if full_completed:
        row["full_sync_completed_at"] = datetime.now(UTC).isoformat()
    try:
        supabase.upsert(_SYNC_STATE_TABLE, row, on_conflict="realm_id")
    except Exception:  # noqa: BLE001 - state telemetry must never crash the sync
        logger.exception("Failed to persist shop inventory sync state for %s", realm_id)


# --------------------------------------------------------------------------- helpers
def _max_cursor(current: str, rows: list[dict[str, Any]]) -> str:
    """Return the lexicographically/chronologically greatest LastUpdatedTime.

    QBO emits ISO-8601 strings with a consistent offset, so the parsed datetime
    comparison is reliable; we fall back to the existing cursor when nothing has
    a newer timestamp.
    """
    best = current
    best_dt = _parse_dt(current)
    for row in rows:
        candidate = str(row.get("qbo_last_updated_at") or "")
        if not candidate:
            continue
        candidate_dt = _parse_dt(candidate)
        if candidate_dt is None:
            continue
        if best_dt is None or candidate_dt > best_dt:
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


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_within_business_hours(now: datetime | None = None) -> bool:
    """True when ``now`` (UTC) falls inside 07:00-17:00 US Central, Mon-Sat.

    Central time handles DST automatically via the zoneinfo database. The cron
    runner uses this so the sync never hits QBO overnight.
    """
    from zoneinfo import ZoneInfo

    moment = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/Chicago"))
    if moment.weekday() == 6:  # Sunday
        return False
    return 7 <= moment.hour < 17


if __name__ == "__main__":  # pragma: no cover - manual / cron entrypoint
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    outcome = sync_shop_inventory(force_full=_truthy(os.getenv("SHOP_FULL_SYNC")))
    logger.info("Result: %s", outcome)
    raise SystemExit(0 if outcome.status == "success" else 1)
