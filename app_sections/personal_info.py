"""Page 1 renderer: personal information."""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from config import MOBILE_CARRIERS, US_STATES
from runtime_context import get_active_company_profile
from services.draft_service import autosave_draft
from state import next_page
from ui.common import (
    clear_missing_fields,
    mark_missing,
    record_missing_fields,
    render_eeo_notice,
    selectbox_with_placeholder,
    show_user_error,
)
from utils.formatting import normalize_digits


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


def _ensure_ssn_widget_state() -> None:
    stored_ssn = st.session_state.form_data.get("ssn", "")

    if SSN_WIDGET_KEY not in st.session_state:
        st.session_state[SSN_WIDGET_KEY] = stored_ssn
        return

    current_widget_value = st.session_state.get(SSN_WIDGET_KEY, "")
    if normalize_digits(current_widget_value) == normalize_digits(stored_ssn):
        st.session_state[SSN_WIDGET_KEY] = stored_ssn


def _normalize_state_input(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""

    upper_cleaned = cleaned.upper()
    if upper_cleaned in US_STATES:
        return upper_cleaned

    return STATE_NAME_TO_CODE.get(cleaned.lower(), "")


def _coerce_date(value: object, default: date) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return default
    return default


def _existing_previous_addresses() -> list[dict[str, object]]:
    stored = st.session_state.form_data.get("previous_addresses")
    if isinstance(stored, list) and stored:
        entries: list[dict[str, object]] = []
        for entry in stored:
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    "address": str(entry.get("address", "") or ""),
                    "city": str(entry.get("city", "") or ""),
                    "state": str(entry.get("state", "") or ""),
                    "zip_code": str(entry.get("zip_code", "") or ""),
                    "from_date": _coerce_date(entry.get("from_date"), date.today()),
                    "to_date": _coerce_date(entry.get("to_date"), date.today()),
                }
            )
        if entries:
            return entries

    legacy = {
        "address": str(st.session_state.form_data.get("prev_address", "") or ""),
        "city": str(st.session_state.form_data.get("prev_city", "") or ""),
        "state": str(st.session_state.form_data.get("prev_state", "") or ""),
        "zip_code": str(st.session_state.form_data.get("prev_zip", "") or ""),
        "from_date": date.today(),
        "to_date": date.today(),
    }
    if any([legacy["address"], legacy["city"], legacy["state"], legacy["zip_code"]]):
        return [legacy]
    return []


