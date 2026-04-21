from __future__ import annotations

from types import SimpleNamespace

import runtime_context


class FakeSessionState(dict):
    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - defensive parity with Streamlit session state
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value):
        self[key] = value


def test_resolve_company_slug_accepts_side_xpress_alias(monkeypatch):
    monkeypatch.setattr(runtime_context, "st", SimpleNamespace(query_params={"company": "sidexpress"}))

    assert runtime_context.resolve_company_slug() == "side-xpress"


def test_get_storage_namespace_uses_company_and_test_mode(monkeypatch):
    fake_state = FakeSessionState(company_slug="side-xpress", test_mode=True)
    monkeypatch.setattr(runtime_context, "st", SimpleNamespace(session_state=fake_state, query_params={}))

    assert runtime_context.get_storage_namespace() == "companies/side-xpress/test-mode"


def test_get_company_profile_returns_real_xpress_details():
    profile = runtime_context.get_company_profile("side-xpress")

    assert profile.name == "Xpress Trans, Inc"
    assert profile.address == "2905 W. Lake St."
    assert profile.city_state_zip == "Melrose Park, IL 60160"
    assert profile.phone == "708-356-4420"
    assert profile.email == "safety@xpresstransinc.com"