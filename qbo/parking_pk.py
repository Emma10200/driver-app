"""Parking PK workflow (Prestig Inc only).

Ported from `QBO_App/src_tkinter/qbo/parking_pk.py`, which in turn was ported
from the Apps Script `findPrestigParkingInvoiceMatches` /
`updateInvoiceDocNumberForCompany_` flow.

Two phases:
    * ``find_matches(start_date, end_date)`` scans Prestig Inc invoices in the
      given date window and returns every invoice that contains the ``Parking``
      item, with its proposed new DocNumber (existing + ``PK``).
    * ``apply_matches(realm_id, matches)`` sparse-updates each invoice's
      DocNumber via ``POST /invoice?operation=update`` with ``sparse=true``
      and writes one audit row per attempt.

This service strictly refuses to operate on any realm whose normalized
company name is not ``prestiginc``. ``Prestige Transportation Inc`` is a
different legal entity and must never be touched by this flow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from qbo.api_client import QboClient
from qbo.lookups import EntityLookupService
from qbo.utils import normalize_company_name
from services.qbo_auth import QboTokenRepository

logger = logging.getLogger(__name__)


_PRESTIG_NORMALIZED = "prestiginc"
_PARKING_ITEM_NAME = "Parking"
_POST_DELAY_SECONDS = 0.35


def _str_list() -> list[str]:
    return []


def _match_list() -> list["ParkingMatch"]:
    return []


@dataclass(slots=True)
class ParkingMatch:
    invoice_id: str
    sync_token: str
    doc_number: str
    proposed_doc_number: str
    txn_date: str
    customer_name: str
    amount: float


@dataclass(slots=True)
class ParkingScanResult:
    realm_id: str
    matches: list[ParkingMatch] = field(default_factory=_match_list)
    warnings: list[str] = field(default_factory=_str_list)


@dataclass(slots=True)
class ParkingApplyResult:
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=_str_list)


class AuditSink(Protocol):
    def record(self, **kwargs: Any) -> Any: ...  # pragma: no cover


class ParkingPkService:
    """Scan + sparse-update DocNumber on Prestig Inc invoices that contain Parking."""

    def __init__(
        self,
        qbo_client: QboClient,
        token_repo: QboTokenRepository,
        lookups: EntityLookupService,
        audit_log: AuditSink | None = None,
    ) -> None:
        self._qbo = qbo_client
        self._token_repo = token_repo
        self._lookups = lookups
        self._audit = audit_log

    # ------------------------------------------------------------------ realm
    def resolve_prestig_realm(self) -> str | None:
        for realm in self._token_repo.list_realms():
            if normalize_company_name(realm.company_name) == _PRESTIG_NORMALIZED:
                return realm.realm_id
        return None

    # ------------------------------------------------------------------- scan
    def find_matches(self, start_date: str, end_date: str) -> ParkingScanResult:
        if not (start_date and end_date):
            raise ValueError("start_date and end_date are required.")
        realm_id = self.resolve_prestig_realm()
        if not realm_id:
            raise RuntimeError(
                "Prestig Inc connection not found. Connect 'Prestig Inc' (no 'e') first."
            )

        parking_item_id = self._lookups.resolve_entity("Item", _PARKING_ITEM_NAME, realm_id)
        if not parking_item_id:
            raise RuntimeError(
                f"Item 'Parking' not found in Prestig Inc (realm {realm_id})."
            )

        page_size = 100
        start = 1
        matches: list[ParkingMatch] = []
        warnings: list[str] = []
        seen_invoice_ids: set[str] = set()

        while True:
            safe_start = start_date.replace("'", "\\'")
            safe_end = end_date.replace("'", "\\'")
            sql = (
                "SELECT * FROM Invoice "
                f"WHERE TxnDate >= '{safe_start}' AND TxnDate <= '{safe_end}' "
                f"STARTPOSITION {start} MAXRESULTS {page_size}"
            )
            payload = self._qbo.query(sql, realm_id=realm_id)
            rows = (payload.get("QueryResponse") or {}).get("Invoice") or []
            if not rows:
                break

            for invoice in rows:
                inv_id = str(invoice.get("Id") or "")
                if not inv_id or inv_id in seen_invoice_ids:
                    continue
                seen_invoice_ids.add(inv_id)

                doc_number = str(invoice.get("DocNumber") or "")
                if not doc_number:
                    continue
                if doc_number.endswith("PK"):
                    continue

                has_parking = False
                for line in invoice.get("Line") or []:
                    if not isinstance(line, dict):
                        continue
                    detail = line.get("SalesItemLineDetail") or {}
                    item_ref = detail.get("ItemRef") or {}
                    if str(item_ref.get("value") or "") == str(parking_item_id):
                        has_parking = True
                        break
                if not has_parking:
                    continue

                customer_ref = invoice.get("CustomerRef") or {}
                try:
                    total = float(invoice.get("TotalAmt") or 0.0)
                except (TypeError, ValueError):
                    total = 0.0
                matches.append(
                    ParkingMatch(
                        invoice_id=inv_id,
                        sync_token=str(invoice.get("SyncToken") or "0"),
                        doc_number=doc_number,
                        proposed_doc_number=f"{doc_number}PK",
                        txn_date=str(invoice.get("TxnDate") or ""),
                        customer_name=str(customer_ref.get("name") or ""),
                        amount=total,
                    )
                )

            if len(rows) < page_size:
                break
            start += page_size

        return ParkingScanResult(realm_id=realm_id, matches=matches, warnings=warnings)

    # ------------------------------------------------------------------ apply
    def apply_matches(
        self, realm_id: str, matches: list[ParkingMatch]
    ) -> ParkingApplyResult:
        result = ParkingApplyResult()
        if not matches:
            return result
        if not realm_id:
            raise ValueError("realm_id is required.")

        # Defensive: re-verify realm identity each apply call.
        prestig_id = self.resolve_prestig_realm()
        if prestig_id != realm_id:
            raise RuntimeError(
                f"Refusing to update DocNumbers \u2014 provided realm '{realm_id}' is not Prestig Inc."
            )

        for match in matches:
            if not match.proposed_doc_number or match.proposed_doc_number == match.doc_number:
                result.skipped += 1
                continue
            payload: dict[str, Any] = {
                "sparse": True,
                "Id": match.invoice_id,
                "SyncToken": match.sync_token,
                "DocNumber": match.proposed_doc_number,
            }
            try:
                response = self._qbo.post(
                    "/invoice",
                    realm_id=realm_id,
                    payload=payload,
                    params={"operation": "update"},
                ).json()
                qbo_id = str(((response or {}).get("Invoice") or {}).get("Id") or match.invoice_id)
                result.updated += 1
                self._audit_record(
                    status="success",
                    realm_id=realm_id,
                    match=match,
                    doc_number=match.proposed_doc_number,
                    qbo_id=qbo_id,
                    message=f"DocNumber: {match.doc_number} -> {match.proposed_doc_number}",
                    raw_response=response,
                )
            except RuntimeError as exc:
                result.failed += 1
                result.errors.append(f"{match.doc_number}: {exc}")
                self._audit_record(
                    status="failed",
                    realm_id=realm_id,
                    match=match,
                    doc_number=match.doc_number,
                    qbo_id="",
                    message=str(exc),
                    raw_response=None,
                )
            finally:
                time.sleep(_POST_DELAY_SECONDS)

        return result

    # ----------------------------------------------------------- audit helper
    def _audit_record(
        self,
        *,
        status: str,
        realm_id: str,
        match: ParkingMatch,
        doc_number: str,
        qbo_id: str,
        message: str,
        raw_response: Any,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.record(
                txn_type="ParkingPk",
                realm_id=realm_id,
                status=status,
                doc_number=doc_number,
                txn_date=match.txn_date,
                entity_name=match.customer_name,
                division="Prestig Inc",
                amount=match.amount,
                qbo_id=qbo_id,
                message=message,
                raw_response=raw_response,
            )
        except Exception as exc:  # noqa: BLE001 - audit must never break the apply
            logger.warning("ParkingPk audit write failed: %s", exc)
