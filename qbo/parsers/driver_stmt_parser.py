from __future__ import annotations

from typing import Any

from qbo.utils import most_recent_friday, parse_source_date, safe_string


def _parse_amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = "".join(ch for ch in str(value) if ch.isdigit() or ch in {".", "-"})
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_header(value: Any) -> str:
    text = safe_string(value).lower()
    return "".join(ch for ch in text if ch.isalnum())


def _build_header_index(headers: list[Any]) -> dict[str, int]:
    aliases: dict[str, list[str]] = {
        "ref_number": ["refnumber", "ref", "reference", "checknumber", "checkno", "checkno#", "refnum"],
        "vendor": ["vendor", "vendorname", "payee", "driver", "drivername"],
        "txn_date": ["txndate", "date", "transactiondate", "checkdate", "paydate"],
        "expense_account": ["expenseaccount", "account", "lineaccount", "category"],
        "expense_amount": ["expenseamount", "amount", "lineamount", "total"],
        "expense_description": ["expensedescription", "description", "memo", "notes"],
    }
    normalized = {_normalize_header(h): i for i, h in enumerate(headers) if h is not None}
    index: dict[str, int] = {}
    for key, candidates in aliases.items():
        for alias in candidates:
            if alias in normalized:
                index[key] = normalized[alias]
                break
    return index


class DriverStatementParser:
    def parse(
        self,
        data: list[list[Any]],
        *,
        target_realm_id: str,
        target_division: str,
        bank_account_id: str = "",
        bank_account_name: str = "",
        override_txn_date: str = "",
    ) -> dict[str, Any]:
        if not data or len(data) < 2:
            raise ValueError("Driver statement sheet is empty or missing headers.")
        if not target_realm_id:
            raise ValueError("target_realm_id is required.")

        header_index = _build_header_index(data[0] or [])
        required = ["ref_number", "vendor", "expense_account", "expense_amount"]
        missing = [key for key in required if key not in header_index]
        if missing:
            raise ValueError("Driver statement is missing required column(s): " + ", ".join(missing))

        override = parse_source_date(override_txn_date) if override_txn_date else ""
        fallback_date = most_recent_friday()

        errors: list[str] = []
        warnings: list[str] = []
        skipped: list[dict[str, Any]] = []
        checks_by_key: dict[str, dict[str, Any]] = {}

        for row_index, raw_row in enumerate(data[1:], start=2):
            row = list(raw_row or [])
            if not row or all(cell in (None, "") for cell in row):
                continue

            ref_number = safe_string(row[header_index["ref_number"]] if header_index["ref_number"] < len(row) else "")
            vendor_name = safe_string(row[header_index["vendor"]] if header_index["vendor"] < len(row) else "")
            expense_account = safe_string(
                row[header_index["expense_account"]] if header_index["expense_account"] < len(row) else ""
            )
            expense_amount_raw = row[header_index["expense_amount"]] if header_index["expense_amount"] < len(row) else None
            description = safe_string(
                row[header_index["expense_description"]]
                if header_index.get("expense_description", -1) >= 0 and header_index["expense_description"] < len(row)
                else ""
            )

            amount = _parse_amount(expense_amount_raw)
            if amount is None:
                errors.append(f"Row {row_index}: Invalid ExpenseAmount for Vendor={vendor_name}, Ref={ref_number}")
                continue
            if not vendor_name:
                errors.append(f"Row {row_index}: Vendor is required (Ref={ref_number})")
                continue
            if not expense_account:
                errors.append(f"Row {row_index}: ExpenseAccount is required for Vendor={vendor_name}, Ref={ref_number}")
                continue

            txn_date = override
            if not txn_date and "txn_date" in header_index:
                raw_date = row[header_index["txn_date"]] if header_index["txn_date"] < len(row) else ""
                txn_date = parse_source_date(raw_date)
            if not txn_date:
                txn_date = fallback_date

            key = f"{target_realm_id}|{ref_number}|{vendor_name.strip().lower()}"
            check = checks_by_key.get(key)
            if check is None:
                check = {
                    "DocNumber": ref_number,
                    "TxnDate": txn_date,
                    "PaymentType": "Check",
                    "AccountRef": {"value": bank_account_id or "", "name": bank_account_name or ""},
                    "Line": [],
                    "_tempVendorName": vendor_name,
                    "_realmId": target_realm_id,
                    "_division": target_division or "",
                    "_bankAccountId": bank_account_id or "",
                }
                checks_by_key[key] = check

            check["Line"].append(
                {
                    "Amount": amount,
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "Description": description,
                    "AccountBasedExpenseLineDetail": {"AccountRef": {"name": expense_account}},
                    "_tempAccountName": expense_account,
                }
            )

        if not bank_account_id and not bank_account_name:
            warnings.append("No bank account selected — Checks will fail to post until you pick one.")

        return {"checks": list(checks_by_key.values()), "errors": errors, "warnings": warnings, "skipped_rows": skipped}
