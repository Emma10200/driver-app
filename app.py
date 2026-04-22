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
    from config import COMPANY_PROFILES

    st.markdown(
        """
        <div style='text-align:center; padding: 1.5rem 0 0.5rem;'>
            <h1 style='margin-bottom:0.25rem;'>🚛 Driver Application Portal</h1>
            <p style='color:#555; font-size:1.05rem;'>Which company are you applying with?</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    profiles = list(COMPANY_PROFILES.values())
    cols = st.columns(len(profiles))
    for col, profile in zip(cols, profiles):
        with col:
            st.markdown(
                f"<div style='text-align:center; padding-top:0.5rem;'><h3 style='margin-bottom:0.25rem;'>{profile.name}</h3>"
                f"<p style='color:#666; min-height:2.5rem;'>{profile.city_state_zip or ''}</p></div>",
                unsafe_allow_html=True,
            )
            if st.button(f"Apply with {profile.name}", key=f"pick_company_{profile.slug}", use_container_width=True, type="primary"):
                st.session_state.company_slug = profile.slug
                st.session_state.company_slug_locked = True
                try:
                    st.query_params["company"] = profile.slug
                except Exception:
                    pass
                st.rerun()

    st.markdown(
        "<p style='text-align:center; color:#888; margin-top:2rem; font-size:0.85rem;'>"
        "If you received a direct link, it should bring you straight to the right application."
        "</p>",
        unsafe_allow_html=True,
    )


if not company_slug_explicitly_provided() and not st.session_state.get("company_slug_locked"):
    _render_company_picker()
    render_version_footer()
    st.stop()

render_app_shell()
page = render_progress_bar()
scroll_to_top_on_page_change(page)

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
