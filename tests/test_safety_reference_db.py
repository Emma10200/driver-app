"""Tests for the safety paperwork reference database."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.safety_paperwork import DriverDetail, TruckOwnerDetail
from services.safety_reference_db import (
    load_drivers,
    load_trucks,
    reference_summary,
    upsert_drivers,
    upsert_trucks,
)


def _driver(**overrides) -> DriverDetail:
    base = dict(
        display_name="JOHN SUPPICICH",
        full_name="JOHN SUPPICICH",
        division="Xpress Trans Inc",
        email="suppicichj@gmail.com",
        phone="(941)730-2261",
        driver_personal_id="128",
        active=True,
        status="128",
    )
    base.update(overrides)
    return DriverDetail(**base)


def _truck(**overrides) -> TruckOwnerDetail:
    base = dict(
        unit_no="128",
        owner_company="SUPPICICH TRUCKING",
        owner_email="suppicichj@gmail.com",
        owner_first="JOHN",
        owner_last="SUPPICICH",
        division="Xpress Trans Inc",
        active=True,
    )
    base.update(overrides)
    return TruckOwnerDetail(**base)


def test_upsert_drivers_adds_then_unchanges_then_updates(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    d1 = _driver()

    r1 = upsert_drivers([d1], submissions_dir=sub, source_name="first.xls")
    assert (r1.added, r1.updated, r1.unchanged, r1.total) == (1, 0, 0, 1)

    # Same data again -> unchanged
    r2 = upsert_drivers([d1], submissions_dir=sub, source_name="second.xls")
    assert (r2.added, r2.updated, r2.unchanged, r2.total) == (0, 0, 1, 1)

    # Email changed -> updated
    d2 = _driver(email="new@example.com")
    r3 = upsert_drivers([d2], submissions_dir=sub, source_name="third.xls")
    assert (r3.added, r3.updated, r3.unchanged, r3.total) == (0, 1, 0, 1)

    drivers = load_drivers(sub)
    assert len(drivers) == 1
    assert drivers[0].email == "new@example.com"


def test_upsert_drivers_uses_personal_id_over_name(tmp_path: Path) -> None:
    """Same Driver Personal Id must collapse two display name variants."""
    sub = tmp_path / "submissions"
    d_legacy = _driver(display_name="SUPPICICH, JOHN D", full_name="JOHN SUPPICICH", driver_personal_id="128")
    d_normal = _driver(display_name="JOHN SUPPICICH", full_name="JOHN SUPPICICH", driver_personal_id="128")

    upsert_drivers([d_legacy], submissions_dir=sub)
    r = upsert_drivers([d_normal], submissions_dir=sub)
    assert r.added == 0  # second insert collapses onto pid:128
    drivers = load_drivers(sub)
    assert len(drivers) == 1
    assert drivers[0].display_name == "JOHN SUPPICICH"  # latest wins


def test_upsert_drivers_falls_back_to_name_key_when_no_pid(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    a = _driver(display_name="MATTHEW KREZEL", full_name="MATTHEW KREZEL", driver_personal_id="")
    b = _driver(display_name="matthew krezel", full_name="matthew krezel", driver_personal_id="")
    upsert_drivers([a], submissions_dir=sub)
    r = upsert_drivers([b], submissions_dir=sub)
    assert r.added == 0  # collapsed by normalized name
    drivers = load_drivers(sub)
    assert len(drivers) == 1


def test_upsert_drivers_does_not_delete_missing_records(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    a = _driver(display_name="ALICE", full_name="ALICE A", driver_personal_id="1", email="alice@example.com")
    b = _driver(display_name="BOB", full_name="BOB B", driver_personal_id="2", email="bob@example.com")
    upsert_drivers([a, b], submissions_dir=sub)
    # Re-upload only Alice — Bob must stay
    upsert_drivers([a], submissions_dir=sub)
    drivers = {d.driver_personal_id for d in load_drivers(sub)}
    assert drivers == {"1", "2"}


def test_upsert_trucks_basic(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    t1 = _truck()
    t2 = _truck(unit_no="395", owner_email="multi@example.com", owner_first="MULTI", owner_last="OWNER")
    r1 = upsert_trucks([t1, t2], submissions_dir=sub, source_name="trucks.xls")
    assert (r1.added, r1.unchanged) == (2, 0)
    r2 = upsert_trucks([t1, t2], submissions_dir=sub, source_name="trucks.xls")
    assert (r2.added, r2.unchanged) == (0, 2)
    # Owner email changed for unit 395 -> update
    t2b = _truck(unit_no="395", owner_email="changed@example.com", owner_first="MULTI", owner_last="OWNER")
    r3 = upsert_trucks([t2b], submissions_dir=sub)
    assert (r3.added, r3.updated, r3.unchanged) == (0, 1, 0)
    trucks = {t.unit_no: t for t in load_trucks(sub)}
    assert trucks["395"].owner_email == "changed@example.com"
    assert trucks["128"].owner_email == "suppicichj@gmail.com"


def test_reference_summary_reports_counts(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    upsert_drivers([_driver()], submissions_dir=sub)
    upsert_trucks([_truck()], submissions_dir=sub)
    summary = reference_summary(sub)
    assert summary["driver_count"] == 1
    assert summary["truck_count"] == 1
    assert summary["drivers_last_updated"]
    assert summary["trucks_last_updated"]
