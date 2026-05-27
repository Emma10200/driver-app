from __future__ import annotations

from qbo.duplicate_check import build_invoice_key, build_money_code_key
from qbo.file_loader import FileLoader
from qbo.parsers import DriverStatementParser, MoneyCodeParser
from services.qbo_auth import qbo_allowed_emails


def test_qbo_allowed_emails_from_env(monkeypatch):
    monkeypatch.setenv("QBO_ALLOWED_EMAILS", "accounts@prestige.inc; Emma@Prestige.inc\nowner@example.com")

    assert qbo_allowed_emails() == {
        "accounts@prestige.inc",
        "emma@prestige.inc",
        "owner@example.com",
    }


def test_file_loader_reads_csv_bytes():
    rows = FileLoader().load_rows_from_bytes("sample.csv", b"Doc,Amount\n1001,42.50\n")

    assert rows == [["Doc", "Amount"], ["1001", "42.50"]]


def test_driver_statement_parser_groups_lines_by_check():
    data = [
        ["RefNumber", "Vendor", "TxnDate", "ExpenseAccount", "ExpenseAmount", "Description"],
        ["CHK-1", "Driver A", "2026-05-22", "Fuel", "10.00", "Fuel line"],
        ["CHK-1", "Driver A", "2026-05-22", "Tolls", "5.25", "Toll line"],
    ]

    parsed = DriverStatementParser().parse(
        data,
        target_realm_id="123",
        target_division="Prestig Inc",
        bank_account_name="Main Checking",
    )

    assert parsed["errors"] == []
    assert len(parsed["checks"]) == 1
    check = parsed["checks"][0]
    assert check["DocNumber"] == "CHK-1"
    assert check["_tempVendorName"] == "Driver A"
    assert len(check["Line"]) == 2
    assert check["AccountRef"]["name"] == "Main Checking"


def test_money_code_parser_only_imports_fuel_card_efs_rows():
    data = [
        ["Ref#", "Vendor", "Memo", "Bill Date", "Amount Used", "Expense Account", "CC Account"],
        ["MC-1", "Fuel Vendor", "fuel", "2026-05-22", "100.00", "Truck Fuel", "Fuel Card - EFS"],
        ["MC-2", "Other Vendor", "other", "2026-05-22", "50.00", "Other", "Other Card"],
    ]

    parsed = MoneyCodeParser().parse(data, target_realm_id="123")

    assert parsed["errors"] == []
    assert len(parsed["expenses"]) == 1
    assert parsed["expenses"][0]["DocNumber"] == "MC-1"
    assert "not 'Fuel Card - EFS'" in parsed["warnings"][0]


def test_qbo_duplicate_keys_are_stable():
    assert build_invoice_key("INV-1", "2026-05-22", "42") == "inv-1|2026-05-22|42"
    assert build_money_code_key("MC-1", "2026-05-22", "9", 12, "Fuel", "Truck Fuel") == (
        "creditcard|mc-1|2026-05-22|9|12.00|fuel|truck fuel"
    )
