from __future__ import annotations

from typing import Any, Iterable

from .api_client import QboClient


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _norm_amount(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def build_invoice_key(doc_number: Any, txn_date: Any, customer_id: Any) -> str:
    doc = str(doc_number or "").strip()
    tx = str(txn_date or "").strip()
    cust = str(customer_id or "").strip()
    if not doc or not tx or not cust:
        return ""
    return f"{doc}|{tx}|{cust}".lower()


def build_purchase_key(doc_number: Any, txn_date: Any, vendor_id: Any, payment_type: str) -> str:
    doc = str(doc_number or "").strip()
    tx = str(txn_date or "").strip()
    vendor = str(vendor_id or "").strip()
    pt = str(payment_type or "").strip()
    if not (doc and tx and vendor and pt):
        return ""
    return f"{pt}|{doc}|{tx}|{vendor}".lower()


def build_money_code_key(doc_number: Any, txn_date: Any, vendor_id: Any, amount: Any, memo: Any, expense_account: Any) -> str:
    doc = str(doc_number or "").strip()
    tx = str(txn_date or "").strip()
    vendor = str(vendor_id or "").strip()
    amt = _norm_amount(amount)
    if not (doc and tx and vendor and amt):
        return ""
    return f"creditcard|{doc}|{tx}|{vendor}|{amt}|{_norm_text(memo)}|{_norm_text(expense_account)}".lower()


def expense_memo(record: dict[str, Any]) -> str:
    lines = record.get("Line") or []
    line = lines[0] if lines else {}
    desc = line.get("Description") if isinstance(line, dict) else None
    if desc is None:
        desc = record.get("_memo") or ""
    return _norm_text(desc)


def expense_account_ref(record: dict[str, Any]) -> str:
    lines = record.get("Line") or []
    line = lines[0] if lines else {}
    if not isinstance(line, dict):
        return ""
    detail = line.get("AccountBasedExpenseLineDetail") or {}
    ref = detail.get("AccountRef") or {}
    return _norm_text(ref.get("value") or ref.get("name") or line.get("_tempAccountName"))


def date_range(records: Iterable[dict[str, Any]], realm_id: str) -> tuple[str, str] | None:
    min_d: str | None = None
    max_d: str | None = None
    for rec in records or ():
        if not rec or not rec.get("TxnDate"):
            continue
        rec_realm = str(rec.get("_realmId") or realm_id or "").strip()
        if rec_realm != str(realm_id):
            continue
        d = str(rec["TxnDate"])
        if min_d is None or d < min_d:
            min_d = d
        if max_d is None or d > max_d:
            max_d = d
    return (min_d, max_d) if min_d and max_d else None


class DuplicateChecker:
    def __init__(self, qbo_client: QboClient) -> None:
        self._qbo = qbo_client

    def preload_invoice_keys(self, realm_id: str, min_date: str, max_date: str) -> set[str]:
        return self._preload_keys(
            realm_id=realm_id,
            sql_template="SELECT DocNumber, TxnDate, CustomerRef FROM Invoice WHERE TxnDate >= '{min}' AND TxnDate <= '{max}' STARTPOSITION {start} MAXRESULTS {size}",
            row_key=lambda row: build_invoice_key(row.get("DocNumber"), row.get("TxnDate"), (row.get("CustomerRef") or {}).get("value")),
            response_key="Invoice",
            min_date=min_date,
            max_date=max_date,
        )

    def preload_purchase_keys(self, realm_id: str, payment_type: str, min_date: str, max_date: str) -> set[str]:
        return self._preload_keys(
            realm_id=realm_id,
            sql_template=(
                "SELECT DocNumber, TxnDate, EntityRef FROM Purchase WHERE PaymentType = '" + payment_type + "' "
                "AND TxnDate >= '{min}' AND TxnDate <= '{max}' STARTPOSITION {start} MAXRESULTS {size}"
            ),
            row_key=lambda row: build_purchase_key(row.get("DocNumber"), row.get("TxnDate"), (row.get("EntityRef") or {}).get("value"), payment_type),
            response_key="Purchase",
            min_date=min_date,
            max_date=max_date,
        )

    def preload_money_code_keys(self, realm_id: str, min_date: str, max_date: str) -> set[str]:
        page_size = 1000
        keys: set[str] = set()
        start = 1
        while True:
            sql = (
                "SELECT * FROM Purchase WHERE PaymentType = 'CreditCard' "
                f"AND TxnDate >= '{min_date}' AND TxnDate <= '{max_date}' STARTPOSITION {start} MAXRESULTS {page_size}"
            )
            rows = (self._qbo.query(sql, realm_id=realm_id).get("QueryResponse") or {}).get("Purchase") or []
            if not rows:
                break
            for row in rows:
                key = build_money_code_key(
                    row.get("DocNumber"),
                    row.get("TxnDate"),
                    (row.get("EntityRef") or {}).get("value"),
                    row.get("TotalAmt"),
                    expense_memo(row),
                    expense_account_ref(row),
                )
                if key:
                    keys.add(key)
            if len(rows) < page_size:
                break
            start += page_size
        return keys

    def invoice_exists(self, doc_number: str, realm_id: str) -> bool:
        if not doc_number:
            return False
        safe = doc_number.replace("'", "\\'")
        rows = (self._qbo.query(f"SELECT Id FROM Invoice WHERE DocNumber = '{safe}'", realm_id=realm_id).get("QueryResponse") or {}).get("Invoice") or []
        return bool(rows)

    def check_exists(self, doc_number: str, txn_date: str, vendor_id: str, realm_id: str) -> bool:
        if not (doc_number and txn_date and vendor_id and realm_id):
            return False
        safe_doc = doc_number.replace("'", "\\'")
        safe_tx = txn_date.replace("'", "\\'")
        # EntityRef is not queryable in WHERE; filter vendor client-side.
        sql = "SELECT * FROM Purchase WHERE PaymentType = 'Check' " + f"AND DocNumber = '{safe_doc}' AND TxnDate = '{safe_tx}'"
        rows = (self._qbo.query(sql, realm_id=realm_id).get("QueryResponse") or {}).get("Purchase") or []
        return any((row.get("EntityRef") or {}).get("value") == vendor_id for row in rows)

    def money_code_exists(self, doc_number: str, txn_date: str, vendor_id: str, realm_id: str, amount: Any, memo: Any, expense_account: Any) -> bool:
        if not (doc_number and txn_date and vendor_id and realm_id):
            return False
        safe_doc = doc_number.replace("'", "\\'")
        safe_tx = txn_date.replace("'", "\\'")
        # EntityRef is not queryable in WHERE; filter vendor client-side.
        sql = "SELECT * FROM Purchase WHERE PaymentType = 'CreditCard' " + f"AND DocNumber = '{safe_doc}' AND TxnDate = '{safe_tx}'"
        rows = (self._qbo.query(sql, realm_id=realm_id).get("QueryResponse") or {}).get("Purchase") or []
        rows = [row for row in rows if (row.get("EntityRef") or {}).get("value") == vendor_id]
        target_amount = _norm_amount(amount)
        target_memo = _norm_text(memo)
        target_account = _norm_text(expense_account)
        for existing in rows:
            amount_ok = _norm_amount(existing.get("TotalAmt")) == target_amount if target_amount else True
            memo_ok = (not target_memo) or expense_memo(existing) == target_memo
            account_ok = (not target_account) or expense_account_ref(existing) == target_account
            if amount_ok and memo_ok and account_ok:
                return True
        return False

    def _preload_keys(self, *, realm_id: str, sql_template: str, row_key: Any, response_key: str, min_date: str, max_date: str) -> set[str]:
        page_size = 1000
        keys: set[str] = set()
        start = 1
        while True:
            sql = sql_template.format(min=min_date, max=max_date, start=start, size=page_size)
            rows = (self._qbo.query(sql, realm_id=realm_id).get("QueryResponse") or {}).get(response_key) or []
            if not rows:
                break
            for row in rows:
                key = row_key(row)
                if key:
                    keys.add(key)
            if len(rows) < page_size:
                break
            start += page_size
        return keys
