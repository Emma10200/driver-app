"""Read-only catalogues of QBO entities (accounts, vendors, customers, items)
used to populate editable dropdowns in the preview UI.

These are *names only* helpers \u2014 they let the user fix a wrong account
selection (e.g. someone picked ``Truck \u2014 ELD`` when they meant
``Truck \u2014 Trailer``) before posting. The authoritative name\u2192Id
resolution still happens inside `qbo.lookups.EntityLookupService` at post time.

Each method pages through the QBO query API in 1000-row chunks and returns a
sorted list of unique names. Callers are expected to cache the result on the
Streamlit session (e.g. via `st.cache_data`) because every call hits the API.
"""

from __future__ import annotations

import logging
from typing import Iterable

from qbo.api_client import QboClient

logger = logging.getLogger(__name__)

# Account types that are valid for the "Expense Account" column on a Check or
# CreditCardPurchase line. Excludes Bank / Accounts Receivable / Accounts
# Payable so the dropdown doesn't list nonsense options. None = no filter.
EXPENSE_ACCOUNT_CLASSIFICATIONS: tuple[str, ...] = (
    "Expense",
    "CostOfGoodsSold",
    "OtherExpense",
    "OtherCurrentAsset",
    "FixedAsset",
)
BANK_ACCOUNT_TYPES: tuple[str, ...] = ("Bank",)
CC_ACCOUNT_TYPES: tuple[str, ...] = ("Credit Card",)


class QboCatalog:
    """Paginated read-only listing of QBO entity names per realm."""

    def __init__(self, qbo_client: QboClient) -> None:
        self._qbo = qbo_client

    def list_accounts(
        self,
        realm_id: str,
        *,
        account_types: Iterable[str] | None = None,
        classifications: Iterable[str] | None = None,
        active_only: bool = True,
    ) -> list[str]:
        clauses: list[str] = []
        if active_only:
            clauses.append("Active = true")
        if account_types:
            quoted = ", ".join(f"'{_escape(value)}'" for value in account_types if value)
            if quoted:
                clauses.append(f"AccountType IN ({quoted})")
        if classifications:
            quoted = ", ".join(f"'{_escape(value)}'" for value in classifications if value)
            if quoted:
                clauses.append(f"Classification IN ({quoted})")

        rows = self._paginated_query(
            entity="Account",
            select_fields="Id, Name, FullyQualifiedName, AccountType, Active",
            where=" AND ".join(clauses),
            realm_id=realm_id,
        )
        names = {
            (row.get("FullyQualifiedName") or row.get("Name") or "").strip()
            for row in rows
            if (row.get("FullyQualifiedName") or row.get("Name"))
        }
        names.discard("")
        return sorted(names, key=str.lower)

    def list_vendors(self, realm_id: str, *, active_only: bool = True) -> list[str]:
        where = "Active = true" if active_only else ""
        rows = self._paginated_query(
            entity="Vendor",
            select_fields="Id, DisplayName, Active",
            where=where,
            realm_id=realm_id,
        )
        names = {str(row.get("DisplayName") or "").strip() for row in rows}
        names.discard("")
        return sorted(names, key=str.lower)

    def list_customers(self, realm_id: str, *, active_only: bool = True) -> list[str]:
        where = "Active = true" if active_only else ""
        rows = self._paginated_query(
            entity="Customer",
            select_fields="Id, DisplayName, Active",
            where=where,
            realm_id=realm_id,
        )
        names = {str(row.get("DisplayName") or "").strip() for row in rows}
        names.discard("")
        return sorted(names, key=str.lower)

    def list_items(self, realm_id: str, *, active_only: bool = True) -> list[str]:
        where = "Active = true" if active_only else ""
        rows = self._paginated_query(
            entity="Item",
            select_fields="Id, Name, FullyQualifiedName, Active",
            where=where,
            realm_id=realm_id,
        )
        names = {
            (row.get("FullyQualifiedName") or row.get("Name") or "").strip()
            for row in rows
        }
        names.discard("")
        return sorted(names, key=str.lower)

    # ----------------------------------------------------------------- internal
    def _paginated_query(
        self,
        *,
        entity: str,
        select_fields: str,
        where: str,
        realm_id: str,
    ) -> list[dict]:
        if not realm_id:
            return []
        page_size = 1000
        start = 1
        out: list[dict] = []
        while True:
            clause = f" WHERE {where}" if where else ""
            sql = (
                f"SELECT {select_fields} FROM {entity}{clause} "
                f"STARTPOSITION {start} MAXRESULTS {page_size}"
            )
            try:
                response = self._qbo.query(sql, realm_id=realm_id)
            except RuntimeError as exc:
                logger.warning("QBO catalog query failed for %s in %s: %s", entity, realm_id, exc)
                return out
            rows = (response.get("QueryResponse") or {}).get(entity) or []
            if not rows:
                break
            out.extend(rows)
            if len(rows) < page_size:
                break
            start += page_size
        return out


def _escape(value: str) -> str:
    return str(value).replace("'", "\\'")
