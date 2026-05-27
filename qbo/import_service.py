from __future__ import annotations

import time
from typing import Any

from qbo.api_client import QboClient
from qbo.duplicate_check import (
    DuplicateChecker,
    build_invoice_key,
    build_money_code_key,
    build_purchase_key,
    date_range,
)
from qbo.lookups import EntityLookupService
from qbo.models import ImportStats

_POST_DELAY_SECONDS = 0.35
_RETRY_DELAYS = (1.0, 3.0, 8.0)


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
    ) -> None:
        self._qbo = qbo_client
        self._lookups = lookups
        self._dup = dup_checker
        self._audit = audit_log

    def post_invoices(self, drafts: list[dict[str, Any]], target_realm_id: str = "") -> ImportStats:
        stats = ImportStats()
        if not drafts:
            return stats
        groups = _grouped_by_realm(drafts, target_realm_id)
        if not groups:
            stats.errors.append("No invoices had a resolvable target realm.")
            return stats

        for realm_id, group in groups.items():
            window = date_range(group, realm_id) or date_range([{**g, "_realmId": realm_id} for g in group], realm_id)
            existing_keys: set[str] = set()
            if window:
                try:
                    existing_keys = self._dup.preload_invoice_keys(realm_id, window[0], window[1])
                except RuntimeError as exc:
                    stats.warnings.append(f"Realm {realm_id}: invoice dedup preload failed, falling back per record ({exc}).")
            for draft in group:
                self._post_one_invoice(draft, realm_id, existing_keys, stats)
        return stats

    def post_checks(self, checks: list[dict[str, Any]], target_realm_id: str = "") -> ImportStats:
        stats = ImportStats()
        if not checks:
            return stats
        groups = _grouped_by_realm(checks, target_realm_id)
        if not groups:
            stats.errors.append("No checks had a resolvable target realm.")
            return stats

        for realm_id, group in groups.items():
            window = date_range(group, realm_id)
            existing_keys: set[str] = set()
            if window:
                try:
                    existing_keys = self._dup.preload_purchase_keys(realm_id, "Check", window[0], window[1])
                except RuntimeError as exc:
                    stats.warnings.append(f"Realm {realm_id}: check dedup preload failed ({exc}).")
            for check in group:
                self._post_one_check(check, realm_id, existing_keys, stats)
        return stats

    def post_money_codes(self, expenses: list[dict[str, Any]], target_realm_id: str = "") -> ImportStats:
        stats = ImportStats()
        if not expenses:
            return stats
        groups = _grouped_by_realm(expenses, target_realm_id)
        if not groups:
            stats.errors.append("No money-code expenses had a resolvable target realm.")
            return stats

        for realm_id, group in groups.items():
            window = date_range(group, realm_id)
            existing_keys: set[str] = set()
            if window:
                try:
                    existing_keys = self._dup.preload_money_code_keys(realm_id, window[0], window[1])
                except RuntimeError as exc:
                    stats.warnings.append(f"Realm {realm_id}: money-code dedup preload failed ({exc}).")
            for expense in group:
                self._post_one_money_code(expense, realm_id, existing_keys, stats)
        return stats

    def _post_one_invoice(self, draft: dict[str, Any], realm_id: str, existing_keys: set[str], stats: ImportStats) -> None:
        doc_number = str(draft.get("DocNumber") or "")
        txn_date = str(draft.get("TxnDate") or "")
        division = str(draft.get("_division") or "")
        customer_name = str(draft.get("_tempCustomerName") or "")
        amount = self._draft_amount(draft)
        try:
            customer_id = self._lookups.resolve_entity("Customer", customer_name, realm_id)
            if not customer_id:
                self._record_failure(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount, f"Customer '{customer_name}' not found in QBO.")
                return
            dup_key = build_invoice_key(doc_number, txn_date, customer_id)
            if dup_key and dup_key in existing_keys:
                self._record_duplicate(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount)
                return
            if not existing_keys and self._dup.invoice_exists(doc_number, realm_id):
                self._record_duplicate(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount)
                return
            payload = self._materialize_invoice_payload(draft, customer_id, realm_id)
            response = self._post_with_retry("/invoice", realm_id=realm_id, payload=payload)
            invoice = (response or {}).get("Invoice") or {}
            qbo_id = str(invoice.get("Id") or "")
            self._record_success(stats, response, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount, qbo_id)
            if dup_key:
                existing_keys.add(dup_key)
        except RuntimeError as exc:
            self._record_failure(stats, "Invoice", realm_id, division, doc_number, txn_date, customer_name, amount, str(exc))
        finally:
            time.sleep(_POST_DELAY_SECONDS)

    def _post_one_check(self, draft: dict[str, Any], realm_id: str, existing_keys: set[str], stats: ImportStats) -> None:
        doc_number = str(draft.get("DocNumber") or "")
        txn_date = str(draft.get("TxnDate") or "")
        division = str(draft.get("_division") or "")
        vendor_name = str(draft.get("_tempVendorName") or "")
        amount = self._draft_amount(draft)
        try:
            vendor_id = self._lookups.resolve_entity("Vendor", vendor_name, realm_id)
            if not vendor_id:
                self._record_failure(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount, f"Vendor '{vendor_name}' not found in QBO.")
                return
            bank_ref = dict(draft.get("AccountRef") or {})
            bank_id = bank_ref.get("value") or ""
            bank_name = bank_ref.get("name") or ""
            if not bank_id and bank_name:
                bank_id = self._lookups.resolve_account(bank_name, realm_id) or ""
            if not bank_id:
                self._record_failure(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount, "No bank account set on check (AccountRef).")
                return
            dup_key = build_purchase_key(doc_number, txn_date, vendor_id, "Check")
            if dup_key and dup_key in existing_keys:
                self._record_duplicate(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount)
                return
            if not existing_keys and self._dup.check_exists(doc_number, txn_date, vendor_id, realm_id):
                self._record_duplicate(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount)
                return
            payload = self._materialize_purchase_payload(draft, vendor_id, bank_id, realm_id, "Check")
            response = self._post_with_retry("/purchase", realm_id=realm_id, payload=payload)
            purchase = (response or {}).get("Purchase") or {}
            qbo_id = str(purchase.get("Id") or "")
            self._record_success(stats, response, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount, qbo_id)
            if dup_key:
                existing_keys.add(dup_key)
        except RuntimeError as exc:
            self._record_failure(stats, "Check", realm_id, division, doc_number, txn_date, vendor_name, amount, str(exc))
        finally:
            time.sleep(_POST_DELAY_SECONDS)

    def _post_one_money_code(self, draft: dict[str, Any], realm_id: str, existing_keys: set[str], stats: ImportStats) -> None:
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
                return
            cc_account_name = str(draft.get("_tempCcAccountName") or "Fuel Card - EFS")
            cc_account_id = self._lookups.resolve_account(cc_account_name, realm_id)
            if not cc_account_id:
                self._record_failure(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount, f"CC account '{cc_account_name}' not found in QBO.")
                return
            dup_key = build_money_code_key(doc_number, txn_date, vendor_id, amount, memo, expense_account_name)
            if dup_key and dup_key in existing_keys:
                self._record_duplicate(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount)
                return
            if not existing_keys and self._dup.money_code_exists(doc_number, txn_date, vendor_id, realm_id, amount, memo, expense_account_name):
                self._record_duplicate(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount)
                return
            payload = self._materialize_purchase_payload(draft, vendor_id, cc_account_id, realm_id, "CreditCard")
            response = self._post_with_retry("/purchase", realm_id=realm_id, payload=payload)
            purchase = (response or {}).get("Purchase") or {}
            qbo_id = str(purchase.get("Id") or "")
            self._record_success(stats, response, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount, qbo_id)
            if dup_key:
                existing_keys.add(dup_key)
        except RuntimeError as exc:
            self._record_failure(stats, "MoneyCode", realm_id, division, doc_number, txn_date, vendor_name, amount, str(exc))
        finally:
            time.sleep(_POST_DELAY_SECONDS)

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

    def _record_duplicate(self, stats: ImportStats, txn_type: str, realm_id: str, division: str, doc_number: str, txn_date: str, entity_name: str, amount: float) -> None:
        stats.skipped_duplicates += 1
        row = {"txn_type": txn_type, "realm_id": realm_id, "division": division, "doc_number": doc_number, "txn_date": txn_date, "entity_name": entity_name, "amount": amount, "status": "duplicate"}
        stats.duplicates.append(row)
        stats.bump_division(division, "duplicate")
        self._audit.record(**row, message="Duplicate detected via composite key.")

    def _record_failure(self, stats: ImportStats, txn_type: str, realm_id: str, division: str, doc_number: str, txn_date: str, entity_name: str, amount: float, message: str) -> None:
        stats.failed += 1
        row = {"txn_type": txn_type, "realm_id": realm_id, "division": division, "doc_number": doc_number, "txn_date": txn_date, "entity_name": entity_name, "amount": amount, "status": "failed", "message": message}
        stats.failures.append(row)
        stats.errors.append(f"{txn_type} {doc_number} @ {realm_id}: {message}")
        stats.bump_division(division, "failed")
        self._audit.record(**row)
