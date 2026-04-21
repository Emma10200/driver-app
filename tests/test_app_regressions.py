from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import services.draft_service as draft_service


APP_FILE = Path(__file__).resolve().parents[1] / "app.py"


def _widget_by_label(collection, label: str):
    for widget in collection:
        if getattr(widget, "label", None) == label:
            return widget
    available = [getattr(widget, "label", None) for widget in collection]
    raise AssertionError(f"Could not find widget labeled {label!r}. Available labels: {available}")


def _build_app(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBMISSION_STORAGE_BACKEND", "local")
    monkeypatch.setattr(draft_service, "LOCAL_STORAGE_DIR", tmp_path)
    return AppTest.from_file(str(APP_FILE), default_timeout=15)


def test_page_one_next_advances_without_ssn_exception(monkeypatch, tmp_path):
    at = _build_app(monkeypatch, tmp_path)
    at.run(timeout=15)

    _widget_by_label(at.text_input, "First Name *").set_value("Emma")
    _widget_by_label(at.text_input, "Last Name *").set_value("Driver")
    _widget_by_label(at.text_input, "Social Security Number *").set_value("123456789")
    _widget_by_label(at.text_input, "Current Address *").set_value("123 Main St")
    _widget_by_label(at.text_input, "City *").set_value("Fontana")
    _widget_by_label(at.text_input, "Zip Code *").set_value("92335")
    _widget_by_label(at.text_input, "Primary Phone *").set_value("5551234567")
    _widget_by_label(at.text_input, "Email Address *").set_value("emma@example.com")
    _widget_by_label(at.text_input, "Emergency Contact Name *").set_value("John Driver")
    _widget_by_label(at.text_input, "Emergency Contact Phone *").set_value("5557654321")
    _widget_by_label(at.selectbox, "State *").set_value("CA")

    _widget_by_label(at.button, "Next →").click().run(timeout=15)

    assert not at.exception
    assert at.session_state["current_page"] == 2
    assert at.session_state["form_data"]["ssn"] == "123456789"
    assert any(node.value == "Company Questions & Driving Experience" for node in at.subheader)


def test_page_seven_next_advances_to_fcra(monkeypatch, tmp_path):
    at = _build_app(monkeypatch, tmp_path)
    at.session_state["current_page"] = 7
    at.run(timeout=15)

    _widget_by_label(at.checkbox, "I certify I have read and understand the Drug and Alcohol Policy *").check()
    _widget_by_label(at.checkbox, "I certify that all information in this application is true and complete *").check()
    _widget_by_label(at.text_input, "Full Legal Name (typed signature) *").set_value("Emma Driver")

    _widget_by_label(at.button, "Next → (FCRA Disclosure)").click().run(timeout=15)

    assert not at.exception
    assert at.session_state["current_page"] == 8
    assert any("FCRA Disclosure" in node.value for node in at.subheader)
