from __future__ import annotations

import pytest

from qbo.duplicate_check import build_invoice_key, build_money_code_key
from qbo.api_client import QboRateLimitError
from qbo.file_loader import FileLoader
from qbo.import_service import ImportService
from qbo.lookups import EntityLookupService
from qbo.money_code_batches import build_money_code_batch_signature
from qbo.models import ConnectedRealm, PreviewResult
from qbo.parsers import DriverStatementParser, MoneyCodeParser
from services.qbo_driver_statement_preview import (
    _apply_driver_statement_preview_edits,
    _driver_statement_preview_rows_from_drafts,
)
from services.qbo_dashboard import (
    _apply_retry_filter,
    _build_preview,
    _create_missing_vendors,
    _driver_statement_vendor_refs,
    _find_missing_driver_statement_vendors,
    _friendly_history_reason,
    _history_display_rows,
    _invoice_customer_refs,
)
import services.qbo_dashboard as qbo_dashboard
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


def test_driver_statement_preview_edits_update_posted_draft():
    preview = PreviewResult(
        template_type="driver_statements",
        source_file="driver.csv",
        source_hash="abc",
        count=1,
        source_count=1,
        skipped_count=0,
        drafts=[
            {
                "DocNumber": "CHK-1",
                "TxnDate": "2026-05-22",
                "PaymentType": "Check",
                "AccountRef": {"name": "Old Checking", "value": "123"},
                "Line": [
                    {
                        "Amount": 20.0,
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Description": "Original description",
                        "AccountBasedExpenseLineDetail": {
                            "AccountRef": {"name": "Statement Deductions:ELD New", "value": "999"}
                        },
                        "_tempAccountName": "Statement Deductions:ELD New",
                    }
                ],
                "_tempVendorName": "Driver A",
                "_realmId": "realm-1",
                "_division": "Prestig Inc",
                "_bankAccountId": "123",
            }
        ],
    )
    preview.rows = _driver_statement_preview_rows_from_drafts(preview.drafts)
    edited_rows = _driver_statement_preview_rows_from_drafts(preview.drafts, include_edit_keys=True)
    edited_rows[0]["Expense Account"] = "ELD New"
    edited_rows[0]["Line Description"] = "Clean ELD description"
    edited_rows[0]["Line Amount"] = "12.50"

    result = _apply_driver_statement_preview_edits(preview, edited_rows)

    assert result["fields"] >= 3
    assert result["removed"] == 0
    line = preview.drafts[0]["Line"][0]
    account_ref = line["AccountBasedExpenseLineDetail"]["AccountRef"]
    assert line["_tempAccountName"] == "ELD New"
    assert account_ref == {"name": "ELD New"}
    assert line["Description"] == "Clean ELD description"
    assert line["Amount"] == 12.5
    assert preview.rows[0]["Expense Account"] == "ELD New"
    assert preview.rows[0]["Check Total"] == 12.5


def test_driver_statement_preview_multi_line_doc_edit_does_not_revert():
    preview = PreviewResult(
        template_type="driver_statements",
        source_file="driver.csv",
        source_hash="abc",
        count=1,
        source_count=2,
        skipped_count=0,
        drafts=[
            {
                "DocNumber": "CHK-1",
                "TxnDate": "2026-05-22",
                "PaymentType": "Check",
                "AccountRef": {"name": "Checking"},
                "Line": [
                    {
                        "Amount": 20.0,
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Description": "Line 1",
                        "AccountBasedExpenseLineDetail": {"AccountRef": {"name": "Fuel"}},
                        "_tempAccountName": "Fuel",
                    },
                    {
                        "Amount": 10.0,
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Description": "Line 2",
                        "AccountBasedExpenseLineDetail": {"AccountRef": {"name": "Tolls"}},
                        "_tempAccountName": "Tolls",
                    },
                ],
                "_tempVendorName": "Driver A",
                "_realmId": "realm-1",
                "_division": "Prestig Inc",
            }
        ],
    )
    edited_rows = _driver_statement_preview_rows_from_drafts(preview.drafts, include_edit_keys=True)
    edited_rows[0]["Vendor"] = "Driver B"

    _apply_driver_statement_preview_edits(preview, edited_rows)

    assert preview.drafts[0]["_tempVendorName"] == "Driver B"
    assert preview.rows[0]["Vendor"] == "Driver B"
    assert preview.rows[1]["Vendor"] == "Driver B"


