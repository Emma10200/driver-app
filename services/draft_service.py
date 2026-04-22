"""Secure draft persistence helpers for the Streamlit driver application."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from runtime_context import get_active_company_profile, get_storage_namespace, sync_runtime_context
from services.error_log_service import log_application_error
from services.test_mode_service import render_admin_test_tools
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


def _render_resume_link_block() -> None:
    """Render the resume-link copy box and email-a-link button."""
    snippet = build_resume_url_snippet()
    if not snippet:
        st.caption("Your resume link will appear here after your first save.")
        return

    # JS composes the full URL from window.location so this works on any
    # host (Streamlit Cloud, local dev, custom domain) without hard-coding.
    components.html(
        f"""
        <div id="drv-resume-block" style="font-family: sans-serif;">
          <div style="font-size: 0.78rem; color: #444; margin-bottom: 0.25rem;">
            Your resume link (save or bookmark it):
          </div>
          <div style="display: flex; gap: 0.4rem; align-items: stretch;">
            <input id="drv-resume-url" readonly
              style="flex:1; padding: 0.55rem 0.6rem; font-size: 0.85rem;
                     border: 1px solid #ccc; border-radius: 6px; background: #fafafa;"
              value=""/>
            <button id="drv-resume-copy" type="button"
              style="padding: 0.55rem 0.9rem; font-size: 0.85rem;
                     border: 1px solid #ccc; border-radius: 6px; background: #fff; cursor: pointer;">
              Copy
            </button>
          </div>
          <div id="drv-resume-copy-msg"
               style="font-size: 0.72rem; color: #3a8a49; margin-top: 0.2rem; min-height: 0.9rem;"></div>
        </div>
        <script>
        (function() {{
          const parentWindow = window.parent;
          const loc = parentWindow.location;
          const suffix = {snippet!r};
          const fullUrl = loc.origin + loc.pathname + suffix;
          const input = document.getElementById('drv-resume-url');
          const btn = document.getElementById('drv-resume-copy');
          const msg = document.getElementById('drv-resume-copy-msg');
          input.value = fullUrl;
          btn.addEventListener('click', () => {{
            input.select();
            try {{
              parentWindow.navigator.clipboard.writeText(fullUrl).then(() => {{
                msg.textContent = 'Copied!';
                setTimeout(() => msg.textContent = '', 2000);
              }}).catch(() => {{
                parentWindow.document.execCommand('copy');
                msg.textContent = 'Copied!';
                setTimeout(() => msg.textContent = '', 2000);
              }});
            }} catch (e) {{
              msg.textContent = 'Select the link and copy manually.';
            }}
          }});
        }})();
        </script>
        """,
        height=110,
    )


def _render_email_resume_link() -> None:
    """Offer to email the resume link to the applicant's address on file."""
    from services.notification_service import send_resume_link_email  # lazy import

    snippet = build_resume_url_snippet()
    if not snippet:
        return

    applicant_email = str(st.session_state.form_data.get("email") or "").strip()
    if not applicant_email:
        st.caption("Enter your email on page 1 to enable emailing the resume link to yourself.")
        return

    if st.button("Email me this link", key="email_resume_link_btn", use_container_width=True):
        company = get_active_company_profile()
        # The email body needs a full URL. We compose a best-effort one from
        # the APP_BASE_URL secret when available; otherwise we fall back to a
        # relative suffix the driver can paste after their known app URL.
        from submission_storage import get_runtime_secret
        base_url = (get_runtime_secret("APP_BASE_URL", "") or "").strip().rstrip("/")
        resume_url = (base_url + snippet) if base_url else snippet
        result = send_resume_link_email(
            to_email=applicant_email,
            resume_url=resume_url,
            company_name=company.name,
            is_relative=not bool(base_url),
        )
        if result.get("status") == "sent":
            st.success(f"Resume link sent to {applicant_email}.")
        elif result.get("status") == "disabled":
            st.info("Email is not configured on this deployment. Copy the link above instead.")
        else:
            st.warning("We couldn't send the email right now. Please copy the link above.")


def render_draft_sidebar() -> None:
    with st.sidebar:
        st.markdown("### Save & resume")
        st.caption(
            "Your progress is saved automatically after each step. Keep the resume link "
            "below to come back to this application later, even on a different device."
        )

        if st.session_state.get("draft_saved_at"):
            st.caption(f"Last saved: {st.session_state.draft_saved_at}")

        _render_resume_link_block()
        _render_email_resume_link()

        if st.session_state.get("draft_save_error"):
            st.warning(f"Autosave warning: {st.session_state.draft_save_error}")

        render_admin_test_tools()
