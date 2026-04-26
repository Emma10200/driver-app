from __future__ import annotations
import html
from types import SimpleNamespace
from unittest.mock import MagicMock


import streamlit as st
import ui.common as common
import app

def test_app_header_escaping(monkeypatch):
    captured_markdown = []
    mock_markdown = MagicMock(side_effect=lambda content, unsafe_allow_html=False: captured_markdown.append(content))
    monkeypatch.setattr(st, "markdown", mock_markdown)

    payload = "<script>alert('xss')</script>"
    escaped_payload = html.escape(payload)

    fake_company = SimpleNamespace(
        name=payload,
        address=payload,
        city_state_zip=payload,
        phone=payload,
        email=payload,
        brand_color="#ffffff",
        slug="test-slug"
    )

    monkeypatch.setattr(common, "get_active_company_profile", lambda: fake_company)
    monkeypatch.setattr(common, "is_test_mode_active", lambda: False)
    monkeypatch.setattr(common, "_sync_browser_autofill_via_js", lambda: None)
    monkeypatch.setattr(common, "render_missing_fields_banner", lambda: None)
    monkeypatch.setattr(common, "st", st)

    common.render_app_shell()

    header_html = next((m for m in captured_markdown if 'class="app-header"' in m), None)
    assert header_html is not None
    assert payload not in header_html
    assert escaped_payload in header_html


def test_brand_color_rejects_markup_injection(monkeypatch):
    captured_markdown = []
    mock_markdown = MagicMock(side_effect=lambda content, unsafe_allow_html=False: captured_markdown.append(content))
    monkeypatch.setattr(st, "markdown", mock_markdown)

    payload = "</style><script>alert('xss')</script>"

    fake_company = SimpleNamespace(
        name="Test Company",
        address="123 Test St",
        city_state_zip="Test City, CA 90000",
        phone="555-0100",
        email="safety@example.com",
        brand_color=payload,
        slug="test-slug",
    )

    monkeypatch.setattr(common, "get_active_company_profile", lambda: fake_company)
    monkeypatch.setattr(common, "is_test_mode_active", lambda: False)
    monkeypatch.setattr(common, "_sync_browser_autofill_via_js", lambda: None)
    monkeypatch.setattr(common, "render_missing_fields_banner", lambda: None)
    monkeypatch.setattr(common, "st", st)

    common.render_app_shell()

    rendered = "\n".join(captured_markdown)
    assert payload not in rendered
    assert "<script>" not in rendered


def test_brand_color_allows_simple_css_colors():
    assert common._is_safe_css_color("#3E6FA3")
    assert common._is_safe_css_color("rgb(62, 111, 163)")
    assert common._is_safe_css_color("steelblue")
    assert not common._is_safe_css_color("url(javascript:alert(1))")

def test_company_picker_does_not_leak_company_list(monkeypatch):
    """The landing page must not expose the list of companies; it should only
    show contact info so visitors can request the right link."""
    captured_markdown = []
    mock_markdown = MagicMock(side_effect=lambda content, unsafe_allow_html=False: captured_markdown.append(content))
    monkeypatch.setattr(st, "markdown", mock_markdown)

    fake_profile = SimpleNamespace(
        name="Secret Company LLC",
        city_state_zip="Secret City, SC 00000",
        slug="secret-co",
    )

    import config
    monkeypatch.setattr(config, "COMPANY_PROFILES", {"secret-co": fake_profile})
    monkeypatch.setattr(app, "st", st)

    app._render_company_picker()

    rendered = "\n".join(captured_markdown)
    assert "Secret Company LLC" not in rendered
    assert "secret-co" not in rendered
    # The help page should surface the office contacts.
    assert "Dann" in rendered
    assert "Emma" in rendered
    assert "Deyana" in rendered

def test_document_filename_escaping(monkeypatch):
    captured_markdown = []

    def fake_markdown(content, **kwargs):
        captured_markdown.append(content)

    mock_markdown = MagicMock(side_effect=fake_markdown)
    monkeypatch.setattr(st, "markdown", mock_markdown)

    payload = "my_file`</script><script>alert(1)</script>.pdf"
    expected_safe_name = html.escape(payload.replace("`", "_"))

    fake_session_state = {
        "uploaded_documents": [
            {
                "file_name": payload,
                "size_bytes": 1024,
            }
        ]
    }

    import services.document_service as document_service

    class FakeSessionState(dict):
        def __getattr__(self, key: str):
            try:
                return self[key]
            except KeyError as exc:
                raise AttributeError(key) from exc

        def __setattr__(self, key: str, value):
            self[key] = value

    fake_st = MagicMock()
    fake_st.session_state = FakeSessionState(fake_session_state)
    fake_st.markdown.side_effect = fake_markdown

    # 1. Test review_submit.py render path (it's hard to test the whole page, so we test document_service instead)
    monkeypatch.setattr(document_service, "st", fake_st)
    monkeypatch.setattr(document_service, "is_test_mode_active", lambda: False)
    monkeypatch.setattr(document_service, "get_pending_uploads", lambda: [])
    monkeypatch.setattr(document_service, "_normalize_pending_uploads", lambda: ([], []))

    document_service.render_supporting_documents_section()

    doc_markdown = next((m for m in captured_markdown if "- `" in m), None)
    assert doc_markdown is not None
    assert payload not in doc_markdown
    assert expected_safe_name in doc_markdown
