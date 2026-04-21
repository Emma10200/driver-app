"""Internal email notification helpers."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from submission_storage import get_runtime_secret


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _notification_settings() -> dict[str, Any]:
    recipients_raw = get_runtime_secret("INTERNAL_NOTIFICATION_TO", "") or ""
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


def notifications_enabled() -> bool:
    settings = _notification_settings()
    return bool(settings["host"] and settings["from_email"] and settings["recipients"])


def send_internal_submission_notification(
    *,
    form_data: dict[str, Any],
    submission_result: dict[str, Any],
    uploaded_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    settings = _notification_settings()
    if not notifications_enabled():
        return {
            "status": "disabled",
            "message": "Internal notification email is not configured yet.",
        }

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
    message["Subject"] = f"New driver application submitted: {applicant_name}"
    message["From"] = settings["from_email"]
    message["To"] = ", ".join(settings["recipients"])
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
        if settings["use_ssl"]:
            with smtplib.SMTP_SSL(settings["host"], settings["port"], timeout=30) as server:
                if settings["username"]:
                    server.login(settings["username"], settings["password"])
                server.send_message(message)
        else:
            with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as server:
                if settings["use_tls"]:
                    server.starttls()
                if settings["username"]:
                    server.login(settings["username"], settings["password"])
                server.send_message(message)
    except Exception as exc:
        return {
            "status": "error",
            "message": str(exc),
        }

    return {
        "status": "sent",
        "message": f"Internal notification sent to {', '.join(settings['recipients'])}.",
    }
