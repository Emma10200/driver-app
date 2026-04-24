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
    company_slug_explicitly_provided,
    get_company_profile,
    resolve_company_slug,
    sync_runtime_context,
)
from services.error_log_service import log_application_error
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
    page_title=f"{REQUESTED_COMPANY.name} - Driver Application",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="collapsed",
)

init_session_state()
sync_runtime_context()


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
            <h1 style='margin-bottom:0.25rem;'>This link doesn't go anywhere yet</h1>
            <p style='color:#bbb; font-size:1.05rem; max-width:640px; margin:0.75rem auto 0;'>
                We couldn't find the application you're looking for. If you were
                sent here to fill out a driver application, please reach out and
                we'll send you the correct link.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div style='max-width:520px; margin:2rem auto 0; padding:1.5rem 1.75rem;
                    border:1px solid rgba(255,255,255,0.12); border-radius:12px;
                    background:rgba(255,255,255,0.03);'>
            <h3 style='margin:0 0 1rem; text-align:center;'>Who to call</h3>
            <p style='margin:0.35rem 0;'>
                <strong>Safety</strong> &mdash; Dann
                &nbsp;<a href='tel:+12245953477'>(224) 595-3477</a>
            </p>
            <p style='margin:0.35rem 0;'>
                <strong>Accounting</strong> &mdash; Emma
                &nbsp;<a href='tel:+17735439577'>(773) 543-9577</a>
            </p>
            <p style='margin:0.35rem 0;'>
                <strong>Owner</strong> &mdash; Deyana
                &nbsp;<a href='tel:+12247151371'>(224) 715-1371</a>
            </p>
        </div>
        <p style='text-align:center; color:#888; margin-top:2rem; font-size:0.9rem;'>
            Already have your link? Make sure you opened the full URL &mdash; the
            company shortcode at the end is what loads your application.
        </p>
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