def test_driver_statement_preview_deleted_rows_are_removed_from_posted_drafts():
    preview = PreviewResult(
        template_type="driver_statements",
        source_file="driver.csv",
        source_hash="abc",
        count=5,
        source_count=5,
        skipped_count=0,
        drafts=[
            {
                "DocNumber": f"CHK-{idx}",
                "TxnDate": "2026-05-22",
                "PaymentType": "Check",
                "AccountRef": {"name": "Checking"},
                "Line": [
                    {
                        "Amount": float(idx),
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Description": f"Line {idx}",
                        "AccountBasedExpenseLineDetail": {"AccountRef": {"name": "Fuel"}},
                        "_tempAccountName": "Fuel",
                    }
                ],
                "_tempVendorName": f"Driver {idx}",
                "_realmId": "realm-1",
                "_division": "Prestig Inc",
            }
            for idx in range(1, 6)
        ],
    )
    edited_rows = _driver_statement_preview_rows_from_drafts(preview.drafts, include_edit_keys=True)
    edited_rows = [row for row in edited_rows if row["Doc #"] == "CHK-3"]

    result = _apply_driver_statement_preview_edits(preview, edited_rows)

    assert result == {"fields": 0, "removed": 4}
    assert [draft["DocNumber"] for draft in preview.drafts] == ["CHK-3"]
    assert preview.count == 1
    assert preview.rows[0]["Doc #"] == "CHK-3"


def test_driver_statement_preview_unchecked_post_row_is_removed():
    preview = PreviewResult(
        template_type="driver_statements",
        source_file="driver.csv",
        source_hash="abc",
        count=1,
        source_count=2,
        skipped_count=0,
        drafts=[
            {
                "DocNumber": "CHK-1",
                "TxnDate": "2026-05-22",
                "PaymentType": "Check",
                "AccountRef": {"name": "Checking"},
                "Line": [
                    {
                        "Amount": 20.0,
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Description": "Keep",
                        "AccountBasedExpenseLineDetail": {"AccountRef": {"name": "Fuel"}},
                        "_tempAccountName": "Fuel",
                    },
                    {
                        "Amount": 10.0,
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "Description": "Remove",
                        "AccountBasedExpenseLineDetail": {"AccountRef": {"name": "Tolls"}},
                        "_tempAccountName": "Tolls",
                    },
                ],
                "_tempVendorName": "Driver A",
                "_realmId": "realm-1",
                "_division": "Prestig Inc",
            }
        ],
    )
    edited_rows = _driver_statement_preview_rows_from_drafts(preview.drafts, include_edit_keys=True)
    edited_rows[1]["Post?"] = False

    result = _apply_driver_statement_preview_edits(preview, edited_rows)

    assert result == {"fields": 0, "removed": 1}
    assert len(preview.drafts) == 1
    assert len(preview.drafts[0]["Line"]) == 1
    assert preview.drafts[0]["Line"][0]["Description"] == "Keep"
    assert preview.rows[0]["Check Total"] == 20.0


def test_driver_statement_vendor_refs_dedupe_for_selected_company():
    realm = ConnectedRealm(realm_id="realm-1", company_name="Prestig Inc")
    preview = PreviewResult(
        template_type="driver_statements",
        source_file="driver.csv",
        source_hash="abc",
        count=3,
        source_count=3,
        skipped_count=0,
        drafts=[
            {"DocNumber": "CHK-1", "_tempVendorName": "Driver One"},
            {"DocNumber": "CHK-2", "_tempVendorName": "driver one"},
            {"DocNumber": "CHK-3", "_tempVendorName": "Driver Two"},
        ],
    )

    refs = _driver_statement_vendor_refs(preview=preview, target_realm=realm)

    assert refs == [
        {
            "vendor_name": "Driver One",
            "realm_id": "realm-1",
            "target_company": "Prestig Inc",
            "check_count": 2,
        },
        {
            "vendor_name": "Driver Two",
            "realm_id": "realm-1",
            "target_company": "Prestig Inc",
            "check_count": 1,
        },
    ]


