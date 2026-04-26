"""Protected admin dashboard for safety/ownership.

Reachable by adding ``?dashboard=1`` to the application URL. The dashboard can
be gated by a runtime ``ADMIN_PASSWORD``, Google SSO with an explicit email
allowlist, or both while SSO is being rolled out. There is intentionally no
hardcoded fallback password.

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

from collections.abc import Mapping
import hashlib
import hmac
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import streamlit as st
try:
    from streamlit.errors import StreamlitSecretNotFoundError as _StreamlitSecretNotFoundError
except ImportError:  # pragma: no cover - defensive fallback for older tooling
    _streamlit_secret_exceptions: tuple[type[BaseException], ...] = (
        FileNotFoundError,
        AttributeError,
        KeyError,
    )
else:
    _streamlit_secret_exceptions = (
        _StreamlitSecretNotFoundError,
        FileNotFoundError,
        AttributeError,
        KeyError,
    )

from config import COMPANY_PROFILES, DEFAULT_COMPANY_SLUG
from services.sheets_export import append_decision_from_payload, append_from_payload
from submission_storage import (
    get_runtime_secret,
    list_supabase_submissions,
    read_remote_file_bytes,
    read_remote_submission_payload,
    supabase_storage_enabled,
)

logger = logging.getLogger(__name__)

SESSION_AUTH_KEY = "admin_dashboard_authenticated"
SESSION_AUTH_FINGERPRINT_KEY = "admin_dashboard_auth_fingerprint"
VALID_ADMIN_AUTH_MODES = {"password", "google", "both", "disabled"}
DEFAULT_ADMIN_AUTH_MODE = "both"
GOOGLE_AUTH_PROVIDER = "google"


def _admin_auth_mode() -> str:
    configured = (get_runtime_secret("ADMIN_AUTH_MODE", DEFAULT_ADMIN_AUTH_MODE) or "").strip().lower()
    return configured if configured in VALID_ADMIN_AUTH_MODES else DEFAULT_ADMIN_AUTH_MODE


def _password_auth_enabled(mode: str | None = None) -> bool:
    selected_mode = mode or _admin_auth_mode()
    return selected_mode in {"password", "both"}


def _google_auth_enabled(mode: str | None = None) -> bool:
    selected_mode = mode or _admin_auth_mode()
    return selected_mode in {"google", "both"}


def _parse_admin_emails(raw_value: str | None) -> set[str]:
    if not raw_value:
        return set()
    normalized = raw_value.replace(";", ",").replace("\n", ",")
    return {item.strip().lower() for item in normalized.split(",") if item.strip()}


def _allowed_admin_emails() -> set[str]:
    return _parse_admin_emails(get_runtime_secret("ADMIN_ALLOWED_EMAILS", ""))


def _expected_password() -> str | None:
    configured = (get_runtime_secret("ADMIN_PASSWORD", "") or "").strip()
    return configured or None


def _mapping_get(mapping: Any, key: str) -> Any:
    if isinstance(mapping, Mapping):
        return cast(Mapping[str, Any], mapping).get(key)
    try:
        return mapping[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(mapping, key, None)


def _streamlit_auth_configured() -> bool:
    if not hasattr(st, "login") or not hasattr(st, "user"):
        return False
    try:
        auth_config = _mapping_get(st.secrets, "auth")
    except _streamlit_secret_exceptions:
        return False
    if not auth_config:
        return False

    redirect_uri = str(_mapping_get(auth_config, "redirect_uri") or "").strip()
    cookie_secret = str(_mapping_get(auth_config, "cookie_secret") or "").strip()
    provider_config = _mapping_get(auth_config, GOOGLE_AUTH_PROVIDER) or auth_config
    client_id = str(_mapping_get(provider_config, "client_id") or "").strip()
    client_secret = str(_mapping_get(provider_config, "client_secret") or "").strip()
    metadata_url = str(_mapping_get(provider_config, "server_metadata_url") or "").strip()

    return bool(redirect_uri and cookie_secret and client_id and client_secret and metadata_url)


def _google_user_is_logged_in() -> bool:
    user = getattr(st, "user", None)
    return bool(user and getattr(user, "is_logged_in", False))


def _google_user_email() -> str:
    user = getattr(st, "user", None)
    if not user:
        return ""
    value = _mapping_get(user, "email") or getattr(user, "email", "")
    return str(value or "").strip().lower()


def _google_user_is_allowed() -> bool:
    email = _google_user_email()
    return bool(email and email in _allowed_admin_emails())


def _admin_access_granted() -> bool:
    mode = _admin_auth_mode()
    if mode == "disabled":
        return False
    if _google_auth_enabled(mode) and _google_user_is_logged_in() and _google_user_is_allowed():
        return True
    if _password_auth_enabled(mode) and _authentication_is_current(_expected_password()):
        return True
    return False


def _password_fingerprint(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _clear_authentication() -> None:
    st.session_state[SESSION_AUTH_KEY] = False
    st.session_state.pop(SESSION_AUTH_FINGERPRINT_KEY, None)


def _mark_authenticated(password: str) -> None:
    st.session_state[SESSION_AUTH_KEY] = True
    st.session_state[SESSION_AUTH_FINGERPRINT_KEY] = _password_fingerprint(password)


def _authentication_is_current(expected_password: str | None) -> bool:
    if not expected_password:
        return False
    return bool(st.session_state.get(SESSION_AUTH_KEY)) and st.session_state.get(
        SESSION_AUTH_FINGERPRINT_KEY
    ) == _password_fingerprint(expected_password)


def _render_google_login(mode: str) -> None:
    if not _google_auth_enabled(mode):
        return

    st.subheader("Google SSO")
    allowed_emails = _allowed_admin_emails()
    if not allowed_emails:
        st.warning(
            "Google SSO is selected, but ADMIN_ALLOWED_EMAILS is empty. "
            "Add your Gmail address before relying on Google login."
        )
        return
    if not _streamlit_auth_configured():
        st.info(
            "Google SSO is ready in code, but OAuth secrets are not configured yet. "
            "Add the [auth] and [auth.google] Streamlit Secrets after creating "
            "a Google OAuth client."
        )
        return

    if _google_user_is_logged_in():
        email = _google_user_email() or "unknown account"
        if _google_user_is_allowed():
            st.success(f"Signed in as {email}.")
            st.rerun()
        else:
            st.error(f"{email} is signed in with Google but is not allowed for this dashboard.")
            if st.button("Sign out of Google", use_container_width=True):
                st.logout()
        return

    if st.button("Continue with Google", use_container_width=True):
        st.login(GOOGLE_AUTH_PROVIDER)


def _render_password_login(mode: str) -> None:
    if not _password_auth_enabled(mode):
        return

    st.subheader("Password fallback")
    expected_password = _expected_password()
    if not expected_password:
        st.caption("Password login is unavailable because ADMIN_PASSWORD is not set.")
        return

    with st.form("admin_login_form", clear_on_submit=False):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if hmac.compare_digest(password, expected_password):
            _mark_authenticated(expected_password)
            st.rerun()
        else:
            st.error("Incorrect password.")


def _render_login() -> None:
    st.title("🔒 Admin Dashboard")
    st.caption("Restricted access. Sign in with an approved Google account or configured admin password.")
    mode = _admin_auth_mode()
    if mode == "disabled":
        st.error("Admin dashboard access is disabled by ADMIN_AUTH_MODE=disabled.")
        return

    _render_google_login(mode)
    if mode == "both":
        st.divider()
    _render_password_login(mode)

    if not _google_auth_enabled(mode) and not _password_auth_enabled(mode):
        st.error("No admin authentication method is enabled.")


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

        st.markdown("---")
        st.markdown("**Decision** — log this applicant in the Approved or Declined tab.")
        decision_log_key = f"decision_log_{source}_{identifier}"
        previous_decision = st.session_state.get(decision_log_key)
        if previous_decision:
            badge = "✅" if previous_decision.get("decision") == "approved" else "❌"
            st.info(
                f"{badge} Last logged: **{previous_decision.get('decision', '').title()}** "
                f"({previous_decision.get('tab', '')}) — {previous_decision.get('decided_by', '?')}"
            )
        decision_form_key = f"decision_form_{source}_{identifier}"
        with st.form(decision_form_key, clear_on_submit=False):
            decided_by = st.text_input(
                "Your name / initials",
                key=f"decided_by_{source}_{identifier}",
                placeholder="e.g. Dann",
            )
            notes = st.text_area(
                "Notes (optional)",
                key=f"notes_{source}_{identifier}",
                placeholder="Optional context for the decision log",
                height=70,
            )
            approve_col, decline_col = st.columns(2)
            with approve_col:
                approve_clicked = st.form_submit_button(
                    "✅ Approve", use_container_width=True
                )
            with decline_col:
                decline_clicked = st.form_submit_button(
                    "❌ Decline", use_container_width=True
                )

        if approve_clicked or decline_clicked:
            decision = "approved" if approve_clicked else "declined"
            if not (decided_by or "").strip():
                st.warning("Please enter your name/initials before logging a decision.")
            else:
                with st.spinner(f"Logging {decision}..."):
                    result = append_decision_from_payload(
                        payload,
                        decision=decision,
                        decided_by=decided_by,
                        notes=notes,
                        storage_location=location_label,
                    )
                status = result.get("status")
                if status == "appended":
                    st.session_state[decision_log_key] = {
                        "decision": decision,
                        "decided_by": decided_by.strip(),
                        "tab": result.get("tab", ""),
                    }
                    st.success(
                        f"Logged **{decision}** in the '{result.get('tab')}' tab."
                    )
                elif status == "disabled":
                    st.warning(
                        "Sheets export is not configured. "
                        "Set GOOGLE_SERVICE_ACCOUNT_JSON and APPLICANTS_SHEET_ID in Streamlit Secrets."
                    )
                else:
                    st.error(
                        f"Could not log decision: {result.get('message', 'unknown error')}"
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

    if not _admin_access_granted():
        _clear_authentication()
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
            _clear_authentication()
            if _google_user_is_logged_in():
                st.logout()
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
