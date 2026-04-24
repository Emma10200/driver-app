"""Internal email notification helpers."""

from __future__ import annotations

from datetime import datetime
import html
import smtplib
from email.message import EmailMessage
from typing import Any

from config import COMPANY_PROFILES, DEFAULT_COMPANY_SLUG
from runtime_context import is_test_mode_active
from submission_storage import get_runtime_secret

try:
    from pypdf import PdfReader, PdfWriter
except ModuleNotFoundError:  # pragma: no cover - optional until dependency is installed
    PdfReader = None
    PdfWriter = None


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _recipient_secret_key(company_slug: str) -> str:
    return f"INTERNAL_NOTIFICATION_TO_{company_slug.upper().replace('-', '_')}"


# Always copied on every live application, regardless of which company the
# applicant chose. Override via the ALWAYS_NOTIFY_EMAILS secret (comma-separated)
# if you ever need to change/add to it without a code deploy.
_DEFAULT_ALWAYS_NOTIFY = ("dann@prestigetransportation.com",)


def _always_notify_recipients() -> list[str]:
    override_raw = (get_runtime_secret("ALWAYS_NOTIFY_EMAILS", "") or "").strip()
    if override_raw:
        return [item.strip() for item in override_raw.split(",") if item.strip()]
    return list(_DEFAULT_ALWAYS_NOTIFY)


def _notification_settings(company_slug: str, *, test_mode: bool) -> dict[str, Any]:
    if test_mode:
        recipients_raw = get_runtime_secret("TEST_INTERNAL_NOTIFICATION_TO", "") or ""
        recipients = [item.strip() for item in recipients_raw.split(",") if item.strip()]
    else:
        recipients_raw = (
            get_runtime_secret(_recipient_secret_key(company_slug), "")
            or get_runtime_secret("INTERNAL_NOTIFICATION_TO", "")
            or ""
        )
        recipients = [item.strip() for item in recipients_raw.split(",") if item.strip()]
        # Always copy the company's safety mailbox so each company gets its own application.
        profile = COMPANY_PROFILES.get(company_slug) or COMPANY_PROFILES.get(DEFAULT_COMPANY_SLUG)
        safety_email = (profile.email if profile else "").strip()
        if safety_email and safety_email.lower() not in {item.lower() for item in recipients}:
            recipients.append(safety_email)
        # Always copy the corporate inbox(es) on every application.
        existing_lower = {item.lower() for item in recipients}
        for extra in _always_notify_recipients():
            if extra.lower() not in existing_lower:
                recipients.append(extra)
                existing_lower.add(extra.lower())
    return {
        "host": (get_runtime_secret("SMTP_HOST", "") or "").strip(),
        "port": int((get_runtime_secret("SMTP_PORT", "587") or "587").strip()),
        "username": (get_runtime_secret("SMTP_USERNAME", "") or "").strip(),
        "password": get_runtime_secret("SMTP_PASSWORD", "") or "",
        "from_email": (get_runtime_secret("SMTP_FROM_EMAIL", "") or "").strip(),
        "recipients": recipients,
        "use_tls": _as_bool(get_runtime_secret("SMTP_USE_TLS", "true"), True),
        "use_ssl": _as_bool(get_runtime_secret("SMTP_USE_SSL", "false"), False),
        "attachment_password": (get_runtime_secret("SMTP_ATTACHMENT_PASSWORD", "") or "").strip(),
        # Total cap on the email payload (PDF + CSV + supporting docs). Most
        # SMTP relays choke around 25 MB; default to 22 MB to leave headroom.
        "max_attachment_bytes": int(
            (get_runtime_secret("SMTP_MAX_ATTACHMENT_BYTES", "23068672") or "23068672").strip()
        ),
    }