def test_find_missing_driver_statement_vendors_checks_qbo_before_post(monkeypatch):
    class _FakeLookup:
        def __init__(self, _qbo):
            pass

        def resolve_entity(self, type_name, name, realm_id):
            assert type_name == "Vendor"
            assert realm_id == "realm-1"
            return "vendor-1" if name == "Existing Driver" else None

    monkeypatch.setattr(qbo_dashboard, "QboClient", lambda auth_service: object())
    monkeypatch.setattr(qbo_dashboard, "EntityLookupService", _FakeLookup)

    realm = ConnectedRealm(realm_id="realm-1", company_name="Prestig Inc")
    preview = PreviewResult(
        template_type="driver_statements",
        source_file="driver.csv",
        source_hash="abc",
        count=2,
        source_count=2,
        skipped_count=0,
        drafts=[
            {"DocNumber": "CHK-1", "_tempVendorName": "Existing Driver"},
            {"DocNumber": "CHK-2", "_tempVendorName": "New Driver"},
        ],
    )

    result = _find_missing_driver_statement_vendors(
        preview=preview,
        target_realm=realm,
        auth_service=object(),
    )

    assert result["checked_count"] == 2
    assert result["found_count"] == 1
    assert result["lookup_errors"] == []
    assert result["missing"] == [
        {
            "vendor_name": "New Driver",
            "realm_id": "realm-1",
            "target_company": "Prestig Inc",
            "check_count": 1,
        }
    ]


def test_create_missing_vendors_posts_vendor_and_returns_new_ids(monkeypatch):
    class _Progress:
        def __init__(self):
            self.updates = []

        def progress(self, value, text=""):
            self.updates.append((value, text))

        def empty(self):
            self.updates.append(("empty", ""))

    class _FakeLookup:
        created = []

        def __init__(self, _qbo):
            pass

        def resolve_entity(self, type_name, name, realm_id):
            assert type_name == "Vendor"
            return None

        def create_entity(self, type_name, display_name, realm_id):
            self.created.append((type_name, display_name, realm_id))
            return f"vendor-{len(self.created)}"

    monkeypatch.setattr(qbo_dashboard, "QboClient", lambda auth_service: object())
    monkeypatch.setattr(qbo_dashboard, "EntityLookupService", _FakeLookup)
    monkeypatch.setattr(qbo_dashboard.st, "progress", lambda *args, **kwargs: _Progress())

    created, failed = _create_missing_vendors(
        [
            {
                "vendor_name": "New Driver",
                "realm_id": "realm-1",
                "target_company": "Prestig Inc",
                "check_count": 2,
            }
        ],
        auth_service=object(),
    )

    assert failed == []
    assert created == [
        {
            "vendor_name": "New Driver",
            "realm_id": "realm-1",
            "target_company": "Prestig Inc",
            "check_count": 2,
            "qbo_id": "vendor-1",
        }
    ]
    assert _FakeLookup.created == [("Vendor", "New Driver", "realm-1")]


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


def test_money_code_batch_signature_is_order_independent_and_preserves_splits():
    one_thousand = build_money_code_batch_signature([_money_code_draft("MC-1", 1000.0)], "realm-1")
    split_a = build_money_code_batch_signature(
        [_money_code_draft("MC-1", 500.0), _money_code_draft("MC-1", 500.0)],
        "realm-1",
    )
    split_b = build_money_code_batch_signature(
        [_money_code_draft("mc-1", 500.0), _money_code_draft("MC-1", 500)],
        "realm-1",
    )

    assert split_a["fingerprint"] == split_b["fingerprint"]
    assert split_a["entries"] == [{"code": "MC-1", "amount": "500.00"}, {"code": "MC-1", "amount": "500.00"}]
    assert split_a["fingerprint"] != one_thousand["fingerprint"]


