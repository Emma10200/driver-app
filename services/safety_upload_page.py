"""Public safety paperwork upload page for recipient-specific links."""

from __future__ import annotations

import hashlib
import html
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from services.document_upload_page import (
    ALLOWED_DOCUMENT_EXTENSIONS,
    MAX_DOCUMENT_UPLOAD_FILES,
    MAX_DOCUMENT_UPLOAD_SIZE_MB,
    MAX_DOCUMENT_UPLOAD_SIZE_BYTES,
    _coerce_upload_list,
)
from services.error_log_service import log_application_error
from services.notification_service import send_internal_document_upload_notification
from services.safety_link_store import get_safety_upload_link
from submission_storage import save_document_upload_bundle
from ui.common import BASE_STYLES, show_missing_fields

_SUCCESS_KEY = "safety_upload_success"
_STORAGE_NAMESPACE = "safety-uploads/live"


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _item_label(item: dict[str, Any]) -> str:
    unit = _safe_text(item.get("unit") or item.get("Unit"))
    document = _safe_text(item.get("document") or item.get("Document") or "Document")
    return f"Unit {unit} - {document}" if unit and unit != "—" else document


def _normalize_uploads(uploads_by_type: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
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


def _render_header() -> None:
    st.markdown(BASE_STYLES, unsafe_allow_html=True)
    st.markdown(
        """
<div class="app-header">
    <h1>Safety Paperwork Upload</h1>
    <p>This link is specific to the paperwork request you received.</p>
    <h3>Secure Document Upload</h3>
</div>
""",
        unsafe_allow_html=True,
    )


def _render_success() -> bool:
    success = st.session_state.get(_SUCCESS_KEY)
    if not isinstance(success, dict):
        return False
    st.success("### Safety documents uploaded successfully")
    st.markdown(
        f"We received **{int(success.get('document_count') or 0)}** document(s) for "
        f"**{html.escape(str(success.get('recipient_name') or 'this request'))}**."
    )
    notification = success.get("notification_result") or {}
    if notification.get("status") == "sent":
        st.info("The safety team was notified.")
    elif notification.get("status"):
        st.warning("The files were saved, but the team notification may need follow-up.")
    return True


def render_safety_upload_page(submissions_dir: Path, token: str) -> None:
    _render_header()
    if _render_success():
        return

    link = get_safety_upload_link(submissions_dir=submissions_dir, token=token)
    if not link:
        st.error("This safety upload link is invalid. Please contact safety for a new link.")
        return
    if link.get("expired"):
        st.error("This safety upload link has expired. Please contact safety for a new link.")
        return

    recipient_name = _safe_text(link.get("recipient_name") or "Driver/Owner")
    recipient_email = _safe_text(link.get("recipient_email"))
    division = _safe_text(link.get("division"))
    items = [dict(item) for item in (link.get("items") or []) if isinstance(item, dict)]

    st.markdown(f"### Hello, {html.escape(recipient_name)}")
    if division:
        st.caption(f"Division: {division}")
    if recipient_email:
        st.caption(f"Request email: {recipient_email}")

    st.markdown("#### Requested paperwork")
    if items:
        st.dataframe(
            [
                {
                    "Unit": item.get("unit") or "—",
                    "Document": item.get("document") or "Document",
                    "Current expiration": item.get("expires") or "—",
                    "Status": item.get("status") or "",
                }
                for item in items
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("No specific paperwork items are attached to this link. You can still upload documents below.")

    st.caption(
        f"Accepted file types: PDF, JPG/JPEG, PNG. Maximum {MAX_DOCUMENT_UPLOAD_FILES} files, "
        f"up to {MAX_DOCUMENT_UPLOAD_SIZE_MB} MB per file."
    )

    uploads_by_type: dict[str, Any] = {}
    with st.form("safety_upload_form"):
        phone = st.text_input("Phone (optional)")
        notes = st.text_area(
            "Notes for safety (optional)",
            placeholder="Example: IFTA is handled by the office, or insurance document attached.",
        )
        st.markdown("#### Upload documents")
        labels = [_item_label(item) for item in items] or ["Safety document"]
        for label in labels:
            uploads_by_type[label] = st.file_uploader(
                label,
                type=sorted(ALLOWED_DOCUMENT_EXTENSIONS),
                accept_multiple_files=True,
                help="Upload a PDF or image. Multiple files are OK for front/back pages or multi-page documents.",
            )
        submitted = st.form_submit_button("Submit safety documents", type="primary", use_container_width=True)

    if not submitted:
        return

    normalized, upload_errors = _normalize_uploads(uploads_by_type)
    errors = list(upload_errors)
    if not normalized:
        errors.append("Upload at least one document.")
    if errors:
        show_missing_fields(errors, "Please fix the safety upload issues:")
        return

    submitted_at = datetime.now().isoformat()
    document_types = sorted({str(item.get("document_type") or "Document") for item in normalized})
    form_data = {
        "upload_type": "safety_document_upload",
        "driver_name": recipient_name,
        "first_name": recipient_name.split()[0] if recipient_name.split() else recipient_name,
        "last_name": " ".join(recipient_name.split()[1:]) if len(recipient_name.split()) > 1 else "",
        "phone": _safe_text(phone),
        "primary_phone": _safe_text(phone),
        "email": recipient_email,
        "notes": _safe_text(notes),
        "document_types": document_types,
        "requested_items": items,
        "safety_link_token": token,
        "division": division,
        "final_submission_timestamp": submitted_at,
    }

    try:
        upload_result = save_document_upload_bundle(
            form_data=form_data,
            documents=normalized,
            local_base_dir=submissions_dir,
            storage_namespace=_STORAGE_NAMESPACE,
        )
    except Exception as exc:  # noqa: BLE001
        log_application_error(
            code="safety_upload_save_failed",
            user_message="Safety document upload could not be saved.",
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
    except Exception as exc:  # noqa: BLE001
        notification_result = {"status": "error", "message": str(exc)}
        log_application_error(
            code="safety_upload_notification_exception",
            user_message="Safety document upload notification raised an exception.",
            technical_details=str(exc),
            severity="warning",
        )

    st.session_state[_SUCCESS_KEY] = {
        "recipient_name": recipient_name,
        "document_count": len(normalized),
        "notification_result": notification_result,
    }
    st.rerun()
