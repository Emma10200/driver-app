from __future__ import annotations

from qbo.duplicate_check import build_invoice_key, build_money_code_key
from qbo.api_client import QboRateLimitError
from qbo.file_loader import FileLoader
from qbo.import_service import ImportService
from qbo.lookups import EntityLookupService
from qbo.models import ConnectedRealm, PreviewResult
from qbo.parsers import DriverStatementParser, MoneyCodeParser
from services.qbo_driver_statement_preview import (
    _apply_driver_statement_preview_edits,
    _driver_statement_preview_rows_from_drafts,
)
from services.qbo_dashboard import (
    _apply_retry_filter,
    _build_preview,
    _friendly_history_reason,
    _history_display_rows,
    _invoice_customer_refs,
)
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
        if type_name == "Item":
            return "item-1"
        if type_name == "Term":
            return "term-1"
        return "ref-1"


class _NoDuplicateInvoices:
    def preload_invoice_keys(self, realm_id, min_date, max_date):
        return set()

    def invoice_exists(self, doc_number, realm_id):
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
    service = ImportService(fake_qbo, _InvoiceLookup(), _NoDuplicateInvoices(), audit)  # type: ignore[arg-type]

    stats = service.post_invoices([{**_invoice_draft(str(num)), "_realmId": "realm-1"} for num in range(12)])

    assert stats.posted == 12
    assert stats.failed == 0
    assert len(fake_qbo.batch_calls) == 2
    assert [len(call[1]) for call in fake_qbo.batch_calls] == [10, 2]
    assert all(request["operation"] == "create" and "Invoice" in request for _, batch in fake_qbo.batch_calls for request in batch)


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