def test_invoice_customer_refs_route_by_division_without_fallback():
    prestige = ConnectedRealm(realm_id="pt-realm", company_name="Prestige Transportation Inc")
    xpress = ConnectedRealm(realm_id="xpress-realm", company_name="Xpress Trans Inc")
    preview = PreviewResult(
        template_type="invoices",
        source_file="invoices.csv",
        source_hash="abc123",
        count=3,
        source_count=3,
        skipped_count=0,
        drafts=[
            {
                "DocNumber": "158591",
                "_tempCustomerName": "TGR Logistics - PT",
                "_realmId": "xpress-realm",
                "_division": "Xpress Trans Inc",
            },
            {
                "DocNumber": "158592",
                "_tempCustomerName": "TGR Logistics - PT",
                "_realmId": "xpress-realm",
                "_division": "Xpress Trans Inc",
            },
            {
                "DocNumber": "158593",
                "_tempCustomerName": "Unmatched Customer",
                "_realmId": None,
                "_division": "",
            },
        ],
    )

    refs = _invoice_customer_refs(preview=preview, realms=[prestige, xpress])

    by_customer = {row["customer_name"]: row for row in refs}
    assert by_customer["TGR Logistics - PT"]["realm_id"] == "xpress-realm"
    assert by_customer["TGR Logistics - PT"]["target_company"] == "Xpress Trans Inc"
    assert by_customer["TGR Logistics - PT"]["invoice_count"] == 2
    assert "Unmatched Customer" not in by_customer


def test_invoice_preview_includes_full_qbo_ready_fields():
    realm = ConnectedRealm(realm_id="123", company_name="Prestig Inc")
    content = (
        "LoadNumber,Division,Customer,Broker Load Number,Date,Amount,QB Exported,"
        "Invoice Last Sent Date,Invoice Remarks,Status\n"
        "158591,Prestig Inc,TGR Logistics - PT,PO-77,2026-05-22,1250.50,true,"
        "2026-05-23,Customer asked for POD,Ready\n"
    ).encode()

    preview = _build_preview(
        template_key="invoices",
        file_name="invoices.csv",
        content=content,
        realms=[realm],
        selected_realm=None,
        bank_account_name="",
        override_date="",
    )

    row = preview.rows[0]
    assert row["QBO Txn Type"] == "Invoice"
    assert row["PO / Broker Load #"] == "PO-77"
    assert row["QBO Terms"] == "Net 30"
    assert row["QBO Item"] == "Freight Income"
    assert row["Line Qty"] == 1
    assert row["Line Rate"] == 1250.50
    assert row["Custom Field Value"] == "PO-77"
    assert row["Invoice Remarks"] == "Customer asked for POD"


def test_qbo_invoice_import_format_requires_selected_company():
    content = (
        "RefNumber,Customer,TxnDate,DueDate,BillAddrLine1,BillAddrLine2,BillAddrLineCity,"
        "BillAddrLineState,BillAddrLinePostalCode,LineItem,LineQty,LineDesc,LineUnitPrice,LineAmount\n"
        "159350,Kiswani Trucking Inc,2026-06-26 11:31:01,2026-07-26 11:31:01,555 W Taft Drive,,"
        "South Holland,IL,60473,Freight Income,1,2570.09,2570.09,2570.09\n"
    ).encode()

    with pytest.raises(ValueError, match="Choose a QuickBooks company"):
        _build_preview(
            template_key="invoices",
            file_name="qbo_invoice_import.csv",
            content=content,
            realms=[ConnectedRealm(realm_id="pt-realm", company_name="Prestige Transportation Inc")],
            selected_realm=None,
            bank_account_name="",
            override_date="",
        )


