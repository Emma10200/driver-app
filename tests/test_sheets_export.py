"""Tests for the Google Sheets export service."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from services import sheets_export


@pytest.fixture(autouse=True)
def _reset_secret_cache():
    from submission_storage import _get_secret

    _get_secret.cache_clear()
    yield
    _get_secret.cache_clear()


def _set_secrets(monkeypatch, **values):
    def fake_get_secret(name, default=None):
        return values.get(name, default)

    monkeypatch.setattr(sheets_export, "get_runtime_secret", fake_get_secret)


def test_build_submission_row_orders_columns_and_stringifies():
    row = sheets_export.build_submission_row(
        form_data={
            "first_name": "Jane",
            "middle_name": "Q",
            "last_name": "Driver",
            "email": "jane@example.com",
            "primary_phone": "555-0101",
            "dob": date(1985, 6, 15),
            "address": "123 Main",
            "city": "Fontana",
            "state": "CA",
            "zip_code": "92335",
            "position_applied": "OTR Driver",
            "has_cdl": True,
            "years_experience": 7,
            "final_submission_timestamp": "2026-04-24T10:30:00",
        },
        licenses=[
            {"license_number": "D1234567", "license_state": "CA", "license_class": "A"},
        ],
        submission_id="sub-001",
        storage_location="submissions/sub-001",
        test_mode=False,
    )

    assert len(row) == len(sheets_export.SHEET_COLUMNS)
    by_column = dict(zip(sheets_export.SHEET_COLUMNS, row))
    assert by_column["Submitted At"] == "2026-04-24T10:30:00"
    assert by_column["Apply Date"] == "2026-04-24"
    assert by_column["First Name"] == "Jane"
    assert by_column["Middle Name"] == "Q"
    assert by_column["Last Name"] == "Driver"
    assert by_column["Display Name"] == "Driver, Jane Q."
    assert by_column["Email"] == "jane@example.com"
    assert by_column["Date Of Birth"] == "1985-06-15"
    assert by_column["Has CDL"] == "Yes"
    assert by_column["CDL Number"] == "D1234567"
    assert by_column["CDL State"] == "CA"
    assert by_column["CDL Class"] == "A"
    assert by_column["Submission ID"] == "sub-001"
    assert "Test Mode" not in by_column
    assert by_column["Send Emails"] == "Yes"
    # Empty ERP-only columns are present but blank.
    assert by_column["FEIN"] == ""
    assert by_column["Hire Date"] == ""
    assert by_column["Status"] == ""


def test_tab_name_routing():
    assert sheets_export._tab_name_for_company("prestige") == "Prestige Transportation"
    assert sheets_export._tab_name_for_company("xpress") == "Xpress"
    # Unknown slug falls back to the default company profile's display name.
    assert sheets_export._tab_name_for_company("unknown-slug") == "PRESTIGE TRANSPORTATION INC."


def test_append_submission_row_disabled_when_secrets_missing(monkeypatch):
    _set_secrets(monkeypatch, GOOGLE_SERVICE_ACCOUNT_JSON="", APPLICANTS_SHEET_ID="")
    result = sheets_export.append_submission_row(
        company_slug="prestige",
        form_data={"first_name": "A", "last_name": "B"},
    )
    assert result["status"] == "disabled"


def test_append_submission_row_invalid_json(monkeypatch):
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON="{not json",
        APPLICANTS_SHEET_ID="abc",
    )
    result = sheets_export.append_submission_row(
        company_slug="prestige",
        form_data={"first_name": "A", "last_name": "B"},
    )
    assert result["status"] == "error"
    assert "valid JSON" in result["message"]


def test_append_submission_row_inserts_at_top_with_header(monkeypatch):
    creds_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "x@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN-----\nfake\n-----END-----\n",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON=creds_json,
        APPLICANTS_SHEET_ID="sheet-xyz",
    )

    fake_worksheet = MagicMock()
    fake_worksheet.row_values.return_value = []  # empty -> header gets written

    def fake_open(spreadsheet_id, tab_name, credentials_info):
        assert spreadsheet_id == "sheet-xyz"
        assert tab_name == "Xpress"
        assert credentials_info["client_email"].endswith(".iam.gserviceaccount.com")
        return fake_worksheet

    monkeypatch.setattr(sheets_export, "_open_worksheet", fake_open)

    result = sheets_export.append_submission_row(
        company_slug="xpress",
        form_data={
            "first_name": "Test",
            "last_name": "Applicant",
            "final_submission_timestamp": "2026-04-24T09:00:00",
        },
        licenses=[],
        submission_id="sub-42",
        storage_location="submissions/sub-42",
    )

    assert result["status"] == "appended"
    assert result["tab"] == "Xpress"
    fake_worksheet.update.assert_called_once()
    fake_worksheet.insert_row.assert_called_once()
    insert_args, insert_kwargs = fake_worksheet.insert_row.call_args
    assert insert_kwargs.get("index") == 2
    assert insert_kwargs.get("value_input_option") == "USER_ENTERED"
    row = insert_args[0]
    by_column = dict(zip(sheets_export.SHEET_COLUMNS, row))
    assert by_column["First Name"] == "Test"
    assert by_column["Last Name"] == "Applicant"
    assert by_column["Division"] == "Xpress Inc"


def test_append_submission_row_skips_header_when_present(monkeypatch):
    creds_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "x@example.iam.gserviceaccount.com",
            "private_key": "k",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON=creds_json,
        APPLICANTS_SHEET_ID="sheet-xyz",
    )

    fake_worksheet = MagicMock()
    # Header that already matches SHEET_COLUMNS exactly -> no rewrite.
    fake_worksheet.row_values.return_value = list(sheets_export.SHEET_COLUMNS)

    monkeypatch.setattr(
        sheets_export, "_open_worksheet", lambda *a, **k: fake_worksheet
    )

    result = sheets_export.append_submission_row(
        company_slug="prestige",
        form_data={"first_name": "R", "last_name": "L"},
    )

    assert result["status"] == "appended"
    fake_worksheet.update.assert_not_called()
    fake_worksheet.insert_row.assert_called_once()


def test_append_submission_row_returns_error_on_api_failure(monkeypatch):
    creds_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "x@example.iam.gserviceaccount.com",
            "private_key": "k",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON=creds_json,
        APPLICANTS_SHEET_ID="sheet-xyz",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(sheets_export, "_open_worksheet", boom)

    result = sheets_export.append_submission_row(
        company_slug="prestige",
        form_data={"first_name": "R", "last_name": "L"},
    )

    assert result["status"] == "error"
    assert "quota exceeded" in result["message"]


def test_division_label_routes_per_company():
    row = sheets_export.build_submission_row(
        form_data={"first_name": "A", "last_name": "B"},
        licenses=[],
        company_slug="prestige",
    )
    by_column = dict(zip(sheets_export.SHEET_COLUMNS, row))
    assert by_column["Division"] == "Prestig Inc"

    row = sheets_export.build_submission_row(
        form_data={"first_name": "A", "last_name": "B"},
        licenses=[],
        company_slug="xpress",
    )
    by_column = dict(zip(sheets_export.SHEET_COLUMNS, row))
    assert by_column["Division"] == "Xpress Inc"


def test_cdl_endorsement_combines_hazmat_and_twic():
    row = sheets_export.build_submission_row(
        form_data={
            "first_name": "A",
            "last_name": "B",
            "hazmat_endorsement": "Yes",
            "twic_card": "Yes",
        },
        licenses=[{"endorsements": "N"}],
        company_slug="prestige",
    )
    by_column = dict(zip(sheets_export.SHEET_COLUMNS, row))
    endorsement = by_column["CDL Endorsement"]
    assert "N" in endorsement
    assert "Hazmat" in endorsement
    assert "TWIC" in endorsement


def test_append_from_payload_uses_company_slug_from_form_data(monkeypatch):
    captured: dict[str, object] = {}

    def fake_append(**kwargs):
        captured.update(kwargs)
        return {"status": "appended", "tab": "Xpress"}

    monkeypatch.setattr(sheets_export, "append_submission_row", fake_append)

    payload = {
        "submission_key": "20260420_120000_inc-test",
        "form_data": {
            "first_name": "Real",
            "last_name": "Applicant",
            "company_slug": "xpress",
            "test_mode": False,
        },
        "licenses": [{"license_number": "X1"}],
    }

    result = sheets_export.append_from_payload(payload, storage_location="/tmp/x")
    assert result["status"] == "appended"
    assert captured["company_slug"] == "xpress"
    assert captured["submission_id"] == "20260420_120000_inc-test"
    assert captured["storage_location"] == "/tmp/x"
    assert captured["test_mode"] is False


def test_ensure_header_rewrites_when_mismatched(monkeypatch):
    creds_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "x@example.iam.gserviceaccount.com",
            "private_key": "k",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON=creds_json,
        APPLICANTS_SHEET_ID="sheet-xyz",
    )

    fake_worksheet = MagicMock()
    # Outdated header missing the new ProTransport columns.
    fake_worksheet.row_values.return_value = ["Submitted At", "Apply Date"]
    fake_worksheet.col_count = 5

    monkeypatch.setattr(
        sheets_export, "_open_worksheet", lambda *a, **k: fake_worksheet
    )

    result = sheets_export.append_submission_row(
        company_slug="prestige",
        form_data={"first_name": "R", "last_name": "L"},
    )

    assert result["status"] == "appended"
    fake_worksheet.update.assert_called_once()
    update_args, _ = fake_worksheet.update.call_args
    assert update_args[0] == "A1"
    assert update_args[1] == [sheets_export.SHEET_COLUMNS]
    fake_worksheet.resize.assert_called_once()
    fake_worksheet.insert_row.assert_called_once()


def test_append_decision_row_writes_to_approved_tab(monkeypatch):
    creds_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "x@example.iam.gserviceaccount.com",
            "private_key": "k",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON=creds_json,
        APPLICANTS_SHEET_ID="sheet-xyz",
    )

    fake_worksheet = MagicMock()
    fake_worksheet.row_values.return_value = list(sheets_export.DECISION_SHEET_COLUMNS)

    captured: dict[str, object] = {}

    def fake_open(spreadsheet_id, tab_name, credentials_info):
        captured["tab_name"] = tab_name
        return fake_worksheet

    monkeypatch.setattr(sheets_export, "_open_worksheet", fake_open)

    result = sheets_export.append_decision_row(
        decision="approved",
        decided_by="Dann",
        notes="looks great",
        company_slug="prestige",
        form_data={"first_name": "Jane", "last_name": "Driver"},
        submission_id="sub-99",
    )

    assert result["status"] == "appended"
    assert result["tab"] == "Approved"
    assert captured["tab_name"] == "Approved"
    fake_worksheet.insert_row.assert_called_once()
    args, kwargs = fake_worksheet.insert_row.call_args
    row_values = args[0]
    # Decision prefix lands at the start of the row.
    assert row_values[0] == "Approved"
    assert row_values[2] == "Dann"
    assert row_values[4] == "looks great"
    assert kwargs.get("index") == 2


def test_append_decision_row_routes_decline_to_declined_tab(monkeypatch):
    creds_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "x@example.iam.gserviceaccount.com",
            "private_key": "k",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON=creds_json,
        APPLICANTS_SHEET_ID="sheet-xyz",
    )

    fake_worksheet = MagicMock()
    fake_worksheet.row_values.return_value = list(sheets_export.DECISION_SHEET_COLUMNS)
    monkeypatch.setattr(
        sheets_export, "_open_worksheet", lambda *a, **k: fake_worksheet
    )

    result = sheets_export.append_decision_row(
        decision="DECLINED",  # case-insensitive
        decided_by="Safety",
        notes=None,
        company_slug="xpress",
        form_data={"first_name": "X", "last_name": "Y"},
    )

    assert result["status"] == "appended"
    assert result["tab"] == "Declined"


def test_append_decision_row_rejects_unknown_decision(monkeypatch):
    creds_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "x@example.iam.gserviceaccount.com",
            "private_key": "k",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    _set_secrets(
        monkeypatch,
        GOOGLE_SERVICE_ACCOUNT_JSON=creds_json,
        APPLICANTS_SHEET_ID="sheet-xyz",
    )

    result = sheets_export.append_decision_row(
        decision="maybe",
        decided_by="Dann",
        notes="",
        company_slug="prestige",
        form_data={"first_name": "X", "last_name": "Y"},
    )
    assert result["status"] == "error"
    assert "maybe" in result["message"]


def test_append_decision_from_payload_pulls_company_from_form_data(monkeypatch):
    captured: dict[str, object] = {}

    def fake_append(**kwargs):
        captured.update(kwargs)
        return {"status": "appended", "tab": "Approved"}

    monkeypatch.setattr(sheets_export, "append_decision_row", fake_append)

    payload = {
        "submission_key": "20260420_120000_inc-test",
        "form_data": {
            "first_name": "Real",
            "last_name": "Applicant",
            "company_slug": "xpress",
        },
    }
    result = sheets_export.append_decision_from_payload(
        payload, decision="approved", decided_by="Dann", notes="ok"
    )
    assert result["status"] == "appended"
    assert captured["company_slug"] == "xpress"
    assert captured["decision"] == "approved"
    assert captured["decided_by"] == "Dann"
    assert captured["notes"] == "ok"
    assert captured["submission_id"] == "20260420_120000_inc-test"
