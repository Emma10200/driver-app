from __future__ import annotations

from types import SimpleNamespace

from config import CompanyProfile
import services.test_mode_service as test_mode_service


class FakeSessionState(dict):
    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - defensive parity with Streamlit session state
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value):
        self[key] = value


def test_activate_safe_test_application_sets_review_ready_state(monkeypatch):
    fake_state = FakeSessionState(company_slug="prestige", admin_tools_enabled=True)

    def fake_reset_application_state():
        fake_state.clear()

    def fake_init_session_state():
        fake_state.update(
            {
                "company_slug": "prestige",
                "admin_tools_enabled": False,
                "test_mode": False,
                "form_data": {},
                "licenses": [],
                "employers": [],
                "accidents": [],
                "violations": [],
                "uploaded_documents": [],
                "current_page": 1,
            }
        )

    monkeypatch.setattr(test_mode_service, "st", SimpleNamespace(session_state=fake_state))
    monkeypatch.setattr(test_mode_service, "reset_application_state", fake_reset_application_state)
    monkeypatch.setattr(test_mode_service, "init_session_state", fake_init_session_state)
    monkeypatch.setattr(
        test_mode_service,
        "get_active_company_profile",
        lambda: CompanyProfile(slug="prestige", name="PRESTIGE TRANSPORTATION INC."),
    )

    test_mode_service.activate_safe_test_application(jump_to_review=True)

    assert fake_state["test_mode"] is True
    assert fake_state["current_page"] == 12
    assert fake_state["form_data"]["company_slug"] == "prestige"
    assert fake_state["form_data"]["review_confirm"] is True
    assert fake_state["licenses"][0]["number"] == "T1234567"
    assert fake_state["personal_ssn_display"] == "123-45-6789"