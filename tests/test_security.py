from __future__ import annotations
import html
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

# Mock dependencies
sys.modules['streamlit'] = MagicMock()
sys.modules['streamlit.components.v1'] = MagicMock()
sys.modules['requests'] = MagicMock()
sys.modules['fpdf2'] = MagicMock()
sys.modules['fpdf'] = MagicMock()
sys.modules['pypdf'] = MagicMock()
sys.modules['dotenv'] = MagicMock()

import streamlit as st
import ui.common as common
import app

def test_app_header_escaping(monkeypatch):
    captured_markdown = []
    st.markdown.side_effect = lambda content, unsafe_allow_html=False: captured_markdown.append(content)

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

def test_company_picker_escaping(monkeypatch):
    captured_markdown = []
    st.markdown.side_effect = lambda content, unsafe_allow_html=False: captured_markdown.append(content)

    payload = "<script>alert('xss')</script>"
    escaped_payload = html.escape(payload)

    fake_profile = SimpleNamespace(
        name=payload,
        city_state_zip=payload,
        slug="test-slug"
    )

    import config
    monkeypatch.setattr(config, "COMPANY_PROFILES", {"test-slug": fake_profile})
    monkeypatch.setattr(app, "st", st)
    st.columns.return_value = [MagicMock()]

    app._render_company_picker()

    # Find the markdown call containing the profile name and city
    picker_markdown = next((m for m in captured_markdown if "</h3>" in m), None)
    assert picker_markdown is not None
    assert payload not in picker_markdown
    assert escaped_payload in picker_markdown
