from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import streamlit as st

INTERNAL_ROUTE_SESSION_KEY = "_post_login_internal_route"

_INTERNAL_ROUTE_PARAMS: dict[str, dict[str, str]] = {
    "dashboard": {"dashboard": "1"},
    "qbo": {"qbo": "1"},
    "safety": {"safety": "1"},
    "gps-map": {"route": "gps-map"},
    "dispatch-board": {"route": "dispatch-board"},
}


def _mapping_get(mapping: Any, key: str) -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    try:
        return mapping[key]
    except Exception:
        return getattr(mapping, key, None)


def _user_logged_in() -> bool:
    user = getattr(st, "user", None)
    return bool(user and getattr(user, "is_logged_in", False))


def remember_internal_route_for_login(route_key: str) -> None:
    """Remember a protected/internal route before starting Streamlit SSO.

    Streamlit's Google login can return without the original query string in
    some hosted flows. Store only while logged out so a normal signed-in user
    does not get pulled back to an old internal page later.
    """
    route_key = str(route_key or "").strip()
    if route_key not in _INTERNAL_ROUTE_PARAMS:
        return
    if _user_logged_in():
        return
    st.session_state[INTERNAL_ROUTE_SESSION_KEY] = route_key


def restore_internal_route_after_login(*, current_route_requested: bool) -> bool:
    """Restore the remembered internal URL after Google SSO returns.

    Returns True when it changed query params and requested a rerun. Call this
    before the public company-link fallback, but after direct URL route checks
    are available.
    """
    if current_route_requested or not _user_logged_in():
        return False

    route_key = str(st.session_state.get(INTERNAL_ROUTE_SESSION_KEY) or "").strip()
    params = _INTERNAL_ROUTE_PARAMS.get(route_key)
    if not params:
        st.session_state.pop(INTERNAL_ROUTE_SESSION_KEY, None)
        return False

    st.session_state.pop(INTERNAL_ROUTE_SESSION_KEY, None)
    for key, value in params.items():
        st.query_params[key] = value
    st.rerun()
    return True
