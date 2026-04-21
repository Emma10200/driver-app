"""Hidden admin test helpers for safe internal QA sessions."""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from runtime_context import admin_tools_enabled, get_active_company_profile
from state import init_session_state, reset_application_state
from utils.formatting import format_ssn

SSN_WIDGET_KEY = "personal_ssn_display"


def _build_test_form_data() -> dict[str, object]:
    company = get_active_company_profile()
    email_slug = company.slug.replace("-", "")
    return {
        "company_slug": company.slug,
        "company_name": company.name,
        "first_name": "Test",
        "middle_name": "QA",
        "last_name": "Applicant",
        "dob": date(1990, 1, 1),
        "ssn": "123456789",
        "address": "123 Testing Lane",
        "city": "Fontana",
        "state": "CA",
        "zip_code": "92335",
        "country": "United States",
        "primary_phone": "5551234567",
        "cell_phone": "5559876543",
        "email": f"qa+{email_slug}@example.com",
        "preferred_contact": "Email",
        "best_time": "Any",
        "resided_3_years": "Yes",
        "prev_address": "",
        "prev_city": "",
        "prev_state": "",
        "prev_zip": "",
        "emergency_name": "Safety Tester",
        "emergency_phone": "5553334444",
        "emergency_relationship": "Supervisor",
        "text_consent": False,
        "position": "Owner Operator",
        "eligible_us": "Yes",
        "read_english": "Yes",
        "currently_employed": "Yes",
        "last_employment_end": None,
        "worked_here_before": "No",
        "preferred_office": "California Office",
        "applying_location": "California Office",
        "twic_card": "No",
        "twic_expiration": None,
        "referral_source": "Internet Search",
        "referral_name": "",
        "prev_dates": "",
        "relatives_here": "No",
        "relatives_names": "",
        "equipment_description": "2022 Freightliner Cascadia",
        "equipment_year": "2022",
        "equipment_make": "Freightliner",
        "equipment_model": "Cascadia",
        "equipment_color": "White",
        "equipment_vin": "1FUJGLDR0NSNA1234",
        "equipment_weight": "80,000 lbs",
        "equipment_mileage": "250000",
        "fifth_wheel_height": "47 in",
        "safe_driving_awards": "Million Safe Miles Club",
        "known_other_name": "No",
        "other_name": "",
        "highest_grade": "High School",
        "last_school": "Testing High School, Fontana, CA",
        "attended_trucking_school": "Yes",
        "ts_name": "QA Truck Driving Academy",
        "ts_city_state": "Riverside, CA",
        "ts_start": date(2018, 1, 15),
        "ts_end": date(2018, 4, 15),
        "ts_graduated": "Yes",
        "ts_fmcsa_subject": "Yes",
        "ref1": "Alex Ops | 555-000-1000 | Mentor",
        "ref2": "Jamie Dispatch | 555-000-2000 | Colleague",
        "disq_391_15": "No",
        "disq_suspended": "No",
        "disq_denied": "No",
        "disq_drug_test": "No",
        "disq_convicted": "No",
        "drug_alcohol_cert": True,
        "applicant_cert": True,
        "sig_full_name": "Test QA Applicant",
        "sig_date": date.today().isoformat(),
        "sig_timestamp": datetime.now().isoformat(),
        "fcra_acknowledge": True,
        "fcra_timestamp": datetime.now().isoformat(),
        "ca_applicable": False,
        "ca_disclosure_acknowledge": False,
        "ca_disclosure_timestamp": None,
        "ca_copy": False,
        "psp_acknowledge": True,
        "psp_timestamp": datetime.now().isoformat(),
        "clearinghouse_acknowledge": True,
        "clearinghouse_timestamp": datetime.now().isoformat(),
        "inv_consumer_report": True,
        "review_confirm": True,
    }


def _build_test_licenses() -> list[dict[str, object]]:
    return [
        {
            "number": "T1234567",
            "state": "CA",
            "class": "Class A",
            "expiration": date(2027, 6, 30),
            "med_card_exp": date(2026, 12, 31),
            "is_cdl": "Yes",
            "tanker": "No",
            "hazmat": "No",
            "hazmat_exp": None,
            "doubles": "Yes",
            "x_endorsement": "No",
        }
    ]


def _build_test_employers() -> list[dict[str, object]]:
    return [
        {
            "company": "Sample Carrier LLC",
            "address": "456 Fleet Blvd",
            "city_state": "Ontario, CA 91761",
            "phone": "5551112222",
            "position": "Owner Operator",
            "start": date(2020, 1, 1),
            "end": date.today(),
            "reason": "Still active",
            "terminated": "No",
            "current": "Yes",
            "contact_ok": "Yes",
            "cmv": "Yes",
            "fmcsa": "Yes",
            "dot_testing": "Yes",
            "areas": "CA, NV, AZ",
            "miles": "2500",
            "truck": "Freightliner Cascadia",
            "trailer": "Van",
            "trailer_len": "53 feet or more",
        }
    ]


def activate_safe_test_application(*, jump_to_review: bool) -> None:
    company_slug = st.session_state.get("company_slug", "prestige")
    admin_enabled = st.session_state.get("admin_tools_enabled", False)

    reset_application_state()
    init_session_state()

    st.session_state.company_slug = company_slug
    st.session_state.admin_tools_enabled = admin_enabled
    st.session_state.test_mode = True
    st.session_state.form_data = _build_test_form_data()
    st.session_state.licenses = _build_test_licenses()
    st.session_state.employers = _build_test_employers()
    st.session_state.accidents = []
    st.session_state.violations = []
    st.session_state.uploaded_documents = []
    st.session_state.current_page = 12 if jump_to_review else 1
    st.session_state[SSN_WIDGET_KEY] = format_ssn(str(st.session_state.form_data.get("ssn", "")))


def clear_test_session() -> None:
    company_slug = st.session_state.get("company_slug", "prestige")
    admin_enabled = st.session_state.get("admin_tools_enabled", False)

    reset_application_state()
    init_session_state()
    st.session_state.company_slug = company_slug
    st.session_state.admin_tools_enabled = admin_enabled


def render_admin_test_tools() -> None:
    if not admin_tools_enabled():
        return

    company = get_active_company_profile()

    st.markdown("---")
    st.markdown("### Admin test tools")
    st.caption(
        f"Internal-only QA mode for `{company.slug}`. Safe test sessions use fake applicant data, separate storage paths, "
        "and no live internal notifications unless a test inbox is configured."
    )

    if st.session_state.get("test_mode"):
        st.success("Safe test mode is active for this browser session.")

    if st.button("🧪 Load safe test application", key="admin_test_fill_page1", use_container_width=True):
        activate_safe_test_application(jump_to_review=False)
        st.rerun()

    if st.button("⚡ Load review-ready test application", key="admin_test_fill_review", use_container_width=True):
        activate_safe_test_application(jump_to_review=True)
        st.rerun()

    if st.button("🧹 Clear test session", key="admin_test_clear", use_container_width=True):
        clear_test_session()
        st.rerun()