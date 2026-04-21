"""Page 1 renderer: personal information."""

from __future__ import annotations

from datetime import date

import streamlit as st

from config import MOBILE_CARRIERS, US_STATES
from runtime_context import get_active_company_profile
from services.draft_service import autosave_draft
from state import next_page
from ui.common import selectbox_with_placeholder, show_missing_fields, show_user_error
from utils.formatting import format_ssn, normalize_digits


SSN_WIDGET_KEY = "personal_ssn_display"
STATE_NAME_TO_CODE = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


def _format_ssn_input() -> None:
    st.session_state[SSN_WIDGET_KEY] = format_ssn(st.session_state.get(SSN_WIDGET_KEY, ""))


def _ensure_ssn_widget_state() -> None:
    stored_ssn = st.session_state.form_data.get("ssn", "")
    formatted_stored_ssn = format_ssn(stored_ssn)

    if SSN_WIDGET_KEY not in st.session_state:
        st.session_state[SSN_WIDGET_KEY] = formatted_stored_ssn
        return

    current_widget_value = st.session_state.get(SSN_WIDGET_KEY, "")
    if normalize_digits(current_widget_value) == normalize_digits(stored_ssn):
        st.session_state[SSN_WIDGET_KEY] = formatted_stored_ssn


def _normalize_state_input(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""

    upper_cleaned = cleaned.upper()
    if upper_cleaned in US_STATES:
        return upper_cleaned

    return STATE_NAME_TO_CODE.get(cleaned.lower(), "")


def render_personal_information_page() -> None:
    _ensure_ssn_widget_state()
    company = get_active_company_profile()

    st.subheader("Personal Information")

    col1, col2 = st.columns(2)
    with col1:
        first_name = st.text_input("First Name *", value=st.session_state.form_data.get("first_name", ""))
        middle_name = st.text_input("Middle Name", value=st.session_state.form_data.get("middle_name", ""))
        last_name = st.text_input("Last Name *", value=st.session_state.form_data.get("last_name", ""))
        dob = st.date_input(
            "Date of Birth *",
            value=st.session_state.form_data.get("dob", date(1990, 1, 1)),
            min_value=date(1940, 1, 1),
            max_value=date(2008, 1, 1),
        )
        ssn_display = st.text_input(
            "Social Security Number *",
            key=SSN_WIDGET_KEY,
            max_chars=11,
            placeholder="000-00-0000",
            help="Enter 9 digits. The field will format the dashes for you.",
            on_change=_format_ssn_input,
        )

    with col2:
        address = st.text_input("Current Address *", value=st.session_state.form_data.get("address", ""))
        city = st.text_input("City *", value=st.session_state.form_data.get("city", ""))
        state_input = st.text_input(
            "State *",
            value=st.session_state.form_data.get("state", ""),
            help="Enter the 2-letter abbreviation or full state name.",
        )
        zip_code = st.text_input("Zip Code *", value=st.session_state.form_data.get("zip_code", ""))
        country = st.text_input("Country", value=st.session_state.form_data.get("country", "United States"))

    st.markdown("---")

    col3, col4 = st.columns(2)
    with col3:
        primary_phone = st.text_input("Primary Phone *", value=st.session_state.form_data.get("primary_phone", ""))
        email = st.text_input("Email Address *", value=st.session_state.form_data.get("email", ""))
        preferred_contact = selectbox_with_placeholder(
            "Preferred Method of Contact",
            ["Cell Phone", "Email", "Either"],
            current_value=st.session_state.form_data.get("preferred_contact"),
        )
    with col4:
        cell_phone = st.text_input(
            "Cell Phone / Text Number",
            value=st.session_state.form_data.get("cell_phone", ""),
            help="If you want text updates sent to a different number than your primary phone, enter it here.",
        )
        mobile_carrier = selectbox_with_placeholder(
            "Mobile Carrier / Provider",
            MOBILE_CARRIERS,
            current_value=st.session_state.form_data.get("mobile_carrier"),
            help="Used only if the company contacts drivers through a carrier email-to-text workflow.",
        )
        mobile_carrier_other = ""
        if mobile_carrier == "Other":
            mobile_carrier_other = st.text_input(
                "Other Mobile Carrier / Provider",
                value=st.session_state.form_data.get("mobile_carrier_other", ""),
            )
        best_time = st.text_input("Best Time to Contact You", value=st.session_state.form_data.get("best_time", "Any"))
        resided_3_years = selectbox_with_placeholder(
            "Have you resided at your current address for 3+ years?",
            ["Yes", "No"],
            current_value=st.session_state.form_data.get("resided_3_years"),
        )

    prev_address = prev_city = prev_state = prev_zip = ""
    if resided_3_years == "No":
        st.markdown("**Previous Address (if less than 3 years at current):**")
        pcol1, pcol2 = st.columns(2)
        with pcol1:
            prev_address = st.text_input("Previous Address", value=st.session_state.form_data.get("prev_address", ""))
            prev_city = st.text_input("Previous City", value=st.session_state.form_data.get("prev_city", ""))
        with pcol2:
            prev_state = selectbox_with_placeholder(
                "Previous State",
                US_STATES,
                current_value=st.session_state.form_data.get("prev_state"),
                key="prev_state_sel",
            )
            prev_zip = st.text_input("Previous Zip", value=st.session_state.form_data.get("prev_zip", ""))

    st.markdown("---")

    emergency_name = st.text_input("Emergency Contact Name *", value=st.session_state.form_data.get("emergency_name", ""))
    ecol1, ecol2 = st.columns(2)
    with ecol1:
        emergency_phone = st.text_input(
            "Emergency Contact Phone *",
            value=st.session_state.form_data.get("emergency_phone", ""),
        )
    with ecol2:
        emergency_relationship = st.text_input(
            "Relationship",
            value=st.session_state.form_data.get("emergency_relationship", ""),
        )

    st.markdown("---")
    text_consent = st.checkbox(
        f"I consent to receive text messages from {company.name} "
        "regarding my application and contracting status. I may opt out at any time by texting STOP.",
        value=st.session_state.form_data.get("text_consent", False),
    )

    if st.button("Next →", key="p1_next", use_container_width=True, type="primary"):
        ssn_digits = normalize_digits(ssn_display)
        state = _normalize_state_input(state_input)
        missing: list[str] = []
        if not first_name:
            missing.append("First Name")
        if not last_name:
            missing.append("Last Name")
        if not ssn_digits:
            missing.append("Social Security Number")
        if not address:
            missing.append("Current Address")
        if not city:
            missing.append("City")
        if not state:
            missing.append("State")
        if not zip_code:
            missing.append("Zip Code")
        if not primary_phone:
            missing.append("Primary Phone")
        if not email:
            missing.append("Email Address")
        if not emergency_name:
            missing.append("Emergency Contact Name")
        if not emergency_phone:
            missing.append("Emergency Contact Phone")

        if missing:
            show_missing_fields(missing, "The following required fields are missing:")
            return

        if len(ssn_digits) != 9:
            show_user_error(
                "Social Security Number must contain exactly 9 digits.",
                code="validation_ssn_length",
                severity="warning",
                extra={"ssn_digits_length": len(ssn_digits)},
            )
            return

        if not state:
            show_user_error(
                "Please enter a valid U.S. state abbreviation or full state name.",
                code="validation_state_invalid",
                severity="warning",
                extra={"state_input": state_input},
            )
            return

        st.session_state.form_data.update(
            {
                "first_name": first_name,
                "middle_name": middle_name,
                "last_name": last_name,
                "dob": dob,
                "ssn": ssn_digits,
                "address": address,
                "city": city,
                "state": state,
                "zip_code": zip_code,
                "country": country,
                "primary_phone": primary_phone,
                "cell_phone": cell_phone,
                "mobile_carrier": mobile_carrier,
                "mobile_carrier_other": mobile_carrier_other if mobile_carrier == "Other" else "",
                "email": email,
                "preferred_contact": preferred_contact,
                "best_time": best_time,
                "resided_3_years": resided_3_years,
                "prev_address": prev_address,
                "prev_city": prev_city,
                "prev_state": prev_state if resided_3_years == "No" else "",
                "prev_zip": prev_zip,
                "emergency_name": emergency_name,
                "emergency_phone": emergency_phone,
                "emergency_relationship": emergency_relationship,
                "text_consent": text_consent,
            }
        )
        next_page()
        autosave_draft()
        st.rerun()
