"""Session-state helpers for the Streamlit driver application."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import streamlit as st


SESSION_DEFAULTS: dict[str, Any] = {
    "current_page": 1,
    "last_rendered_page": None,
    "company_slug": "prestige",
    "admin_tools_enabled": False,
    "test_mode": False,
    "form_data": {},
    "submitted": False,
    "draft_id": None,
    "draft_saved_at": None,
    "draft_save_error": None,
    "draft_load_error": None,
    "draft_resume_code": "",
    "employers": [],
    "accidents": [],
    "violations": [],
    "licenses": [],
    "uploaded_documents": [],
    "submission_artifacts": None,
    "saved_submission_dir": None,
    "submission_save_error": None,
    "submission_save_notice": None,
    "submission_notification_sent": False,
    "submission_notification_status_code": None,
    "submission_notification_status": None,
    "submission_notification_error": None,
}


def init_session_state() -> None:
    for key, default_value in SESSION_DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = deepcopy(default_value)


def next_page() -> None:
    st.session_state.current_page += 1


def prev_page() -> None:
    st.session_state.current_page -= 1


def reset_application_state() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