def _protect_pdf(pdf_bytes: bytes, password: str) -> bytes:
    if not password:
        return pdf_bytes

    if PdfReader is None or PdfWriter is None:
        raise RuntimeError(
            "Password-protected PDF attachments require pypdf. Add `pypdf` to dependencies and redeploy."
        )

    from io import BytesIO

    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=password, owner_password=password)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _merge_pdfs(pdf_parts: list[bytes]) -> bytes:
    """Combine multiple PDF byte blobs into a single PDF."""
    pdf_parts = [part for part in pdf_parts if part]
    if not pdf_parts:
        return b""
    if len(pdf_parts) == 1:
        return pdf_parts[0]
    if PdfReader is None or PdfWriter is None:
        # pypdf unavailable; fall back to just the first PDF rather than failing.
        return pdf_parts[0]

    from io import BytesIO

    writer = PdfWriter()
    for part in pdf_parts:
        try:
            reader = PdfReader(BytesIO(part))
            for page in reader.pages:
                writer.add_page(page)
        except Exception:
            # Skip any unreadable PDF rather than blocking the whole email.
            continue

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


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
    application_pdf: bytes | None = None,
    artifacts: dict[str, bytes | None] | None = None,
    application_csv: bytes | None = None,
    supporting_document_payloads: list[dict[str, Any]] | None = None,
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

    if not application_pdf and not artifacts:
        return {
            "status": "error",
            "message": "Notification email requires application PDF bytes, but none were available.",
        }

    # Build the merged PDF bundle: application + standalone disclosures in order.
    artifacts = artifacts or {}
    pdf_parts: list[bytes] = []
    bundle_sections: list[str] = []
    for label, key in [
        ("Application", "application_pdf"),
        ("FCRA Disclosure", "fcra_pdf"),
        ("California Disclosure", "california_pdf"),
        ("PSP Disclosure", "psp_pdf"),
        ("Clearinghouse Release", "clearinghouse_pdf"),
    ]:
        part = artifacts.get(key)
        if key == "application_pdf" and not part:
            part = application_pdf
        if part:
            pdf_parts.append(part)
            bundle_sections.append(label)

    if not pdf_parts and application_pdf:
        pdf_parts.append(application_pdf)
        bundle_sections.append("Application")

    bundle_sections_line = ", ".join(bundle_sections) if bundle_sections else "Application"

    merged_pdf = _merge_pdfs(pdf_parts) or application_pdf or b""
    pdf_bytes = _protect_pdf(merged_pdf, settings.get("attachment_password", ""))
    applicant_last = str(form_data.get("last_name", "driver")).strip().lower().replace(" ", "-") or "driver"
    timestamp = datetime.now().strftime("%Y%m%d")
    pdf_filename = f"driver_application_packet_{applicant_last}_{timestamp}.pdf"

    # Track total attachment size and decide which supporting docs we can fit.
    max_total = max(0, int(settings.get("max_attachment_bytes", 0) or 0))
    used_bytes = len(pdf_bytes)

    csv_filename: str | None = None
    if application_csv:
        csv_filename = f"driver_application_{applicant_last}_{timestamp}.csv"
        used_bytes += len(application_csv)

    supporting_payloads = supporting_document_payloads or []
    attached_docs: list[dict[str, Any]] = []
    skipped_docs: list[dict[str, Any]] = []
    for payload in supporting_payloads:
        content = payload.get("content")
        if not isinstance(content, (bytes, bytearray)) or not content:
            skipped_docs.append({**payload, "_reason": "no bytes available"})
            continue
        size = len(content)
        if max_total and (used_bytes + size) > max_total:
            skipped_docs.append({**payload, "_reason": "would exceed email size limit"})
            continue
        attached_docs.append({**payload, "_size": size})
        used_bytes += size

    body_lines = [
        "A new driver application was submitted.",
        "",
        f"Applicant: {applicant_name}",
        f"Preferred office: {preferred_office}",
        f"Phone: {form_data.get('primary_phone', 'Not provided')}",
        f"Email: {form_data.get('email', 'Not provided')}",
        f"Submitted at: {form_data.get('final_submission_timestamp', 'Unknown')}",
        f"Saved to: {submission_result.get('location_label', 'Unknown location')}",
        f"Supporting document count: {len(uploaded_documents)}",
        f"Attached PDF includes: {bundle_sections_line}.",
    ]
    if csv_filename:
        body_lines.append(
            f"Attached CSV ({csv_filename}) contains every form field for spreadsheet use."
        )
    if attached_docs:
        body_lines.append("")
        body_lines.append("Supporting documents attached to this email:")
        for doc in attached_docs:
            body_lines.append(f"  - {doc.get('file_name', 'document')}")
    if skipped_docs:
        body_lines.append("")
        body_lines.append(
            "The following supporting documents were NOT attached (size limit or missing bytes); "
            "they remain available at the saved location above:"
        )
        for doc in skipped_docs:
            body_lines.append(
                f"  - {doc.get('file_name', 'document')} ({doc.get('_reason', 'unavailable')})"
            )

    message.set_content("\n".join(body_lines))

    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_filename)
    if application_csv and csv_filename:
        message.add_attachment(
            application_csv, maintype="text", subtype="csv", filename=csv_filename
        )
    for doc in attached_docs:
        ctype = str(doc.get("content_type") or "application/octet-stream")
        if "/" in ctype:
            maintype, _, subtype = ctype.partition("/")
        else:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            bytes(doc["content"]),
            maintype=maintype,
            subtype=subtype,
            filename=str(doc.get("file_name") or "document"),
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


