"""Supporting document upload helpers."""

from __future__ import annotations

import html

import hashlib
from pathlib import Path
from typing import Any

import streamlit as st

from services.draft_service import LOCAL_STORAGE_DIR, ensure_draft_id
from runtime_context import get_storage_namespace, is_test_mode_active
from submission_storage import save_supporting_documents
from ui.common import show_missing_fields

UPLOAD_WIDGET_KEY = "supporting_documents_uploader"
ALLOWED_UPLOAD_EXTENSIONS = {"pdf", "png", "jpg", "jpeg"}
MAX_SUPPORTING_DOCUMENTS = 6
MAX_SUPPORTING_DOCUMENT_SIZE_MB = 10
MAX_SUPPORTING_DOCUMENT_SIZE_BYTES = MAX_SUPPORTING_DOCUMENT_SIZE_MB * 1024 * 1024
REQUESTED_SUPPORTING_DOCUMENTS = [
    {
        "label": "CDL (Commercial Driver’s License)",
        "form_key": "supporting_doc_cdl",
        "status": "Required before onboarding",
    },
    {
        "label": "Medical Card",
        "form_key": "supporting_doc_medical_card",
        "status": "Required before onboarding",
    },
    {
        "label": "W-9 Form",
        "form_key": "supporting_doc_w9",
        "status": "Required before onboarding",
    },
    {
        "label": "Direct Deposit Form / Voided Check / Bank Letter",
        "form_key": "supporting_doc_direct_deposit",
        "status": "Required before onboarding",
    },
]


def _requested_document_widget_key(form_key: str) -> str:
    return f"requested_upload_{form_key}"


def _ensure_requested_document_checklist_state() -> None:
    form_data = st.session_state.setdefault("form_data", {})
    for document in REQUESTED_SUPPORTING_DOCUMENTS:
        widget_key = _requested_document_widget_key(str(document["form_key"]))
        if widget_key not in st.session_state:
            st.session_state[widget_key] = bool(form_data.get(str(document["form_key"]), False))


def get_pending_uploads() -> list[Any]:
    uploads = st.session_state.get(UPLOAD_WIDGET_KEY) or []
    if uploads is None:
        return []
    if isinstance(uploads, list):
        return [upload for upload in uploads if upload is not None]
    return [uploads]


def _normalize_pending_uploads() -> tuple[list[dict[str, Any]], list[str]]:
    uploads = get_pending_uploads()
    errors: list[str] = []
    normalized: list[dict[str, Any]] = []

    if len(uploads) > MAX_SUPPORTING_DOCUMENTS:
        errors.append(f"Upload no more than {MAX_SUPPORTING_DOCUMENTS} supporting documents at a time.")

    for upload in uploads:
        file_name = Path(str(getattr(upload, 'name', '') or '')).name
        extension = Path(file_name).suffix.lower().lstrip('.')
        content = upload.getvalue()
        size_bytes = int(getattr(upload, 'size', len(content)) or len(content))
        content_type = str(getattr(upload, 'type', '') or 'application/octet-stream')

        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            errors.append(f"`{file_name}` must be a PDF, JPG, or PNG file.")
            continue
        if size_bytes > MAX_SUPPORTING_DOCUMENT_SIZE_BYTES:
            errors.append(
                f"`{file_name}` exceeds the {MAX_SUPPORTING_DOCUMENT_SIZE_MB} MB per-file limit."
            )
            continue

        normalized.append(
            {
                "file_name": file_name,
                "content": content,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "content_digest": hashlib.sha256(content).hexdigest(),
            }
        )

    return normalized, errors


def sync_pending_uploads() -> dict[str, Any]:
    normalized, errors = _normalize_pending_uploads()
    existing_documents = st.session_state.get("uploaded_documents", [])

    if errors:
        return {"ok": False, "errors": errors, "documents": existing_documents}

    if not normalized:
        return {"ok": True, "saved": 0, "documents": existing_documents, "warnings": []}

    existing_digests = {document["content_digest"] for document in existing_documents if "content_digest" in document}
    new_documents = [document for document in normalized if document["content_digest"] not in existing_digests]

    if not new_documents:
        return {"ok": True, "saved": 0, "documents": existing_documents, "warnings": []}

    draft_id = ensure_draft_id()
    result = save_supporting_documents(
        draft_id=draft_id,
        documents=new_documents,
        local_base_dir=LOCAL_STORAGE_DIR,
        storage_namespace=get_storage_namespace(),
    )
    merged_documents = [*existing_documents, *result.get("documents", [])]
    st.session_state.uploaded_documents = merged_documents
    return {
        "ok": True,
        "saved": len(result.get("documents", [])),
        "documents": merged_documents,
        "warnings": result.get("warnings", []),
    }


def render_supporting_documents_section() -> None:
    st.markdown("---")
    st.subheader("Supporting Documents")
    st.caption(
        f"Accepted file types: PDF, JPG/JPEG, PNG. Maximum {MAX_SUPPORTING_DOCUMENTS} files, up to "
        f"{MAX_SUPPORTING_DOCUMENT_SIZE_MB} MB per file. Files are stored server-side when you save a draft or submit."
    )
    st.markdown("**Requested uploads**")
    st.caption("Check off any document included with this upload. These items are required before onboarding.")
    _ensure_requested_document_checklist_state()
    form_data = st.session_state.setdefault("form_data", {})
    for document in REQUESTED_SUPPORTING_DOCUMENTS:
        label = str(document["label"])
        form_key = str(document["form_key"])
        status = str(document["status"])
        checked = st.checkbox(
            f"{label} — {status}",
            key=_requested_document_widget_key(form_key),
        )
        form_data[form_key] = bool(checked)
    st.caption("Optional: you can also upload any extra endorsements, certificates, or supporting paperwork below.")
    st.caption("You can upload combined PDFs or separate images if a document has multiple pages or front/back sides.")
    if is_test_mode_active():
        st.info("Safe test mode stores uploaded files in a separate company test namespace.")

    st.file_uploader(
        "Upload supporting documents",
        type=["pdf", "png", "jpg", "jpeg"],
        accept_multiple_files=True,
        key=UPLOAD_WIDGET_KEY,
        help="Upload requested documents such as your CDL, medical card, W-9 form, or direct deposit form / voided check / bank letter.",
    )

    saved_documents = st.session_state.get("uploaded_documents", [])
    if saved_documents:
        st.markdown("**Saved documents**")
        for document in saved_documents:
            size_kb = max(1, int(document.get("size_bytes", 0) / 1024))
            safe_file_name = html.escape(document.get('file_name', 'document').replace("`", "_"))
            st.markdown(f"- `{safe_file_name}` ({size_kb} KB)")

    pending_uploads = get_pending_uploads()
    normalized, errors = _normalize_pending_uploads()
    if errors:
        show_missing_fields(errors, "Please fix the supporting document upload issues:")
    elif pending_uploads:
        duplicate_digests = {document["content_digest"] for document in saved_documents if "content_digest" in document}
        new_count = sum(1 for document in normalized if document["content_digest"] not in duplicate_digests)
        if new_count:
            st.info(f"{new_count} new document(s) selected. They’ll be saved securely when you save a draft or submit.")
