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
from config import COMPANY_NAME
from state import init_session_state
from ui.common import render_app_shell, render_progress_bar, scroll_to_top_on_page_change

SUBMISSIONS_DIR = Path(__file__).resolve().parent / "submissions"

st.set_page_config(
    page_title=f"{COMPANY_NAME} - Driver Application",
    page_icon="🚛",
    layout="wide",
)

init_session_state()
render_app_shell()
page = render_progress_bar()
scroll_to_top_on_page_change(page)

if st.session_state.submitted:
    render_submission_complete(SUBMISSIONS_DIR)
elif page == 1:
    render_personal_information_page()
elif page == 2:
    render_company_questions_page()
elif page == 12:
    render_review_submit_page(SUBMISSIONS_DIR)
elif not render_remaining_page(page):
    st.error("This application step could not be loaded. Please refresh and try again.")
