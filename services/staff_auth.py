from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import streamlit as st

from services.dispatch_contacts import load_company_info, load_dispatcher_contacts
from services.qbo_auth import qbo_allowed_emails

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_EMAIL = "accounts@prestige.inc"
_EXTRA_CONTACT_ALLOWED_EMAILS = {"deyana@prestigetransportation.com"}


def _mapping_get(mapping: Any, key: str) -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    try:
        return mapping[key]
    except Exception:
        return getattr(mapping, key, None)


def _normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    return email if "@" in email and "." in email.rsplit("@", 1)[-1] else ""


def contact_directory_allowed_emails(
    companies: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
) -> set[str]:
    """Emails referenced by the dispatch-board contacts dialog.

    Includes company dispatch inboxes, dispatcher/division emails, shared inboxes,
    and explicitly approved internal contacts that are referenced operationally
    but not stored as email fields in the source phone sheet.
    """
    emails = set(_EXTRA_CONTACT_ALLOWED_EMAILS)
    if companies is None:
        companies = load_company_info()
    if contacts is None:
        contacts = load_dispatcher_contacts()

    for company in companies:
        email = _normalize_email(company.get("dispatch_email"))
        if email:
            emails.add(email)
    for contact in contacts:
        email = _normalize_email(contact.get("email"))
        if email:
            emails.add(email)
    return emails


def _safe_contact_directory_allowed_emails() -> set[str]:
    try:
        return contact_directory_allowed_emails()
    except Exception as exc:  # pragma: no cover - Supabase/config dependent
        logger.warning("Contact-directory staff allowlist unavailable, using explicit extras: %s", exc)
        return set(_EXTRA_CONTACT_ALLOWED_EMAILS)


def staff_allowed_emails() -> set[str]:
    """Return the staff SSO allowlist used for sensitive internal pages.

    GPS and dispatch-board access is broader than QBO: it includes the QBO
    accounting login plus every email referenced in the editable contacts page.
    This keeps the page user list editable/visible without requiring a Secrets
    deploy for each dispatcher email.
    """
    return {_DEFAULT_ALLOWED_EMAIL} | qbo_allowed_emails() | _safe_contact_directory_allowed_emails()


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

    st.caption(
        "Dispatcher emails listed in the Company Contacts / Info page have access. "
        "Accounting and approved company contacts are included too."
    )
    st.caption(
        "Use your assigned dispatch email, e.g. `dispatch#@prestige.inc`, "
        "`dispatch#@prestigecalifornia.com`, or the shared Xpress dispatch email."
    )

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
    st.caption(
        "Access is controlled by the contacts page dispatcher emails plus the QBO accounting allowlist."
    )
    if st.button("Sign out of Google", use_container_width=True):
        try:
            st.logout()
        except Exception:  # pragma: no cover - deployed Streamlit behavior
            pass
    return False
