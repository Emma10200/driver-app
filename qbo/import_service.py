from __future__ import annotations

import time
from typing import Any, Callable

from qbo.api_client import QboClient, QboRateLimitError
from qbo.duplicate_check import (
    DuplicateChecker,
    build_invoice_key,
    build_money_code_key,
    build_purchase_key,
    date_range,
)
from qbo.lookups import EntityLookupService
from qbo.models import ImportStats

_RETRY_DELAYS = (1.0, 3.0, 8.0)
_QBO_BATCH_SIZE = 10


def _grouped_by_realm(records: list[dict[str, Any]], default_realm: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        realm = str(rec.get("_realmId") or default_realm or "").strip()
        if realm:
            groups.setdefault(realm, []).append(rec)
    return groups


class ImportService:
    def __init__(
        self,
        qbo_client: QboClient,
        lookups: EntityLookupService,
        dup_checker: DuplicateChecker,
        audit_log: Any,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> None:
        self._qbo = qbo_client
        self._lookups = lookups
        self._dup = dup_checker
        self._audit = audit_log
        self._progress_callback = progress_callback
        self._progress_done = 0
        self._progress_total = 0
        self._progress_label = "Posting to QuickBooks"

    def _begin_progress(self, total: int, label: str) -> None:
        self._progress_done = 0
        self._progress_total = max(0, int(total or 0))
        self._progress_label = label or "Posting to QuickBooks"
        self._emit_progress("Starting…")

    def _advance_progress(self, message: str) -> None:
        if self._progress_total <= 0:
            return
        self._progress_done = min(self._progress_total, self._progress_done + 1)
        self._emit_progress(message)

    def _emit_progress(self, message: str) -> None:
        if not self._progress_callback or self._progress_total <= 0:
            return
        self._progress_callback(
            self._progress_done,
            self._progress_total,
            f"{self._progress_label}: {self._progress_done}/{self._progress_total} — {message}",
        )

    def post_invoices(self, drafts: list[dict[str, Any]], target_realm_id: str = "") -> ImportStats:
        stats = ImportStats()
        if not drafts:
            return stats
        self._begin_progress(len(drafts), "Posting invoices")
        groups = _grouped_by_realm(drafts, target_realm_id)
        if not groups:
            stats.errors.append("No invoices had a resolvable target realm.")
            return stats

        for realm_id, group in groups.items():
            window = date_range(group, realm_id) or date_range([{**g, "_realmId": realm_id} for g in group], realm_id)
            existing_keys: set[str] = set()
            preload_complete = False
            if window:
                try:
                    existing_keys = self._dup.preload_invoice_keys(realm_id, window[0], window[1])
                    preload_complete = True
                except RuntimeError as exc:
                    stats.warnings.append(f"Realm {realm_id}: invoice dedup preload failed, falling back per record ({exc}).")
            prepared = self._prepare_invoice_batch(group, realm_id, existing_keys, stats, preload_complete=preload_complete)
            self._post_prepared_batches(
                prepared,
                realm_id,
                existing_keys,
                stats,
                txn_type="Invoice",
                response_key="Invoice",
                batch_entity_key="Invoice",
                bid_prefix="inv",
            )
        return stats

    def _prepare_invoice_batch(
        self,
        drafts: list[dict[str, Any]],
        realm_id: str,
        existing_keys: set[str],
        stats: ImportStats,
        *,
        preload_complete: bool,
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for draft in drafts:
            doc_number = str(draft.get("DocNumber") or "")
            txn_date = str(draft.get("TxnDate") or "")
            division = str(draft.get("_division") or "")
            customer_name = str(draft.get("_tempCustomerName") or "")
            amount = self._draft_amount(draft)
            try:
                customer_id = self._lookups.resolve_entity("Customer", customer_name, realm_id)
                if not customer_id:
                    self._record_failure(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount, f"Customer '{customer_name}' not found in QBO.")
                    continue
                dup_key = build_invoice_key(doc_number, txn_date, customer_id)
                if dup_key and dup_key in existing_keys:
                    self._record_duplicate(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount)
                    continue
                if not preload_complete and self._dup.invoice_exists(doc_number, realm_id):
                    self._record_duplicate(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount)
                    continue
                payload = self._materialize_invoice_payload(draft, customer_id, realm_id)
            except RuntimeError as exc:
                self._record_failure(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount, str(exc))
                continue
            prepared.append(
                {
                    "payload": payload,
                    "dup_key": dup_key,
                    "realm_id": realm_id,
                    "division": division,
                    "doc_number": doc_number,
                    "txn_date": txn_date,
                    "entity_name": customer_name,
                    "amount": amount,
                }
            )
        return prepared

    def _post_prepared_batches(
        self,
        prepared: list[dict[str, Any]],
        realm_id: str,
        existing_keys: set[str],
        stats: ImportStats,
        *,
        txn_type: str,
        response_key: str,
        batch_entity_key: str,
        bid_prefix: str,
    ) -> None:
        index = 0
        while index < len(prepared):
            chunk = prepared[index : index + _QBO_BATCH_SIZE]
            try:
                response = self._post_batch_with_retry(
                    realm_id=realm_id,
                    batch_items=[
                        {
                            "bId": f"{bid_prefix}{index + offset + 1}",
                            "operation": "create",
                            batch_entity_key: item["payload"],
                        }
                        for offset, item in enumerate(chunk)
                    ],
                )
            except QboRateLimitError as exc:
                held = prepared[index:]
                for item in held:
                    self._record_failure(
                        stats,
                        txn_type,
                        item["realm_id"],
                        item["division"],
                        item["doc_number"],
                        item["txn_date"],
                        item["entity_name"],
                        item["amount"],
                        _rate_limit_hold_message(exc),
                        retryable=True,
                    )
                stats.warnings.append(f"Realm {realm_id}: QuickBooks rate limit reached. Held {len(held)} {txn_type.lower()} row(s) for retry instead of continuing to hammer the API.")
                return
            except RuntimeError as exc:
                for item in chunk:
                    self._record_failure(
                        stats,
                        txn_type,
                        item["realm_id"],
                        item["division"],
                        item["doc_number"],
                        item["txn_date"],
                        item["entity_name"],
                        item["amount"],
                        str(exc),
                        retryable=_is_transient_error(str(exc)),
                    )
                index += _QBO_BATCH_SIZE
                continue

            by_bid = {
                str(item.get("bId") or ""): item
                for item in (response.get("BatchItemResponse") or [])
                if isinstance(item, dict)
            }
            for offset, item in enumerate(chunk):
                b_id = f"{bid_prefix}{index + offset + 1}"
                item_response = by_bid.get(b_id) or {}
                fault = item_response.get("Fault")
                if fault:
                    message = _batch_fault_message(fault)
                    self._record_failure(
                        stats,
                        txn_type,
                        item["realm_id"],
                        item["division"],
                        item["doc_number"],
                        item["txn_date"],
                        item["entity_name"],
                        item["amount"],
                        message,
                        retryable=_is_transient_error(message),
                    )
                    continue
                entity = item_response.get(response_key) or {}
                qbo_id = str(entity.get("Id") or "")
                if not qbo_id:
                    self._record_failure(
                        stats,
                        txn_type,
                        item["realm_id"],
                        item["division"],
                        item["doc_number"],
                        item["txn_date"],
                        item["entity_name"],
                        item["amount"],
                        f"QuickBooks batch response did not include a {response_key} Id. Retry this row after reviewing QBO.",
                        retryable=True,
                    )
                    continue
                self._record_success(stats, item_response, txn_type, item["realm_id"], item["division"], item["doc_number"], item["txn_date"], item["entity_name"], item["amount"], qbo_id)
                if item.get("dup_key"):
                    existing_keys.add(str(item["dup_key"]))
            index += _QBO_BATCH_SIZE

    def post_checks(self, checks: list[dict[str, Any]], target_realm_id: str = "") -> ImportStats:
        stats = ImportStats()
        if not checks:
            return stats
        self._begin_progress(len(checks), "Posting checks")
        groups = _grouped_by_realm(checks, target_realm_id)
        if not groups:
            stats.errors.append("No checks had a resolvable target realm.")
            return stats

        for realm_id, group in groups.items():
            window = date_range(group, realm_id)
            existing_keys: set[str] = set()
            preload_complete = False
            if window:
                try:
                    existing_keys = self._dup.preload_purchase_keys(realm_id, "Check", window[0], window[1])
                    preload_complete = True
                except RuntimeError as exc:
                    stats.warnings.append(f"Realm {realm_id}: check dedup preload failed ({exc}).")
            prepared = self._prepare_check_batch(group, realm_id, existing_keys, stats, preload_complete=preload_complete)
            self._post_prepared_batches(
                prepared,
                realm_id,
                existing_keys,
                stats,
                txn_type="Check",
                response_key="Purchase",
                batch_entity_key="Purchase",
                bid_prefix="chk",
            )
        return stats

    def post_money_codes(self, expenses: list[dict[str, Any]], target_realm_id: str = "") -> ImportStats:
        stats = ImportStats()
        if not expenses:
            return stats
        self._begin_progress(len(expenses), "Posting money codes")
        groups = _grouped_by_realm(expenses, target_realm_id)
        if not groups:
            stats.errors.append("No money-code expenses had a resolvable target realm.")
            return stats

        for realm_id, group in groups.items():
            window = date_range(group, realm_id)
            existing_keys: set[str] = set()
            preload_complete = False
            if window:
                try:
                    existing_keys = self._dup.preload_money_code_keys(realm_id, window[0], window[1])
                    preload_complete = True
                except RuntimeError as exc:
                    stats.warnings.append(f"Realm {realm_id}: money-code dedup preload failed ({exc}).")
            prepared = self._prepare_money_code_batch(group, realm_id, existing_keys, stats, preload_complete=preload_complete)
            self._post_prepared_batches(
                prepared,
                realm_id,
                existing_keys,
                stats,
                txn_type="MoneyCode",
                response_key="Purchase",
                batch_entity_key="Purchase",
                bid_prefix="mc",
            )
        return stats

    def _prepare_check_batch(
        self,
        drafts: list[dict[str, Any]],
        realm_id: str,
        existing_keys: set[str],
        stats: ImportStats,
        *,
        preload_complete: bool,
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for draft in drafts:
            doc_number = str(draft.get("DocNumber") or "")
            txn_date = str(draft.get("TxnDate") or "")
            division = str(draft.get("_division") or "")
            vendor_name = str(draft.get("_tempVendorName") or "")
            amount = self._draft_amount(draft)
            try:
                vendor_id = self._lookups.resolve_entity("Vendor", vendor_name, realm_id)
                if not vendor_id:
                    self._record_failure(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount, f"Vendor '{vendor_name}' not found in QBO.")
                    continue
                bank_ref = dict(draft.get("AccountRef") or {})
                bank_id = bank_ref.get("value") or ""
                bank_name = bank_ref.get("name") or ""
                if not bank_id and bank_name:
                    bank_id = self._lookups.resolve_account(bank_name, realm_id) or ""
                if not bank_id:
                    self._record_failure(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount, "No bank account set on check (AccountRef).")
                    continue
                dup_key = build_purchase_key(doc_number, txn_date, vendor_id, "Check")
                if dup_key and dup_key in existing_keys:
                    self._record_duplicate(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount)
                    continue
                if not preload_complete and self._dup.check_exists(doc_number, txn_date, vendor_id, realm_id):
                    self._record_duplicate(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount)
                    continue
                payload = self._materialize_purchase_payload(draft, vendor_id, bank_id, realm_id, "Check")
            except RuntimeError as exc:
                self._record_failure(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount, str(exc))
                continue
            prepared.append({"payload": payload, "dup_key": dup_key, "realm_id": realm_id, "division": division, "doc_number": doc_number, "txn_date": txn_date, "entity_name": vendor_name, "amount": amount})
        return prepared

    def _prepare_money_code_batch(
        self,
        drafts: list[dict[str, Any]],
        realm_id: str,
        existing_keys: set[str],
        stats: ImportStats,
        *,
        preload_complete: bool,
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for draft in drafts:
            doc_number = str(draft.get("DocNumber") or "")
            txn_date = str(draft.get("TxnDate") or "")
            division = str(draft.get("_division") or "")
            vendor_name = str(draft.get("_tempVendorName") or "")
            amount = self._draft_amount(draft)
            memo = str(draft.get("_memo") or "")
            first_line = (draft.get("Line") or [{}])[0] or {}
            expense_account_name = str(first_line.get("_tempAccountName") or "") if isinstance(first_line, dict) else ""
            try:
                vendor_id = self._lookups.resolve_entity("Vendor", vendor_name, realm_id)
                if not vendor_id:
                    self._record_failure(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount, f"Vendor '{vendor_name}' not found in QBO.")
                    continue
                cc_account_name = str(draft.get("_tempCcAccountName") or "Fuel Card - EFS")
                cc_account_id = self._lookups.resolve_account(cc_account_name, realm_id)
                if not cc_account_id:
                    self._record_failure(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount, f"CC account '{cc_account_name}' not found in QBO.")
                    continue
                dup_key = build_money_code_key(doc_number, txn_date, vendor_id, amount, memo, expense_account_name)
                if dup_key and dup_key in existing_keys:
                    self._record_duplicate(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount)
                    continue
                if not preload_complete and self._dup.money_code_exists(doc_number, txn_date, vendor_id, realm_id, amount, memo, expense_account_name):
                    self._record_duplicate(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount)
                    continue
                payload = self._materialize_purchase_payload(draft, vendor_id, cc_account_id, realm_id, "CreditCard")
            except RuntimeError as exc:
                self._record_failure(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount, str(exc))
                continue
            prepared.append({"payload": payload, "dup_key": dup_key, "realm_id": realm_id, "division": division, "doc_number": doc_number, "txn_date": txn_date, "entity_name": vendor_name, "amount": amount})
        return prepared

    def _materialize_invoice_payload(self, draft: dict[str, Any], customer_id: str, realm_id: str) -> dict[str, Any]:
        payload = {key: value for key, value in draft.items() if not key.startswith("_")}
        payload["CustomerRef"] = {"value": customer_id}
        term_name = str(draft.get("_tempTermName") or "").strip()
        if term_name:
            term_id = self._lookups.resolve_entity("Term", term_name, realm_id)
            if term_id:
                payload["SalesTermRef"] = {"value": term_id}
        lines: list[dict[str, Any]] = []
        for raw_line in payload.get("Line") or []:
            line = {key: value for key, value in (raw_line or {}).items() if not key.startswith("_")}
            item_name = str((raw_line or {}).get("_tempItemName") or "").strip()
            if item_name:
                item_id = self._lookups.resolve_entity("Item", item_name, realm_id)
                if not item_id:
                    raise RuntimeError(f"Item '{item_name}' not found in QBO for realm {realm_id}.")
                detail = dict(line.get("SalesItemLineDetail") or {})
                detail["ItemRef"] = {"value": item_id}
                line["SalesItemLineDetail"] = detail
            lines.append(line)
        payload["Line"] = lines
        return payload

    def _materialize_purchase_payload(self, draft: dict[str, Any], entity_id: str, account_id: str, realm_id: str, payment_type: str) -> dict[str, Any]:
        payload = {key: value for key, value in draft.items() if not key.startswith("_")}
        payload["PaymentType"] = payment_type
        payload["EntityRef"] = {"value": entity_id, "type": "Vendor"}
        payload["AccountRef"] = {"value": account_id}
        lines: list[dict[str, Any]] = []
        for raw_line in payload.get("Line") or []:
            line = {key: value for key, value in (raw_line or {}).items() if not key.startswith("_")}
            account_name = str((raw_line or {}).get("_tempAccountName") or "").strip()
            if account_name:
                line_account_id = self._lookups.resolve_account(account_name, realm_id)
                if not line_account_id:
                    raise RuntimeError(f"Expense account '{account_name}' not found in QBO for realm {realm_id}.")
                detail = dict(line.get("AccountBasedExpenseLineDetail") or {})
                detail["AccountRef"] = {"value": line_account_id}
                line["AccountBasedExpenseLineDetail"] = detail
            line.setdefault("DetailType", "AccountBasedExpenseLineDetail")
            lines.append(line)
        payload["Line"] = lines
        return payload

    def _post_with_retry(self, path: str, *, realm_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                response = self._qbo.post(path, realm_id=realm_id, payload=payload)
                return response.json() if hasattr(response, "json") else response
            except RuntimeError as exc:
                msg = str(exc)
                transient = any(token in msg for token in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")) or "rate limit" in msg.lower()
                last_error = exc
                if not transient or attempt == len(_RETRY_DELAYS):
                    raise
        if last_error:
            raise last_error
        raise RuntimeError("Unknown POST failure")

    def _post_batch_with_retry(self, *, realm_id: str, batch_items: list[dict[str, Any]]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                return self._qbo.batch(realm_id=realm_id, requests=batch_items)
            except QboRateLimitError as exc:
                # Intuit's QBO guidance for HTTP 429 is to wait about 60 seconds
                # before retrying. Do not retry these short-delay attempts;
                # bubble up so the caller can hold remaining rows for retry.
                raise exc
            except RuntimeError as exc:
                msg = str(exc)
                last_error = exc
                if not _is_transient_error(msg) or attempt == len(_RETRY_DELAYS):
                    raise
        if last_error:
            raise last_error
        raise RuntimeError("Unknown QBO batch failure")

    @staticmethod
    def _draft_amount(draft: dict[str, Any]) -> float:
        total = 0.0
        for line in draft.get("Line") or []:
            try:
                total += float((line or {}).get("Amount") or 0.0)
            except (TypeError, ValueError):
                continue
        if total:
            return total
        try:
            return float(draft.get("TotalAmt") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _record_success(self, stats: ImportStats, raw_response: Any, txn_type: str, realm_id: str, division: str, doc_number: str, txn_date: str, entity_name: str, amount: float, qbo_id: str) -> None:
        stats.posted += 1
        row = {"txn_type": txn_type, "realm_id": realm_id, "division": division, "doc_number": doc_number, "txn_date": txn_date, "entity_name": entity_name, "amount": amount, "qbo_id": qbo_id, "status": "success"}
        stats.successes.append(row)
        stats.bump_division(division, "posted")
        self._audit.record(**row, raw_response=raw_response)
        self._advance_progress(f"posted {txn_type} {doc_number or qbo_id}")

    def _record_duplicate(self, stats: ImportStats, txn_type: str, realm_id: str, division: str, doc_number: str, txn_date: str, entity_name: str, amount: float) -> None:
        stats.skipped_duplicates += 1
        row = {"txn_type": txn_type, "realm_id": realm_id, "division": division, "doc_number": doc_number, "txn_date": txn_date, "entity_name": entity_name, "amount": amount, "status": "duplicate"}
        stats.duplicates.append(row)
        stats.bump_division(division, "duplicate")
        self._audit.record(**row, message="Duplicate detected via composite key.")
        self._advance_progress(f"skipped duplicate {txn_type} {doc_number}")

    def _record_failure(self, stats: ImportStats, txn_type: str, realm_id: str, division: str, doc_number: str, txn_date: str, entity_name: str, amount: float, message: str, *, retryable: bool = False) -> None:
        stats.failed += 1
        if retryable:
            stats.held_for_retry += 1
        row = {"txn_type": txn_type, "realm_id": realm_id, "division": division, "doc_number": doc_number, "txn_date": txn_date, "entity_name": entity_name, "amount": amount, "status": "failed", "message": message, "retryable": retryable}
        stats.failures.append(row)
        stats.errors.append(f"{txn_type} {doc_number} @ {realm_id}: {message}")
        stats.bump_division(division, "failed")
        audit_row = {key: value for key, value in row.items() if key != "retryable"}
        self._audit.record(**audit_row)
        self._advance_progress(f"failed {txn_type} {doc_number}")


def _batch_fault_message(fault: dict[str, Any]) -> str:
    errors = fault.get("Error") or []
    if isinstance(errors, dict):
        errors = [errors]
    parts: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        detail = str(err.get("Detail") or err.get("Message") or err.get("code") or "").strip()
        if detail:
            parts.append(detail)
    return "; ".join(parts) or str(fault)[:500] or "QuickBooks rejected this batch item."


def _is_transient_error(message: str) -> bool:
    lower = str(message or "").lower()
    return any(token in lower for token in ("http 429", "rate limit", "throttl", "http 500", "http 502", "http 503", "http 504", "timeout"))


def _rate_limit_hold_message(exc: QboRateLimitError) -> str:
    retry_after = exc.retry_after_seconds or 60
    return f"Retryable: QuickBooks rate limit reached (HTTP 429). Held for retry; wait at least {retry_after} seconds before resending."
