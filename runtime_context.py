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
        "sidexpress": "side-xpress",
        "sideexpress": "side-xpress",
        "side-xpress-inc": "side-xpress",
    }
    slug = aliases.get(slug, slug)
    return slug if slug in COMPANY_PROFILES else DEFAULT_COMPANY_SLUG


def resolve_company_slug() -> str:
    return normalize_company_slug(_query_param_value("company"))


def get_company_profile(slug: str | None = None) -> CompanyProfile:
    return COMPANY_PROFILES[normalize_company_slug(slug)]


def get_active_company_profile() -> CompanyProfile:
    return get_company_profile(st.session_state.get("company_slug") or resolve_company_slug())


def admin_tools_requested() -> bool:
    return _truthy(_query_param_value("admin")) or _truthy(_query_param_value("mode"))


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
    profile = get_company_profile(resolve_company_slug())
    st.session_state.company_slug = profile.slug
    st.session_state.admin_tools_enabled = admin_tools_enabled()

    form_data = st.session_state.get("form_data")
    if isinstance(form_data, dict):
        form_data["company_slug"] = profile.slug
        form_data["company_name"] = profile.name
        form_data["test_mode"] = bool(st.session_state.get("test_mode"))
