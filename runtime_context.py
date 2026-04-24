"""Runtime company/test-mode context helpers."""

from __future__ import annotations

from typing import Any

import streamlit as st

from config import COMPANY_PROFILES, DEFAULT_COMPANY_SLUG, CompanyProfile
from submission_storage import get_runtime_secret


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "test", "admin"}


def _query_param_value(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
    except Exception:  # pragma: no cover - defensive fallback for non-Streamlit tooling
        return ""

    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def normalize_company_slug(value: str | None) -> str:
    slug = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "": DEFAULT_COMPANY_SLUG,
        "prestige-transportation": "prestige",
        "prestige-transportation-inc": "prestige",
        # Legacy slug -- keep so old printed/bookmarked links resolve.
        "side-xpress": "xpress",
        "sidexpress": "xpress",
        "sideexpress": "xpress",
        "side-xpress-inc": "xpress",
        "xpress-inc": "xpress",
        "xpress-trans": "xpress",
        "xpresstrans": "xpress",
    }
    slug = aliases.get(slug, slug)
    return slug if slug in COMPANY_PROFILES else DEFAULT_COMPANY_SLUG


def _try_resolve_known_slug(value: str | None) -> str | None:
    """Return a valid slug if value matches a known one (after alias normalization), else None."""
    raw = str(value or "").strip().lower().replace("_", "-")
    if not raw:
        return None
    aliases = {
        "prestige-transportation": "prestige",
        "prestige-transportation-inc": "prestige",
        "side-xpress": "xpress",
        "sidexpress": "xpress",
        "sideexpress": "xpress",
        "side-xpress-inc": "xpress",
        "xpress-inc": "xpress",
        "xpress-trans": "xpress",
        "xpresstrans": "xpress",
    }
    candidate = aliases.get(raw, raw)
    return candidate if candidate in COMPANY_PROFILES else None


def extract_slug_from_query() -> str | None:
    """Return slug if explicitly provided in URL, else None.

    Supports (in order):
      - ?company=<slug>   (canonical, kept for backwards compatibility)
      - ?c=<slug>         (short alias)
      - ?co=<slug>        (short alias)
      - ?<slug>           (keyless, e.g. ?prestige or ?xpress)
    """
    for key in ("company", "c", "co"):
        resolved = _try_resolve_known_slug(_query_param_value(key))
        if resolved:
            return resolved

    try:
        keys = list(st.query_params.keys())
    except Exception:
        keys = []
    for key in keys:
        resolved = _try_resolve_known_slug(key)
        if resolved:
            return resolved
    return None


def resolve_company_slug() -> str:
    return extract_slug_from_query() or DEFAULT_COMPANY_SLUG


def company_slug_explicitly_provided() -> bool:
    return extract_slug_from_query() is not None


def get_company_profile(slug: str | None = None) -> CompanyProfile:
    return COMPANY_PROFILES[normalize_company_slug(slug)]


def get_active_company_profile() -> CompanyProfile:
    return get_company_profile(st.session_state.get("company_slug") or resolve_company_slug())


def admin_tools_requested() -> bool:
    return _truthy(_query_param_value("admin")) or _truthy(_query_param_value("mode"))


def admin_dashboard_requested() -> bool:
    """True when the URL carries ``?dashboard=1`` (or similar truthy value).

    Distinct from ``admin_tools_requested`` -- ``?admin=1`` toggles in-page
    test/admin tools inside the regular application flow, while
    ``?dashboard=1`` opens the standalone password-protected dashboard.
    """
    return _truthy(_query_param_value("dashboard"))


def admin_tools_enabled() -> bool:
    if not admin_tools_requested():
        return False

    required_token = (get_runtime_secret("ADMIN_TEST_TOKEN", "") or "").strip()
    if not required_token:
        return True

    return _query_param_value("token") == required_token


def is_test_mode_active() -> bool:
    return bool(st.session_state.get("test_mode"))


def get_storage_namespace() -> str:
    profile = get_active_company_profile()
    mode_segment = "test-mode" if is_test_mode_active() else "live"
    return f"companies/{profile.slug}/{mode_segment}"


def sync_runtime_context() -> None:
    url_slug = extract_slug_from_query()
    if url_slug:
        st.session_state.company_slug = url_slug
        st.session_state.company_slug_locked = True
    elif st.session_state.get("company_slug_locked"):
        # User picked via the in-app picker; keep their selection.
        pass
    # Otherwise leave session as default; app.py will render the picker.

    _maybe_resume_draft_from_query()

    profile = get_company_profile(st.session_state.get("company_slug") or DEFAULT_COMPANY_SLUG)
    st.session_state.admin_tools_enabled = admin_tools_enabled()

    form_data = st.session_state.get("form_data")
    if isinstance(form_data, dict):
        form_data["company_slug"] = profile.slug
        form_data["company_name"] = profile.name
        form_data["test_mode"] = bool(st.session_state.get("test_mode"))


DRAFT_RESUME_GUARD_KEY = "_draft_resume_attempted"


def _maybe_resume_draft_from_query() -> None:
    """If the URL carries ?draft=<id> and we haven't loaded it yet this
    session, restore that draft into session state.

    The guard key is intentionally preserved across reset_application_state
    (see state.py) so the inner sync_runtime_context() call inside
    load_draft_into_session does not recurse into another load.
    """
    draft_id = _query_param_value("draft").strip()
    if not draft_id:
        return

    if st.session_state.get(DRAFT_RESUME_GUARD_KEY) == draft_id:
        return

    # Mark BEFORE loading so the inner sync_runtime_context sees the guard.
    st.session_state[DRAFT_RESUME_GUARD_KEY] = draft_id

    from services.draft_service import load_draft_into_session

    try:
        load_draft_into_session(draft_id)
    except Exception:
        st.session_state["draft_load_error"] = (
            "We couldn't find that saved draft. You can start a new application "
            "or try the link again later."
        )
