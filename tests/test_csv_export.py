"""Tests for the per-applicant CSV export used in safety notifications."""

from __future__ import annotations

import csv
from io import StringIO

from services.csv_export import build_application_csv


def _decode_rows(payload: bytes) -> list[list[str]]:
    text = payload.decode("utf-8-sig")
    reader = csv.reader(StringIO(text))
    return list(reader)


def test_build_application_csv_has_header_and_form_fields():
    payload = build_application_csv(
        form_data={"first_name": "Jane", "last_name": "Doe", "ssn": "123-45-6789"},
    )
    rows = _decode_rows(payload)
    assert rows[0] == ["Field", "Value"]
    flat = {row[0]: row[1] for row in rows[1:]}
    assert flat["First Name"] == "Jane"
    assert flat["Last Name"] == "Doe"
    assert flat["Ssn"] == "123-45-6789"


def test_build_application_csv_flattens_repeating_sections():
    payload = build_application_csv(
        form_data={"first_name": "Jane"},
        employers=[{"name": "Old Corp", "ended": "2025-01-01"}],
        licenses=[{"number": "AB123"}, {"number": "CD456"}],
        accidents=[{"date": "2024-06-01", "fatalities": 0}],
        violations=[],
    )
    rows = _decode_rows(payload)
    flat = {row[0]: row[1] for row in rows[1:]}
    assert flat["Employer 1 - Name"] == "Old Corp"
    assert flat["Employer 1 - Ended"] == "2025-01-01"
    assert flat["License 1 - Number"] == "AB123"
    assert flat["License 2 - Number"] == "CD456"
    assert flat["Accident 1 - Fatalities"] == "0"


def test_build_application_csv_lists_supporting_documents():
    payload = build_application_csv(
        form_data={},
        uploaded_documents=[
            {
                "file_name": "license.pdf",
                "size_bytes": 1024,
                "storage_path": "companies/x/uploads/license.pdf",
            }
        ],
    )
    rows = _decode_rows(payload)
    flat = {row[0]: row[1] for row in rows[1:]}
    assert flat["Supporting Document 1 - File Name"] == "license.pdf"
    assert flat["Supporting Document 1 - Size Bytes"] == "1024"
    assert flat["Supporting Document 1 - Storage Path"] == "companies/x/uploads/license.pdf"


def test_build_application_csv_renders_booleans_as_yes_no():
    payload = build_application_csv(
        form_data={"eligible_us": True, "ca_applicable": False},
    )
    rows = _decode_rows(payload)
    flat = {row[0]: row[1] for row in rows[1:]}
    assert flat["Eligible Us"] == "Yes"
    assert flat["Ca Applicable"] == "No"
