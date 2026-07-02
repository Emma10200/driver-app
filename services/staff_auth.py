from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import streamlit as st

from services.qbo_auth import qbo_allowed_emails

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_EMAIL = "accounts@prestige.inc"


def _mapping_get(mapping: Any, key: str) -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    try:
        return mapping[key]
    except Exception:
        return getattr(mapping, key, None)


def staff_allowed_emails() -> set[str]:
    """Return the staff SSO allowlist used for sensitive internal pages.

    For now this intentionally mirrors the QBO importer allowlist. If the QBO
    secret is not present in a local/dev environment, keep the requested single
    accounting login as the safe default instead of opening access broadly.
    """
    return qbo_allowed_emails() or {_DEFAULT_ALLOWED_EMAIL}


def google_user_is_logged_in() -> bool:
    user = getattr(st, "user", None)
    return bool(user and getattr(user, "is_logged_in", False))


def google_user_email() -> str:
    user = getattr(st, "user", None)
    if not user:
        return ""
    return str(_mapping_get(user, "email") or getattr(user, "email", "") or "").strip().lower()


def staff_access_granted() -> bool:
    email = google_user_email()
    return bool(google_user_is_logged_in() and email and email in staff_allowed_emails())


def render_staff_login_gate(
    *,
    title: str,
    caption: str = "Sensitive internal page. Sign in with an approved company Google account.",
) -> bool:
    """Render a Google SSO gate and return True only for allowlisted staff.

    This uses the same Streamlit Google login and QBO allowlist that protects
    the QuickBooks importer. Callers should stop rendering the sensitive page
    when this returns False.
    """
    if staff_access_granted():
        return True

    st.title(f"🔒 {title}")
    st.caption(caption)

    st.caption("Access is currently limited to the accounting allowlist.")

    if not hasattr(st, "login") or not hasattr(st, "user"):
        st.error("This Streamlit version does not support native Google login.")
        return False

    if not google_user_is_logged_in():
        st.info("Sign in with the approved Google account to continue.")
        if st.button("Continue with Google", type="primary", use_container_width=True):
            try:
                st.login("google")
            except Exception as exc:  # pragma: no cover - provider/config dependent
                logger.warning("Named Google login failed; retrying default provider: %s", exc)
                try:
                    st.login()
                except Exception as fallback_exc:  # pragma: no cover - provider/config dependent
                    logger.exception("Streamlit Google login failed to start")
                    st.error("Google login is not configured correctly for this app yet.")
                    detail = str(fallback_exc).strip()
                    if detail:
                        st.caption(detail)
        return False

    email = google_user_email() or "unknown account"
    st.error(f"{email} is signed in but is not allowed to view this page.")
    st.caption("Access is controlled by the QBO importer allowlist (`qbo.allowed_emails` / `QBO_ALLOWED_EMAILS`).")
    if st.button("Sign out of Google", use_container_width=True):
        try:
            st.logout()
        except Exception:  # pragma: no cover - deployed Streamlit behavior
            pass
    return False
