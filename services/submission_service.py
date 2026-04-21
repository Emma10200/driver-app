"""Submission artifact and persistence helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from pdf_generator import (
    generate_application_pdf,
    generate_california_disclosure_pdf,
    generate_clearinghouse_pdf,
    generate_fcra_pdf,
    generate_psp_pdf,
)
from runtime_context import get_storage_namespace
from submission_storage import save_submission_bundle as persist_submission_bundle


def build_submission_artifacts() -> dict[str, bytes | None]:
    return {
        "application_pdf": generate_application_pdf(
            st.session_state.form_data,
            st.session_state.employers,
            st.session_state.licenses,
            st.session_state.accidents,
            st.session_state.violations,
        ),
        "fcra_pdf": generate_fcra_pdf(st.session_state.form_data),
        "california_pdf": generate_california_disclosure_pdf(st.session_state.form_data)
        if st.session_state.form_data.get("ca_applicable")
        else None,
        "psp_pdf": generate_psp_pdf(st.session_state.form_data),
        "clearinghouse_pdf": generate_clearinghouse_pdf(st.session_state.form_data),
    }


def save_submission_bundle(local_base_dir: Path, artifacts: dict[str, bytes | None]) -> dict[str, Any]:
    return persist_submission_bundle(
        form_data=st.session_state.form_data,
        employers=st.session_state.employers,
        licenses=st.session_state.licenses,
        accidents=st.session_state.accidents,
        violations=st.session_state.violations,
        uploaded_documents=st.session_state.get("uploaded_documents", []),
        artifacts=artifacts,
        local_base_dir=local_base_dir,
        storage_namespace=get_storage_namespace(),
    )
