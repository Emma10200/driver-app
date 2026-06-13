"""
Prestige Transportation Inc. - Driver Application Portal (Phase 1)
MVP Streamlit Application

Compliance notes:
- No criminal history questions (California Fair Chance Act / AB 1008)
- FCRA disclosure rendered as standalone separate page (15 U.S.C. § 1681b)
- All language uses independent contractor terminology (no W-2 / employee terms)
- DOT-specific disqualification questions only (49 CFR 391.15)
"""

from pathlib import Path

import streamlit as st

from app_sections.company_questions import render_company_questions_page
from app_sections.personal_info import render_personal_information_page
from app_sections.remaining_pages import render_remaining_page
from app_sections.review_submit import render_review_submit_page, render_submission_complete
from runtime_context import (
    admin_dashboard_requested,
    company_slug_explicitly_provided,
    get_company_profile,
    qbo_importer_requested,
    qbo_oauth_callback_requested,
    resolve_company_slug,
    sync_runtime_context,
)
from services.admin_dashboard import render_admin_dashboard
from services.error_log_service import log_application_error
from services.qbo_dashboard import render_qbo_dashboard
from state import init_session_state
from ui.common import (
    _wire_back_button_shim,
    render_app_shell,
    render_progress_bar,
    scroll_to_top_on_page_change,
    show_user_error,
)

try:
    from ui.common import render_version_footer
except ImportError:
    def render_version_footer() -> None:
        return None

SUBMISSIONS_DIR = Path(__file__).resolve().parent / "submissions"
REQUESTED_COMPANY = get_company_profile(resolve_company_slug())

st.set_page_config(
    page_title="Driver Application",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="collapsed",
)

init_session_state()
sync_runtime_context()


def _truthy_query_param(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "test", "admin"}


def _query_param_value(name: str) -> str:
    try:
        value = st.query_params.get(name, "")
    except Exception:
        return ""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _document_upload_requested() -> bool:
    """Local route check so stale Cloud imports cannot break app startup."""
    route = _query_param_value("route").strip().lower().replace("_", "-")
    upload = _query_param_value("upload").strip().lower().replace("_", "-")
    return (
        _truthy_query_param(_query_param_value("documents"))
        or _truthy_query_param(_query_param_value("docs"))
        or route in {"documents", "document-upload", "driver-documents"}
        or upload in {"documents", "document-upload", "driver-documents"}
    )


def _safety_portal_requested() -> bool:
    """Local route check for the staff Safety Paperwork Portal."""
    route = _query_param_value("route").strip().lower().replace("_", "-")
    return (
        _truthy_query_param(_query_param_value("safety"))
        or route in {"safety", "safety-portal", "safety-paperwork"}
    )


def _shop_inventory_requested() -> bool:
    """Local route check for the mobile shop Inventory List (General Truck Service)."""
    route = _query_param_value("route").strip().lower().replace("_", "-")
    return (
        _truthy_query_param(_query_param_value("shop"))
        or route in {"shop", "inventory", "shop-inventory"}
    )


def _safety_upload_token() -> str:
    """Return a recipient-specific safety upload token, if present."""
    for key in ("safety_upload", "safety_token"):
        token = _query_param_value(key).strip()
        if token:
            return token

    # Also support ?safety=<token>; ?safety=1 remains the staff portal.
    safety_value = _query_param_value("safety").strip()
    if safety_value and not _truthy_query_param(safety_value):
        return safety_value
    return ""


# Standalone admin dashboard route. Reachable via ?dashboard=1; gated by admin
# auth config (Google SSO, password fallback, or both). Short-circuits the
# entire application flow so the dashboard renders by itself.
if admin_dashboard_requested():
    render_admin_dashboard(SUBMISSIONS_DIR)
    render_version_footer()
    st.stop()


# Standalone QBO importer route for accounting. Reachable via ?qbo=1, plus
# the Intuit OAuth callback query param. This short-circuits the driver
# application flow exactly like the admin dashboard route above.
if qbo_importer_requested() or qbo_oauth_callback_requested():
    render_qbo_dashboard()
    render_version_footer()
    st.stop()


# Standalone driver document upload route. Reachable via ?documents=1 (also
# ?docs=1 / ?route=document-upload). This intentionally bypasses the company
# application flow and does not require a company slug.
if _document_upload_requested():
    from services.document_upload_page import render_document_upload_page

    render_document_upload_page(SUBMISSIONS_DIR)
    render_version_footer()
    st.stop()


