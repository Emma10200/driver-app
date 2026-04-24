"""Tests for the password-protected admin dashboard service."""

from __future__ import annotations

import json

import pytest

from services import admin_dashboard


@pytest.fixture(autouse=True)
def _reset_secret_cache():
    from submission_storage import _get_secret

    _get_secret.cache_clear()
    yield
    _get_secret.cache_clear()


def test_default_admin_password_used_when_secret_unset(monkeypatch):
    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", lambda *_a, **_k: None)
    assert admin_dashboard._expected_password() == admin_dashboard.DEFAULT_ADMIN_PASSWORD


def test_admin_password_overridable_via_secret(monkeypatch):
    def fake_secret(name, default=None):
        if name == "ADMIN_PASSWORD":
            return "S3curePass!"
        return default

    monkeypatch.setattr(admin_dashboard, "get_runtime_secret", fake_secret)
    assert admin_dashboard._expected_password() == "S3curePass!"


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
