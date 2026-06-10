"""Internal email notification helpers."""

from __future__ import annotations

from datetime import datetime
import html
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from config import COMPANY_PROFILES, COMPANY_SLUG_ALIASES, DEFAULT_COMPANY_SLUG
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


def _division_smtp_override(company_slug: str) -> dict[str, str]:
    """Per-division SMTP sender overrides (SMTP_USERNAME_<SLUG>, etc.).

    Returns only the keys that are actually set, so callers can fall back to
    the generic SMTP_* credentials for any value that is missing.
    """
    suffix = (company_slug or "").upper().replace("-", "_")
    if not suffix:
        return {}
    override: dict[str, str] = {}
    username = (get_runtime_secret(f"SMTP_USERNAME_{suffix}", "") or "").strip()
    password = get_runtime_secret(f"SMTP_PASSWORD_{suffix}", "") or ""
    from_email = (get_runtime_secret(f"SMTP_FROM_EMAIL_{suffix}", "") or "").strip()
    if username:
        override["username"] = username
    if password:
        override["password"] = password
    if from_email:
        override["from_email"] = from_email
    return override


# Always copied on every live application, regardless of which company the
# applicant chose. Override via the ALWAYS_NOTIFY_EMAILS secret (comma-separated)
# if you ever need to change/add to it without a code deploy.
_DEFAULT_ALWAYS_NOTIFY = ("dann@prestigetransportation.com",)


def _always_notify_recipients() -> list[str]:
    override_raw = (get_runtime_secret("ALWAYS_NOTIFY_EMAILS", "") or "").strip()
    if override_raw:
        return [item.strip() for item in override_raw.split(",") if item.strip()]
    return list(_DEFAULT_ALWAYS_NOTIFY)


def _notification_settings(company_slug: str, *, test_mode: bool, use_division_sender: bool = False) -> dict[str, Any]:
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
    username = (get_runtime_secret("SMTP_USERNAME", "") or "").strip()
    password = get_runtime_secret("SMTP_PASSWORD", "") or ""
    from_email = (get_runtime_secret("SMTP_FROM_EMAIL", "") or "").strip()
    # Per-division sending mailbox (e.g. safety@... for each company). Only the
    # safety request/warning path opts in; application packets keep using the
    # generic statements mailbox to preserve existing behaviour.
    if use_division_sender:
        override = _division_smtp_override(company_slug)
        username = override.get("username", username)
        password = override.get("password", password)
        from_email = override.get("from_email", from_email)
    return {
        "host": (get_runtime_secret("SMTP_HOST", "") or "").strip(),
        "port": int((get_runtime_secret("SMTP_PORT", "587") or "587").strip()),
        "username": username,
        "password": password,
        "from_email": from_email,
        "recipients": recipients,
        "use_tls": _as_bool(get_runtime_secret("SMTP_USE_TLS", "true"), True),
        "use_ssl": _as_bool(get_runtime_secret("SMTP_USE_SSL", "false"), False),
        "attachment_password": (get_runtime_secret("SMTP_ATTACHMENT_PASSWORD", "") or "").strip(),
        # Total cap on the email payload (PDF + supporting docs). Most
        # SMTP relays choke around 25 MB; default to 22 MB to leave headroom.
        "max_attachment_bytes": int(
            (get_runtime_secret("SMTP_MAX_ATTACHMENT_BYTES", "23068672") or "23068672").strip()
        ),
    }