def test_qbo_invoice_import_format_uses_selected_company_and_groups_lines():
    realm = ConnectedRealm(realm_id="pt-realm", company_name="Prestige Transportation Inc")
    content = (
        "RefNumber,Customer,PONumber,TxnDate,DueDate,BillAddrLine1,BillAddrLine2,BillAddrLineCity,"
        "BillAddrLineState,BillAddrLinePostalCode,LineItem,LineQty,LineDesc,LineUnitPrice,LineAmount\n"
        "159350,Kiswani Trucking Inc,PO-77,2026-06-26 11:31:01,2026-07-26 11:31:01,555 W Taft Drive,,"
        "South Holland,IL,60473,Freight Income,1,2000,2000,2000\n"
        "159350,Kiswani Trucking Inc,PO-77,2026-06-26 11:31:01,2026-07-26 11:31:01,555 W Taft Drive,,"
        "South Holland,IL,60473,Accessorial Charges,1,570.09,570.09,570.09\n"
    ).encode()

    preview = _build_preview(
        template_key="invoices",
        file_name="qbo_invoice_import.csv",
        content=content,
        realms=[realm],
        selected_realm=realm,
        bank_account_name="",
        override_date="",
    )

    assert preview.errors == []
    assert preview.count == 1
    assert len(preview.drafts) == 1
    draft = preview.drafts[0]
    assert draft["DocNumber"] == "159350"
    assert draft["TxnDate"] == "2026-06-26"
    assert draft["DueDate"] == "2026-07-26"
    assert draft["_realmId"] == "pt-realm"
    assert draft["_division"] == "Prestige Transportation Inc"
    assert draft["_tempCustomerName"] == "Kiswani Trucking Inc"
    assert draft["CustomField"][0]["StringValue"] == "PO-77"
    assert [line["_tempItemName"] for line in draft["Line"]] == ["Freight Income", "Accessorial Charges"]
    assert [line["Description"] for line in draft["Line"]] == ["Load 159350", "Load 159350"]
    assert sum(float(line["Amount"]) for line in draft["Line"]) == 2570.09
    assert len(preview.rows) == 2
    assert preview.rows[0]["QBO Company"] == "Prestige Transportation Inc"
    assert preview.rows[0]["Division"] == "Prestige Transportation Inc"
    assert preview.rows[0]["PO / Broker Load #"] == "PO-77"
    assert preview.rows[0]["Custom Field Value"] == "PO-77"
    assert preview.rows[0]["Line Description"] == "Load 159350"
    assert preview.rows[0]["Invoice Amount"] == 2570.09
    assert preview.rows[1]["QBO Item"] == "Accessorial Charges"


def test_entity_lookup_create_customer_posts_display_name_and_primes_cache():
    class _Response:
        def json(self):
            return {"Customer": {"Id": "123"}}

    class _FakeQbo:
        def __init__(self):
            self.calls = []

        def post(self, path, *, realm_id, payload):
            self.calls.append((path, realm_id, payload))
            return _Response()

    fake_qbo = _FakeQbo()
    lookups = EntityLookupService(fake_qbo)  # type: ignore[arg-type]

    assert lookups.create_entity("Customer", "TGR Logistics - PT", "realm-1") == "123"
    assert fake_qbo.calls == [("/customer", "realm-1", {"DisplayName": "TGR Logistics - PT"})]
    assert lookups.resolve_entity("Customer", "TGR Logistics - PT", "realm-1") == "123"


def test_history_reason_is_friendly_and_hides_email_column():
    row = {
        "created_at": "2026-05-27T10:00:00Z",
        "imported_by_email": "someone@example.com",
        "txn_type": "Invoice",
        "status": "failed",
        "message": "Customer 'TGR Logistics - PT' not found in QBO.",
        "doc_number": "158591",
        "entity_name": "TGR Logistics - PT",
    }

    assert _friendly_history_reason(row) == "Missing customer in QuickBooks: TGR Logistics - PT. Retry after creating the customer."
    display = _history_display_rows([row])[0]
    assert "imported_by_email" not in display
    assert "someone@example.com" not in str(display)
    assert display["Reason"].startswith("Missing customer")


