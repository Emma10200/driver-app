from __future__ import annotations

from typing import Any, Iterable

from qbo.company_directory import CompanyDirectory
from qbo.utils import add_days_to_iso_date, normalize_key, parse_optional_date, parse_source_date, safe_string


_QBO_IMPORT_FORMAT = "qbo_import"
_DIVISION_EXPORT_FORMAT = "division_export"


class InvoiceParser:
    def __init__(self, company_directory: CompanyDirectory) -> None:
        self._company_directory = company_directory

    def parse(
        self,
        data: list[list[Any]],
        *,
        target_realm_id: str = "",
        target_division: str = "",
    ) -> dict[str, Any]:
        if not data or len(data) < 2:
            raise ValueError("Invoice sheet is empty or missing headers.")

        header_index = self._detect_header_row(data)
        header_row = data[header_index] or []
        source_format = self.detect_source_format_from_headers(header_row)
        header_map = self._build_header_map(header_row)
        rows: list[dict[str, Any]] = []
        skipped_rows: list[dict[str, Any]] = []

        for row_number, row in enumerate(data[header_index + 1:], start=header_index + 2):
            if self._is_blank_row(row):
                continue

            parsed_row, skip_reason = self._parse_source_row(
                row,
                row_number,
                header_map,
                source_format=source_format,
                target_realm_id=target_realm_id,
                target_division=target_division,
            )
            if skip_reason:
                skipped_rows.append(skip_reason)
                continue
            if parsed_row:
                rows.append(parsed_row)

        return {
            "rows": rows,
            "skipped_rows": skipped_rows,
            "header_map": header_map,
            "header_index": header_index,
            "source_count": max(len(data) - header_index - 1, 0),
            "source_format": source_format,
            "requires_target_realm": source_format == _QBO_IMPORT_FORMAT and header_map.get("division", -1) < 0,
        }

    @classmethod
    def inspect_source(cls, data: list[list[Any]]) -> dict[str, Any]:
        if not data:
            return {"source_format": _DIVISION_EXPORT_FORMAT, "requires_target_realm": False, "header_index": 0}
        header_index = cls._detect_header_row(data)
        header_row = data[header_index] or []
        source_format = cls.detect_source_format_from_headers(header_row)
        normalized = {normalize_key(value) for value in header_row if normalize_key(value)}
        has_division = bool(normalized & {"division", "company", "qbcompany", "quickbookscompany", "ownercompany", "branch", "office"})
        return {
            "source_format": source_format,
            "requires_target_realm": source_format == _QBO_IMPORT_FORMAT and not has_division,
            "header_index": header_index,
        }

    @classmethod
    def detect_source_format_from_headers(cls, headers: Iterable[Any]) -> str:
        normalized = {normalize_key(value) for value in headers if normalize_key(value)}
        qbo_markers = {"refnumber", "lineitem", "lineqty", "linedesc", "lineunitprice", "lineamount"}
        if "refnumber" in normalized and len(normalized & qbo_markers) >= 3:
            return _QBO_IMPORT_FORMAT
        return _DIVISION_EXPORT_FORMAT

    @classmethod
    def _detect_header_row(cls, data: list[list[Any]]) -> int:
        # Scan the first 20 rows for the first row that exposes every required header
        # alias. This lets ERP exports include title/preamble rows above the headers.
        scan_limit = min(len(data) - 1, 20)
        for index in range(scan_limit + 1):
            row = data[index] or []
            normalized = {normalize_key(value) for value in row}
            if all(any(alias in normalized for alias in aliases) for aliases in cls._REQUIRED_ALIASES):
                return index
        return 0

    _REQUIRED_ALIASES: tuple[tuple[str, ...], ...] = (
        ("loadnumber", "loadnum", "invoicenumber", "invoice", "docnumber", "docnum", "invoiceno", "invoicenbr", "refnumber", "refnum", "refno"),
        ("customer", "customername", "billto", "billtocustomer", "billtoname", "client", "clientname"),
        ("date", "invoicedate", "txndate", "transactiondate", "invdate"),
        ("amount", "invoiceamount", "totalamount", "total", "invoicetotal", "invtotal", "grandtotal", "lineamount"),
    )

    def build_qbo_drafts(self, parsed: dict[str, Any]) -> list[dict[str, Any]]:
        rows = list(parsed.get("rows") or [])
        if parsed.get("source_format") == _QBO_IMPORT_FORMAT:
            return self._build_qbo_import_payloads(rows)
        return [self._build_qbo_invoice_payload(row) for row in rows]

    def _parse_source_row(
        self,
        row: list[Any],
        row_number: int,
        header_map: dict[str, int],
        *,
        source_format: str,
        target_realm_id: str,
        target_division: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        doc_number = self._cell_string(row, header_map["doc_number"])
        source_division_name = self._cell_string(row, header_map["division"])
        division_name = source_division_name or (target_division if source_format == _QBO_IMPORT_FORMAT else "")
        customer_name = self._cell_string(row, header_map["customer"])
        broker_load_number = self._cell_string(row, header_map["broker_load_number"])
        txn_date = parse_source_date(self._cell_value(row, header_map["txn_date"]))
        source_due_date = parse_optional_date(self._cell_value(row, header_map["due_date"]))
        amount = self._parse_amount(self._cell_value(row, header_map["amount"]))
        qb_exported = self._parse_boolean(self._cell_value(row, header_map["qb_exported"]))
        invoice_last_sent_date = parse_optional_date(self._cell_value(row, header_map["invoice_last_sent_date"]))
        invoice_remarks = self._cell_string(row, header_map["invoice_remarks"])
        status = self._cell_string(row, header_map["status"])
        line_item = self._cell_string(row, header_map["line_item"]) or "Freight Income"
        line_qty = self._parse_amount(self._cell_value(row, header_map["line_qty"])) or 1.0
        line_description = self._cell_string(row, header_map["line_description"])
        line_unit_price = self._parse_amount(self._cell_value(row, header_map["line_unit_price"]))

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

        if source_format == _QBO_IMPORT_FORMAT and target_realm_id:
            realm_id = target_realm_id
        else:
            realm_id = self._company_directory.resolve_realm_id_by_name_loose(division_name) if division_name else ""
        return {
            "row_number": row_number,
            "doc_number": doc_number,
            "division_name": division_name,
            "source_division_name": source_division_name,
            "realm_id": realm_id,
            "customer_name": customer_name,
            "broker_load_number": broker_load_number,
            "txn_date": txn_date,
            "due_date": source_due_date or add_days_to_iso_date(txn_date, 30),
            "amount": float(amount),
            "line_item": line_item,
            "line_qty": float(line_qty),
            "line_unit_price": float(line_unit_price if line_unit_price is not None else amount),
            "line_description": line_description or f"Load {doc_number}",
            "qb_exported": qb_exported,
            "invoice_last_sent_date": invoice_last_sent_date,
            "invoice_remarks": invoice_remarks,
            "status": status,
            "source_format": source_format,
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
        line_amount = float(row["amount"])
        line_qty = float(row.get("line_qty") or 1)
        line_unit_price = float(row.get("line_unit_price") or line_amount)
        line_item = row.get("line_item") or "Freight Income"
        line_description = row.get("line_description") or f"Load {row['doc_number']}"
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
                    "Amount": line_amount,
                    "DetailType": "SalesItemLineDetail",
                    "Description": line_description,
                    "SalesItemLineDetail": {"Qty": line_qty, "UnitPrice": line_unit_price},
                    "_tempItemName": line_item,
                }
            ],
            "_tempCustomerName": row["customer_name"],
            "_realmId": row.get("realm_id") or None,
            "_division": row.get("division_name") or "",
        }

    @classmethod
    def _build_qbo_import_payloads(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str, str, str]] = []
        for row in rows:
            key = (
                str(row.get("realm_id") or ""),
                str(row.get("doc_number") or ""),
                str(row.get("customer_name") or ""),
                str(row.get("txn_date") or ""),
                str(row.get("due_date") or ""),
            )
            if key not in grouped:
                grouped[key] = {**row, "_source_lines": []}
                order.append(key)
            grouped[key]["_source_lines"].append(row)

        drafts: list[dict[str, Any]] = []
        for key in order:
            base = grouped[key]
            lines = []
            total = 0.0
            for line_row in base.get("_source_lines") or []:
                amount = float(line_row.get("amount") or 0.0)
                total += amount
                lines.append(
                    {
                        "Amount": amount,
                        "DetailType": "SalesItemLineDetail",
                        "Description": line_row.get("line_description") or f"Load {line_row.get('doc_number') or ''}",
                        "SalesItemLineDetail": {
                            "Qty": float(line_row.get("line_qty") or 1),
                            "UnitPrice": float(line_row.get("line_unit_price") or amount),
                        },
                        "_tempItemName": line_row.get("line_item") or "Freight Income",
                    }
                )
            draft = cls._build_qbo_invoice_payload({**base, "amount": total})
            draft["Line"] = lines
            drafts.append(draft)
        return drafts

    def _build_header_map(self, headers: Iterable[Any]) -> dict[str, int]:
        normalized = {normalize_key(value): index for index, value in enumerate(headers) if normalize_key(value)}
        return {
            "doc_number": self._find_header(normalized, ["loadnumber", "loadnum", "invoicenumber", "invoice", "docnumber", "docnum", "invoiceno", "invoicenbr", "refnumber", "refnum", "refno"], True, "Invoice number / load number"),
            "qb_exported": self._find_header(normalized, ["qbexported", "exportedtoqbo", "quickbooksexported", "exported"], False, "QB Exported"),
            "division": self._find_header(normalized, ["division", "company", "qbcompany", "quickbookscompany", "ownercompany", "branch", "office"], False, "Division / company"),
            "customer": self._find_header(normalized, ["customer", "customername", "billto", "billtocustomer", "billtoname", "client", "clientname"], True, "Customer"),
            "broker_load_number": self._find_header(normalized, ["brokerloadnumber", "brokerloadnum", "brokerload", "ponumber", "pnumber", "po", "brokerloadno", "ponum", "purchaseorder"], False, "Broker load / PO number"),
            "txn_date": self._find_header(normalized, ["date", "invoicedate", "txndate", "transactiondate", "invdate"], True, "Invoice date"),
            "due_date": self._find_header(normalized, ["duedate", "due", "duedateinvoice"], False, "Due date"),
            "amount": self._find_header(normalized, ["amount", "invoiceamount", "totalamount", "total", "invoicetotal", "invtotal", "grandtotal", "lineamount"], True, "Amount"),
            "line_item": self._find_header(normalized, ["lineitem", "item", "productservice", "productserviceitem", "productservicename"], False, "Line item"),
            "line_qty": self._find_header(normalized, ["lineqty", "qty", "quantity", "linequantity"], False, "Line quantity"),
            "line_description": self._find_header(normalized, ["linedesc", "linedescription", "description", "desc"], False, "Line description"),
            "line_unit_price": self._find_header(normalized, ["lineunitprice", "unitprice", "rate", "linerate"], False, "Line unit price"),
            "invoice_last_sent_date": self._find_header(normalized, ["invoicelastsentdate", "lastsentdate", "lastsent", "sentdate"], False, "Invoice Last Sent Date"),
            "invoice_remarks": self._find_header(normalized, ["invoiceremarks", "remarks", "notes", "comment", "comments", "memo"], False, "Invoice Remarks"),
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
