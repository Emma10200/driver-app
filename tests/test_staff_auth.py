from __future__ import annotations

from types import SimpleNamespace

from services import staff_auth


def test_contact_directory_allowed_emails_include_company_dispatchers_and_deyana():
    companies = [{"dispatch_email": "Dispatch@Prestige.Inc"}]
    contacts = [
        {"email": "dispatch7@prestige.inc"},
        {"email": "dispatch@xpresstransinc.com"},
        {"email": ""},
    ]

    assert staff_auth.contact_directory_allowed_emails(companies, contacts) == {
        "dispatch@prestige.inc",
        "dispatch7@prestige.inc",
        "dispatch@xpresstransinc.com",
        "deyana@prestigetransportation.com",
    }


def test_staff_allowed_emails_combines_qbo_contacts_and_accounts_default(monkeypatch):
    monkeypatch.setattr(
        staff_auth,
        "qbo_allowed_emails",
        lambda: {"owner@example.com"},
    )
    monkeypatch.setattr(staff_auth, "_safe_contact_directory_allowed_emails", lambda: {"dispatch1@prestige.inc"})

    assert staff_auth.staff_allowed_emails() == {
        "accounts@prestige.inc",
        "owner@example.com",
        "dispatch1@prestige.inc",
    }


def test_staff_allowed_emails_defaults_to_accounts_when_qbo_secret_missing(monkeypatch):
    monkeypatch.setattr(staff_auth, "qbo_allowed_emails", lambda: set())
    monkeypatch.setattr(staff_auth, "_safe_contact_directory_allowed_emails", lambda: set())

    assert staff_auth.staff_allowed_emails() == {"accounts@prestige.inc"}


def test_staff_allowed_emails_include_dispatch_seed_and_deyana(monkeypatch):
    monkeypatch.setattr(staff_auth, "qbo_allowed_emails", lambda: set())

    allowed = staff_auth.staff_allowed_emails()

    assert "accounts@prestige.inc" in allowed
    assert "dispatch7@prestige.inc" in allowed
    assert "dispatch7@prestigecalifornia.com" in allowed
    assert "dispatch@xpresstransinc.com" in allowed
    assert "deyana@prestigetransportation.com" in allowed


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
