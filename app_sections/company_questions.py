"""Page 2 renderer: company questions and driving experience."""

from __future__ import annotations

from datetime import date

import streamlit as st

from config import EQUIPMENT_TYPES, OFFICE_LOCATIONS, REFERRAL_SOURCES
from services.draft_service import autosave_draft
from state import next_page, prev_page
from ui.common import render_save_draft_button, selectbox_with_placeholder, show_missing_fields


OWNER_OPERATOR_POSITION = "Owner Operator"


def render_company_questions_page() -> None:
    st.subheader("Company Questions & Driving Experience")
    st.caption("This application is currently for owner-operators only.")

    currently_employed_value = st.session_state.form_data.get("currently_employed")
    relatives_here_value = st.session_state.form_data.get("relatives_here")
    known_other_name_value = st.session_state.form_data.get("known_other_name")

    col1, col2 = st.columns(2)
    with col1:
        st.text_input(
            "Position applying for",
            value=OWNER_OPERATOR_POSITION,
            disabled=True,
            help="This portal is for Owner Operators only.",
        )
        eligible_us = selectbox_with_placeholder(
            "Are you legally eligible to provide contracted services in the United States? *",
            ["Yes", "No"],
            current_value=st.session_state.form_data.get("eligible_us"),
        )
        read_english = selectbox_with_placeholder(
            "Do you read, write, and speak English? *",
            ["Yes", "No"],
            current_value=st.session_state.form_data.get("read_english"),
        )
        currently_employed = selectbox_with_placeholder(
            "Are you currently employed/contracted elsewhere?",
            ["Yes", "No"],
            current_value=currently_employed_value,
        )
        worked_here_before = selectbox_with_placeholder(
            "Have you ever contracted with this company before?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("worked_here_before"),
        )
    with col2:
        preferred_office = selectbox_with_placeholder(
            "Preferred office for onboarding *",
            OFFICE_LOCATIONS,
            current_value=st.session_state.form_data.get("preferred_office") or st.session_state.form_data.get("applying_location"),
        )
        twic_card = selectbox_with_placeholder(
            "Do you have a current TWIC card?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("twic_card"),
        )
        if twic_card == "Yes":
            twic_expiration = st.date_input(
                "TWIC Expiration Date",
                value=st.session_state.form_data.get("twic_expiration", date.today()),
            )
        else:
            twic_expiration = None
        hazmat_endorsement = selectbox_with_placeholder(
            "Do you have a current Hazmat (HM) endorsement?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("hazmat_endorsement"),
            help="Hazmat endorsement is checked frequently for our freight type.",
        )
        if hazmat_endorsement == "Yes":
            hazmat_expiration = st.date_input(
                "Hazmat Endorsement Expiration Date",
                value=st.session_state.form_data.get("hazmat_expiration", date.today()),
            )
        else:
            hazmat_expiration = None
        referral_source = selectbox_with_placeholder(
            "How did you hear about us?",
            REFERRAL_SOURCES,
            current_value=st.session_state.form_data.get("referral_source"),
        )

    if currently_employed == "No":
        last_employment_end = st.date_input(
            "What date did your last employment/contract end?",
            value=st.session_state.form_data.get("last_employment_end", date.today()),
        )
    else:
        last_employment_end = None

    if referral_source == "Driver Referral":
        referral_name = st.text_input("Referral Driver Name", value=st.session_state.form_data.get("referral_name", ""))
    elif referral_source == "Other":
        referral_name = st.text_input("Please explain", value=st.session_state.form_data.get("referral_name", ""))
    else:
        referral_name = ""

    if worked_here_before == "Yes":
        prev_dates = st.text_input(
            "Enter start/end dates, position, and reason for leaving",
            value=st.session_state.form_data.get("prev_dates", ""),
        )
    else:
        prev_dates = ""

    relatives_here = selectbox_with_placeholder(
        "Do you have any relatives contracted here?",
        ["No", "Yes"],
        current_value=relatives_here_value,
    )
    relatives_names = ""
    if relatives_here == "Yes":
        relatives_names = st.text_input("Names of relatives", value=st.session_state.form_data.get("relatives_names", ""))

    st.markdown("---")
    st.subheader("Owner Operator Equipment")
    ocol1, ocol2 = st.columns(2)
    with ocol1:
        equipment_description = st.text_input(
            "Equipment Description (Tractor)",
            value=st.session_state.form_data.get("equipment_description", ""),
        )
        equipment_year = st.text_input("Year", value=st.session_state.form_data.get("equipment_year", ""))
        equipment_make = st.text_input("Make", value=st.session_state.form_data.get("equipment_make", ""))
        equipment_model = st.text_input("Model", value=st.session_state.form_data.get("equipment_model", ""))
        equipment_color = st.text_input("Color", value=st.session_state.form_data.get("equipment_color", ""))
    with ocol2:
        equipment_vin = st.text_input("VIN", value=st.session_state.form_data.get("equipment_vin", ""))
        equipment_weight = st.text_input("Weight", value=st.session_state.form_data.get("equipment_weight", ""))
        equipment_mileage = st.text_input("Mileage", value=st.session_state.form_data.get("equipment_mileage", ""))
        fifth_wheel_height = st.text_input(
            "Fifth Wheel Height",
            value=st.session_state.form_data.get("fifth_wheel_height", ""),
        )

    st.markdown("---")
    st.subheader("Driving Experience")
    st.markdown(
        "For each class of equipment, enter type of equipment, start and end dates, "
        "and approximate number of total miles. If no experience in a class, enter NONE."
    )

    experience_data: dict[str, dict[str, str]] = {}
    for eq_type in EQUIPMENT_TYPES:
        with st.expander(eq_type):
            ecol1, ecol2, ecol3 = st.columns(3)
            key_prefix = f"exp_{eq_type.lower().replace(' ', '_').replace('-', '_')}"
            with ecol1:
                exp_type = st.text_input(
                    "Equipment Detail",
                    key=f"{key_prefix}_type",
                    value=st.session_state.form_data.get(f"{key_prefix}_type", ""),
                )
            with ecol2:
                exp_miles = st.text_input(
                    "Total Miles",
                    key=f"{key_prefix}_miles",
                    value=st.session_state.form_data.get(f"{key_prefix}_miles", ""),
                )
            with ecol3:
                exp_dates = st.text_input(
                    "Date Range",
                    key=f"{key_prefix}_dates",
                    value=st.session_state.form_data.get(f"{key_prefix}_dates", ""),
                )
            experience_data[key_prefix] = {"type": exp_type, "miles": exp_miles, "dates": exp_dates}

    st.markdown("---")
    st.subheader("Additional Driving Info")
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        safe_driving_awards = st.text_input(
            "Safe driving awards held (and from whom)?",
            value=st.session_state.form_data.get("safe_driving_awards", ""),
        )
    with dcol2:
        known_other_name = selectbox_with_placeholder(
            "Have you ever been known by another name?",
            ["No", "Yes"],
            current_value=known_other_name_value,
        )
        other_name = ""
        if known_other_name == "Yes":
            other_name = st.text_input("Enter other name(s)", value=st.session_state.form_data.get("other_name", ""))

    bcol1, bcol2, bcol3 = st.columns(3)
    with bcol1:
        if st.button("← Back", key="p2_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        render_save_draft_button("p2_save_draft")
    with bcol3:
        if st.button("Next →", key="p2_next", use_container_width=True, type="primary"):
            missing: list[str] = []
            if not preferred_office:
                missing.append("Preferred office for onboarding")
            if not eligible_us:
                missing.append("Legal eligibility question")
            if not read_english:
                missing.append("English proficiency question")

            if missing:
                show_missing_fields(missing, "Please complete the required company questions:")
                return

            st.session_state.form_data.update(
                {
                    "position": OWNER_OPERATOR_POSITION,
                    "eligible_us": eligible_us,
                    "read_english": read_english,
                    "currently_employed": currently_employed,
                    "last_employment_end": last_employment_end,
                    "worked_here_before": worked_here_before,
                    "preferred_office": preferred_office,
                    "applying_location": preferred_office,
                    "twic_card": twic_card,
                    "twic_expiration": twic_expiration,
                    "hazmat_endorsement": hazmat_endorsement,
                    "hazmat_expiration": hazmat_expiration,
                    "referral_source": referral_source,
                    "referral_name": referral_name,
                    "prev_dates": prev_dates,
                    "relatives_here": relatives_here,
                    "relatives_names": relatives_names,
                    "equipment_description": equipment_description,
                    "equipment_year": equipment_year,
                    "equipment_make": equipment_make,
                    "equipment_model": equipment_model,
                    "equipment_color": equipment_color,
                    "equipment_vin": equipment_vin,
                    "equipment_weight": equipment_weight,
                    "equipment_mileage": equipment_mileage,
                    "fifth_wheel_height": fifth_wheel_height,
                    "safe_driving_awards": safe_driving_awards,
                    "known_other_name": known_other_name,
                    "other_name": other_name,
                }
            )
            st.session_state.form_data.pop("email_marketing_opt_in", None)
            for key_prefix, values in experience_data.items():
                st.session_state.form_data[f"{key_prefix}_type"] = values["type"]
                st.session_state.form_data[f"{key_prefix}_miles"] = values["miles"]
                st.session_state.form_data[f"{key_prefix}_dates"] = values["dates"]
            next_page()
            autosave_draft()
            st.rerun()
