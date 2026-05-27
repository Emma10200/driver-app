from __future__ import annotations

from typing import Any

from qbo.utils import parse_source_date, safe_string


_FUEL_CARD_ACCOUNT = "fuelcardefs"


def _normalize_header_text(value: Any) -> str:
    text = safe_string(value).lower()
    return "".join(ch for ch in text if ch.isalnum() or ch == "#")


def _normalize_match(value: Any) -> str:
    text = safe_string(value).lower()
    return "".join(ch for ch in text if ch.isalnum())


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


def _find_header_row(data: list[list[Any]]) -> dict[str, int] | None:
    for i in range(min(len(data), 15)):
        row = data[i] or []
        normalized = [_normalize_header_text(c) for c in row]
        idx = {
            "ref_number": normalized.index("ref#") if "ref#" in normalized else -1,
            "vendor": normalized.index("vendor") if "vendor" in normalized else -1,
            "memo": normalized.index("memo") if "memo" in normalized else -1,
            "bill_date": normalized.index("billdate") if "billdate" in normalized else -1,
            "amount_used": normalized.index("amountused") if "amountused" in normalized else -1,
            "expense_account": normalized.index("expenseaccount") if "expenseaccount" in normalized else -1,
            "cc_account": normalized.index("ccaccount") if "ccaccount" in normalized else -1,
        }
        required = ("ref_number", "vendor", "bill_date", "amount_used", "expense_account", "cc_account")
        if all(idx[key] >= 0 for key in required):
            return {"_header_row_index": i, **idx}
    return None


class MoneyCodeParser:
    def parse(self, data: list[list[Any]], *, target_realm_id: str) -> dict[str, Any]:
        if not data or len(data) < 2:
            raise ValueError("Money Codes sheet is empty or missing rows.")
        if not target_realm_id:
            raise ValueError("target_realm_id is required for Money Codes import.")

        header_info = _find_header_row(data)
        if not header_info:
            raise ValueError(
                "Money Codes header row not found. Required headers: Ref#, Vendor, Bill Date, "
                "Amount Used, Expense Account, CC Account."
            )

        header_row_index = header_info["_header_row_index"]
        rows = data[header_row_index + 1:]

        expenses: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []

        for offset, raw_row in enumerate(rows):
            row_num = header_row_index + 2 + offset
            row = list(raw_row or [])

            def cell(name: str, _row: list[Any] = row) -> Any:
                col = header_info.get(name, -1)
                return _row[col] if 0 <= col < len(_row) else ""

            ref_number = safe_string(cell("ref_number"))
            vendor_name = safe_string(cell("vendor"))
            memo = safe_string(cell("memo"))
            bill_date_raw = cell("bill_date")
            amount_raw = cell("amount_used")
            expense_account = safe_string(cell("expense_account"))
            cc_account = safe_string(cell("cc_account"))

            if not any((ref_number, vendor_name, bill_date_raw, amount_raw, expense_account, cc_account)):
                continue
            if not vendor_name:
                errors.append(f"Row {row_num}: Vendor is required.")
                continue
            if not ref_number:
                errors.append(f"Row {row_num}: Ref# is required.")
                continue
            if not bill_date_raw:
                errors.append(f"Row {row_num}: Bill Date is required.")
                continue

            txn_date = parse_source_date(bill_date_raw)
            if not txn_date:
                errors.append(f"Row {row_num}: Invalid Bill Date.")
                continue

            amount = _parse_amount(amount_raw)
            if amount is None or amount <= 0:
                errors.append(f"Row {row_num}: Amount Used must be a positive number.")
                continue
            if not expense_account:
                errors.append(f"Row {row_num}: Expense Account is required.")
                continue
            if not cc_account:
                errors.append(f"Row {row_num}: CC Account is required.")
                continue

            if _normalize_match(cc_account) != _FUEL_CARD_ACCOUNT:
                warnings.append(f"Row {row_num}: Skipped because CC Account is not 'Fuel Card - EFS' ({cc_account}).")
                continue

            expenses.append(
                {
                    "DocNumber": ref_number,
                    "TxnDate": txn_date,
                    "PaymentType": "CreditCard",
                    "_tempVendorName": vendor_name,
                    "_tempCcAccountName": cc_account,
                    "_realmId": target_realm_id,
                    "_memo": memo,
                    "Line": [
                        {
                            "Amount": amount,
                            "DetailType": "AccountBasedExpenseLineDetail",
                            "Description": memo,
                            "AccountBasedExpenseLineDetail": {"AccountRef": {"name": expense_account}},
                            "_tempAccountName": expense_account,
                        }
                    ],
                }
            )

        return {"expenses": expenses, "errors": errors, "warnings": warnings}
