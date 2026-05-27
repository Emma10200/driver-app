from __future__ import annotations

from typing import Any, Iterable

from qbo.company_directory import CompanyDirectory
from qbo.utils import add_days_to_iso_date, normalize_key, parse_optional_date, parse_source_date, safe_string


class InvoiceParser:
    def __init__(self, company_directory: CompanyDirectory) -> None:
        self._company_directory = company_directory

    def parse(self, data: list[list[Any]]) -> dict[str, Any]:
        if not data or len(data) < 2:
            raise ValueError("Invoice sheet is empty or missing headers.")

        header_map = self._build_header_map(data[0] or [])
        rows: list[dict[str, Any]] = []
        skipped_rows: list[dict[str, Any]] = []

        for row_number, row in enumerate(data[1:], start=2):
            if self._is_blank_row(row):
                continue

            parsed_row, skip_reason = self._parse_source_row(row, row_number, header_map)
            if skip_reason:
                skipped_rows.append(skip_reason)
                continue
            if parsed_row:
                rows.append(parsed_row)

        return {"rows": rows, "skipped_rows": skipped_rows, "header_map": header_map}

    def build_qbo_drafts(self, parsed: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._build_qbo_invoice_payload(row) for row in parsed.get("rows") or []]

    def _parse_source_row(
        self,
        row: list[Any],
        row_number: int,
        header_map: dict[str, int],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        doc_number = self._cell_string(row, header_map["doc_number"])
        division_name = self._cell_string(row, header_map["division"])
        customer_name = self._cell_string(row, header_map["customer"])
        broker_load_number = self._cell_string(row, header_map["broker_load_number"])
        txn_date = parse_source_date(self._cell_value(row, header_map["txn_date"]))
        amount = self._parse_amount(self._cell_value(row, header_map["amount"]))
        qb_exported = self._parse_boolean(self._cell_value(row, header_map["qb_exported"]))
        invoice_last_sent_date = parse_optional_date(self._cell_value(row, header_map["invoice_last_sent_date"]))
        invoice_remarks = self._cell_string(row, header_map["invoice_remarks"])
        status = self._cell_string(row, header_map["status"])

        validation_error = self._validate_source_row(
            row_number=row_number,
            doc_number=doc_number,
            customer_name=customer_name,
            txn_date=txn_date,
            amount=amount,
        )
        if validation_error:
            return None, validation_error

        if amount is None:
            return None, {"row_number": row_number, "doc_number": doc_number, "reason": "Invalid or non-positive invoice amount."}

        realm_id = self._company_directory.resolve_realm_id_by_name_loose(division_name) if division_name else ""
        return {
            "row_number": row_number,
            "doc_number": doc_number,
            "division_name": division_name,
            "realm_id": realm_id,
            "customer_name": customer_name,
            "broker_load_number": broker_load_number,
            "txn_date": txn_date,
            "due_date": add_days_to_iso_date(txn_date, 30),
            "amount": float(amount),
            "qb_exported": qb_exported,
            "invoice_last_sent_date": invoice_last_sent_date,
            "invoice_remarks": invoice_remarks,
            "status": status,
        }, None

    @staticmethod
    def _validate_source_row(
        *, row_number: int, doc_number: str, customer_name: str, txn_date: str, amount: float | None
    ) -> dict[str, Any] | None:
        if not doc_number or "#ERROR" in doc_number:
            return {"row_number": row_number, "reason": "Missing or invalid invoice number."}
        if not customer_name:
            return {"row_number": row_number, "doc_number": doc_number, "reason": "Missing customer."}
        if not txn_date:
            return {"row_number": row_number, "doc_number": doc_number, "reason": "Invalid or missing invoice date."}
        if amount is None or amount <= 0:
            return {"row_number": row_number, "doc_number": doc_number, "reason": "Invalid or non-positive invoice amount."}
        return None

    @staticmethod
    def _build_qbo_invoice_payload(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "DocNumber": row["doc_number"],
            "TxnDate": row["txn_date"],
            "DueDate": row["due_date"],
            "_tempTermName": "Net 30",
            "CustomField": [
                {
                    "DefinitionId": "1",
                    "Type": "StringType",
                    "StringValue": row.get("broker_load_number") or "",
                }
            ],
            "PrivateNote": f"Load {row['doc_number']}",
            "Line": [
                {
                    "Amount": row["amount"],
                    "DetailType": "SalesItemLineDetail",
                    "Description": f"Load {row['doc_number']}",
                    "SalesItemLineDetail": {"Qty": 1, "UnitPrice": row["amount"]},
                    "_tempItemName": "Freight Income",
                }
            ],
            "_tempCustomerName": row["customer_name"],
            "_realmId": row.get("realm_id") or None,
            "_division": row.get("division_name") or "",
        }

    def _build_header_map(self, headers: Iterable[Any]) -> dict[str, int]:
        normalized = {normalize_key(value): index for index, value in enumerate(headers) if normalize_key(value)}
        return {
            "doc_number": self._find_header(normalized, ["loadnumber", "loadnum", "invoicenumber", "invoice", "docnumber", "docnum"], True, "Invoice number / load number"),
            "qb_exported": self._find_header(normalized, ["qbexported", "exportedtoqbo", "quickbooksexported"], False, "QB Exported"),
            "division": self._find_header(normalized, ["division", "company", "qbcompany", "quickbookscompany", "ownercompany"], False, "Division / company"),
            "customer": self._find_header(normalized, ["customer", "customername", "billto", "billtocustomer"], True, "Customer"),
            "broker_load_number": self._find_header(normalized, ["brokerloadnumber", "brokerloadnum", "brokerload", "ponumber", "pnumber", "po", "brokerloadno"], False, "Broker load / PO number"),
            "txn_date": self._find_header(normalized, ["date", "invoicedate", "txndate", "transactiondate"], True, "Invoice date"),
            "amount": self._find_header(normalized, ["amount", "invoiceamount", "totalamount", "total"], True, "Amount"),
            "invoice_last_sent_date": self._find_header(normalized, ["invoicelastsentdate", "lastsentdate", "lastsent"], False, "Invoice Last Sent Date"),
            "invoice_remarks": self._find_header(normalized, ["invoiceremarks", "remarks", "notes", "comment"], False, "Invoice Remarks"),
            "status": self._find_header(normalized, ["status", "invoicestatus"], False, "Status"),
        }

    @staticmethod
    def _find_header(index: dict[str, int], aliases: list[str], required: bool, label: str) -> int:
        for alias in aliases:
            if alias in index:
                return index[alias]
        if required:
            raise ValueError(f"Missing required invoice header: {label}. Supported aliases: {', '.join(aliases)}")
        return -1

    @staticmethod
    def _cell_value(row: list[Any], index: int) -> Any:
        if index < 0 or index >= len(row):
            return None
        return row[index]

    @staticmethod
    def _cell_string(row: list[Any], index: int) -> str:
        return safe_string(InvoiceParser._cell_value(row, index))

    @staticmethod
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

    @staticmethod
    def _parse_boolean(value: Any) -> bool | None:
        if value is True or value is False:
            return value
        text = safe_string(value).lower()
        if not text:
            return None
        if text in {"true", "yes", "y", "1"}:
            return True
        if text in {"false", "no", "n", "0"}:
            return False
        return None

    @staticmethod
    def _is_blank_row(row: list[Any]) -> bool:
        return all(value in (None, "") for value in row)
