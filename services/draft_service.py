"""Secure draft persistence helpers for the Streamlit driver application."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from runtime_context import get_storage_namespace, sync_runtime_context
from services.error_log_service import log_application_error
from state import init_session_state, reset_application_state
from submission_storage import load_draft_bundle, save_draft_bundle

LOCAL_STORAGE_DIR = Path(__file__).resolve().parent.parent / "submissions"


def _generate_draft_id() -> str:
    return uuid.uuid4().hex


def ensure_draft_id() -> str:
    draft_id = st.session_state.get("draft_id")
    if not draft_id:
        draft_id = _generate_draft_id()
        st.session_state.draft_id = draft_id
    return draft_id


def _has_meaningful_progress() -> bool:
    return bool(
        st.session_state.form_data
        or st.session_state.employers
        or st.session_state.licenses
        or st.session_state.accidents
        or st.session_state.violations
        or st.session_state.uploaded_documents
    )


def build_draft_snapshot() -> dict[str, Any]:
    draft_id = ensure_draft_id()
    saved_at = datetime.now().isoformat()
    return {
        "draft_id": draft_id,
        "saved_at": saved_at,
        "company_slug": st.session_state.get("company_slug", "prestige"),
        "test_mode": bool(st.session_state.get("test_mode")),
        "current_page": st.session_state.get("current_page", 1),
        "form_data": st.session_state.form_data,
        "employers": st.session_state.employers,
        "licenses": st.session_state.licenses,
        "accidents": st.session_state.accidents,
        "violations": st.session_state.violations,
        "uploaded_documents": st.session_state.uploaded_documents,
    }


def autosave_draft() -> dict[str, Any] | None:
    if not _has_meaningful_progress():
        return None

    snapshot = build_draft_snapshot()
    try:
        result = save_draft_bundle(
            draft_id=snapshot["draft_id"],
            draft_payload=snapshot,
            local_base_dir=LOCAL_STORAGE_DIR,
            storage_namespace=get_storage_namespace(),
        )
    except Exception as exc:
        log_application_error(
            code="draft_autosave_failed",
            user_message="Draft autosave failed.",
            technical_details=str(exc),
            severity="warning",
        )
        st.session_state.draft_save_error = "Secure autosave is temporarily unavailable."
        return {"ok": False, "error": "draft_autosave_failed"}

    st.session_state.draft_id = snapshot["draft_id"]
    st.session_state.draft_saved_at = snapshot["saved_at"]
    st.session_state.draft_save_error = None
    return {"ok": True, **result}


def load_draft_into_session(draft_id: str) -> dict[str, Any]:
    snapshot = load_draft_bundle(
        draft_id=draft_id,
        local_base_dir=LOCAL_STORAGE_DIR,
        storage_namespace=get_storage_namespace(),
    )

    reset_application_state()
    init_session_state()
    sync_runtime_context()

    st.session_state.current_page = int(snapshot.get("current_page") or 1)
    st.session_state.company_slug = snapshot.get("company_slug") or st.session_state.company_slug
    st.session_state.test_mode = bool(snapshot.get("test_mode", False))
    st.session_state.form_data = snapshot.get("form_data", {})
    st.session_state.employers = snapshot.get("employers", [])
    st.session_state.licenses = snapshot.get("licenses", [])
    st.session_state.accidents = snapshot.get("accidents", [])
    st.session_state.violations = snapshot.get("violations", [])
    st.session_state.uploaded_documents = snapshot.get("uploaded_documents", [])
    st.session_state.draft_id = snapshot.get("draft_id") or draft_id.strip()
    st.session_state.draft_saved_at = snapshot.get("saved_at")
    st.session_state.draft_save_error = None
    st.session_state.draft_load_error = None
    st.session_state.submitted = False
    return snapshot


def build_resume_url_snippet() -> str | None:
    """Return the ?company=...&draft=... query-string suffix for the current
    draft, or None if no draft has been saved yet."""
    draft_id = st.session_state.get("draft_id")
    if not draft_id:
        return None
    company_slug = st.session_state.get("company_slug") or "prestige"
    return f"?company={company_slug}&draft={draft_id}"
