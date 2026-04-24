from __future__ import annotations

from email.message import EmailMessage
from types import SimpleNamespace

import app_sections.review_submit as review_submit
import services.notification_service as notification_service
from submission_storage import get_runtime_secret as _real_get_runtime_secret


def _fake_secrets(values: dict[str, str]):
    def _lookup(name, default=""):
        return values.get(name, default)

    return _lookup


def test_notification_settings_auto_includes_company_safety_email(monkeypatch):
    monkeypatch.setattr(
        notification_service,
        "get_runtime_secret",
        _fake_secrets({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_EMAIL": "alerts@example.com",
            "INTERNAL_NOTIFICATION_TO": "ops@example.com",
        }),
    )

    prestige = notification_service._notification_settings("prestige", test_mode=False)
    assert prestige["recipients"] == [
        "ops@example.com",
        "safety@prestigecalifornia.com",
        "dann@prestigetransportation.com",
    ]

    xpress = notification_service._notification_settings("side-xpress", test_mode=False)
    assert xpress["recipients"] == [
        "ops@example.com",
        "safety@xpresstransinc.com",
        "dann@prestigetransportation.com",
    ]


def test_notification_settings_test_mode_skips_safety_email(monkeypatch):
    monkeypatch.setattr(
        notification_service,
        "get_runtime_secret",
        _fake_secrets({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_EMAIL": "alerts@example.com",
            "INTERNAL_NOTIFICATION_TO": "ops@example.com",
            "TEST_INTERNAL_NOTIFICATION_TO": "qa@example.com",
        }),
    )

    settings = notification_service._notification_settings("prestige", test_mode=True)
    assert settings["recipients"] == ["qa@example.com"]
    assert "safety@prestigecalifornia.com" not in settings["recipients"]
    assert "dann@prestigetransportation.com" not in settings["recipients"]


def test_notification_settings_always_cc_override(monkeypatch):
    monkeypatch.setattr(
        notification_service,
        "get_runtime_secret",
        _fake_secrets({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_EMAIL": "alerts@example.com",
            "INTERNAL_NOTIFICATION_TO": "ops@example.com",
            "ALWAYS_NOTIFY_EMAILS": "owner@example.com, exec@example.com",
        }),
    )

    settings = notification_service._notification_settings("prestige", test_mode=False)
    assert "owner@example.com" in settings["recipients"]
    assert "exec@example.com" in settings["recipients"]
    assert "dann@prestigetransportation.com" not in settings["recipients"]


def test_notification_settings_always_cc_dedupes(monkeypatch):
    monkeypatch.setattr(
        notification_service,
        "get_runtime_secret",
        _fake_secrets({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_FROM_EMAIL": "alerts@example.com",
            "INTERNAL_NOTIFICATION_TO": "ops@example.com, dann@prestigetransportation.com",
        }),
    )

    settings = notification_service._notification_settings("prestige", test_mode=False)
    # dann should only appear once even though it's both in INTERNAL_NOTIFICATION_TO
    # and in the always-CC default list.
    lowered = [r.lower() for r in settings["recipients"]]
    assert lowered.count("dann@prestigetransportation.com") == 1


# Avoid unused-import warning when running selectively.
_ = _real_get_runtime_secret


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
        application_pdf=b"%PDF-1.4\n% test packet\n",
    )

    assert result["status"] == "sent"
    assert smtp_instance is not None
    assert [call[0] for call in smtp_instance.calls] == ["ehlo", "starttls", "ehlo", "login", "send_message"]
    assert smtp_instance.sent_message is not None
    assert smtp_instance.sent_message["Reply-To"] == "emma@example.com"
    text_part = smtp_instance.sent_message.get_body(preferencelist=("plain",))
    assert text_part is not None
    assert "Supporting document count: 1" in text_part.get_content()


