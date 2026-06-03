"""Persistent reference database for the Safety Paperwork Portal.

Drivers and truck-owners are upserted from the ProTransport detail exports
the staff upload. The exports are not authoritative on their own — every
re-upload should *grow and refresh* the same store, never reset it. We key
by:
  - driver_personal_id (preferred), else normalized display name
  - unit_no

Storage: a JSON file per kind under ``<submissions_dir>/safety/reference/``.
Local-only in phase 1; a Supabase mirror can be bolted on later without
changing the call sites.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from services.safety_paperwork import (
    DriverDetail,
    TruckOwnerDetail,
    normalize_person_name,
)

_REFERENCE_DIRNAME = "reference"
_DRIVERS_FILE = "drivers.json"
_TRUCKS_FILE = "trucks.json"

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

class UpsertResult:
    __slots__ = ("added", "updated", "unchanged", "total")

    def __init__(self, added: int = 0, updated: int = 0, unchanged: int = 0, total: int = 0) -> None:
        self.added = added
        self.updated = updated
        self.unchanged = unchanged
        self.total = total

    def as_dict(self) -> dict[str, int]:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "total": self.total,
        }


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _reference_dir(submissions_dir: Path) -> Path:
    path = submissions_dir / "safety" / _REFERENCE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _drivers_path(submissions_dir: Path) -> Path:
    return _reference_dir(submissions_dir) / _DRIVERS_FILE


def _trucks_path(submissions_dir: Path) -> Path:
    return _reference_dir(submissions_dir) / _TRUCKS_FILE


def _read_json(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def _write_json(path: Path, payload: dict[str, dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def driver_key(detail: DriverDetail) -> str:
    pid = (detail.driver_personal_id or "").strip()
    if pid:
        return f"pid:{pid}"
    name_key = normalize_person_name(detail.display_name) or normalize_person_name(detail.full_name)
    return f"name:{name_key}" if name_key else f"name:{detail.display_name.lower().strip()}"


def truck_key(detail: TruckOwnerDetail) -> str:
    return f"unit:{detail.unit_no}"


# ---------------------------------------------------------------------------
# Diff helper — only "data" fields drive update/unchanged classification.
# Bookkeeping fields like last_seen_at and source_name don't count.
# ---------------------------------------------------------------------------

_DRIVER_DATA_FIELDS = (
    "display_name",
    "full_name",
    "division",
    "email",
    "phone",
    "driver_personal_id",
    "active",
    "status",
)
_TRUCK_DATA_FIELDS = (
    "unit_no",
    "owner_company",
    "owner_email",
    "owner_first",
    "owner_last",
    "division",
    "active",
)


def _data_subset(record: dict, fields: tuple[str, ...]) -> dict:
    return {f: record.get(f) for f in fields}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upsert_drivers(
    details: Iterable[DriverDetail],
    *,
    submissions_dir: Path,
    source_name: str = "",
) -> UpsertResult:
    details = list(details)
    path = _drivers_path(submissions_dir)
    with _lock:
        store = _read_json(path)
        result = UpsertResult(total=len(details))
        now = _now_iso()
        for detail in details:
            key = driver_key(detail)
            new_record = asdict(detail)
            existing = store.get(key)
            if existing is None:
                store[key] = {
                    **new_record,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_source": source_name,
                }
                result.added += 1
                continue
            if _data_subset(existing, _DRIVER_DATA_FIELDS) == _data_subset(new_record, _DRIVER_DATA_FIELDS):
                existing["last_seen_at"] = now
                if source_name:
                    existing["last_source"] = source_name
                result.unchanged += 1
                continue
            existing.update(new_record)
            existing["last_seen_at"] = now
            if source_name:
                existing["last_source"] = source_name
            result.updated += 1
        _write_json(path, store)
    return result


def upsert_trucks(
    details: Iterable[TruckOwnerDetail],
    *,
    submissions_dir: Path,
    source_name: str = "",
) -> UpsertResult:
    details = list(details)
    path = _trucks_path(submissions_dir)
    with _lock:
        store = _read_json(path)
        result = UpsertResult(total=len(details))
        now = _now_iso()
        for detail in details:
            key = truck_key(detail)
            new_record = asdict(detail)
            existing = store.get(key)
            if existing is None:
                store[key] = {
                    **new_record,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_source": source_name,
                }
                result.added += 1
                continue
            if _data_subset(existing, _TRUCK_DATA_FIELDS) == _data_subset(new_record, _TRUCK_DATA_FIELDS):
                existing["last_seen_at"] = now
                if source_name:
                    existing["last_source"] = source_name
                result.unchanged += 1
                continue
            existing.update(new_record)
            existing["last_seen_at"] = now
            if source_name:
                existing["last_source"] = source_name
            result.updated += 1
        _write_json(path, store)
    return result


def load_drivers(submissions_dir: Path) -> list[DriverDetail]:
    store = _read_json(_drivers_path(submissions_dir))
    out: list[DriverDetail] = []
    for record in store.values():
        out.append(
            DriverDetail(
                display_name=str(record.get("display_name") or ""),
                full_name=str(record.get("full_name") or ""),
                division=str(record.get("division") or ""),
                email=str(record.get("email") or ""),
                phone=str(record.get("phone") or ""),
                driver_personal_id=str(record.get("driver_personal_id") or ""),
                active=bool(record.get("active", True)),
                status=str(record.get("status") or ""),
            )
        )
    return out


def load_trucks(submissions_dir: Path) -> list[TruckOwnerDetail]:
    store = _read_json(_trucks_path(submissions_dir))
    out: list[TruckOwnerDetail] = []
    for record in store.values():
        out.append(
            TruckOwnerDetail(
                unit_no=str(record.get("unit_no") or ""),
                owner_company=str(record.get("owner_company") or ""),
                owner_email=str(record.get("owner_email") or ""),
                owner_first=str(record.get("owner_first") or ""),
                owner_last=str(record.get("owner_last") or ""),
                division=str(record.get("division") or ""),
                active=bool(record.get("active", True)),
            )
        )
    return out


def reference_summary(submissions_dir: Path) -> dict[str, object]:
    drivers_store = _read_json(_drivers_path(submissions_dir))
    trucks_store = _read_json(_trucks_path(submissions_dir))

    def _max_seen(store: dict[str, dict]) -> str | None:
        seen = [r.get("last_seen_at") for r in store.values() if r.get("last_seen_at")]
        return max(seen) if seen else None

    return {
        "driver_count": len(drivers_store),
        "truck_count": len(trucks_store),
        "drivers_last_updated": _max_seen(drivers_store),
        "trucks_last_updated": _max_seen(trucks_store),
    }
