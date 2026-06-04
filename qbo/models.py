from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _string_list() -> list[str]:
    return []


def _dict_list() -> list[dict[str, Any]]:
    return []


def _preview_row_list() -> list[dict[str, Any]]:
    return []


def _by_division() -> dict[str, dict[str, int]]:
    return {}


@dataclass(slots=True)
class ConnectedRealm:
    realm_id: str
    company_name: str
    environment: str = "production"
    default_bank_account_name: str = ""
    default_money_code_cc_account_name: str = "Fuel Card - EFS"
    connected_by_email: str = ""
    connected_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class ImportStats:
    posted: int = 0
    skipped_duplicates: int = 0
    failed: int = 0
    held_for_retry: int = 0
    errors: list[str] = field(default_factory=_string_list)
    warnings: list[str] = field(default_factory=_string_list)
    successes: list[dict[str, Any]] = field(default_factory=_dict_list)
    failures: list[dict[str, Any]] = field(default_factory=_dict_list)
    duplicates: list[dict[str, Any]] = field(default_factory=_dict_list)
    by_division: dict[str, dict[str, int]] = field(default_factory=_by_division)

    def bump_division(self, division: str, key: str) -> None:
        bucket = self.by_division.setdefault(
            division or "(unspecified)", {"posted": 0, "duplicate": 0, "failed": 0}
        )
        bucket[key] = bucket.get(key, 0) + 1


@dataclass(slots=True)
class PreviewResult:
    template_type: str
    source_file: str
    source_hash: str
    count: int
    source_count: int
    skipped_count: int
    rows: list[dict[str, Any]] = field(default_factory=_preview_row_list)
    errors: list[str] = field(default_factory=_string_list)
    warnings: list[str] = field(default_factory=_string_list)
    drafts: list[dict[str, Any]] = field(default_factory=_dict_list)