def _normalize_company_slug_for_notifications(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    slug = raw.lower().replace("_", "-")
    slug = COMPANY_SLUG_ALIASES.get(slug, slug)
    return slug if slug in COMPANY_PROFILES else ""


def _document_upload_company_slug(form_data: dict[str, Any]) -> str:
    """Resolve the intended company/division for document-upload notifications."""
    for key in ("company_slug", "company", "company_id"):
        slug = _normalize_company_slug_for_notifications(form_data.get(key))
        if slug:
            return slug

    division = str(
        form_data.get("division")
        or form_data.get("company_name")
        or form_data.get("target_division")
        or ""
    ).strip()
    if division:
        return _safety_company_slug_for_division(division)

    return DEFAULT_COMPANY_SLUG


def _internal_recipients_only(recipients: list[str], applicant_email: str) -> list[str]:
    """Return notification recipients, excluding the applicant if misconfigured.

    The applicant may appear as Reply-To so staff can answer directly, but they
    should never be an envelope/header recipient of the internal packet email.
    """
    applicant_email = applicant_email.strip().lower()
    cleaned: list[str] = []
    seen: set[str] = set()
    for recipient in recipients:
        normalized = recipient.strip()
        normalized_lower = normalized.lower()
        if not normalized or normalized_lower in seen:
            continue
        if applicant_email and normalized_lower == applicant_email:
            continue
        cleaned.append(normalized)
        seen.add(normalized_lower)
    return cleaned


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


def notifications_enabled(company_slug: str, *, test_mode: bool, use_division_sender: bool = False) -> bool:
    settings = _notification_settings(company_slug, test_mode=test_mode, use_division_sender=use_division_sender)
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
    applicant_email = str(form_data.get("email", "") or "").strip()
    internal_recipients = _internal_recipients_only(settings["recipients"], applicant_email)
    if not internal_recipients:
        return {
            "status": "error",
            "message": "Internal notification has no recipients after excluding applicant email.",
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
    subject_prefix = "[TEST] " if test_mode else ""
    message["Subject"] = f"{subject_prefix}New driver application submitted: {applicant_name}"
    message["From"] = settings["from_email"]
    message["To"] = ", ".join(internal_recipients)
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

    if application_csv:
        # Keep this argument backward-compatible, but do not attach CSV files
        # to internal email. Spreadsheet data is handled by the separate
        # Google Sheets export path in services/sheets_export.py.
        pass

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
        "message": f"Internal notification sent to {', '.join(internal_recipients)}.",
    }


def send_internal_document_upload_notification(
    *,
    form_data: dict[str, Any],
    upload_result: dict[str, Any],
    uploaded_documents: list[dict[str, Any]] | None = None,
    supporting_document_payloads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Notify staff about a document-only upload without requiring an application packet."""
    company_slug = _document_upload_company_slug(form_data)
    test_mode = bool(form_data.get("test_mode")) or is_test_mode_active()

    if not notifications_enabled(company_slug, test_mode=test_mode):
        return {
            "status": "disabled",
            "message": "Internal notification email is not configured yet.",
        }

    settings = _notification_settings(company_slug, test_mode=test_mode)
    applicant_email = str(form_data.get("email", "") or "").strip()
    internal_recipients = _internal_recipients_only(settings["recipients"], applicant_email)
    if not internal_recipients:
        return {
            "status": "error",
            "message": "Internal notification has no recipients after excluding driver email.",
        }

    driver_name = str(form_data.get("driver_name") or "").strip()
    if not driver_name:
        driver_name = " ".join(
            part
            for part in [
                str(form_data.get("first_name", "")).strip(),
                str(form_data.get("last_name", "")).strip(),
            ]
            if part
        ) or "Unnamed driver"

    message = EmailMessage()
    subject_prefix = "[TEST] " if test_mode else ""
    message["Subject"] = f"{subject_prefix}Driver documents uploaded: {driver_name}"
    message["From"] = settings["from_email"]
    message["To"] = ", ".join(internal_recipients)
    if applicant_email:
        message["Reply-To"] = applicant_email

    uploaded_documents = uploaded_documents or []
    supporting_payloads = supporting_document_payloads or []
    max_total = max(0, int(settings.get("max_attachment_bytes", 0) or 0))
    used_bytes = 0
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

    selected_document_types = form_data.get("document_types") or []
    profile = COMPANY_PROFILES.get(company_slug) or COMPANY_PROFILES.get(DEFAULT_COMPANY_SLUG)
    division = str(form_data.get("division") or form_data.get("company_name") or (profile.name if profile else "")).strip()
    body_lines = [
        "A driver submitted documents through the document-only upload link.",
        "",
        f"Driver: {driver_name}",
        f"Company / division: {division or 'Not provided'}",
        f"Phone: {form_data.get('phone', 'Not provided') or 'Not provided'}",
        f"Email: {form_data.get('email', 'Not provided') or 'Not provided'}",
        f"Submitted at: {form_data.get('final_submission_timestamp', 'Unknown')}",
        f"Saved to: {upload_result.get('location_label', 'Unknown location')}",
        f"Document count: {len(uploaded_documents)}",
    ]
    if selected_document_types:
        body_lines.extend(["", "Document types selected:"])
        for document_type in selected_document_types:
            body_lines.append(f"  - {document_type}")
    notes = str(form_data.get("notes") or "").strip()
    if notes:
        body_lines.extend(["", "Driver notes:", notes])
    if attached_docs:
        body_lines.extend(["", "Documents attached to this email:"])
        for doc in attached_docs:
            label = doc.get("document_type") or "Document"
            body_lines.append(f"  - {label}: {doc.get('file_name', 'document')}")
    if skipped_docs:
        body_lines.append("")
        body_lines.append(
            "The following documents were NOT attached (size limit or missing bytes); "
            "they remain available at the saved location above:"
        )
        for doc in skipped_docs:
            body_lines.append(
                f"  - {doc.get('file_name', 'document')} ({doc.get('_reason', 'unavailable')})"
            )

    message.set_content("\n".join(body_lines))

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
        "message": f"Internal document upload notification sent to {', '.join(internal_recipients)}.",
    }


def _safety_upload_url() -> str:
    override = (get_runtime_secret("SAFETY_UPLOAD_URL", "") or "").strip()
    if override:
        return override
    # Intentionally do NOT read APP_BASE_URL here. That secret has pointed at
    # the old hashed Streamlit domain before, and safety emails should always
    # use the stable public app URL unless SAFETY_UPLOAD_URL is explicitly set.
    return "https://driver-application.streamlit.app/?documents=1"


def _safety_company_slug_for_division(division: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in str(division or ""))
    normalized = " ".join(normalized.split())
    if "xpress" in normalized:
        return "xpress"
    # ProTransport exports Prestig, Inc. as "Prestig Inc". Prestige
    # Transportation includes "prestige" too, so only treat it as PG when the
    # transportation/california wording is absent.
    if "prestig" in normalized and "transportation" not in normalized and "california" not in normalized:
        return "pg"
    return "prestige"


def _safety_identity_for_division(division: str) -> tuple[str, str, str]:
    """Return (display name, mailbox, company_slug) for a safety division."""
    company_slug = _safety_company_slug_for_division(division)
    profile = COMPANY_PROFILES.get(company_slug) or COMPANY_PROFILES[DEFAULT_COMPANY_SLUG]
    display_name = "Safety Department" if company_slug == "xpress" else "Safety Dept"
    return display_name, profile.email, company_slug


def _clean_subject_part(value: Any, *, fallback: str = "Safety request") -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text or fallback


def _safety_subject_context(recipient_name: str, items: list[dict[str, Any]]) -> str:
    units: list[str] = []
    has_driver_docs = False
    for item in items:
        unit = _clean_subject_part(item.get("unit") or item.get("Unit") or "", fallback="")
        if unit and unit != "—":
            if unit not in units:
                units.append(unit)
        else:
            has_driver_docs = True

    name = _clean_subject_part(recipient_name, fallback="Safety request")
    if not units:
        return f"{name} - Driver docs" if has_driver_docs else name
    if len(units) == 1:
        return f"{name} - Driver + Unit {units[0]}" if has_driver_docs else f"Unit {units[0]} - {name}"
    if len(units) <= 3:
        unit_part = "Units " + ", ".join(units)
    else:
        unit_part = f"{len(units)} units ({', '.join(units[:3])}, +{len(units) - 3})"
    return f"{name} - Driver + {unit_part}" if has_driver_docs else f"{name} - {unit_part}"


def _safety_email_subject(
    *,
    recipient_name: str,
    division: str,
    items: list[dict[str, Any]],
    test_mode: bool,
) -> str:
    subject_prefix = "[TEST] " if test_mode else ""
    context = _safety_subject_context(recipient_name, items)
    division_part = _clean_subject_part(division, fallback="")
    suffix = f" - {division_part}" if division_part else ""
    subject = f"{subject_prefix}Safety paperwork needed - {context}{suffix}"
    return subject[:180]


def send_safety_document_request_email(
    *,
    to_email: str,
    recipient_name: str,
    division: str,
    items: list[dict[str, Any]],
    upload_url: str | None = None,
    test_mode: bool | None = None,
    token: str | None = None,
    ref_code: str | None = None,
) -> dict[str, Any]:
    """Send a driver/owner-facing safety paperwork request.

    The safety portal's first outbound version uses the existing SMTP settings
    and sends from the normal statements mailbox. The recipient is the driver
    or owner; internal safety/statement recipients are CC'd using the existing
    notification settings so staff can see exactly what went out.

    When ``ref_code`` is supplied it is stamped into the subject and body and a
    Message-ID is set so replies (drivers who answer instead of using the link)
    can be matched back to this exact recipient by the email-reply ingester.
    """
    to_email = str(to_email or "").strip()
    if not to_email:
        return {"status": "error", "message": "Missing recipient email."}
    items = [dict(item) for item in items if item]
    if not items:
        return {"status": "skipped", "message": "No selected safety paperwork items to send."}

    resolved_test_mode = is_test_mode_active() if test_mode is None else bool(test_mode)
    recipient_name = str(recipient_name or "Driver/Owner").strip() or "Driver/Owner"
    division = str(division or "").strip()
    safety_display_name, safety_email, company_slug = _safety_identity_for_division(division)
    if not notifications_enabled(company_slug, test_mode=resolved_test_mode, use_division_sender=True):
        return {
            "status": "disabled",
            "message": "Email is not configured on this deployment.",
        }

    settings = _notification_settings(company_slug, test_mode=resolved_test_mode, use_division_sender=True)
    cc_recipients = _internal_recipients_only(settings["recipients"], to_email)
    upload_url = str(upload_url or "").strip() or _safety_upload_url()

    message = EmailMessage()
    ref_code = str(ref_code or "").strip().upper()
    subject = _safety_email_subject(
        recipient_name=recipient_name,
        division=division,
        items=items,
        test_mode=resolved_test_mode,
    )
    if ref_code:
        subject = f"{subject} [Ref: {ref_code}]"[:200]
    message["Subject"] = subject
    message["From"] = formataddr((safety_display_name, safety_email))
    message_id = make_msgid(domain=(safety_email.split("@")[-1] or "prestige.local"))
    message["Message-ID"] = message_id
    smtp_from = str(settings.get("from_email") or "").strip()
    if smtp_from and smtp_from.lower() != safety_email.lower():
        message["Sender"] = smtp_from
    message["Reply-To"] = formataddr((safety_display_name, safety_email))
    message["To"] = to_email
    if cc_recipients:
        message["Cc"] = ", ".join(cc_recipients)

    requested_lines: list[str] = []
    requested_html: list[str] = []
    for item in items:
        unit = str(item.get("unit") or item.get("Unit") or "").strip()
        document = str(item.get("document") or item.get("Document") or "Document").strip()
        expires = str(item.get("expires") or item.get("Expires") or "—").strip() or "—"
        status = str(item.get("status") or item.get("Status") or "").strip()
        unit_part = f"Unit {unit}: " if unit and unit != "—" else ""
        status_part = f" ({status})" if status else ""
        requested_lines.append(f"  - {unit_part}{document} — current expiration: {expires}{status_part}")
        requested_html.append(
            "<li>"
            f"<strong>{html.escape(unit_part + document)}</strong>"
            f" — current expiration: {html.escape(expires)}"
            f"{html.escape(status_part)}"
            "</li>"
        )

    body_lines = [
        f"Hello {recipient_name},",
        "",
        "This is an automated message from our safety system.",
        "",
        "Our safety records show that we need updated paperwork for the item(s) below.",
        "You can submit the requested document(s) either way:",
        "  1. Upload them through our secure website using this link:",
        f"     {upload_url}",
        "  2. Or simply reply to this email and attach the document(s).",
        "",
        "Requested item(s):",
    ]
    body_lines.extend(requested_lines)

    body_lines.extend(
        [
            "",
            "If one of these items does not apply to you, please reply to this email and let safety know.",
            "",
            "Thank you,",
            "Safety Department",
        ]
    )
    if ref_code:
        body_lines.extend(["", f"Safety-Ref: {ref_code} (please keep this on any reply)"])
    message.set_content("\n".join(body_lines))
    safe_upload_url = html.escape(upload_url, quote=True)
    safe_name = html.escape(recipient_name)
    safe_division = html.escape(division)
    division_line = f"<p><strong>Division:</strong> {safe_division}</p>" if safe_division else ""
    message.add_alternative(
        f"""
<html>
    <body style="font-family: Arial, sans-serif; color: #172033; line-height: 1.5;">
        <p>Hello {safe_name},</p>
        {division_line}
        <p style="font-size: 13px; color: #475569;"><em>This is an automated message from our safety system.</em></p>
        <p>Our safety records show that we need updated paperwork for the item(s) below.</p>
        <p>You can submit the requested document(s) <strong>either way</strong>:</p>
        <ol style="margin-top:0;">
            <li>Upload them through our secure website using the button below, or</li>
            <li>Simply <strong>reply to this email</strong> and attach the document(s).</li>
        </ol>
        <p>
            <a href="{safe_upload_url}" style="background:#0f766e;color:#ffffff;padding:10px 14px;text-decoration:none;border-radius:8px;font-weight:700;display:inline-block;">
                Upload requested documents
            </a>
        </p>
        <p style="font-size: 13px; color: #475569;">If the button does not open, copy and paste this link:<br>{safe_upload_url}</p>
        <p><strong>Requested item(s):</strong></p>
        <ul>
            {''.join(requested_html)}
        </ul>
        <p>If one of these items does not apply to you, please reply to this email and let safety know.</p>
        <p>Thank you,<br>Safety Department</p>
    </body>
</html>
""".strip(),
        subtype="html",
    )

    try:
        _deliver_message(message, settings)
    except Exception as exc:
        return {"status": "error", "message": str(exc)}

    cc_note = f"; cc: {', '.join(cc_recipients)}" if cc_recipients else ""
    return {
        "status": "sent",
        "message": f"Safety paperwork request sent to {to_email}{cc_note}.",
        "message_id": message_id,
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