def test_attempt_submission_notification_retries_after_error(monkeypatch):
    fake_state = FakeSessionState(
        form_data={"first_name": "Emma", "last_name": "Driver"},
        uploaded_documents=[],
        saved_submission_dir="driver-applications/submissions/test",
        submission_artifacts={"application_pdf": b"%PDF-1.4\n% test packet\n"},
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
        application_pdf=b"%PDF-1.4\n% test packet\n",
    )

    assert result["status"] == "sent"
    assert smtp_instance is not None
    assert smtp_instance.sent_message is not None
    assert smtp_instance.sent_message["Subject"].startswith("[TEST] ")
    assert smtp_instance.sent_message["To"] == "statements@prestigetransportation.com"

def _stub_settings(monkeypatch, *, max_attachment_bytes: int = 0):
    monkeypatch.setattr(
        notification_service,
        "_notification_settings",
        lambda company_slug, test_mode: {
            "host": "smtp.example.com",
            "port": 587,
            "username": "mailer",
            "password": "secret",
            "from_email": "alerts@example.com",
            "recipients": ["safety@example.com"],
            "use_tls": True,
            "use_ssl": False,
            "attachment_password": "",
            "max_attachment_bytes": max_attachment_bytes,
        },
    )


def test_notification_attaches_csv_and_supporting_documents(monkeypatch):
    smtp_instance: FakeSMTP | None = None

    def fake_smtp(host, port, timeout=30):
        nonlocal smtp_instance
        smtp_instance = FakeSMTP(host, port, timeout)
        return smtp_instance

    _stub_settings(monkeypatch)
    monkeypatch.setattr(notification_service.smtplib, "SMTP", fake_smtp)

    result = notification_service.send_internal_submission_notification(
        form_data={"first_name": "Jane", "last_name": "Doe"},
        submission_result={"location_label": "local/path"},
        uploaded_documents=[{"file_name": "license.pdf"}],
        application_pdf=b"%PDF-1.4 packet",
        application_csv=b"Field,Value\nFirst Name,Jane\n",
        supporting_document_payloads=[
            {
                "file_name": "license.pdf",
                "content_type": "application/pdf",
                "size_bytes": 12,
                "content": b"FAKEPDFBYTES",
            }
        ],
    )

    assert result["status"] == "sent"
    assert smtp_instance is not None and smtp_instance.sent_message is not None
    filenames = sorted(
        part.get_filename() for part in smtp_instance.sent_message.iter_attachments()
    )
    assert any(name.endswith(".pdf") and "packet" in name for name in filenames)
    assert any(name.endswith(".csv") for name in filenames)
    assert "license.pdf" in filenames
    body = smtp_instance.sent_message.get_body(preferencelist=("plain",)).get_content()
    assert "license.pdf" in body
    assert "spreadsheet" in body.lower()


def test_notification_skips_oversize_supporting_documents(monkeypatch):
    smtp_instance: FakeSMTP | None = None

    def fake_smtp(host, port, timeout=30):
        nonlocal smtp_instance
        smtp_instance = FakeSMTP(host, port, timeout)
        return smtp_instance

    # Cap so small that even one tiny doc cannot fit alongside the PDF + CSV.
    _stub_settings(monkeypatch, max_attachment_bytes=50)
    monkeypatch.setattr(notification_service.smtplib, "SMTP", fake_smtp)

    result = notification_service.send_internal_submission_notification(
        form_data={"first_name": "Jane", "last_name": "Doe"},
        submission_result={"location_label": "local/path"},
        uploaded_documents=[{"file_name": "huge.pdf"}],
        application_pdf=b"%PDF-1.4 " + b"x" * 40,
        application_csv=b"Field,Value\n",
        supporting_document_payloads=[
            {
                "file_name": "huge.pdf",
                "content_type": "application/pdf",
                "size_bytes": 1000,
                "content": b"x" * 1000,
            }
        ],
    )

    assert result["status"] == "sent"
    assert smtp_instance is not None and smtp_instance.sent_message is not None
    filenames = [part.get_filename() for part in smtp_instance.sent_message.iter_attachments()]
    assert "huge.pdf" not in filenames  # was skipped due to size cap
    body = smtp_instance.sent_message.get_body(preferencelist=("plain",)).get_content()
    assert "NOT attached" in body
    assert "huge.pdf" in body