def test_retry_filter_keeps_only_selected_failed_docs():
    preview = PreviewResult(
        template_type="invoices",
        source_file="invoices.csv",
        source_hash="abc",
        count=2,
        source_count=2,
        skipped_count=0,
        rows=[{"Doc #": "100"}, {"Doc #": "200"}],
        drafts=[{"DocNumber": "100"}, {"DocNumber": "200"}],
    )

    filtered = _apply_retry_filter(preview, {"doc_numbers": ["200"]})

    assert filtered.count == 1
    assert filtered.rows == [{"Doc #": "200"}]
    assert filtered.drafts == [{"DocNumber": "200"}]


def _invoice_draft(doc_number: str) -> dict[str, object]:
    return {
        "DocNumber": doc_number,
        "TxnDate": "2026-05-22",
        "_division": "Prestig Inc",
        "_tempCustomerName": "TGR Logistics - PT",
        "Line": [{"Amount": 100.0, "DetailType": "SalesItemLineDetail"}],
    }


class _NoopAudit:
    def __init__(self):
        self.rows = []

    def record(self, **row):
        self.rows.append(row)


class _InvoiceLookup:
    def resolve_entity(self, type_name, name, realm_id):
        if type_name == "Customer":
            return "cust-1"
        if type_name == "Vendor":
            return "vendor-1"
        if type_name == "Item":
            return "item-1"
        if type_name == "Term":
            return "term-1"
        return "ref-1"

    def resolve_account(self, account_name, realm_id):
        return "acct-1"


class _NoDuplicateInvoices:
    def __init__(self):
        self.invoice_exists_calls = 0
        self.check_exists_calls = 0
        self.money_code_exists_calls = 0

    def preload_invoice_keys(self, realm_id, min_date, max_date):
        return set()

    def preload_purchase_keys(self, realm_id, payment_type, min_date, max_date):
        return set()

    def preload_money_code_keys(self, realm_id, min_date, max_date):
        return set()

    def invoice_exists(self, doc_number, realm_id):
        self.invoice_exists_calls += 1
        return False

    def check_exists(self, doc_number, txn_date, vendor_id, realm_id):
        self.check_exists_calls += 1
        return False

    def money_code_exists(self, doc_number, txn_date, vendor_id, realm_id, amount, memo, expense_account_name):
        self.money_code_exists_calls += 1
        return False


def test_invoice_import_uses_qbo_batch_chunks_of_ten(monkeypatch):
    class _BatchQbo:
        def __init__(self):
            self.batch_calls = []

        def batch(self, *, realm_id, requests):
            self.batch_calls.append((realm_id, requests))
            return {
                "BatchItemResponse": [
                    {"bId": request["bId"], "Invoice": {"Id": f"qbo-{request['bId']}"}}
                    for request in requests
                ]
            }

    fake_qbo = _BatchQbo()
    audit = _NoopAudit()
    dup = _NoDuplicateInvoices()
    service = ImportService(fake_qbo, _InvoiceLookup(), dup, audit)  # type: ignore[arg-type]

    stats = service.post_invoices([{**_invoice_draft(str(num)), "_realmId": "realm-1"} for num in range(12)])

    assert stats.posted == 12
    assert stats.failed == 0
    assert len(fake_qbo.batch_calls) == 2
    assert [len(call[1]) for call in fake_qbo.batch_calls] == [10, 2]
    assert all(request["operation"] == "create" and "Invoice" in request for _, batch in fake_qbo.batch_calls for request in batch)
    assert dup.invoice_exists_calls == 0


def test_invoice_import_holds_rate_limited_batch_rows_for_retry(monkeypatch):
    monkeypatch.setattr("qbo.import_service.time.sleep", lambda *_args, **_kwargs: None)

    class _RateLimitedQbo:
        def batch(self, *, realm_id, requests):
            raise QboRateLimitError(
                "QBO POST /batch failed: HTTP 429 throttled",
                status_code=429,
                response_text="throttled",
                retry_after_seconds=60,
            )

    audit = _NoopAudit()
    service = ImportService(_RateLimitedQbo(), _InvoiceLookup(), _NoDuplicateInvoices(), audit)  # type: ignore[arg-type]

    stats = service.post_invoices([{**_invoice_draft(str(num)), "_realmId": "realm-1"} for num in range(3)])

    assert stats.posted == 0
    assert stats.failed == 3
    assert stats.held_for_retry == 3
    assert len(stats.failures) == 3
    assert all(row["retryable"] is True for row in stats.failures)
    assert all("rate limit" in row["message"].lower() for row in stats.failures)
    assert len(audit.rows) == 3
    assert all("retryable" not in row for row in audit.rows)


