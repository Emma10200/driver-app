"""Password-protected admin dashboard for safety/ownership.

Reachable by adding ``?dashboard=1`` to the application URL. The default
password is ``Prestige2021!`` and is overridable via the ``ADMIN_PASSWORD``
secret (Streamlit Cloud Secrets, env var, or local secrets.toml).

The dashboard:
- Lists every saved submission found under the local ``submissions/`` tree.
- Surfaces the applicant's headline info plus per-submission download
  buttons for every file in the submission folder (PDFs, JSON, supporting
  documents).
- Provides a one-click "Push to shared sheet" button that re-exports the
  submission to the shared 'Applicants' Google Sheet via the same code path
  used by live submissions. Useful for backfilling submissions that landed
  before Sheets was wired up.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from config import COMPANY_PROFILES, DEFAULT_COMPANY_SLUG
from services.sheets_export import append_from_payload
from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_PASSWORD = "Prestige2021!"
SESSION_AUTH_KEY = "admin_dashboard_authenticated"


def _expected_password() -> str:
    configured = (get_runtime_secret("ADMIN_PASSWORD", "") or "").strip()
    return configured or DEFAULT_ADMIN_PASSWORD


def _render_login() -> None:
    st.title("🔒 Admin Dashboard")
    st.caption("Restricted access. Enter the admin password to continue.")
    with st.form("admin_login_form", clear_on_submit=False):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if password == _expected_password():
            st.session_state[SESSION_AUTH_KEY] = True
            st.rerun()
        else:
            st.error("Incorrect password.")


def _iter_submission_dirs(submissions_root: Path) -> list[Path]:
    """Return every directory containing a ``submission.json`` under root."""
    if not submissions_root.exists():
        return []
    found: list[Path] = []
    for json_path in submissions_root.rglob("submission.json"):
        if json_path.is_file():
            found.append(json_path.parent)
    return sorted(found, key=lambda p: p.name, reverse=True)


def _load_submission(submission_dir: Path) -> dict[str, Any] | None:
    json_path = submission_dir / "submission.json"
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - never crash the dashboard
        logger.warning("Could not read %s: %s", json_path, exc)
        return None


def _format_submitted_at(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value
    return str(value)


def _company_label(slug: str) -> str:
    profile = COMPANY_PROFILES.get(slug) or COMPANY_PROFILES.get(DEFAULT_COMPANY_SLUG)
    return profile.name if profile else slug


def _render_submission_card(submission_dir: Path, payload: dict[str, Any]) -> None:
    form_data = payload.get("form_data") or {}
    first = (form_data.get("first_name") or "").strip()
    last = (form_data.get("last_name") or "").strip()
    name = f"{first} {last}".strip() or "(unnamed applicant)"
    submitted_at = _format_submitted_at(
        form_data.get("final_submission_timestamp") or payload.get("submitted_at")
    )
    slug = form_data.get("company_slug") or DEFAULT_COMPANY_SLUG
    test_mode = bool(form_data.get("test_mode"))
    label_bits = [name, _company_label(slug)]
    if submitted_at:
        label_bits.append(submitted_at)
    if test_mode:
        label_bits.append("TEST MODE")
    header_label = "  ·  ".join(label_bits)

    with st.expander(header_label, expanded=False):
        col_a, col_b = st.columns([2, 1])
        with col_a:
            st.markdown(
                f"**Email:** {form_data.get('email', '') or '—'}  \n"
                f"**Phone:** {form_data.get('primary_phone', '') or '—'}  \n"
                f"**City/State:** "
                f"{(form_data.get('city') or '—')}, {(form_data.get('state') or '')}  \n"
                f"**Submission ID:** `{payload.get('submission_key') or submission_dir.name}`  \n"
                f"**Folder:** `{submission_dir}`"
            )
        with col_b:
            push_key = f"push_{submission_dir.name}"
            if st.button("📤 Push to shared sheet", key=push_key, use_container_width=True):
                with st.spinner("Pushing to Google Sheets..."):
                    result = append_from_payload(
                        payload, storage_location=str(submission_dir)
                    )
                status = result.get("status")
                if status == "appended":
                    st.success(
                        f"✅ Added to '{result.get('tab')}' tab."
                    )
                elif status == "disabled":
                    st.warning(
                        "Sheets export is not configured. "
                        "Set GOOGLE_SERVICE_ACCOUNT_JSON and APPLICANTS_SHEET_ID in Streamlit Secrets."
                    )
                else:
                    st.error(
                        f"Push failed: {result.get('message', 'unknown error')}"
                    )

        st.markdown("**Files**")
        files = sorted(p for p in submission_dir.iterdir() if p.is_file())
        if not files:
            st.caption("No files in this submission folder.")
            return
        cols = st.columns(min(len(files), 3) or 1)
        for index, file_path in enumerate(files):
            try:
                data = file_path.read_bytes()
            except OSError as exc:
                cols[index % len(cols)].error(f"{file_path.name}: {exc}")
                continue
            cols[index % len(cols)].download_button(
                label=f"⬇️ {file_path.name}",
                data=data,
                file_name=file_path.name,
                key=f"dl_{submission_dir.name}_{file_path.name}",
                use_container_width=True,
            )


def render_admin_dashboard(submissions_root: Path) -> None:
    """Top-level entry point. Renders login OR the dashboard content."""

    if not st.session_state.get(SESSION_AUTH_KEY):
        _render_login()
        return

    st.title("🛠️ Admin Dashboard")
    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.caption(
            "Browse submissions, download their files, and push individual "
            "applicants to the shared 'Applicants' Google Sheet."
        )
    with top_right:
        if st.button("Sign out", use_container_width=True):
            st.session_state[SESSION_AUTH_KEY] = False
            st.rerun()

    submission_dirs = _iter_submission_dirs(submissions_root)
    if not submission_dirs:
        st.info(
            f"No submissions found under `{submissions_root}`. "
            "If real submissions live in Supabase only, they won't appear here."
        )
        return

    st.caption(f"Found **{len(submission_dirs)}** submission(s).")
    for submission_dir in submission_dirs:
        payload = _load_submission(submission_dir)
        if payload is None:
            with st.expander(f"⚠️  {submission_dir.name} (could not read submission.json)"):
                st.caption(f"Folder: `{submission_dir}`")
            continue
        _render_submission_card(submission_dir, payload)
