"""Tests for the password-protected admin dashboard service."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services import admin_dashboard


@pytest.fixture(autouse=True)
def _reset_secret_cache():
    from submission_storage import _get_secret

    _get_secret.cache_clear()
    yield
    _get_secret.cache_clear()


def test_admin_password_required_when_secret_unset(monkeypatch):
    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", lambda *_a, **_k: None)
    assert admin_dashboard._expected_password() is None


def test_admin_password_overridable_via_secret(monkeypatch):
    def fake_secret(name, default=None):
        if name == "ADMIN_PASSWORD":
            return "S3curePass!"
        return default

    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", fake_secret)
    assert admin_dashboard._expected_password() == "S3curePass!"


def test_admin_auth_mode_defaults_to_both(monkeypatch):
    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", lambda *_a, **_k: None)

    assert admin_dashboard._admin_auth_mode() == "both"


def test_invalid_admin_auth_mode_falls_back_to_both(monkeypatch):
    def fake_secret(name, default=None):
        if name == "ADMIN_AUTH_MODE":
            return "surprise"
        return default

    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", fake_secret)

    assert admin_dashboard._admin_auth_mode() == "both"


def test_allowed_admin_emails_are_normalized(monkeypatch):
    def fake_secret(name, default=None):
        if name == "ADMIN_ALLOWED_EMAILS":
            return " Owner@Gmail.com, safety@gmail.com\naccounting@gmail.com; "
        return default

    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", fake_secret)

    assert admin_dashboard._allowed_admin_emails() == {
        "owner@gmail.com",
        "safety@gmail.com",
        "accounting@gmail.com",
    }


def test_google_user_must_be_allowlisted(monkeypatch):
    def fake_secret(name, default=None):
        if name == "ADMIN_ALLOWED_EMAILS":
            return "owner@gmail.com"
        return default

    fake_st = SimpleNamespace(user=SimpleNamespace(is_logged_in=True, email="OWNER@gmail.com"))
    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", fake_secret)
    monkeypatch.setattr(admin_dashboard, "st", fake_st)

    assert admin_dashboard._google_user_is_allowed()

    fake_st.user.email = "someoneelse@gmail.com"
    assert not admin_dashboard._google_user_is_allowed()


def test_admin_access_allows_google_without_password(monkeypatch):
    def fake_secret(name, default=None):
        values = {
            "ADMIN_AUTH_MODE": "google",
            "ADMIN_ALLOWED_EMAILS": "owner@gmail.com",
            "ADMIN_PASSWORD": "",
        }
        return values.get(name, default)

    fake_st = SimpleNamespace(user=SimpleNamespace(is_logged_in=True, email="owner@gmail.com"))
    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", fake_secret)
    monkeypatch.setattr(admin_dashboard, "st", fake_st)

    assert admin_dashboard._admin_access_granted()


def test_admin_access_rejects_non_allowlisted_google_user(monkeypatch):
    def fake_secret(name, default=None):
        values = {
            "ADMIN_AUTH_MODE": "google",
            "ADMIN_ALLOWED_EMAILS": "owner@gmail.com",
            "ADMIN_PASSWORD": "",
        }
        return values.get(name, default)

    fake_st = SimpleNamespace(user=SimpleNamespace(is_logged_in=True, email="intruder@gmail.com"))
    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", fake_secret)
    monkeypatch.setattr(admin_dashboard, "st", fake_st)

    assert not admin_dashboard._admin_access_granted()


def test_authentication_requires_current_password(monkeypatch):
    fake_st = SimpleNamespace(session_state={})
    monkeypatch.setattr(admin_dashboard, "st", fake_st)

    assert not admin_dashboard._authentication_is_current("old-pass")

    admin_dashboard._mark_authenticated("old-pass")

    assert admin_dashboard._authentication_is_current("old-pass")
    assert not admin_dashboard._authentication_is_current("new-pass")
    assert not admin_dashboard._authentication_is_current(None)


def test_clear_authentication_removes_fingerprint(monkeypatch):
    fake_st = SimpleNamespace(
        session_state={
            admin_dashboard.SESSION_AUTH_KEY: True,
            admin_dashboard.SESSION_AUTH_FINGERPRINT_KEY: "abc",
        }
    )
    monkeypatch.setattr(admin_dashboard, "st", fake_st)

    admin_dashboard._clear_authentication()

    assert fake_st.session_state[admin_dashboard.SESSION_AUTH_KEY] is False
    assert admin_dashboard.SESSION_AUTH_FINGERPRINT_KEY not in fake_st.session_state


def test_iter_submission_dirs_finds_nested_submissions(tmp_path):
    # Mimic both legacy (root-level) and namespaced layouts.
    legacy = tmp_path / "20260101_aaa_smith"
    legacy.mkdir()
    (legacy / "submission.json").write_text("{}")

    nested = tmp_path / "companies" / "prestige" / "live" / "20260102_bbb_jones"
    nested.mkdir(parents=True)
    (nested / "submission.json").write_text("{}")

    decoy = tmp_path / "companies" / "prestige" / "drafts" / "draft-1"
    decoy.mkdir(parents=True)
    (decoy / "draft.json").write_text("{}")  # no submission.json -> ignored

    found = admin_dashboard._iter_submission_dirs(tmp_path)
    assert legacy in found
    assert nested in found
    assert decoy not in found


def test_load_submission_returns_none_for_invalid_json(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "submission.json").write_text("{not valid")
    assert admin_dashboard._load_submission(bad) is None


def test_load_submission_round_trip(tmp_path):
    good = tmp_path / "ok"
    good.mkdir()
    payload = {"submission_key": "abc", "form_data": {"first_name": "Z"}}
    (good / "submission.json").write_text(json.dumps(payload))
    assert admin_dashboard._load_submission(good) == payload
