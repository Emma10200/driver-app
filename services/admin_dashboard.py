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
from submission_storage import (
    get_runtime_secret,
    list_supabase_submissions,
    read_remote_file_bytes,
    read_remote_submission_payload,
    supabase_storage_enabled,
)

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


def _render_submission_card(
    *,
    source: str,
    identifier: str,
    payload: dict[str, Any],
    location_label: str,
    files: list[dict[str, Any]],
) -> None:
    """Render one submission card.

    ``source`` is either ``"local"`` or ``"supabase"``. ``files`` is a list of
    ``{"name": str, "fetch": Callable[[], bytes | None]}`` dicts so we can
    reuse this card for both filesystem-backed and Supabase-backed
    submissions without leaking storage details.
    """

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
    label_bits.append("☁️ Supabase" if source == "supabase" else "💾 Local")
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
                f"**Submission ID:** `{payload.get('submission_key') or identifier}`  \n"
                f"**Location:** `{location_label}`"
            )
        with col_b:
            push_key = f"push_{source}_{identifier}"
            if st.button("📤 Push to shared sheet", key=push_key, use_container_width=True):
                with st.spinner("Pushing to Google Sheets..."):
                    result = append_from_payload(
                        payload, storage_location=location_label
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
        if not files:
            st.caption("No files in this submission.")
            return
        cols = st.columns(min(len(files), 3) or 1)
        for index, file_entry in enumerate(files):
            file_name = file_entry["name"]
            target_col = cols[index % len(cols)]
            try:
                data = file_entry["fetch"]()
            except Exception as exc:  # noqa: BLE001 - never crash the dashboard
                target_col.error(f"{file_name}: {exc}")
                continue
            if data is None:
                target_col.warning(f"{file_name}: could not fetch")
                continue
            target_col.download_button(
                label=f"⬇️ {file_name}",
                data=data,
                file_name=file_name,
                key=f"dl_{source}_{identifier}_{file_name}",
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

    show_test_mode = st.checkbox(
        "Show test-mode submissions",
        value=False,
        help=(
            "Hidden by default. Toggle on to also list applications that were "
            "submitted with test mode enabled (these go to the test-mode "
            "namespace and are tagged TEST MODE in the card header)."
        ),
        key="admin_show_test_mode",
    )

    def _is_test_mode(payload: dict[str, Any], *, mode_hint: str | None = None) -> bool:
        if mode_hint and mode_hint.lower() == "test-mode":
            return True
        form_data = payload.get("form_data") or {}
        return bool(form_data.get("test_mode"))

    # Build the merged list: Supabase first (these are the real production
    # submissions), then any local submissions that are not also represented
    # in Supabase by the same submission key.
    seen_keys: set[str] = set()
    rendered_count = 0
    hidden_count = 0

    if supabase_storage_enabled():
        try:
            remote_submissions = list_supabase_submissions()
        except Exception as exc:  # noqa: BLE001 - dashboard must not crash
            st.warning(f"Could not list Supabase submissions: {exc}")
            remote_submissions = []

        for entry in remote_submissions:
            key = entry["submission_key"]
            seen_keys.add(key)
            payload = read_remote_submission_payload(entry["remote_prefix"])
            if payload is None:
                with st.expander(f"⚠️  {key} (could not read submission.json)"):
                    st.caption(f"Location: `{entry['location_label']}`")
                continue
            if not show_test_mode and _is_test_mode(payload, mode_hint=entry.get("mode")):
                hidden_count += 1
                continue
            files = [
                {
                    "name": file_name,
                    "fetch": (
                        lambda prefix=entry["remote_prefix"], name=file_name: read_remote_file_bytes(prefix, name)
                    ),
                }
                for file_name in entry["files"]
            ]
            _render_submission_card(
                source="supabase",
                identifier=key,
                payload=payload,
                location_label=entry["location_label"],
                files=files,
            )
            rendered_count += 1
    else:
        st.caption(
            "Supabase is not configured for this deployment — only local "
            "submissions on the running container will appear below."
        )

    local_dirs = _iter_submission_dirs(submissions_root)
    for submission_dir in local_dirs:
        if submission_dir.name in seen_keys:
            # Already shown via the Supabase listing.
            continue
        payload = _load_submission(submission_dir)
        if payload is None:
            with st.expander(f"⚠️  {submission_dir.name} (could not read submission.json)"):
                st.caption(f"Folder: `{submission_dir}`")
            continue
        if not show_test_mode and _is_test_mode(payload):
            hidden_count += 1
            continue
        local_files = sorted(p for p in submission_dir.iterdir() if p.is_file())
        files = [
            {
                "name": file_path.name,
                "fetch": (lambda path=file_path: path.read_bytes()),
            }
            for file_path in local_files
        ]
        _render_submission_card(
            source="local",
            identifier=submission_dir.name,
            payload=payload,
            location_label=str(submission_dir),
            files=files,
        )
        rendered_count += 1

    if rendered_count == 0:
        if hidden_count > 0:
            st.info(
                f"All {hidden_count} submission(s) are test-mode — toggle "
                "'Show test-mode submissions' above to view them."
            )
        elif supabase_storage_enabled():
            st.info(
                "No submissions found in Supabase or in the local "
                f"`{submissions_root}` folder."
            )
        else:
            st.info(
                f"No submissions found under `{submissions_root}`. "
                "If real submissions live in Supabase only, configure the "
                "SUPABASE_URL / SUPABASE_SERVICE_KEY secrets so the "
                "dashboard can list them."
            )
    else:
        suffix = (
            f" ({hidden_count} test-mode hidden)" if hidden_count and not show_test_mode else ""
        )
        st.caption(f"Rendered **{rendered_count}** submission(s).{suffix}")
