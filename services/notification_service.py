"""Internal email notification helpers."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from config import DEFAULT_COMPANY_SLUG
from runtime_context import is_test_mode_active
from submission_storage import get_runtime_secret


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _recipient_secret_key(company_slug: str) -> str:
    return f"INTERNAL_NOTIFICATION_TO_{company_slug.upper().replace('-', '_')}"


def _notification_settings(company_slug: str, *, test_mode: bool) -> dict[str, Any]:
    recipients_raw = (
        (get_runtime_secret("TEST_INTERNAL_NOTIFICATION_TO", "") or "") if test_mode else ""
    ) or (
        get_runtime_secret(_recipient_secret_key(company_slug), "")
        or get_runtime_secret("INTERNAL_NOTIFICATION_TO", "")
        or ""
    )
    recipients = [item.strip() for item in recipients_raw.split(",") if item.strip()]
    return {
        "host": (get_runtime_secret("SMTP_HOST", "") or "").strip(),
        "port": int((get_runtime_secret("SMTP_PORT", "587") or "587").strip()),
        "username": (get_runtime_secret("SMTP_USERNAME", "") or "").strip(),
        "password": get_runtime_secret("SMTP_PASSWORD", "") or "",
        "from_email": (get_runtime_secret("SMTP_FROM_EMAIL", "") or "").strip(),
        "recipients": recipients,
        "use_tls": _as_bool(get_runtime_secret("SMTP_USE_TLS", "true"), True),
        "use_ssl": _as_bool(get_runtime_secret("SMTP_USE_SSL", "false"), False),
    }


def notifications_enabled(company_slug: str, *, test_mode: bool) -> bool:
    settings = _notification_settings(company_slug, test_mode=test_mode)
    return bool(settings["host"] and settings["from_email"] and settings["recipients"])


def _deliver_message(message: EmailMessage, settings: dict[str, Any]) -> None:
    if settings["use_ssl"]:
        with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30) as server:
            server.ehlo()
            if settings["username"]:
                server.login(settings["username"], settings["password"])
            server.send_message(message)
        return

    with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as server:
        server.ehlo()
        if settings["use_tls"]:
            server.starttls()
            server.ehlo()
        if settings["username"]:
            server.login(settings["username"], settings["password"])
        server.send_message(message)


def send_internal_submission_notification(
    *,
    form_data: dict[str, Any],
    submission_result: dict[str, Any],
    uploaded_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    company_slug = str(form_data.get("company_slug") or DEFAULT_COMPANY_SLUG).strip() or DEFAULT_COMPANY_SLUG
    test_mode = bool(form_data.get("test_mode")) or is_test_mode_active()

    if not notifications_enabled(company_slug, test_mode=test_mode):
        return {
            "status": "disabled",
            "message": "Internal notification email is not configured yet.",
        }

    settings = _notification_settings(company_slug, test_mode=test_mode)

    applicant_name = " ".join(
        part
        for part in [
            str(form_data.get("first_name", "")).strip(),
            str(form_data.get("last_name", "")).strip(),
        ]
        if part
    ) or "Unnamed applicant"
    preferred_office = form_data.get("preferred_office") or form_data.get("applying_location") or "Not specified"
    uploaded_documents = uploaded_documents or []

    message = EmailMessage()
    subject_prefix = "[TEST] " if test_mode else ""
    message["Subject"] = f"{subject_prefix}New driver application submitted: {applicant_name}"
    message["From"] = settings["from_email"]
    message["To"] = ", ".join(settings["recipients"])
    applicant_email = str(form_data.get("email", "") or "").strip()
    if applicant_email:
        message["Reply-To"] = applicant_email
    message.set_content(
        "\n".join(
            [
                "A new driver application was submitted.",
                "",
                f"Applicant: {applicant_name}",
                f"Preferred office: {preferred_office}",
                f"Phone: {form_data.get('primary_phone', 'Not provided')}",
                f"Email: {form_data.get('email', 'Not provided')}",
                f"Submitted at: {form_data.get('final_submission_timestamp', 'Unknown')}",
                f"Saved to: {submission_result.get('location_label', 'Unknown location')}",
                f"Supporting document count: {len(uploaded_documents)}",
                "",
                "This notification intentionally excludes attachments and sensitive SSN data.",
            ]
        )
    )

    try:
        _deliver_message(message, settings)
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    return {
        "status": "sent",
        "message": f"Internal notification sent to {', '.join(settings['recipients'])}.",
    }