def _check_draft(doc_number: str) -> dict[str, object]:
    return {
        "DocNumber": doc_number,
        "TxnDate": "2026-05-22",
        "_division": "Prestig Inc",
        "_tempVendorName": "Driver One",
        "AccountRef": {"name": "Main Checking"},
        "Line": [
            {
                "Amount": 100.0,
                "DetailType": "AccountBasedExpenseLineDetail",
                "_tempAccountName": "Driver Pay",
            }
        ],
    }


def _money_code_draft(doc_number: str, amount: float = 50.0) -> dict[str, object]:
    return {
        "DocNumber": doc_number,
        "TxnDate": "2026-05-22",
        "_division": "Prestig Inc",
        "_tempVendorName": "Fuel Vendor",
        "_tempCcAccountName": "Fuel Card - EFS",
        "_memo": "fuel",
        "Line": [
            {
                "Amount": amount,
                "DetailType": "AccountBasedExpenseLineDetail",
                "_tempAccountName": "Truck Fuel",
            }
        ],
    }


def test_check_import_uses_qbo_batch_and_reports_progress():
    class _BatchQbo:
        def __init__(self):
            self.batch_calls = []

        def batch(self, *, realm_id, requests):
            self.batch_calls.append((realm_id, requests))
            return {
                "BatchItemResponse": [
                    {"bId": request["bId"], "Purchase": {"Id": f"qbo-{request['bId']}"}}
                    for request in requests
                ]
            }

    fake_qbo = _BatchQbo()
    dup = _NoDuplicateInvoices()
    progress = []
    service = ImportService(fake_qbo, _InvoiceLookup(), dup, _NoopAudit(), progress_callback=lambda *args: progress.append(args))  # type: ignore[arg-type]

    stats = service.post_checks([_check_draft(str(num)) for num in range(11)], target_realm_id="realm-1")

    assert stats.posted == 11
    assert stats.failed == 0
    assert [len(call[1]) for call in fake_qbo.batch_calls] == [10, 1]
    assert all("Purchase" in request and request["Purchase"]["PaymentType"] == "Check" for _, batch in fake_qbo.batch_calls for request in batch)
    assert dup.check_exists_calls == 0
    assert progress[-1][0:2] == (11, 11)
    assert "11/11" in progress[-1][2]


def test_money_code_import_uses_qbo_batch_purchase_create():
    class _BatchQbo:
        def __init__(self):
            self.batch_calls = []

        def batch(self, *, realm_id, requests):
            self.batch_calls.append((realm_id, requests))
            return {
                "BatchItemResponse": [
                    {"bId": request["bId"], "Purchase": {"Id": f"qbo-{request['bId']}"}}
                    for request in requests
                ]
            }

    fake_qbo = _BatchQbo()
    dup = _NoDuplicateInvoices()
    service = ImportService(fake_qbo, _InvoiceLookup(), dup, _NoopAudit())  # type: ignore[arg-type]

    stats = service.post_money_codes([_money_code_draft(str(num)) for num in range(3)], target_realm_id="realm-1")

    assert stats.posted == 3
    assert stats.failed == 0
    assert len(fake_qbo.batch_calls) == 1
    assert all("Purchase" in request and request["Purchase"]["PaymentType"] == "CreditCard" for _, batch in fake_qbo.batch_calls for request in batch)
    assert dup.money_code_exists_calls == 0


