"""Tests for the Safety Paperwork Portal Phase 1 parser/joiner."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from services.safety_paperwork import (
    EXPIRING_SOON_DAYS,
    classify_status,
    build_preview,
    normalize_person_name,
    normalize_unit_no,
    parse_warning_date,
)


# ---------- normalize_person_name ----------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("JOHN SUPPICICH", "john suppicich"),
        ("SUPPICICH, JOHN D", "john suppicich"),
        ("matthew krezel", "krezel matthew"),
        ("KIATPHONG  TEERATHAJARUPONG OLD FILE", "kiatphong teerathajarupong"),
        ("ANGEL PACHECO JR.", "angel jr pacheco"),
        ("", ""),
    ],
)
def test_normalize_person_name(raw: str, expected: str) -> None:
    assert normalize_person_name(raw) == expected


def test_normalize_unit_no_handles_floats_and_padding() -> None:
    assert normalize_unit_no("802.0") == "802"
    assert normalize_unit_no(802) == "802"
    assert normalize_unit_no("  0067 ") == "67"
    assert normalize_unit_no("") == ""


# ---------- parse_warning_date ----------


def test_parse_warning_date_accepts_normal_dates() -> None:
    today = date(2026, 6, 1)
    parsed, err = parse_warning_date("12/31/2027", today=today)
    assert parsed == date(2027, 12, 31)
    assert err is None


def test_parse_warning_date_rejects_implausible_future() -> None:
    today = date(2026, 6, 1)
    parsed, err = parse_warning_date("12/31/9999", today=today)
    assert parsed is None
    assert err is not None and "Implausible" in err


def test_parse_warning_date_rejects_unparseable() -> None:
    parsed, err = parse_warning_date("not a date", today=date(2026, 6, 1))
    assert parsed is None
    assert err is not None and "Unparseable" in err


def test_parse_warning_date_blank_is_not_error() -> None:
    parsed, err = parse_warning_date("", today=date(2026, 6, 1))
    assert parsed is None
    assert err is None


# ---------- classify_status ----------


def test_classify_status_uses_thresholds() -> None:
    today = date(2026, 6, 1)
    assert classify_status(None, today=today) == "missing"
    assert classify_status(date(2025, 1, 1), today=today) == "expired"
    assert classify_status(today, today=today) == "expiring_soon"
    assert (
        classify_status(date(2030, 1, 1), today=today, expired_flag=True) == "expired"
    )
    assert classify_status(date(2030, 1, 1), today=today) == "ok"
    soon = date(2026, 6, 1) + __import__("datetime").timedelta(days=EXPIRING_SOON_DAYS - 1)
    assert classify_status(soon, today=today) == "expiring_soon"


# ---------- build_preview ----------


def _driver_warnings_csv() -> bytes:
    return (
        "Driver,Truck #,CDL Expiration Date, CDL Expired,CDL Expiring Soon,"
        "Medical Card Expiration Date, Medical Card Expired,Medical Card Expiring Soon,"
        "Last Annual MVR Review Expired,Last Annual MVR Review Expiring Soon,"
        "Pre-Employment Drug Test,Custom Warning,Date of Birth,Driver Birthday,"
        "Driver Birthday Soon,Last Annual Clearinghouse Expired,"
        "Last Annual Clearinghouse Expiring Soon\n"
        # Owner-operator with expired medical card
        "JOHN SUPPICICH,128,3/1/2028,Unchecked,Unchecked,5/19/2026,Checked,Unchecked,"
        "Unchecked,Unchecked,Checked,,3/1/1967,Unchecked,Unchecked,Checked,Unchecked\n"
        # Driver-only matching by reversed name in details list
        "ABRAHAM PEREZ,802,1/29/2031,Unchecked,Unchecked,7/1/2026,Unchecked,Checked,"
        "Unchecked,Unchecked,Checked,,1/29/1978,Unchecked,Unchecked,Unchecked,Unchecked\n"
        # OLD FILE -> review queue
        "KIATPHONG TEERATHAJARUPONG OLD FILE,,7/13/2020,Checked,Unchecked,6/26/2019,"
        "Checked,Unchecked,Checked,Unchecked,Checked,,7/13/1972,Unchecked,Unchecked,"
        "Checked,Unchecked\n"
        # Unknown driver -> review queue (CDL expired so it's actionable)
        "GHOST DRIVER,999,1/1/2024,Checked,Unchecked,1/1/2030,Unchecked,Unchecked,"
        "Unchecked,Unchecked,Checked,,1/1/1990,Unchecked,Unchecked,Unchecked,Unchecked\n"
        # Bad date -> warning
        "ABRAHAM PEREZ,802,12/31/9999,Unchecked,Unchecked,,Unchecked,Unchecked,"
        "Unchecked,Unchecked,Checked,,1/29/1978,Unchecked,Unchecked,Unchecked,Unchecked\n"
    ).encode()


def _truck_warnings_csv() -> bytes:
    return (
        "Unit #,Driver 1,Equipment,DOT Inspection Expiration Date,DOT Expired,"
        " Insurance Expired,DOT Expiring Soon,Insurance Expiration Date,"
        "Insurance Expiring Soon,Plates Expiration Date, Plates Expired,"
        "Plates Expiring Soon,IFTA Expiration Date,Maintenance Overdue,Truck Custom Warning\n"
        # Owner-op (matches john suppicich) — DOT expiring soon
        "128,JOHN SUPPICICH,4400-14,2026-07-01,Unchecked,Unchecked,Checked,2027-09-01,"
        "Unchecked,2027-03-31,Unchecked,Unchecked,,Unchecked,\n"
        # Owner with multiple trucks (same email)
        "395,,,2026-12-31,Unchecked,Checked,Unchecked,,Unchecked,2027-01-31,Unchecked,"
        "Unchecked,,Unchecked,\n"
        # Unit not in details
        "9999,SOMEONE ELSE,,2026-07-01,Unchecked,Unchecked,Checked,2027-09-01,"
        "Unchecked,2027-03-31,Unchecked,Unchecked,,Unchecked,\n"
    ).encode()


def _driver_details_xlsx() -> bytes:
    df = pd.DataFrame(
        [
            {
                "Active": 1.0,
                "Status": 128,
                "DisplayName": "SUPPICICH, JOHN D",
                "Full Name": "JOHN SUPPICICH",
                "Division": "Xpress Trans Inc",
                "Date Of Birth": "1967-03-01",
                "Hire Date": "2019-11-05",
                "Cell-Phone": "(941)730-2261",
                "Email": "suppicichj@gmail.com",
                "Driver Personal Id": 128,
            },
            {
                "Active": 1.0,
                "Status": 802,
                "DisplayName": "ABRAHAM PEREZ",
                "Full Name": "ABRAHAM PEREZ",
                "Division": "Prestige Transportation Inc",
                "Date Of Birth": "1978-01-29",
                "Hire Date": "2018-05-01",
                "Cell-Phone": "",
                "Email": "abrahamp@example.com",
                "Driver Personal Id": 802,
            },
            {
                # No email -> blocker if their warning fires
                "Active": 1.0,
                "Status": 0,
                "DisplayName": "MARCO TORRES",
                "Full Name": "MARCO TORRES",
                "Division": "Prestige Transportation Inc",
                "Date Of Birth": "1973-01-21",
                "Hire Date": "2020-01-01",
                "Cell-Phone": "",
                "Email": "",
                "Driver Personal Id": 2173,
            },
        ]
    )
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _truck_owner_xlsx() -> bytes:
    df = pd.DataFrame(
        [
            {
                "Active": 1.0,
                "Unit #": 128,
                "Owner Company": "SUPPICICH TRUCKING",
                "Owner eMail Address": "suppicichj@gmail.com",  # owner-operator
                "Division": "Xpress Trans Inc",
                "Owner First Name": "JOHN",
                "Owner Last Name": "SUPPICICH",
            },
            {
                "Active": 1.0,
                "Unit #": 395,
                "Owner Company": "MULTI OWNER LLC",
                "Owner eMail Address": "multi@example.com",
                "Division": "Prestige Transportation Inc",
                "Owner First Name": "MULTI",
                "Owner Last Name": "OWNER",
            },
            {
                "Active": 1.0,
                "Unit #": 802,
                "Owner Company": "ABRAHAM TRUCKING",
                "Owner eMail Address": "abrahamp@example.com",
                "Division": "Prestige Transportation Inc",
                "Owner First Name": "ABRAHAM",
                "Owner Last Name": "PEREZ",
            },
        ]
    )
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def test_build_preview_basic_flow() -> None:
    preview = build_preview(
        driver_warnings_csv=_driver_warnings_csv(),
        truck_warnings_csv=_truck_warnings_csv(),
        driver_details_xls=_driver_details_xlsx(),
        truck_owner_xls=_truck_owner_xlsx(),
        today=date(2026, 6, 1),
    )

    assert preview.driver_warning_rows == 5
    assert preview.truck_warning_rows == 3
    assert preview.driver_detail_rows == 3
    assert preview.truck_detail_rows == 3

    by_email = {b.email: b for b in preview.recipients}

    # Owner-operator merged into one bundle
    op = by_email["suppicichj@gmail.com"]
    assert op.kind == "driver_owner"
    assert "128" in op.units
    doc_types = {item.doc_type for item in op.items}
    assert "MEDICAL_CARD" in doc_types  # driver-side expired
    assert "DOT_INSPECTION" in doc_types  # truck-side expiring soon

    # Abraham — driver only
    abe = by_email["abrahamp@example.com"]
    assert abe.kind == "driver"
    # Expiring-soon medical card should fire
    assert any(i.doc_type == "MEDICAL_CARD" and i.status == "expiring_soon" for i in abe.items)

    # Multi-truck owner with no driver warnings
    multi = by_email["multi@example.com"]
    assert multi.kind == "owner"
    assert multi.units == ["395"]


def test_build_preview_review_queue_categories() -> None:
    preview = build_preview(
        driver_warnings_csv=_driver_warnings_csv(),
        truck_warnings_csv=_truck_warnings_csv(),
        driver_details_xls=_driver_details_xlsx(),
        truck_owner_xls=_truck_owner_xlsx(),
        today=date(2026, 6, 1),
    )

    categories = {issue.category for issue in preview.review}
    assert "driver_old_file" in categories
    assert "driver_not_in_details" in categories
    assert "unit_not_in_details" in categories
    assert "driver_bad_date" in categories  # 12/31/9999

    # Bad-date row is a warning, not a blocker
    bad_dates = [r for r in preview.review if r.category == "driver_bad_date"]
    assert all(r.severity == "warning" for r in bad_dates)


def test_build_preview_accepts_preloaded_detail_lists() -> None:
    """Reference-DB path: pass DriverDetail/TruckOwnerDetail directly."""
    from services.safety_paperwork import load_driver_details, load_truck_owner_details

    drivers = load_driver_details(_driver_details_xlsx())
    trucks = load_truck_owner_details(_truck_owner_xlsx())

    preview = build_preview(
        driver_warnings_csv=_driver_warnings_csv(),
        truck_warnings_csv=_truck_warnings_csv(),
        driver_details=drivers,
        truck_details=trucks,
        today=date(2026, 6, 1),
    )
    by_email = {b.email: b for b in preview.recipients}
    assert "suppicichj@gmail.com" in by_email
    assert by_email["suppicichj@gmail.com"].kind == "driver_owner"


def test_build_preview_allows_driver_only_upload() -> None:
    preview = build_preview(
        driver_warnings_csv=_driver_warnings_csv(),
        driver_details_xls=_driver_details_xlsx(),
        today=date(2026, 6, 1),
    )

    assert preview.driver_warning_rows == 5
    assert preview.truck_warning_rows == 0
    assert preview.driver_detail_rows == 3
    assert preview.truck_detail_rows == 0
    assert {bundle.email for bundle in preview.recipients} >= {"suppicichj@gmail.com", "abrahamp@example.com"}


def test_build_preview_allows_truck_only_upload() -> None:
    preview = build_preview(
        truck_warnings_csv=_truck_warnings_csv(),
        truck_owner_xls=_truck_owner_xlsx(),
        today=date(2026, 6, 1),
    )

    assert preview.driver_warning_rows == 0
    assert preview.truck_warning_rows == 3
    assert preview.driver_detail_rows == 0
    assert preview.truck_detail_rows == 3
    assert {bundle.email for bundle in preview.recipients} >= {"suppicichj@gmail.com", "multi@example.com"}


def test_build_preview_requires_at_least_one_warning_file() -> None:
    with pytest.raises(ValueError, match="At least one warnings CSV"):
        build_preview(
            driver_details_xls=_driver_details_xlsx(),
            truck_owner_xls=_truck_owner_xlsx(),
            today=date(2026, 6, 1),
        )


def test_build_preview_against_real_attachments_if_available() -> None:
    """If the local Downloads exports are present, smoke-test against them."""
    downloads = Path.home() / "Downloads"
    files = {
        "driver_warnings_csv": downloads / "driver warnings.csv",
        "truck_warnings_csv": downloads / "Truck warnings.csv",
        "driver_details_xls": downloads / "Driver_details_list.xls",
        "truck_owner_xls": downloads / "Truck_owner_detail_list.xls",
    }
    if not all(p.exists() for p in files.values()):
        pytest.skip("Real ProTransport exports not present in Downloads/")

    preview = build_preview(
        **{k: p.read_bytes() for k, p in files.items()},
        today=date(2026, 6, 3),
    )

    # Sanity bounds — these are real numbers so just assert non-empty + reasonable.
    assert preview.driver_warning_rows > 10
    assert preview.truck_warning_rows > 10
    # At least one recipient should land in the clean queue.
    assert preview.recipients
    # OLD FILE should always be filtered as a blocker.
    assert any(r.category == "driver_old_file" for r in preview.review)
