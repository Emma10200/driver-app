"""Standalone driver document upload page."""

from __future__ import annotations

import hashlib
import html
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from config import COMPANY_PROFILES, DEFAULT_COMPANY_SLUG

try:
    from runtime_context import get_document_upload_storage_namespace, is_test_mode_active, normalize_company_slug
except ImportError:  # pragma: no cover - defensive during Streamlit Cloud deploy/import races
    from runtime_context import is_test_mode_active

    def get_document_upload_storage_namespace() -> str:
        mode_segment = "test-mode" if is_test_mode_active() else "live"
        return f"document-uploads/{mode_segment}"

    def normalize_company_slug(value: str | None) -> str:
        return str(value or DEFAULT_COMPANY_SLUG).strip().lower() or DEFAULT_COMPANY_SLUG

from services.error_log_service import log_application_error
from services.notification_service import send_internal_document_upload_notification
from submission_storage import save_document_upload_bundle
from ui.common import BASE_STYLES, show_missing_fields

DOCUMENT_UPLOAD_OPTIONS: tuple[str, ...] = (
    "Owner IFTA (if applicable)",
    "Owner registration",
    "Owner's insurance",
    "DOT inspections (truck and trailer if applicable)",
    "CDL",
    "W9",
    "Direct deposit info",
)
ALLOWED_DOCUMENT_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_DOCUMENT_UPLOAD_FILES = 18
MAX_DOCUMENT_UPLOAD_SIZE_MB = 10
MAX_DOCUMENT_UPLOAD_SIZE_BYTES = MAX_DOCUMENT_UPLOAD_SIZE_MB * 1024 * 1024
DOCUMENT_UPLOAD_SUCCESS_KEY = "document_only_upload_success"
DOCUMENT_UPLOAD_RESET_KEY = "document_only_upload_reset_counter"


def _upload_widget_key(document_type: str, reset_counter: int) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in document_type).strip("_")
    return f"document_only_upload_{slug}_{reset_counter}"


def _coerce_upload_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item is not None]
    return [value]