class _MoneyCodeBatchAudit(_NoopAudit):
    def __init__(self, existing=None):
        super().__init__()
        self.existing = existing
        self.claims = []
        self.updates = []

    def claim_money_code_batch_signature(self, signature):
        self.claims.append(signature)
        if self.existing:
            return False, self.existing
        return True, None

    def update_money_code_batch_signature(self, signature, **kwargs):
        self.updates.append({"signature": signature, **kwargs})


def test_money_code_exact_batch_duplicate_rejects_before_qbo_post():
    class _BatchQbo:
        def __init__(self):
            self.batch_calls = []

        def batch(self, *, realm_id, requests):
            self.batch_calls.append((realm_id, requests))
            raise AssertionError("duplicate money-code batch should not post to QBO")

    existing = {
        "created_at": "2026-05-28T12:30:00Z",
        "source_file_name": "money-codes.csv",
        "status": "complete",
    }
    fake_qbo = _BatchQbo()
    audit = _MoneyCodeBatchAudit(existing=existing)
    service = ImportService(fake_qbo, _InvoiceLookup(), _NoDuplicateInvoices(), audit)  # type: ignore[arg-type]

    stats = service.post_money_codes(
        [_money_code_draft("MC-1", 500.0), _money_code_draft("MC-1", 500.0)],
        target_realm_id="realm-1",
    )

    assert stats.posted == 0
    assert stats.failed == 0
    assert stats.skipped_duplicates == 2
    assert not fake_qbo.batch_calls
    assert "already imported" in stats.warnings[0]
    assert "money-codes.csv" in stats.warnings[0]
    assert len(audit.rows) == 2
    assert all(row["status"] == "duplicate" for row in audit.rows)
    assert not audit.updates


def test_money_code_successful_import_records_complete_batch_signature():
    class _BatchQbo:
        def batch(self, *, realm_id, requests):
            return {
                "BatchItemResponse": [
                    {"bId": request["bId"], "Purchase": {"Id": f"qbo-{request['bId']}"}}
                    for request in requests
                ]
            }

    audit = _MoneyCodeBatchAudit()
    service = ImportService(_BatchQbo(), _InvoiceLookup(), _NoDuplicateInvoices(), audit)  # type: ignore[arg-type]

    stats = service.post_money_codes(
        [_money_code_draft("MC-2", 200.0), _money_code_draft("MC-1", 100.0)],
        target_realm_id="realm-1",
    )

    assert stats.posted == 2
    assert stats.failed == 0
    assert audit.claims[0]["entries"] == [{"code": "MC-1", "amount": "100.00"}, {"code": "MC-2", "amount": "200.00"}]
    assert audit.updates[-1]["status"] == "complete"
    assert audit.updates[-1]["posted_count"] == 2
    assert audit.updates[-1]["failed_count"] == 0


def test_money_code_partial_import_records_partial_batch_signature(monkeypatch):
    monkeypatch.setattr("qbo.import_service.time.sleep", lambda *_args, **_kwargs: None)

    class _PartiallyRateLimitedQbo:
        def __init__(self):
            self.calls = 0

        def batch(self, *, realm_id, requests):
            self.calls += 1
            if self.calls == 2:
                raise QboRateLimitError(
                    "QBO POST /batch failed: HTTP 429 throttled",
                    status_code=429,
                    response_text="throttled",
                    retry_after_seconds=60,
                )
            return {
                "BatchItemResponse": [
                    {"bId": request["bId"], "Purchase": {"Id": f"qbo-{request['bId']}"}}
                    for request in requests
                ]
            }

    audit = _MoneyCodeBatchAudit()
    service = ImportService(_PartiallyRateLimitedQbo(), _InvoiceLookup(), _NoDuplicateInvoices(), audit)  # type: ignore[arg-type]

    stats = service.post_money_codes([_money_code_draft(str(num), 50.0) for num in range(11)], target_realm_id="realm-1")

    assert stats.posted == 10
    assert stats.failed == 1
    assert stats.held_for_retry == 1
    assert audit.updates[-1]["status"] == "partial"
    assert audit.updates[-1]["posted_count"] == 10
    assert audit.updates[-1]["failed_count"] == 1
