from __future__ import annotations

from collections.abc import Iterable

from .models import ConnectedRealm
from .utils import normalize_company_name


class CompanyDirectory:
    """Loose division/company-name resolver for source spreadsheets."""

    def __init__(self, realms: Iterable[ConnectedRealm]) -> None:
        self._realms = list(realms)

    def list_realms(self) -> list[ConnectedRealm]:
        return list(self._realms)

    def resolve_realm_id_by_name_loose(self, division_name: str) -> str:
        target = normalize_company_name(division_name)
        if not target:
            return ""

        for realm in self._realms:
            if normalize_company_name(realm.company_name) == target:
                return realm.realm_id

        for realm in self._realms:
            normalized = normalize_company_name(realm.company_name)
            if target in normalized or normalized in target:
                return realm.realm_id
        return ""

    def company_name_for_realm(self, realm_id: str) -> str:
        for realm in self._realms:
            if realm.realm_id == realm_id:
                return realm.company_name
        return realm_id