def _normalize_document_uploads(uploads_by_type: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    normalized: list[dict[str, Any]] = []
    upload_items: list[tuple[str, Any]] = []

    for document_type, uploads in uploads_by_type.items():
        for upload in _coerce_upload_list(uploads):
            upload_items.append((document_type, upload))

    if len(upload_items) > MAX_DOCUMENT_UPLOAD_FILES:
        errors.append(f"Upload no more than {MAX_DOCUMENT_UPLOAD_FILES} documents at a time.")

    seen_digests: set[str] = set()
    for document_type, upload in upload_items:
        file_name = Path(str(getattr(upload, "name", "") or "")).name
        extension = Path(file_name).suffix.lower().lstrip(".")
        content = upload.getvalue()
        size_bytes = int(getattr(upload, "size", len(content)) or len(content))
        content_type = str(getattr(upload, "type", "") or "application/octet-stream")
        digest = hashlib.sha256(content).hexdigest()

        if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
            errors.append(f"`{file_name}` must be a PDF, JPG, or PNG file.")
            continue
        if size_bytes > MAX_DOCUMENT_UPLOAD_SIZE_BYTES:
            errors.append(f"`{file_name}` exceeds the {MAX_DOCUMENT_UPLOAD_SIZE_MB} MB per-file limit.")
            continue
        if digest in seen_digests:
            continue
        seen_digests.add(digest)
        normalized.append(
            {
                "document_type": document_type,
                "file_name": file_name,
                "content": content,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "content_digest": digest,
            }
        )

    return normalized, errors


def _split_driver_name(driver_name: str) -> tuple[str, str]:
    parts = [part for part in driver_name.strip().split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _default_company_slug_from_query() -> str:
    for key in ("company", "c", "co"):
        try:
            value = st.query_params.get(key, "")
        except Exception:
            value = ""
        if isinstance(value, list):
            value = value[0] if value else ""
        slug = normalize_company_slug(str(value or ""))
        if slug in COMPANY_PROFILES:
            return slug
    return DEFAULT_COMPANY_SLUG


def _render_upload_success() -> bool:
    success = st.session_state.get(DOCUMENT_UPLOAD_SUCCESS_KEY)
    if not isinstance(success, dict):
        return False

    st.success("### Documents uploaded successfully")
    st.markdown(
        f"We received **{int(success.get('document_count') or 0)}** document(s) for "
        f"**{html.escape(str(success.get('driver_name') or 'the driver'))}**."
    )
    notification = success.get("notification_result") or {}
    status = notification.get("status")
    if status == "sent":
        st.info("The internal notification email was sent to the normal team inboxes.")
    elif status == "disabled":
        st.warning("The files were saved, but internal notification email is not configured on this deployment.")
    elif status:
        st.warning("The files were saved, but the internal notification email needs follow-up.")

    if st.button("Upload another set of documents", use_container_width=True):
        st.session_state.pop(DOCUMENT_UPLOAD_SUCCESS_KEY, None)
        st.session_state[DOCUMENT_UPLOAD_RESET_KEY] = int(st.session_state.get(DOCUMENT_UPLOAD_RESET_KEY, 0) or 0) + 1
        st.rerun()
    return True


def _render_header() -> None:
    st.markdown(BASE_STYLES, unsafe_allow_html=True)
    st.markdown(
        """
<div class="app-header">
    <h1>Driver Document Upload</h1>
    <p>Use this page only to send onboarding documents. No driver application pages are required here.</p>
    <h3>Secure Document Upload</h3>
</div>
""",
        unsafe_allow_html=True,
    )


def render_document_upload_page(submissions_dir: Path) -> None:
    """Render the company-agnostic document-only upload flow."""
    _render_header()
    if is_test_mode_active():
        st.warning("Safe test mode is active. This upload will be tagged as a test upload.")
    if _render_upload_success():
        return

    st.markdown("### Documents Needed from Drivers")
    for option in DOCUMENT_UPLOAD_OPTIONS:
        st.markdown(f"- {option}")
    st.caption(
        f"Accepted file types: PDF, JPG/JPEG, PNG. Maximum {MAX_DOCUMENT_UPLOAD_FILES} files, "
        f"up to {MAX_DOCUMENT_UPLOAD_SIZE_MB} MB per file. Upload what you have; the team will follow up if anything is missing."
    )

    reset_counter = int(st.session_state.get(DOCUMENT_UPLOAD_RESET_KEY, 0) or 0)
    uploads_by_type: dict[str, Any] = {}
    with st.form(f"document_only_upload_form_{reset_counter}"):
        st.markdown("#### Driver info")
        driver_name = st.text_input("Driver name *", key=f"document_only_driver_name_{reset_counter}")
        company_slugs = list(COMPANY_PROFILES.keys())
        default_slug = _default_company_slug_from_query()
        company_slug = st.selectbox(
            "Company / division *",
            options=company_slugs,
            index=company_slugs.index(default_slug) if default_slug in company_slugs else 0,
            format_func=lambda slug: COMPANY_PROFILES[slug].name,
            key=f"document_only_company_slug_{reset_counter}",
            help="Pick the company/division these documents belong to so the right safety inbox is copied.",
        )
        col_phone, col_email = st.columns(2)
        with col_phone:
            phone = st.text_input("Phone", key=f"document_only_phone_{reset_counter}")
        with col_email:
            email = st.text_input("Email", key=f"document_only_email_{reset_counter}")
        notes = st.text_area(
            "Notes for the team (optional)",
            key=f"document_only_notes_{reset_counter}",
            placeholder="Example: Owner IFTA is not applicable, or CDL front/back included.",
        )

        st.markdown("#### Upload documents")
        for option in DOCUMENT_UPLOAD_OPTIONS:
            uploads_by_type[option] = st.file_uploader(
                option,
                type=sorted(ALLOWED_DOCUMENT_EXTENSIONS),
                accept_multiple_files=True,
                key=_upload_widget_key(option, reset_counter),
                help="Upload a PDF or image. Multiple files are OK for front/back pages or multi-page documents.",
            )

        submitted = st.form_submit_button("Submit documents", type="primary", use_container_width=True)

    if not submitted:
        return

    driver_name = (driver_name or "").strip()
    company_slug = normalize_company_slug(company_slug)
    company_profile = COMPANY_PROFILES.get(company_slug) or COMPANY_PROFILES[DEFAULT_COMPANY_SLUG]
    phone = (phone or "").strip()
    email = (email or "").strip()
    notes = (notes or "").strip()
    normalized, upload_errors = _normalize_document_uploads(uploads_by_type)
    errors = list(upload_errors)
    if not driver_name:
        errors.append("Driver name is required.")
    if not normalized:
        errors.append("Upload at least one document.")
    if email and "@" not in email:
        errors.append("Enter a valid email address or leave Email blank.")
    if errors:
        show_missing_fields(errors, "Please fix the document upload issues:")
        return

    first_name, last_name = _split_driver_name(driver_name)
    submitted_at = datetime.now().isoformat()
    document_types = sorted({str(item.get("document_type") or "Document") for item in normalized})
    form_data = {
        "upload_type": "driver_document_upload_only",
        "company_slug": company_slug,
        "company_name": company_profile.name,
        "division": company_profile.name,
        "driver_name": driver_name,
        "first_name": first_name,
        "last_name": last_name,
        "phone": phone,
        "primary_phone": phone,
        "email": email,
        "notes": notes,
        "document_types": document_types,
        "final_submission_timestamp": submitted_at,
        "test_mode": is_test_mode_active(),
    }

    try:
        upload_result = save_document_upload_bundle(
            form_data=form_data,
            documents=normalized,
            local_base_dir=submissions_dir,
            storage_namespace=get_document_upload_storage_namespace(),
        )
    except Exception as exc:  # noqa: BLE001 - show friendly message, log details
        log_application_error(
            code="document_only_upload_save_failed",
            user_message="Document-only upload could not be saved.",
            technical_details=str(exc),
            severity="error",
        )
        st.warning("We couldn't save the uploaded documents right now. Please try again.")
        return

    try:
        notification_result = send_internal_document_upload_notification(
            form_data=form_data,
            upload_result=upload_result,
            uploaded_documents=upload_result.get("documents", []),
            supporting_document_payloads=normalized,
        )
    except Exception as exc:  # noqa: BLE001 - saved files are still valid
        notification_result = {"status": "error", "message": str(exc)}
        log_application_error(
            code="document_only_upload_notification_exception",
            user_message="Document-only upload notification raised an exception.",
            technical_details=str(exc),
            severity="warning",
        )

    if notification_result.get("status") in {"disabled", "error"}:
        log_application_error(
            code="document_only_upload_notification_not_sent",
            user_message="Document-only upload notification was not sent.",
            technical_details=notification_result.get("message"),
            severity="warning",
        )

    st.session_state[DOCUMENT_UPLOAD_SUCCESS_KEY] = {
        "driver_name": driver_name,
        "document_count": len(upload_result.get("documents", [])),
        "upload_result": upload_result,
        "notification_result": notification_result,
    }
    st.session_state[DOCUMENT_UPLOAD_RESET_KEY] = reset_counter + 1
    st.rerun()