def send_resume_link_email(
    *,
    to_email: str,
    resume_url: str,
    company_name: str,
    is_relative: bool = False,
) -> dict[str, Any]:
    """Email the applicant a link to resume their saved draft application.

    When is_relative is True, the deployment didn't configure APP_BASE_URL
    and resume_url is just the ?company=...&draft=... suffix; we include
    instructions to paste it after the app URL they already know.
    """
    company_slug = DEFAULT_COMPANY_SLUG
    test_mode = is_test_mode_active()

    if not notifications_enabled(company_slug, test_mode=test_mode):
        return {"status": "disabled", "message": "Email is not configured on this deployment."}

    settings = _notification_settings(company_slug, test_mode=test_mode)
    if not settings["from_email"]:
        return {"status": "disabled", "message": "No from-email configured."}

    message = EmailMessage()
    subject_prefix = "[TEST] " if test_mode else ""
    message["Subject"] = f"{subject_prefix}{company_name} driver application — resume link"
    message["From"] = settings["from_email"]
    message["To"] = to_email.strip()

    if is_relative:
        body = (
            f"Here is the resume link for your {company_name} driver application.\n\n"
            f"Open the application URL you were given, then append this to it:\n"
                        f"  <{resume_url}>\n\n"
            "Your progress is saved. You can return to this application from any device."
        )
    else:
        body = (
            f"Here is the resume link for your {company_name} driver application.\n\n"
            f"Click or paste this link to continue where you left off:\n"
                        f"  <{resume_url}>\n\n"
            "Your progress is saved. You can return to this application from any device."
        )
    message.set_content(body)

    if not is_relative:
        safe_url = html.escape(resume_url, quote=True)
        safe_company_name = html.escape(company_name)
        message.add_alternative(
            f"""
            <html>
              <body>
                <p>Here is the resume link for your {safe_company_name} driver application.</p>
                <p>
                  <a href=\"{safe_url}\">Resume your application</a>
                </p>
                <p>
                  If the button above does not work, copy and paste this link into your browser:<br>
                  <a href=\"{safe_url}\">{safe_url}</a>
                </p>
                <p>Your progress is saved. You can return to this application from any device.</p>
              </body>
            </html>
            """,
            subtype="html",
        )

    try:
        _deliver_message(message, settings)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    return {"status": "sent", "message": f"Resume link emailed to {to_email}."}
