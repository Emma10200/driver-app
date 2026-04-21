"""Secure draft persistence helpers for the Streamlit driver application."""

from __future__ import annotations

import secrets
import string
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from runtime_context import get_storage_namespace, sync_runtime_context
from services.error_log_service import log_application_error
from services.test_mode_service import render_admin_test_tools
from state import init_session_state, reset_application_state
from submission_storage import load_draft_bundle, save_draft_bundle

LOCAL_STORAGE_DIR = Path(__file__).resolve().parent.parent / "submissions"
_DRAFT_ALPHABET = string.ascii_uppercase + string.digits


def _generate_draft_id(length: int = 8) -> str:
    return "DRAFT-" + "".join(secrets.choice(_DRAFT_ALPHABET) for _ in range(length))


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


def render_draft_sidebar() -> None:
    with st.sidebar:
        st.markdown("### Secure draft resume")
        st.caption(
            "Completed steps are saved to the server-side storage backend as you continue through the application. "
            "Nothing relies on browser-local storage."
        )

        if st.session_state.get("draft_id"):
            st.code(st.session_state.draft_id)
            if st.session_state.get("draft_saved_at"):
                st.caption(f"Last saved: {st.session_state.draft_saved_at}")
        else:
            st.caption("A draft code will appear after your first completed step is saved.")

        resume_code = st.text_input(
            "Resume draft code",
            key="draft_resume_code",
            placeholder="DRAFT-ABCDEFGH",
        )
        if st.button("Load saved draft", use_container_width=True):
            if not resume_code.strip():
                st.session_state.draft_load_error = "Enter a draft code before loading."
            else:
                try:
                    load_draft_into_session(resume_code)
                except Exception as exc:
                    log_application_error(
                        code="draft_load_failed",
                        user_message="Draft load failed.",
                        technical_details=str(exc),
                        severity="warning",
                        extra={"resume_code": resume_code.strip()},
                    )
                    st.session_state.draft_load_error = "We couldn't load that draft code. Please check it and try again."
                else:
                    st.session_state.draft_load_error = None
                    st.rerun()

        if st.session_state.get("draft_load_error"):
            st.warning(st.session_state.draft_load_error)
        if st.session_state.get("draft_save_error"):
            st.warning(f"Autosave warning: {st.session_state.draft_save_error}")

        render_admin_test_tools()
