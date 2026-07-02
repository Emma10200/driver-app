from __future__ import annotations

from types import SimpleNamespace

from services import staff_auth


def test_staff_allowed_emails_mirrors_qbo_allowlist(monkeypatch):
    monkeypatch.setattr(
        staff_auth,
        "qbo_allowed_emails",
        lambda: {"accounts@prestige.inc", "owner@example.com"},
    )

    assert staff_auth.staff_allowed_emails() == {"accounts@prestige.inc", "owner@example.com"}


def test_staff_allowed_emails_defaults_to_accounts_when_qbo_secret_missing(monkeypatch):
    monkeypatch.setattr(staff_auth, "qbo_allowed_emails", lambda: set())

    assert staff_auth.staff_allowed_emails() == {"accounts@prestige.inc"}


def test_staff_access_granted_for_allowed_google_user(monkeypatch):
    fake_st = SimpleNamespace(user=SimpleNamespace(is_logged_in=True, email="ACCOUNTS@PRESTIGE.INC"))
    monkeypatch.setattr(staff_auth, "st", fake_st)
    monkeypatch.setattr(staff_auth, "staff_allowed_emails", lambda: {"accounts@prestige.inc"})

    assert staff_auth.staff_access_granted()


def test_staff_access_rejects_non_allowlisted_google_user(monkeypatch):
    fake_st = SimpleNamespace(user=SimpleNamespace(is_logged_in=True, email="someone@example.com"))
    monkeypatch.setattr(staff_auth, "st", fake_st)
    monkeypatch.setattr(staff_auth, "staff_allowed_emails", lambda: {"accounts@prestige.inc"})

    assert not staff_auth.staff_access_granted()


def test_staff_access_rejects_logged_out_user(monkeypatch):
    fake_st = SimpleNamespace(user=SimpleNamespace(is_logged_in=False, email="accounts@prestige.inc"))
    monkeypatch.setattr(staff_auth, "st", fake_st)
    monkeypatch.setattr(staff_auth, "staff_allowed_emails", lambda: {"accounts@prestige.inc"})

    assert not staff_auth.staff_access_granted()