def render_personal_information_page() -> None:
    _ensure_ssn_widget_state()
    company = get_active_company_profile()

    render_eeo_notice()
    st.subheader("Personal Information")

    col1, col2 = st.columns(2)
    with col1:
        mark_missing("first_name")
        first_name = st.text_input("First Name *", value=st.session_state.form_data.get("first_name", ""))
        middle_name = st.text_input("Middle Name", value=st.session_state.form_data.get("middle_name", ""))
        mark_missing("last_name")
        last_name = st.text_input("Last Name *", value=st.session_state.form_data.get("last_name", ""))
        dob = st.date_input(
            "Date of Birth *",
            value=_coerce_date(st.session_state.form_data.get("dob"), date(1990, 1, 1)),
            min_value=date(1940, 1, 1),
            max_value=date(2008, 1, 1),
        )
        mark_missing("ssn")
        ssn_display = st.text_input(
            "Social Security Number *",
            key=SSN_WIDGET_KEY,
            placeholder="123-45-6789 or 123456789",
            help="Enter the SSN with or without dashes. We'll normalize it for you.",
        )

    with col2:
        mark_missing("address")
        address = st.text_input("Current Address *", value=st.session_state.form_data.get("address", ""))
        mark_missing("city")
        city = st.text_input("City *", value=st.session_state.form_data.get("city", ""))
        mark_missing("state")
        state_input = st.text_input(
            "State *",
            value=st.session_state.form_data.get("state", ""),
            help="Enter the 2-letter abbreviation or full state name.",
        )
        mark_missing("zip_code")
        zip_code = st.text_input("Zip Code *", value=st.session_state.form_data.get("zip_code", ""))
        country = st.text_input("Country", value=st.session_state.form_data.get("country", "United States"))

    st.markdown("---")

    col3, col4 = st.columns(2)
    with col3:
        mark_missing("primary_phone")
        primary_phone = st.text_input("Primary Phone *", value=st.session_state.form_data.get("primary_phone", ""))
        mark_missing("email")
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

    previous_addresses: list[dict[str, object]] = []
    if resided_3_years == "No":
        existing_previous_addresses = _existing_previous_addresses()
        previous_address_count = st.number_input(
            "How many previous addresses should we capture for the last 3 years?",
            min_value=1,
            max_value=5,
            value=max(1, len(existing_previous_addresses) or 1),
            help="Add enough prior addresses to cover the full 3-year residence history.",
        )
        for index in range(int(previous_address_count)):
            existing = existing_previous_addresses[index] if index < len(existing_previous_addresses) else {}
            with st.expander(f"Previous Address #{index + 1}", expanded=(index == 0)):
                pcol1, pcol2 = st.columns(2)
                with pcol1:
                    prev_address = st.text_input(
                        "Address",
                        key=f"prev_address_{index}",
                        value=str(existing.get("address", "") or ""),
                    )
                    prev_city = st.text_input(
                        "City",
                        key=f"prev_city_{index}",
                        value=str(existing.get("city", "") or ""),
                    )
                    prev_state = selectbox_with_placeholder(
                        "State",
                        US_STATES,
                        current_value=str(existing.get("state", "") or ""),
                        key=f"prev_state_{index}",
                    )
                with pcol2:
                    prev_zip = st.text_input(
                        "Zip Code",
                        key=f"prev_zip_{index}",
                        value=str(existing.get("zip_code", "") or ""),
                    )
                    prev_from_date = st.date_input(
                        "Dates Lived There — From",
                        key=f"prev_from_{index}",
                        value=_coerce_date(existing.get("from_date"), date.today()),
                    )
                    prev_to_date = st.date_input(
                        "Dates Lived There — To",
                        key=f"prev_to_{index}",
                        value=_coerce_date(existing.get("to_date"), date.today()),
                    )
                previous_addresses.append(
                    {
                        "address": prev_address,
                        "city": prev_city,
                        "state": prev_state,
                        "zip_code": prev_zip,
                        "from_date": prev_from_date,
                        "to_date": prev_to_date,
                    }
                )

    st.markdown("---")

    emergency_name = st.text_input("Emergency Contact Name", value=st.session_state.form_data.get("emergency_name", ""))
    ecol1, ecol2 = st.columns(2)
    with ecol1:
        emergency_phone = st.text_input(
            "Emergency Contact Phone",
            value=st.session_state.form_data.get("emergency_phone", ""),
        )
    with ecol2:
        emergency_relationship = st.text_input(
            "Relationship",
            value=st.session_state.form_data.get("emergency_relationship", ""),
        )
    emergency_address = st.text_input(
        "Emergency Contact Address",
        value=st.session_state.form_data.get("emergency_address", ""),
    )

    st.markdown("---")
    text_consent = st.checkbox(
        f"I agree to receive text messages from {company.name} that may be sent using an automatic telephone dialing system "
        "and may include recruiting or advertising messages related to my application, contracting status, or future opportunities. "
        "Consent is not a condition of being hired, contracted, or leased on. Message and data rates may apply. "
        "Reply STOP at any time to opt out.",
        value=st.session_state.form_data.get("text_consent", False),
    )

    if st.button("Next →", key="p1_next", use_container_width=True, type="primary"):
        ssn_digits = normalize_digits(ssn_display)
        state = _normalize_state_input(state_input)
        missing: list[tuple[str, str]] = []
        if not first_name:
            missing.append(("first_name", "First Name"))
        if not last_name:
            missing.append(("last_name", "Last Name"))
        if not ssn_digits:
            missing.append(("ssn", "Social Security Number"))
        if not address:
            missing.append(("address", "Current Address"))
        if not city:
            missing.append(("city", "City"))
        if not state and not str(state_input or "").strip():
            missing.append(("state", "State"))
        if not zip_code:
            missing.append(("zip_code", "Zip Code"))
        if not primary_phone:
            missing.append(("primary_phone", "Primary Phone"))
        if not email:
            missing.append(("email", "Email Address"))
        if resided_3_years == "No":
            for index, entry in enumerate(previous_addresses, start=1):
                if not entry.get("address"):
                    missing.append(("address", f"Previous Address #{index} address"))
                if not entry.get("city"):
                    missing.append(("city", f"Previous Address #{index} city"))
                if not entry.get("state"):
                    missing.append(("state", f"Previous Address #{index} state"))
                if not entry.get("zip_code"):
                    missing.append(("zip_code", f"Previous Address #{index} zip code"))

        if missing:
            record_missing_fields(missing, "The following required fields are missing:")
            st.rerun()
            return

        clear_missing_fields()

        if len(ssn_digits) != 9:
            show_user_error(
                "Social Security Number must contain exactly 9 digits after removing spaces or dashes.",
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

        legacy_previous = previous_addresses[0] if previous_addresses else {}
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
                "previous_addresses": previous_addresses if resided_3_years == "No" else [],
                "prev_address": str(legacy_previous.get("address", "") or "") if resided_3_years == "No" else "",
                "prev_city": str(legacy_previous.get("city", "") or "") if resided_3_years == "No" else "",
                "prev_state": str(legacy_previous.get("state", "") or "") if resided_3_years == "No" else "",
                "prev_zip": str(legacy_previous.get("zip_code", "") or "") if resided_3_years == "No" else "",
                "emergency_name": emergency_name,
                "emergency_phone": emergency_phone,
                "emergency_relationship": emergency_relationship,
                "emergency_address": emergency_address,
                "text_consent": text_consent,
            }
        )
        next_page()
        autosave_draft()
        st.rerun()
