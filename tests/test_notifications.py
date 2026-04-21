from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace

import app_sections.review_submit as review_submit
import services.notification_service as notification_service


class FakeSessionState(dict):
    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - defensive parity with Streamlit session state
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value):
        self[key] = value


class FakeSMTP:
    def __init__(self, host: str, port: int, timeout: int = 30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple[str, object]] = []
        self.sent_message: EmailMessage | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        self.calls.append(("ehlo", None))

    def starttls(self):
        self.calls.append(("starttls", None))

    def login(self, username: str, password: str):
        self.calls.append(("login", (username, password)))

    def send_message(self, message: EmailMessage):
        self.calls.append(("send_message", None))
        self.sent_message = message


def test_send_internal_submission_notification_uses_tls_and_reply_to(monkeypatch):
    smtp_instance: FakeSMTP | None = None

    def fake_smtp(host: str, port: int, timeout: int = 30):
        nonlocal smtp_instance
        smtp_instance = FakeSMTP(host, port, timeout)
        return smtp_instance

    monkeypatch.setattr(
        notification_service,
        "_notification_settings",
        lambda company_slug, test_mode: {
            "host": "smtp.example.com",
            "port": 587,
            "username": "mailer",
            "password": "secret",
            "from_email": "alerts@example.com",
            "recipients": ["ops@example.com"],
            "use_tls": True,
            "use_ssl": False,
        },
    )
    monkeypatch.setattr(notification_service.smtplib, "SMTP", fake_smtp)

    result = notification_service.send_internal_submission_notification(
        form_data={
            "first_name": "Emma",
            "last_name": "Driver",
            "email": "emma@example.com",
            "primary_phone": "5551234567",
            "preferred_office": "California Office",
            "final_submission_timestamp": "2026-04-21T16:05:00",
        },
        submission_result={"location_label": "driver-applications/submissions/test"},
        uploaded_documents=[{"file_name": "license.pdf"}],
    )

    assert result["status"] == "sent"
    assert smtp_instance is not None
    assert [call[0] for call in smtp_instance.calls] == ["ehlo", "starttls", "ehlo", "login", "send_message"]
    assert smtp_instance.sent_message is not None
    assert smtp_instance.sent_message["Reply-To"] == "emma@example.com"
    assert "Supporting document count: 1" in smtp_instance.sent_message.get_content()


def test_attempt_submission_notification_retries_after_error(monkeypatch):
    fake_state = FakeSessionState(
        form_data={"first_name": "Emma", "last_name": "Driver"},
        uploaded_documents=[],
        saved_submission_dir="driver-applications/submissions/test",
        submission_notification_sent=False,
        submission_notification_status_code=None,
        submission_notification_status=None,
        submission_notification_error="temporary smtp outage",
    )
    calls: list[str] = []

    def fake_send_internal_submission_notification(**kwargs):
        calls.append(kwargs["submission_result"]["location_label"])
        return {"status": "sent", "message": "Internal notification sent to ops@example.com."}

    monkeypatch.setattr(review_submit, "st", SimpleNamespace(session_state=fake_state))
    monkeypatch.setattr(review_submit, "send_internal_submission_notification", fake_send_internal_submission_notification)

    review_submit._attempt_submission_notification()

    assert calls == ["driver-applications/submissions/test"]
    assert fake_state["submission_notification_sent"] is True
    assert fake_state["submission_notification_status_code"] == "sent"
    assert fake_state["submission_notification_error"] is None


def test_attempt_submission_notification_skips_when_disabled(monkeypatch):
    fake_state = FakeSessionState(
        form_data={"first_name": "Emma", "last_name": "Driver"},
        uploaded_documents=[],
        saved_submission_dir="driver-applications/submissions/test",
        submission_notification_sent=False,
        submission_notification_status_code="disabled",
        submission_notification_status="Internal notification email is not configured yet.",
        submission_notification_error=None,
    )
    calls: list[str] = []

    monkeypatch.setattr(review_submit, "st", SimpleNamespace(session_state=fake_state))
    monkeypatch.setattr(
        review_submit,
        "send_internal_submission_notification",
        lambda **kwargs: calls.append("called") or {"status": "sent", "message": "unexpected"},
    )

    review_submit._attempt_submission_notification()

    assert calls == []


def test_send_internal_submission_notification_in_test_mode_uses_fallback_recipients(monkeypatch):
    smtp_instance: FakeSMTP | None = None

    def fake_smtp(host: str, port: int, timeout: int = 30):
        nonlocal smtp_instance
        smtp_instance = FakeSMTP(host, port, timeout)
        return smtp_instance

    monkeypatch.setattr(
        notification_service,
        "_notification_settings",
        lambda company_slug, test_mode: {
            "host": "smtp.gmail.com",
            "port": 587,
            "username": "mailer",
            "password": "secret",
            "from_email": "alerts@example.com",
            "recipients": ["statements@prestigetransportation.com"],
            "use_tls": True,
            "use_ssl": False,
        },
    )
    monkeypatch.setattr(notification_service.smtplib, "SMTP", fake_smtp)

    result = notification_service.send_internal_submission_notification(
        form_data={
            "company_slug": "prestige",
            "test_mode": True,
            "first_name": "Test",
            "last_name": "Applicant",
            "email": "qa@example.com",
        },
        submission_result={"location_label": "driver-applications/companies/prestige/test-mode/submissions/test"},
        uploaded_documents=[],
    )

    assert result["status"] == "sent"
    assert smtp_instance is not None
    assert smtp_instance.sent_message is not None
    assert smtp_instance.sent_message["Subject"].startswith("[TEST] ")
    assert smtp_instance.sent_message["To"] == "statements@prestigetransportation.com"