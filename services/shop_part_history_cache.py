"""Derived Supabase cache for ultra-fast shop Part history.

This table is a rebuildable index, not a source of truth. It flattens the cached
QBO invoice, purchase/bill, and inventory-adjustment history into one row per
item/transaction line so the Part detail page can do a single indexed lookup by
``item_id``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from services.qbo_supabase import SupabaseRestClient
from services.shop_inventory_adjustment_cache import list_cached_inventory_adjustments
from services.shop_invoice_history_cache import list_cached_invoices
from services.shop_purchase_history_cache import list_cached_purchases

logger = logging.getLogger(__name__)

_TABLE = "shop_part_history_index"
_STATE_TABLE = "shop_part_history_index_state"
_REBUILD_LIMIT = 50000
_UPSERT_CHUNK = 500
_HISTORY_SELECT = (
    "event_id,item_id,item_name,kind,qbo_txn_type,qbo_txn_id,doc_number,txn_date,"
    "counterparty_name,unit,vin,miles,qty,rate,amount,memo,raw_line"
)


@dataclass(slots=True)
class PartHistoryRebuildResult:
    realm_id: str = ""
    events_upserted: int = 0
    status: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)


def rebuild_shop_part_history_index(realm_id: str) -> PartHistoryRebuildResult:
    """Rebuild the derived part-history index from cached QBO history tables."""
    result = PartHistoryRebuildResult(realm_id=str(realm_id or ""))
    if not result.realm_id:
        result.status = "failed"
        result.message = "No shop QuickBooks company is connected yet."
        result.errors.append(result.message)
        return result

    run_started = datetime.now(UTC).isoformat()
    supabase = SupabaseRestClient()
    try:
        rows = _build_rows(result.realm_id, run_started)
        upserted = _upsert_rows(supabase, rows)
        _prune_stale(supabase, result.realm_id, run_started)
    except Exception as exc:  # noqa: BLE001 - Sync All should warn, not crash
        result.status = "failed"
        result.message = f"Part history index rebuild failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Part history index rebuild failed")
        _save_state(supabase, result.realm_id, run_started, "failed", result.message, 0)
        return result

    result.events_upserted = upserted
    result.status = "success"
    result.message = f"Rebuilt {upserted} part-history event(s)."
    _save_state(
        supabase,
        result.realm_id,
        run_started,
        "success",
        result.message,
        upserted,
        full_completed=True,
    )
    return result


def list_part_history_events(realm_id: str, item_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
    """Return prebuilt part-history events for one item, newest first."""
    if not realm_id or not str(item_id or "").strip():
        return []
    supabase = SupabaseRestClient()
    rows = supabase.select_all(
        _TABLE,
        select=_HISTORY_SELECT,
        filters={"realm_id": f"eq.{realm_id}", "item_id": f"eq.{item_id}"},
        order="txn_date.desc,event_id.desc",
        page_size=1000,
        hard_cap=max(1000, int(limit or 2000)),
    )
    return [_event_from_row(row) for row in rows[: max(1, int(limit or 2000))]]


def part_history_index_ready(realm_id: str) -> bool:
    """True once the derived index has completed at least one successful rebuild."""
    if not realm_id:
        return False
    supabase = SupabaseRestClient()
    rows = supabase.select(
        _STATE_TABLE,
        select="last_run_status,full_rebuild_completed_at",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    state = rows[0] if rows else {}
    return str(state.get("last_run_status") or "") == "success" and bool(
        state.get("full_rebuild_completed_at")
    )


def _build_rows(realm_id: str, run_started: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for inv in list_cached_invoices(realm_id, limit=_REBUILD_LIMIT):
        rows.extend(_invoice_rows(realm_id, inv, run_started))
    for doc in list_cached_purchases(realm_id, limit=_REBUILD_LIMIT):
        rows.extend(_purchase_rows(realm_id, doc, run_started))
    for doc in list_cached_inventory_adjustments(realm_id, limit=_REBUILD_LIMIT):
        rows.extend(_adjustment_rows(realm_id, doc, run_started))
    return rows


def _invoice_rows(realm_id: str, inv: dict[str, Any], run_started: str) -> list[dict[str, Any]]:
    invoice_id = str(inv.get("qbo_invoice_id") or inv.get("Id") or "")
    raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else {}
    lines = [
        line
        for line in (raw.get("Line") or [])
        if str(line.get("DetailType") or "") == "SalesItemLineDetail"
    ] or (inv.get("line_items") or [])
    out: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        item_name, item_id = _sales_line_item_ref(line)
        if not item_id:
            continue
        qty, rate, amount = _sales_line_qty_rate_amount(line)
        out.append(
            _base_row(
                realm_id=realm_id,
                run_started=run_started,
                event_id=_event_id("sold", invoice_id, item_id, line, idx),
                item_id=item_id,
                item_name=item_name,
                kind="sold",
                qbo_txn_type="Invoice",
                qbo_txn_id=invoice_id,
                doc_number=str(inv.get("doc_number") or inv.get("DocNumber") or ""),
                txn_date=str(inv.get("txn_date") or inv.get("TxnDate") or ""),
                counterparty_name=str(inv.get("customer_name") or _ref_name(raw.get("CustomerRef")) or ""),
                unit=str(inv.get("unit") or ""),
                vin=str(inv.get("vin") or ""),
                miles=str(inv.get("miles") or ""),
                qty=qty,
                rate=rate,
                amount=amount,
                memo=str(line.get("description") or line.get("Description") or ""),
                source_updated_at=str(inv.get("qbo_last_updated_at") or "") or None,
                raw_line=line,
            )
        )
    return out


def _purchase_rows(realm_id: str, doc: dict[str, Any], run_started: str) -> list[dict[str, Any]]:
    txn_id = str(doc.get("qbo_txn_id") or doc.get("Id") or "")
    txn_type = str(doc.get("qbo_txn_type") or "Purchase")
    raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
    lines = (raw.get("Line") or []) or (doc.get("line_items") or [])
    out: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        item_name, item_id = _purchase_line_item_ref(line)
        if not item_id:
            continue
        qty, rate, amount = _purchase_line_qty_rate_amount(line)
        out.append(
            _base_row(
                realm_id=realm_id,
                run_started=run_started,
                event_id=_event_id("bought", txn_id, item_id, line, idx),
                item_id=item_id,
                item_name=item_name,
                kind="bought",
                qbo_txn_type=txn_type,
                qbo_txn_id=txn_id,
                doc_number=str(doc.get("doc_number") or doc.get("DocNumber") or txn_id),
                txn_date=str(doc.get("txn_date") or doc.get("TxnDate") or ""),
                counterparty_name=str(doc.get("vendor_name") or _vendor_name(raw) or ""),
                unit="",
                vin="",
                miles="",
                qty=qty,
                rate=rate,
                amount=amount,
                memo=str(line.get("description") or line.get("Description") or txn_type),
                source_updated_at=str(doc.get("qbo_last_updated_at") or "") or None,
                raw_line=line,
            )
        )
    return out


def _adjustment_rows(realm_id: str, doc: dict[str, Any], run_started: str) -> list[dict[str, Any]]:
    txn_id = str(doc.get("qbo_adjustment_id") or doc.get("Id") or "")
    raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
    lines = (raw.get("Line") or []) or (doc.get("line_items") or [])
    account = str(doc.get("adjust_account_name") or "Inventory adjustment")
    memo = str(doc.get("reason") or doc.get("private_note") or "")
    out: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        item_name, item_id = _adjustment_line_item_ref(line)
        if not item_id:
            continue
        out.append(
            _base_row(
                realm_id=realm_id,
                run_started=run_started,
                event_id=_event_id("adjusted", txn_id, item_id, line, idx),
                item_id=item_id,
                item_name=item_name,
                kind="adjusted",
                qbo_txn_type="InventoryAdjustment",
                qbo_txn_id=txn_id,
                doc_number=str(doc.get("doc_number") or doc.get("ReferenceNumber") or txn_id),
                txn_date=str(doc.get("txn_date") or doc.get("AdjustmentDate") or ""),
                counterparty_name=account,
                unit="",
                vin="",
                miles="",
                qty=_adjustment_line_qty_diff(line),
                rate=None,
                amount=None,
                memo=str(line.get("description") or line.get("Description") or memo),
                source_updated_at=str(doc.get("qbo_last_updated_at") or "") or None,
                raw_line=line,
            )
        )
    return out


def _base_row(**kwargs: Any) -> dict[str, Any]:
    return {
        "realm_id": kwargs["realm_id"],
        "event_id": kwargs["event_id"],
        "item_id": kwargs["item_id"],
        "item_name": kwargs.get("item_name") or "",
        "kind": kwargs["kind"],
        "qbo_txn_type": kwargs.get("qbo_txn_type") or "",
        "qbo_txn_id": kwargs.get("qbo_txn_id") or "",
        "doc_number": kwargs.get("doc_number") or "",
        "txn_date": kwargs.get("txn_date") or None,
        "counterparty_name": kwargs.get("counterparty_name") or "",
        "unit": kwargs.get("unit") or "",
        "vin": kwargs.get("vin") or "",
        "miles": kwargs.get("miles") or "",
        "qty": _as_number(kwargs.get("qty")),
        "rate": _as_number(kwargs.get("rate")),
        "amount": _as_number(kwargs.get("amount")),
        "memo": kwargs.get("memo") or "",
        "source_updated_at": kwargs.get("source_updated_at"),
        "raw_line": kwargs.get("raw_line"),
        "last_rebuilt_at": kwargs["run_started"],
    }


def _event_from_row(row: dict[str, Any]) -> dict[str, Any]:
    kind = str(row.get("kind") or "")
    event: dict[str, Any] = {
        "kind": kind,
        "date": str(row.get("txn_date") or ""),
        "doc": str(row.get("doc_number") or ""),
        "name": str(row.get("counterparty_name") or ""),
        "unit": str(row.get("unit") or ""),
        "vin": str(row.get("vin") or ""),
        "miles": str(row.get("miles") or ""),
        "qty": row.get("qty"),
        "rate": row.get("rate"),
        "amount": row.get("amount"),
        "memo": str(row.get("memo") or ""),
    }
    if kind == "sold":
        event["invoice_id"] = str(row.get("qbo_txn_id") or "")
    elif kind == "bought":
        event["purchase_id"] = str(row.get("qbo_txn_id") or "")
        event["purchase_type"] = str(row.get("qbo_txn_type") or "Purchase")
    return event


def _upsert_rows(supabase: SupabaseRestClient, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    count = 0
    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        supabase.upsert(_TABLE, chunk, on_conflict="realm_id,event_id")
        count += len(chunk)
    return count


def _prune_stale(supabase: SupabaseRestClient, realm_id: str, run_started: str) -> None:
    supabase.delete(
        _TABLE,
        filters={"realm_id": f"eq.{realm_id}", "last_rebuilt_at": f"lt.{run_started}"},
    )


def _save_state(
    supabase: SupabaseRestClient,
    realm_id: str,
    run_at: str,
    status: str,
    message: str,
    events_upserted: int,
    *,
    full_completed: bool = False,
) -> None:
    row: dict[str, Any] = {
        "realm_id": realm_id,
        "last_run_at": run_at,
        "last_run_status": status,
        "last_run_message": message[:1000],
        "events_upserted": int(events_upserted),
    }
    if full_completed:
        row["full_rebuild_completed_at"] = run_at
    try:
        supabase.upsert(_STATE_TABLE, row, on_conflict="realm_id")
    except Exception:  # noqa: BLE001 - state is telemetry, not business data
        logger.exception("Part history index state save failed")


def _sales_line_item_ref(line: dict[str, Any]) -> tuple[str, str]:
    if line.get("item_name") or line.get("item_id"):
        return str(line.get("item_name") or "").strip(), str(line.get("item_id") or "").strip()
    detail = line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    return _ref_name(item_ref), _ref_value(item_ref)


def _sales_line_qty_rate_amount(line: dict[str, Any]) -> tuple[Any, Any, Any]:
    if "item_name" in line or "unit_price" in line:
        return line.get("qty"), line.get("unit_price"), line.get("amount")
    detail = line.get("SalesItemLineDetail") or {}
    return detail.get("Qty"), detail.get("UnitPrice"), line.get("Amount")


def _purchase_line_item_ref(line: dict[str, Any]) -> tuple[str, str]:
    if line.get("item_name") or line.get("item_id"):
        return str(line.get("item_name") or "").strip(), str(line.get("item_id") or "").strip()
    detail = line.get("ItemBasedExpenseLineDetail") or line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    return _ref_name(item_ref), _ref_value(item_ref)


def _purchase_line_qty_rate_amount(line: dict[str, Any]) -> tuple[Any, Any, Any]:
    if "item_name" in line or "unit_price" in line:
        return line.get("qty"), line.get("unit_price"), line.get("amount")
    detail = line.get("ItemBasedExpenseLineDetail") or {}
    return detail.get("Qty"), detail.get("UnitPrice"), line.get("Amount")


def _adjustment_line_item_ref(line: dict[str, Any]) -> tuple[str, str]:
    if line.get("item_name") or line.get("item_id"):
        return str(line.get("item_name") or "").strip(), str(line.get("item_id") or "").strip()
    detail = line.get("ItemAdjustmentLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    return _ref_name(item_ref), _ref_value(item_ref)


def _adjustment_line_qty_diff(line: dict[str, Any]) -> Any:
    if "qty_diff" in line:
        return line.get("qty_diff")
    detail = line.get("ItemAdjustmentLineDetail") or {}
    return detail.get("QtyDiff") if isinstance(detail, dict) else None


def _vendor_name(doc: dict[str, Any]) -> str:
    for key in ("EntityRef", "VendorRef"):
        ref = doc.get(key)
        if isinstance(ref, dict) and ref.get("name"):
            return str(ref.get("name") or "")
    return ""


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


def _event_id(kind: str, txn_id: str, item_id: str, line: dict[str, Any], index: int) -> str:
    line_id = str(line.get("Id") or line.get("line_id") or index)
    return "|".join([kind, str(txn_id or ""), str(item_id or ""), line_id])