# Recipient-specific Safety Paperwork upload link. Reachable via
# ?safety_upload=<token> (or ?safety=<token>). This is public-by-link and
# intentionally does not require staff Google SSO.
_recipient_safety_token = _safety_upload_token()
if _recipient_safety_token:
    from services.safety_upload_page import render_safety_upload_page

    render_safety_upload_page(SUBMISSIONS_DIR, _recipient_safety_token)
    render_version_footer()
    st.stop()


# Standalone Safety Paperwork Portal (staff-only). Reachable via ?safety=1 or
# ?route=safety. SSO-gated inside the page using the QBO allowlist. Lazy
# import so a parser-side error cannot break overall startup.
if _safety_portal_requested():
    from services.safety_portal_page import render_safety_portal_page

    render_safety_portal_page(SUBMISSIONS_DIR)
    render_version_footer()
    st.stop()


# Standalone mobile shop Inventory List (General Truck Service repair shop).
# Reachable via ?shop=1 (also ?route=inventory / ?route=shop). Read-only browser
# over the QBO-synced parts catalog. Public-by-link for now; auth is pending.
if _shop_inventory_requested():
    from services.shop_inventory_page import render_shop_inventory_page

    render_shop_inventory_page()
    render_version_footer()
    st.stop()


def _render_company_picker() -> None:
    """Render the 'broken link' help page shown when no company slug was provided.

    Per ownership: the public landing page should NOT expose the list of
    companies. If someone reaches this page, the link they followed was either
    incomplete or wrong. Show a friendly help message with phone numbers so they
    can call us for the correct application link.
    """
    st.markdown(
        """
        <div style='text-align:center; padding: 1.5rem 0 0.5rem;'>
            <h1 style='margin-bottom:0.25rem;'>Whoops &mdash; this link looks off</h1>
            <p style='color:#bbb; font-size:1.05rem; max-width:640px; margin:0.75rem auto 0;'>
                It looks like the application link was pasted incomplete or
                copied wrong. No worries &mdash; reach out below and we'll get
                you a working link right away.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div style='max-width:560px; margin:2rem auto 0; padding:1.5rem 1.75rem;
                    border:1px solid rgba(255,255,255,0.12); border-radius:12px;
                    background:rgba(255,255,255,0.03);'>
            <h3 style='margin:0 0 1rem; text-align:center;'>
                Call or text any of the following for a working application link
            </h3>
            <p style='margin:0.35rem 0;'>
                <strong>Safety</strong> &mdash; Dann
                &nbsp;<a href='tel:+12245953477'>(224) 595-3477</a>
                &nbsp;&middot;&nbsp;
                <a href='sms:+12245953477'>text</a>
            </p>
            <p style='margin:0.35rem 0;'>
                <strong>Accounting</strong> &mdash; Emma
                &nbsp;<a href='tel:+17735439577'>(773) 543-9577</a>
                &nbsp;&middot;&nbsp;
                <a href='sms:+17735439577'>text</a>
            </p>
            <p style='margin:0.35rem 0;'>
                <strong>Owner</strong> &mdash; Deyana
                &nbsp;<a href='tel:+12247151371'>(224) 715-1371</a>
                &nbsp;&middot;&nbsp;
                <a href='sms:+12247151371'>text</a>
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


if not company_slug_explicitly_provided() and not st.session_state.get("company_slug_locked"):
    _render_company_picker()
    render_version_footer()
    st.stop()

render_app_shell()
page = render_progress_bar()
scroll_to_top_on_page_change(page)
_wire_back_button_shim(page)

try:
    if st.session_state.submitted:
        render_submission_complete(SUBMISSIONS_DIR)
    elif page == 1:
        render_personal_information_page()
    elif page == 2:
        render_company_questions_page()
    elif page == 12:
        render_review_submit_page(SUBMISSIONS_DIR)
    elif not render_remaining_page(page):
        show_user_error(
            "This application step could not be loaded. Please refresh and try again.",
            code="page_render_not_found",
            severity="warning",
            extra={"page": page},
        )
except Exception as exc:
    log_application_error(
        code="unhandled_app_exception",
        user_message="The application encountered an unexpected issue.",
        technical_details=str(exc),
        extra={"page": page},
    )
    st.warning("We hit an unexpected issue while loading this step. Please refresh and try again.")

render_version_footer()
