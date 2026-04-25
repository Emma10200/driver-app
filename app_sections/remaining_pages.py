"""Renderers for pages 3 through 11 of the application flow."""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from config import (
    AREAS_DRIVEN_OPTIONS,
    LICENSE_COUNTRIES,
    LICENSE_CLASSES,
    TRAILER_LENGTHS,
    TRAILER_TYPES,
    TRUCK_TYPES,
    US_STATES,
)
from runtime_context import get_active_company_profile
from services.draft_service import autosave_draft
from state import next_page, prev_page
from ui.common import default_california_applicability, render_save_draft_button, selectbox_with_placeholder, show_missing_fields, show_user_error


def _blank_reference() -> dict[str, str]:
    return {"name": "", "phone": "", "relationship": "", "city": "", "state": ""}


def _coerce_date(value: object, default: date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return default
    return default


def _coerce_reference_entries() -> list[dict[str, str]]:
    stored = st.session_state.form_data.get("references")
    entries: list[dict[str, str]] = []
    if isinstance(stored, list):
        for entry in stored:
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    "name": str(entry.get("name", "") or ""),
                    "phone": str(entry.get("phone", "") or ""),
                    "relationship": str(entry.get("relationship", "") or ""),
                    "city": str(entry.get("city", "") or ""),
                    "state": str(entry.get("state", "") or ""),
                }
            )
    if entries:
        while len(entries) < 2:
            entries.append(_blank_reference())
        return entries[:2]

    def parse_legacy(raw: str) -> dict[str, str]:
        text = str(raw or "").strip()
        if not text:
            return _blank_reference()
        if "|" in text:
            parts = [part.strip() for part in text.split("|")]
            while len(parts) < 3:
                parts.append("")
            return {
                "name": parts[0],
                "phone": parts[1],
                "relationship": parts[2],
                "city": "",
                "state": "",
            }
        parts = [part.strip() for part in text.split(",")]
        while len(parts) < 5:
            parts.append("")
        return {
            "name": parts[0],
            "city": parts[1],
            "state": parts[2],
            "phone": parts[3],
            "relationship": parts[4],
        }

    entries = [
        parse_legacy(str(st.session_state.form_data.get("ref1", "") or "")),
        parse_legacy(str(st.session_state.form_data.get("ref2", "") or "")),
    ]
    while len(entries) < 2:
        entries.append(_blank_reference())
    return entries[:2]


def _format_reference_summary(reference: dict[str, str]) -> str:
    city_state = ", ".join(part for part in [reference.get("city", ""), reference.get("state", "")] if part)
    parts = [reference.get("name", ""), city_state, reference.get("phone", ""), reference.get("relationship", "")]
    return ", ".join(part for part in parts if part)


def render_remaining_page(page: int) -> bool:
    company = get_active_company_profile()

    if page == 3:
        st.subheader("Licenses & Endorsements")
        st.markdown("List all driver's licenses held in the past 3 years and current endorsements.")

        st.markdown("#### Additional Credentials")
        cred_col1, cred_col2 = st.columns(2)
        with cred_col1:
            twic_card = selectbox_with_placeholder(
                "Do you have a current TWIC card?",
                ["No", "Yes"],
                current_value=st.session_state.form_data.get("twic_card"),
                key="twic_card_page3",
                help="TWIC is a transportation security credential rather than a CDL endorsement, but it belongs with your license credentials.",
            )
        with cred_col2:
            if twic_card == "Yes":
                twic_expiration = st.date_input(
                    "TWIC Expiration Date",
                    value=_coerce_date(st.session_state.form_data.get("twic_expiration"), date.today()),
                    key="twic_expiration_page3",
                )
            else:
                twic_expiration = None

        st.markdown("---")

        num_licenses = st.number_input(
            "How many licenses do you want to add?",
            min_value=1,
            max_value=5,
            value=max(1, len(st.session_state.licenses) if st.session_state.licenses else 1),
        )

        licenses_input = []
        for i in range(int(num_licenses)):
            st.markdown(f"**License #{i+1}**")
            existing = st.session_state.licenses[i] if i < len(st.session_state.licenses) else {}
            lcol1, lcol2, lcol3 = st.columns(3)
            with lcol1:
                lic_number = st.text_input("License Number", key=f"lic_num_{i}", value=existing.get("number", ""))
                lic_state = selectbox_with_placeholder(
                    "Licensing State",
                    US_STATES,
                    current_value=existing.get("state"),
                    key=f"lic_state_{i}",
                )
            with lcol2:
                lic_country = selectbox_with_placeholder(
                    "Country",
                    LICENSE_COUNTRIES,
                    current_value=existing.get("country"),
                    key=f"lic_country_{i}",
                )
                lic_class = selectbox_with_placeholder(
                    "License Class",
                    LICENSE_CLASSES,
                    current_value=existing.get("class"),
                    key=f"lic_class_{i}",
                )
                current_license = selectbox_with_placeholder(
                    "Current License?",
                    ["Yes", "No"],
                    current_value=existing.get("current_license"),
                    key=f"lic_current_{i}",
                )
            with lcol3:
                lic_expiration = st.date_input(
                    "License Expiration",
                    key=f"lic_exp_{i}",
                    value=_coerce_date(existing.get("expiration"), date.today()),
                )
                med_card_exp = st.date_input(
                    "DOT Medical Card Expiration",
                    key=f"med_exp_{i}",
                    value=_coerce_date(existing.get("med_card_exp"), date.today()),
                )
                is_cdl = selectbox_with_placeholder(
                    "Commercial Driver License?",
                    ["Yes", "No"],
                    current_value=existing.get("is_cdl"),
                    key=f"is_cdl_{i}",
                )

            ecol1, ecol2, ecol3 = st.columns(3)
            with ecol1:
                tanker_end = selectbox_with_placeholder(
                    "Tanker Endorsement?",
                    ["No", "Yes"],
                    current_value=existing.get("tanker"),
                    key=f"tanker_{i}",
                )
                hazmat_end = selectbox_with_placeholder(
                    "HAZMAT Endorsement?",
                    ["No", "Yes"],
                    current_value=existing.get("hazmat"),
                    key=f"hazmat_{i}",
                )
                hazmat_exp = (
                    st.date_input(
                        "HAZMAT Expiration Date",
                        key=f"hazmat_exp_{i}",
                        value=_coerce_date(existing.get("hazmat_exp"), date.today()),
                    )
                    if hazmat_end == "Yes"
                    else None
                )
            with ecol2:
                doubles_end = selectbox_with_placeholder(
                    "Doubles/Triples Endorsement?",
                    ["No", "Yes"],
                    current_value=existing.get("doubles"),
                    key=f"doubles_{i}",
                )
                x_end = selectbox_with_placeholder(
                    "X Endorsement?",
                    ["No", "Yes"],
                    current_value=existing.get("x_endorsement"),
                    key=f"x_end_{i}",
                )
            with ecol3:
                other_endorsement = st.text_input(
                    "Other Endorsement (if any)",
                    key=f"other_end_{i}",
                    value=existing.get("other_endorsement", ""),
                )

            licenses_input.append(
                {
                    "number": lic_number,
                    "state": lic_state,
                    "country": lic_country,
                    "class": lic_class,
                    "current_license": current_license,
                    "expiration": lic_expiration,
                    "med_card_exp": med_card_exp,
                    "is_cdl": is_cdl,
                    "tanker": tanker_end,
                    "hazmat": hazmat_end,
                    "hazmat_exp": hazmat_exp,
                    "doubles": doubles_end,
                    "x_endorsement": x_end,
                    "other_endorsement": other_endorsement,
                }
            )
            st.markdown("---")

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p3_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p3_save_draft")
        with bcol3:
            if st.button("Next →", key="p3_next", use_container_width=True, type="primary"):
                missing: list[str] = []
                for index, license_entry in enumerate(licenses_input, start=1):
                    if not license_entry["number"]:
                        missing.append(f"License #{index} number")
                    if not license_entry["state"]:
                        missing.append(f"License #{index} state")
                    if not license_entry["country"]:
                        missing.append(f"License #{index} country")
                    if not license_entry["class"]:
                        missing.append(f"License #{index} class")
                    if not license_entry["current_license"]:
                        missing.append(f"License #{index} current license question")
                    if not license_entry["is_cdl"]:
                        missing.append(f"License #{index} CDL question")
                    if not license_entry["tanker"]:
                        missing.append(f"License #{index} tanker endorsement question")
                    if not license_entry["hazmat"]:
                        missing.append(f"License #{index} HAZMAT endorsement question")
                    if not license_entry["doubles"]:
                        missing.append(f"License #{index} doubles/triples endorsement question")
                    if not license_entry["x_endorsement"]:
                        missing.append(f"License #{index} X endorsement question")

                if missing:
                    show_missing_fields(missing, "Please complete the required license details:")
                    return

                st.session_state.licenses = licenses_input
                st.session_state.form_data["twic_card"] = twic_card
                st.session_state.form_data["twic_expiration"] = twic_expiration
                st.session_state.form_data["hazmat_endorsement"] = "Yes" if any(
                    license_entry["hazmat"] == "Yes" for license_entry in licenses_input
                ) else "No"
                hazmat_expirations = [license_entry["hazmat_exp"] for license_entry in licenses_input if license_entry["hazmat_exp"]]
                st.session_state.form_data["hazmat_expiration"] = hazmat_expirations[0] if hazmat_expirations else None
                next_page()
                autosave_draft()
                st.rerun()
        return True

    if page == 4:
        st.subheader("Employment / Contracting History — Past 10 Years")
        st.markdown(
            "Federal regulations require a complete 10-year work history. "
            "Please list every employer or contractor you've worked with in the last 10 years, "
            "starting with the most recent. Be sure to account for any gaps."
        )

        num_employers = st.number_input(
            "How many employers/contractors would you like to add?",
            min_value=1,
            max_value=25,
            value=max(1, len(st.session_state.employers) if st.session_state.employers else 1),
            help="You can add more entries if needed.",
        )

        employers_input = []
        for i in range(int(num_employers)):
            existing = st.session_state.employers[i] if i < len(st.session_state.employers) else {}
            with st.expander(f"Employer / Contractor #{i+1}", expanded=(i == 0)):
                ecol1, ecol2 = st.columns(2)
                with ecol1:
                    emp_company = st.text_input("Company Name *", key=f"emp_company_{i}", value=existing.get("company", ""))
                    emp_address = st.text_input("Address", key=f"emp_addr_{i}", value=existing.get("address", ""))
                    emp_city_state = st.text_input("City, State, Zip", key=f"emp_csz_{i}", value=existing.get("city_state", ""))
                    emp_country = st.text_input("Country", key=f"emp_country_{i}", value=existing.get("country", "United States"))
                    emp_phone = st.text_input("Phone", key=f"emp_phone_{i}", value=existing.get("phone", ""))
                    emp_position = st.text_input("Position Held", key=f"emp_pos_{i}", value=existing.get("position", ""))
                with ecol2:
                    emp_start = st.date_input("Start Date", key=f"emp_start_{i}", value=_coerce_date(existing.get("start"), date(2020, 1, 1)))
                    emp_end = st.date_input("End Date", key=f"emp_end_{i}", value=_coerce_date(existing.get("end"), date.today()))
                    emp_reason = st.text_input("Reason for Leaving", key=f"emp_reason_{i}", value=existing.get("reason", ""))
                    emp_pay_range = st.text_input(
                        "Pay Range (cents/mile or $/hr)",
                        key=f"emp_pay_{i}",
                        value=existing.get("pay_range", ""),
                    )
                    emp_terminated = selectbox_with_placeholder(
                        "Were you terminated/discharged/laid off?",
                        ["No", "Yes"],
                        current_value=existing.get("terminated"),
                        key=f"emp_term_{i}",
                    )
                    emp_current = selectbox_with_placeholder(
                        "Is this your current contractor/employer?",
                        ["No", "Yes"],
                        current_value=existing.get("current"),
                        key=f"emp_current_{i}",
                    )

                ecol3, ecol4 = st.columns(2)
                with ecol3:
                    emp_contact_ok = selectbox_with_placeholder(
                        "May we contact this company?",
                        ["Yes", "No"],
                        current_value=existing.get("contact_ok"),
                        key=f"emp_contact_{i}",
                    )
                    emp_cmv = selectbox_with_placeholder(
                        "Did you operate a commercial motor vehicle?",
                        ["Yes", "No"],
                        current_value=existing.get("cmv"),
                        key=f"emp_cmv_{i}",
                    )
                with ecol4:
                    emp_fmcsa = selectbox_with_placeholder(
                        "Subject to FMCSA/Transport Canada regulations?",
                        ["Yes", "No"],
                        current_value=existing.get("fmcsa"),
                        key=f"emp_fmcsa_{i}",
                    )
                    emp_dot_testing = selectbox_with_placeholder(
                        "Subject to DOT drug and alcohol testing?",
                        ["Yes", "No"],
                        current_value=existing.get("dot_testing"),
                        key=f"emp_dot_{i}",
                    )

                if emp_cmv == "Yes":
                    mcol1, mcol2, mcol3 = st.columns(3)
                    with mcol1:
                        default_areas = existing.get("areas_type") or (
                            existing.get("areas") if existing.get("areas") in AREAS_DRIVEN_OPTIONS else None
                        )
                        emp_areas_type = selectbox_with_placeholder(
                            "Areas Driven",
                            AREAS_DRIVEN_OPTIONS,
                            current_value=default_areas,
                            key=f"emp_areas_type_{i}",
                        )
                        emp_areas_other = ""
                        if emp_areas_type == "Other":
                            emp_areas_other = st.text_input(
                                "Other Areas Driven",
                                key=f"emp_areas_other_{i}",
                                value=existing.get("areas_other", existing.get("areas", "")),
                            )
                        emp_miles = st.text_input("Miles Driven Weekly", key=f"emp_miles_{i}", value=existing.get("miles", ""))
                    with mcol2:
                        default_truck = existing.get("truck_type") or (
                            existing.get("truck") if existing.get("truck") in TRUCK_TYPES else None
                        )
                        emp_truck_type = selectbox_with_placeholder(
                            "Most Common Truck Driven",
                            TRUCK_TYPES,
                            current_value=default_truck,
                            key=f"emp_truck_type_{i}",
                        )
                        emp_truck_other = ""
                        if emp_truck_type == "Other":
                            emp_truck_other = st.text_input(
                                "Other Truck Type",
                                key=f"emp_truck_other_{i}",
                                value=existing.get("truck_other", existing.get("truck", "")),
                            )
                        emp_trailer = selectbox_with_placeholder(
                            "Most Common Trailer",
                            TRAILER_TYPES,
                            current_value=existing.get("trailer"),
                            key=f"emp_trailer_{i}",
                        )
                    with mcol3:
                        emp_trailer_len = selectbox_with_placeholder(
                            "Trailer Length",
                            TRAILER_LENGTHS,
                            current_value=existing.get("trailer_len"),
                            key=f"emp_tlen_{i}",
                        )
                else:
                    emp_areas_type = emp_areas_other = emp_miles = emp_truck_type = emp_truck_other = emp_trailer = emp_trailer_len = ""

                emp_areas = emp_areas_other if emp_areas_type == "Other" else emp_areas_type
                emp_truck = emp_truck_other if emp_truck_type == "Other" else emp_truck_type

                employers_input.append(
                    {
                        "company": emp_company,
                        "address": emp_address,
                        "city_state": emp_city_state,
                        "country": emp_country,
                        "phone": emp_phone,
                        "position": emp_position,
                        "start": emp_start,
                        "end": emp_end,
                        "reason": emp_reason,
                        "pay_range": emp_pay_range,
                        "terminated": emp_terminated,
                        "current": emp_current,
                        "contact_ok": emp_contact_ok,
                        "cmv": emp_cmv,
                        "fmcsa": emp_fmcsa,
                        "dot_testing": emp_dot_testing,
                        "areas_type": emp_areas_type,
                        "areas_other": emp_areas_other,
                        "areas": emp_areas,
                        "miles": emp_miles,
                        "truck_type": emp_truck_type,
                        "truck_other": emp_truck_other,
                        "truck": emp_truck,
                        "trailer": emp_trailer,
                        "trailer_len": emp_trailer_len,
                    }
                )

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p4_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p4_save_draft")
        with bcol3:
            if st.button("Next →", key="p4_next", use_container_width=True, type="primary"):
                missing: list[str] = []
                for index, employer in enumerate(employers_input, start=1):
                    if not employer["company"]:
                        missing.append(f"Employer #{index} company name")
                    if not employer["terminated"]:
                        missing.append(f"Employer #{index} termination question")
                    if not employer["current"]:
                        missing.append(f"Employer #{index} current employer question")
                    if not employer["contact_ok"]:
                        missing.append(f"Employer #{index} contact permission question")
                    if not employer["cmv"]:
                        missing.append(f"Employer #{index} CMV question")
                    if not employer["fmcsa"]:
                        missing.append(f"Employer #{index} FMCSA question")
                    if not employer["dot_testing"]:
                        missing.append(f"Employer #{index} DOT testing question")
                    if employer["cmv"] == "Yes" and not employer["areas_type"]:
                        missing.append(f"Employer #{index} areas driven")
                    if employer["cmv"] == "Yes" and employer["areas_type"] == "Other" and not employer["areas_other"]:
                        missing.append(f"Employer #{index} other areas driven")
                    if employer["cmv"] == "Yes" and not employer["truck_type"]:
                        missing.append(f"Employer #{index} truck type")
                    if employer["cmv"] == "Yes" and employer["truck_type"] == "Other" and not employer["truck_other"]:
                        missing.append(f"Employer #{index} other truck type")
                    if employer["cmv"] == "Yes" and not employer["trailer"]:
                        missing.append(f"Employer #{index} most common trailer")
                    if employer["cmv"] == "Yes" and not employer["trailer_len"]:
                        missing.append(f"Employer #{index} trailer length")

                if missing:
                    show_missing_fields(missing, "Please complete the required employment history fields:")
                    return

                st.session_state.employers = employers_input
                next_page()
                autosave_draft()
                st.rerun()
        return True

    if page == 5:
        st.subheader("Education & Trucking School")

        col1, col2 = st.columns(2)
        with col1:
            highest_grade = selectbox_with_placeholder(
                "Highest Grade Completed",
                ["High School", "GED", "Some College", "College - 2 Year", "College - 4 Year", "Graduate Degree"],
                current_value=st.session_state.form_data.get("highest_grade"),
            )
        with col2:
            last_school = st.text_input("Last School Attended (Name, City, State)", value=st.session_state.form_data.get("last_school", ""))

        st.markdown("---")
        st.subheader("Trucking School")
        attended_trucking_school = selectbox_with_placeholder(
            "Did you attend a trucking school?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("attended_trucking_school"),
        )

        if attended_trucking_school == "Yes":
            tcol1, tcol2 = st.columns(2)
            with tcol1:
                ts_name = st.text_input("School Name", value=st.session_state.form_data.get("ts_name", ""))
                ts_city_state = st.text_input("City, State", value=st.session_state.form_data.get("ts_city_state", ""))
                ts_start = st.date_input("Start Date", key="ts_start", value=_coerce_date(st.session_state.form_data.get("ts_start"), date(2020, 1, 1)))
            with tcol2:
                ts_end = st.date_input("End Date", key="ts_end", value=_coerce_date(st.session_state.form_data.get("ts_end"), date(2020, 6, 1)))
                ts_graduated = selectbox_with_placeholder(
                    "Did you graduate?",
                    ["Yes", "No"],
                    current_value=st.session_state.form_data.get("ts_graduated"),
                    key="ts_grad",
                )
                ts_fmcsa_subject = selectbox_with_placeholder(
                    "Were you subject to FMCSA regulations while attending?",
                    ["Yes", "No"],
                    current_value=st.session_state.form_data.get("ts_fmcsa_subject"),
                    key="ts_fmcsa",
                )
        else:
            ts_name = ts_city_state = ""
            ts_start = ts_end = None
            ts_graduated = ts_fmcsa_subject = ""

        st.markdown("---")
        st.subheader("Personal References")
        st.markdown("Personal references are optional. Add up to two if you'd like.")
        existing_references = _coerce_reference_entries()
        references: list[dict[str, str]] = []
        for index in range(2):
            existing = existing_references[index] if index < len(existing_references) else _blank_reference()
            with st.expander(f"Reference #{index + 1} (optional)", expanded=False):
                rcol1, rcol2 = st.columns(2)
                with rcol1:
                    ref_name = st.text_input(
                        "Name",
                        key=f"ref_name_{index}",
                        value=existing.get("name", ""),
                    )
                    ref_phone = st.text_input(
                        "Phone",
                        key=f"ref_phone_{index}",
                        value=existing.get("phone", ""),
                    )
                with rcol2:
                    ref_relationship = st.text_input(
                        "Relationship",
                        key=f"ref_relationship_{index}",
                        value=existing.get("relationship", ""),
                    )
                    ref_city = st.text_input(
                        "City",
                        key=f"ref_city_{index}",
                        value=existing.get("city", ""),
                    )
                    ref_state = selectbox_with_placeholder(
                        "State",
                        US_STATES,
                        current_value=existing.get("state"),
                        key=f"ref_state_{index}",
                    )
            references.append(
                {
                    "name": ref_name,
                    "phone": ref_phone,
                    "relationship": ref_relationship,
                    "city": ref_city,
                    "state": ref_state,
                }
            )

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p5_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p5_save_draft")
        with bcol3:
            if st.button("Next →", key="p5_next", use_container_width=True, type="primary"):
                missing: list[str] = []
                if not highest_grade:
                    missing.append("Highest grade completed")
                if not attended_trucking_school:
                    missing.append("Trucking school question")
                if attended_trucking_school == "Yes" and not ts_graduated:
                    missing.append("Did you graduate? question")
                if attended_trucking_school == "Yes" and not ts_fmcsa_subject:
                    missing.append("FMCSA trucking school question")

                if missing:
                    show_missing_fields(missing, "Please complete the required education fields:")
                    return

                ref1 = _format_reference_summary(references[0])
                ref2 = _format_reference_summary(references[1])

                st.session_state.form_data.update(
                    {
                        "highest_grade": highest_grade,
                        "last_school": last_school,
                        "attended_trucking_school": attended_trucking_school,
                        "ts_name": ts_name,
                        "ts_city_state": ts_city_state,
                        "ts_start": ts_start,
                        "ts_end": ts_end,
                        "ts_graduated": ts_graduated,
                        "ts_fmcsa_subject": ts_fmcsa_subject,
                        "references": references,
                        "ref1": ref1,
                        "ref2": ref2,
                    }
                )
                next_page()
                autosave_draft()
                st.rerun()
        return True

    if page == 6:
        st.subheader("Safety Record & Disqualification Questions")

        st.markdown("#### DOT Disqualification Questions")
        st.caption("These questions are required for all commercial drivers under federal regulations.")

        disq_391_15 = selectbox_with_placeholder(
            "Are you currently disqualified from driving a commercial motor vehicle?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("disq_391_15"),
            help="Per 49 CFR 391.15: a current disqualification from operating a CMV for any reason.",
        )
        disq_suspended = selectbox_with_placeholder(
            "Has your license ever been suspended or revoked?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("disq_suspended"),
            help="Has your license, permit, or privilege to drive ever been suspended or revoked for any reason?",
        )
        disq_denied = selectbox_with_placeholder(
            "Have you ever been denied a license?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("disq_denied"),
            help="Has your license, permit, or privilege to operate a motor vehicle ever been denied?",
        )
        disq_drug_test = selectbox_with_placeholder(
            "In the past 2 years, have you tested positive or refused a pre-employment DOT drug/alcohol test?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("disq_drug_test"),
            help=(
                "Within the past two years, have you tested positive, or refused to test, on a "
                "pre-employment drug or alcohol test by an employer to whom you applied for "
                "safety-sensitive transportation work?"
            ),
        )

        DISQ_CONVICTION_OPTIONS = [
            "Driving a CMV with a BAC of .04 or more",
            "Driving under the influence of alcohol (state law)",
            "Refusal to undergo drug and alcohol testing",
            "Driving a CMV under the influence of a Schedule I controlled substance",
            "Transportation/possession/unlawful use of controlled substances while driving for a motor carrier",
            "Leaving the scene of an accident while operating a CMV",
            "Any other felony involving the use of a CMV",
        ]
        disq_convicted = selectbox_with_placeholder(
            "In the past 3 years, convicted of any DOT-disqualifying offense?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("disq_convicted"),
            help=(
                "Covers: driving a CMV with BAC .04+; DUI; refusal to test; CMV under "
                "Schedule I controlled substance; transporting/possessing/using controlled "
                "substances while driving; leaving the scene of a CMV accident; any other "
                "felony involving a CMV."
            ),
        )

        disq_convicted_which: list[str] = []
        disq_convicted_details = ""
        if disq_convicted == "Yes":
            st.markdown("**Please indicate which offense(s) apply.** Select all that apply.")
            disq_convicted_which = st.multiselect(
                "Which offense(s)?",
                DISQ_CONVICTION_OPTIONS,
                default=st.session_state.form_data.get("disq_convicted_which", []),
                key="disq_convicted_which",
            )
            disq_convicted_details = st.text_area(
                "Additional details (date, state, disposition)",
                value=st.session_state.form_data.get("disq_convicted_details", ""),
                key="disq_convicted_details_input",
                help="Optional context for safety review.",
            )

        st.markdown("---")
        st.markdown("#### Motor Vehicle Record (MVR)")
        st.caption("These questions help match the standard carrier qualification file review.")

        mvr_suspension_conviction = selectbox_with_placeholder(
            "Have you ever been convicted of driving during license suspension/revocation, driving without a valid license, or are related charges pending?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("mvr_suspension_conviction"),
        )
        mvr_no_valid_license = selectbox_with_placeholder(
            "Have you ever been convicted of driving without a valid or current license, or are related charges pending?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("mvr_no_valid_license"),
        )
        mvr_alcohol_controlled_substance = selectbox_with_placeholder(
            "Have you ever been convicted of an alcohol or controlled substance offense while operating a motor vehicle, or are related charges pending?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("mvr_alcohol_controlled_substance"),
        )
        mvr_illegal_substance_on_duty = selectbox_with_placeholder(
            "Have you ever been convicted of possession, sale, or transfer of an illegal substance while on duty, or are related charges pending?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("mvr_illegal_substance_on_duty"),
        )
        mvr_reckless_driving = selectbox_with_placeholder(
            "Have you ever been convicted of reckless driving, careless driving, or careless operation of a motor vehicle, or are related charges pending?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("mvr_reckless_driving"),
        )
        mvr_any_dot_test_positive = selectbox_with_placeholder(
            "Have you ever tested positive, or refused to test, on any DOT-mandated drug or alcohol test?",
            ["No", "Yes"],
            current_value=st.session_state.form_data.get("mvr_any_dot_test_positive"),
        )

        st.markdown("---")
        st.markdown("#### Vehicle Accident Record")
        st.caption("Please list any accidents or incidents from the last 5 years, even if you were not at fault. If you've had none, just select 'No'.")

        has_accidents = selectbox_with_placeholder(
            "Any accidents to report?",
            ["No", "Yes"],
            current_value="Yes" if st.session_state.accidents else "No",
            key="has_acc",
        )
        if has_accidents == "Yes":
            num_accidents = st.number_input(
                "Number of accidents",
                min_value=1,
                max_value=10,
                value=max(1, len(st.session_state.accidents) if st.session_state.accidents else 1),
            )
            accidents_input = []
            for i in range(int(num_accidents)):
                existing = st.session_state.accidents[i] if i < len(st.session_state.accidents) else {}
                with st.expander(f"Accident #{i+1}"):
                    acol1, acol2 = st.columns(2)
                    with acol1:
                        acc_date = st.date_input("Date of Accident", key=f"acc_date_{i}", value=_coerce_date(existing.get("date"), date.today()))
                        acc_location = st.text_input("Location", key=f"acc_loc_{i}", value=existing.get("location", ""))
                        acc_fatalities = st.number_input("Fatalities", min_value=0, key=f"acc_fat_{i}", value=int(existing.get("fatalities", 0) or 0))
                    with acol2:
                        acc_injuries = st.number_input("Injuries", min_value=0, key=f"acc_inj_{i}", value=int(existing.get("injuries", 0) or 0))
                        acc_hazmat = st.radio("Hazmat Spill?", ["No", "Yes"], key=f"acc_haz_{i}", index=0 if existing.get("hazmat", "No") == "No" else 1, horizontal=True)
                        acc_description = st.text_area("Description", key=f"acc_desc_{i}", value=existing.get("description", ""))
                    accidents_input.append(
                        {
                            "date": acc_date,
                            "location": acc_location,
                            "fatalities": acc_fatalities,
                            "injuries": acc_injuries,
                            "hazmat": acc_hazmat,
                            "description": acc_description,
                        }
                    )
            st.session_state.accidents = accidents_input
        else:
            st.session_state.accidents = []

        st.markdown("---")
        st.markdown("#### Traffic Convictions / Violations")
        st.caption("Please list any moving violations or traffic convictions from the past 3 years. If you've had none, just select 'No'.")

        has_violations = selectbox_with_placeholder(
            "Any violations to report?",
            ["No", "Yes"],
            current_value="Yes" if st.session_state.violations else "No",
            key="has_viol",
        )
        if has_violations == "Yes":
            num_violations = st.number_input(
                "Number of violations",
                min_value=1,
                max_value=10,
                value=max(1, len(st.session_state.violations) if st.session_state.violations else 1),
            )
            violations_input = []
            for i in range(int(num_violations)):
                existing = st.session_state.violations[i] if i < len(st.session_state.violations) else {}
                with st.expander(f"Violation #{i+1}"):
                    vcol1, vcol2, vcol3 = st.columns(3)
                    with vcol1:
                        viol_date = st.date_input("Date", key=f"viol_date_{i}", value=_coerce_date(existing.get("date"), date.today()))
                        viol_location = st.text_input("Violation State / Province", key=f"viol_loc_{i}", value=existing.get("location", ""))
                    with vcol2:
                        viol_charge = st.text_input("Charge / Description", key=f"viol_charge_{i}", value=existing.get("charge", ""))
                        viol_in_commercial_vehicle = selectbox_with_placeholder(
                            "In Commercial Vehicle?",
                            ["Yes", "No"],
                            current_value=existing.get("in_commercial_vehicle"),
                            key=f"viol_cmv_{i}",
                        )
                    violations_input.append(
                        {}
                    )
                    with vcol3:
                        viol_fined = selectbox_with_placeholder(
                            "Fined?",
                            ["Yes", "No"],
                            current_value=existing.get("fined"),
                            key=f"viol_fined_{i}",
                        )
                        viol_fine_amount = st.text_input(
                            "Fine Amount",
                            key=f"viol_fine_amount_{i}",
                            value=existing.get("fine_amount", ""),
                        )
                    wcol1, wcol2, wcol3 = st.columns(3)
                    with wcol1:
                        viol_license_suspended = selectbox_with_placeholder(
                            "License Suspended?",
                            ["Yes", "No"],
                            current_value=existing.get("license_suspended"),
                            key=f"viol_suspended_{i}",
                        )
                    with wcol2:
                        viol_license_revoked = selectbox_with_placeholder(
                            "License Revoked?",
                            ["Yes", "No"],
                            current_value=existing.get("license_revoked"),
                            key=f"viol_revoked_{i}",
                        )
                    with wcol3:
                        viol_penalty = st.text_input(
                            "Other Penalty / Outcome",
                            key=f"viol_pen_{i}",
                            value=existing.get("penalty", ""),
                        )
                    viol_comments = st.text_area(
                        "Comments",
                        key=f"viol_comments_{i}",
                        value=existing.get("comments", ""),
                    )
                    violations_input[-1] = {
                        "date": viol_date,
                        "location": viol_location,
                        "charge": viol_charge,
                        "in_commercial_vehicle": viol_in_commercial_vehicle,
                        "fined": viol_fined,
                        "license_suspended": viol_license_suspended,
                        "license_revoked": viol_license_revoked,
                        "fine_amount": viol_fine_amount,
                        "penalty": viol_penalty or viol_fine_amount,
                        "comments": viol_comments,
                    }
            st.session_state.violations = violations_input
        else:
            st.session_state.violations = []

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p6_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p6_save_draft")
        with bcol3:
            if st.button("Next →", key="p6_next", use_container_width=True, type="primary"):
                missing = []
                if not disq_391_15:
                    missing.append("Disqualification question")
                if not disq_suspended:
                    missing.append("License suspension/revocation question")
                if not disq_denied:
                    missing.append("License denial question")
                if not disq_drug_test:
                    missing.append("Pre-employment drug/alcohol test question")
                if not disq_convicted:
                    missing.append("DOT offense conviction question")
                if disq_convicted == "Yes" and not disq_convicted_which:
                    missing.append("Which DOT offense(s) apply")
                if not mvr_suspension_conviction:
                    missing.append("MVR suspension/revocation question")
                if not mvr_no_valid_license:
                    missing.append("MVR valid license question")
                if not mvr_alcohol_controlled_substance:
                    missing.append("MVR alcohol/controlled substance question")
                if not mvr_illegal_substance_on_duty:
                    missing.append("MVR illegal substance on duty question")
                if not mvr_reckless_driving:
                    missing.append("MVR reckless/careless driving question")
                if not mvr_any_dot_test_positive:
                    missing.append("MVR any DOT drug/alcohol test question")
                if not has_accidents:
                    missing.append("Accident history question")
                if not has_violations:
                    missing.append("Violation history question")

                if missing:
                    show_missing_fields(missing, "Please complete the required safety questions:")
                    return

                st.session_state.form_data.update(
                    {
                        "disq_391_15": disq_391_15,
                        "disq_suspended": disq_suspended,
                        "disq_denied": disq_denied,
                        "disq_drug_test": disq_drug_test,
                        "disq_convicted": disq_convicted,
                        "disq_convicted_which": disq_convicted_which if disq_convicted == "Yes" else [],
                        "disq_convicted_details": disq_convicted_details if disq_convicted == "Yes" else "",
                        "mvr_suspension_conviction": mvr_suspension_conviction,
                        "mvr_no_valid_license": mvr_no_valid_license,
                        "mvr_alcohol_controlled_substance": mvr_alcohol_controlled_substance,
                        "mvr_illegal_substance_on_duty": mvr_illegal_substance_on_duty,
                        "mvr_reckless_driving": mvr_reckless_driving,
                        "mvr_any_dot_test_positive": mvr_any_dot_test_positive,
                    }
                )
                next_page()
                autosave_draft()
                st.rerun()
        return True

    if page == 7:
        st.subheader("Certifications & Signature")

        with st.expander("Drug and Alcohol Policy Certification", expanded=True):
            st.markdown(
                f"""
    I certify that I have received a copy of, and have read, the Drug and Alcohol Policy.
    I understand that as a **condition of this independent contractor agreement**, I must comply
    with these guidelines and agree that I will remain medically qualified by these procedures.
    I also acknowledge that if I become disqualified as a driver for any reason, I have
    self-terminated my contract with {company.name}.
    """
            )
            drug_alcohol_cert = st.checkbox("I certify I have read and understand the Drug and Alcohol Policy *")

        with st.expander("Applicant Certification", expanded=True):
            st.markdown(
                f"""
    I certify that all information provided in this application is true and complete to the
    best of my knowledge. I understand that any misrepresentation or omission of facts may
    result in rejection of this application or termination of my independent contractor agreement
    with {company.name}.

    I authorize {company.name} to make such investigations and inquiries of my personal,
    contracting, financial, driving, and other related matters as may be necessary in arriving
    at a contracting decision. I understand that this application is not and is not intended to
    be a contract for services.

    I understand that engagement as an independent contractor with {company.name} is based
    on mutual agreement and allows either party to terminate the contractor relationship at any
    time, with or without cause or advance notice.

    I also understand that {company.name} reserves the right to require all independent
    contractors to submit to substance abuse testing in accordance with applicable federal
    and state regulations.
    """
            )
            applicant_cert = st.checkbox("I certify that all information in this application is true and complete *")

        st.markdown("---")
        st.subheader("Digital Signature")
        sig_full_name = st.text_input("Full Legal Name (typed signature) *", value=st.session_state.form_data.get("sig_full_name", ""))
        sig_date = st.date_input("Date", value=date.today(), disabled=True)

        st.info(
            "By typing your full legal name above, you agree that this electronic signature is as "
            "legally binding as an ink signature. A submission timestamp will be recorded."
        )

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p7_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p7_save_draft")
        with bcol3:
            if st.button("Next → (FCRA Disclosure)", key="p7_next", use_container_width=True, type="primary"):
                missing = []
                if not drug_alcohol_cert:
                    missing.append("Drug and Alcohol Policy certification")
                if not applicant_cert:
                    missing.append("Applicant certification checkbox")
                if not sig_full_name:
                    missing.append("Full Legal Name (typed signature)")
                if missing:
                    show_missing_fields(missing, "The following are required:")
                else:
                    st.session_state.form_data.update(
                        {
                            "drug_alcohol_cert": drug_alcohol_cert,
                            "applicant_cert": applicant_cert,
                            "sig_full_name": sig_full_name,
                            "sig_date": sig_date.isoformat(),
                            "sig_timestamp": datetime.now().isoformat(),
                        }
                    )
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    if page == 8:
        st.subheader("📄 Background Check Disclosure & Authorization")
        st.caption(
            "This is a separate disclosure document. Please review it carefully before acknowledging."
        )

        st.markdown(
            f"""
    ### Disclosure Regarding Background Investigation

    {company.name} ("the Company") may obtain information about you from a third-party
    consumer reporting agency for contracting purposes. Thus, you may be the subject of
    a "consumer report" and/or an "investigative consumer report" which may include
    information about your character, general reputation, personal characteristics, and/or
    mode of living, and which can involve personal interviews with sources such as your
    neighbors, friends, or associates. These reports may contain information regarding your
    credit history, criminal history, social security verification, motor vehicle records
    ("driving records"), verification of your education or contracting history, or other
    background checks.

    ### Summary of Your Rights Under the Fair Credit Reporting Act

    The federal Fair Credit Reporting Act (FCRA) promotes the accuracy, fairness, and
    privacy of information in the files of consumer reporting agencies. You have the right to:

    - Request and obtain all information about you in the files of a consumer reporting agency.
    - Know if information in your file has been used against you.
    - Dispute inaccurate or incomplete information.
    - Have a consumer reporting agency correct or delete inaccurate, incomplete, or
      unverifiable information.
    - Have outdated negative information excluded from your report.
    - Seek damages from violators.

    You may have additional rights under state law. For more information, visit
    www.consumerfinance.gov/learnmore.

    ### Authorization

    By acknowledging below, I authorize {company.name} to obtain a consumer report and/or
    investigative consumer report about me for contracting and independent contractor
    qualification purposes.
    """
        )

        fcra_acknowledge = st.checkbox(
            "I acknowledge that I have read and understand the FCRA Disclosure above, "
            "have been given the opportunity to copy/print the Summary of Rights, and "
            "authorize the background investigation. *"
        )

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p8_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p8_save_draft")
        with bcol3:
            if st.button("Next → (California Disclosure)", key="p8_next", use_container_width=True, type="primary"):
                if not fcra_acknowledge:
                    show_user_error(
                        "You must acknowledge the FCRA Disclosure to proceed.",
                        code="validation_fcra_ack_required",
                        severity="warning",
                    )
                else:
                    st.session_state.form_data["fcra_acknowledge"] = True
                    st.session_state.form_data["fcra_timestamp"] = datetime.now().isoformat()
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    if page == 9:
        st.subheader("📄 California Disclosure Regarding Background Checks")

        ca_default_applies = default_california_applicability()
        applicability_options = ["Yes — I live or work in California", "No — I do not live or work in California"]
        prior_applicable = st.session_state.form_data.get("ca_applicable")
        if prior_applicable is True:
            default_index = 0
        elif prior_applicable is False:
            default_index = 1
        else:
            default_index = 0 if ca_default_applies else 1

        st.markdown("This disclosure only applies to applicants who live or work in California.")
        applicability_choice = st.radio(
            "Does this California disclosure apply to you?",
            applicability_options,
            index=default_index,
            key="ca_applicable_radio",
        )
        ca_applicable = applicability_choice == applicability_options[0]

        ca_disclosure_acknowledge = False
        ca_copy = st.session_state.form_data.get("ca_copy", False)

        if ca_applicable:
            st.markdown(
                f"""
        ### California Disclosure

        {company.name} may obtain information about you from a consumer reporting agency
        for contracting purposes. Thus, you may be the subject of a consumer report and/or
        an investigative consumer report under California law. These reports may include
        information about your character, general reputation, personal characteristics,
        and mode of living.

        You may request the nature and scope of any investigative consumer report and may
        request a copy of any report obtained about you, where permitted by law.
        """
            )

            ca_disclosure_acknowledge = st.checkbox(
                "I acknowledge that I have read and understood this California Disclosure Regarding Background Checks document. *",
                value=st.session_state.form_data.get("ca_disclosure_acknowledge", False),
            )

            st.markdown("---")
            st.subheader("Consumer Copy Request")
            ca_copy = st.checkbox(
                "Check this box if you'd like to receive a copy of any consumer report obtained about you "
                "(applies to applicants in California, Minnesota, or Oklahoma).",
                value=ca_copy,
            )
        else:
            st.info("This California-specific disclosure is not required based on your selection. Click Next to continue.")

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p9_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p9_save_draft")
        with bcol3:
            if st.button("Next → (PSP Disclosure)", key="p9_next", use_container_width=True, type="primary"):
                if ca_applicable and not ca_disclosure_acknowledge:
                    show_user_error(
                        "You must acknowledge the California Disclosure to proceed.",
                        code="validation_ca_disclosure_ack_required",
                        severity="warning",
                    )
                else:
                    st.session_state.form_data["ca_applicable"] = ca_applicable
                    st.session_state.form_data["ca_disclosure_acknowledge"] = ca_disclosure_acknowledge
                    st.session_state.form_data["ca_disclosure_timestamp"] = (
                        datetime.now().isoformat() if ca_applicable and ca_disclosure_acknowledge else None
                    )
                    st.session_state.form_data["ca_copy"] = ca_copy
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    if page == 10:
        st.subheader("📄 PSP Disclosure and Authorization")
        st.caption(
            "This is a separate disclosure document. Please review it carefully before acknowledging."
        )

        st.markdown(
            f"""
    ### Pre-Employment Screening Program (PSP) Disclosure

    In connection with your application for contracting with {company.name}, we may obtain
    one or more reports from the Federal Motor Carrier Safety Administration (FMCSA)
    Pre-Employment Screening Program (PSP) regarding your safety record.

    The PSP report will contain your crash and inspection history from the FMCSA's
    Motor Carrier Management Information System (MCMIS) for the preceding five (5) years
    of crash data and three (3) years of inspection data.

    ### Your Rights

    - You have the right to review the PSP report before any adverse action is taken.
    - You may obtain a copy of the report by contacting FMCSA.
    - You may challenge the accuracy of any information contained in the report.
    - You may request correction of inaccurate information through the DataQs system
      at https://dataqs.fmcsa.dot.gov.

    ### Authorization

    By acknowledging below, I authorize {company.name} and its agents to access my
    PSP record from FMCSA in connection with my application to contract as an
    independent contractor driver.
    """
        )

        psp_acknowledge = st.checkbox(
            "I acknowledge that I have read and understand the PSP Disclosure "
            "and Authorization above, and authorize access to my PSP record. *"
        )

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p10_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p10_save_draft")
        with bcol3:
            if st.button("Next → (Clearinghouse Release)", key="p10_next", use_container_width=True, type="primary"):
                if not psp_acknowledge:
                    show_user_error(
                        "You must acknowledge the PSP Disclosure to proceed.",
                        code="validation_psp_ack_required",
                        severity="warning",
                    )
                else:
                    st.session_state.form_data["psp_acknowledge"] = True
                    st.session_state.form_data["psp_timestamp"] = datetime.now().isoformat()
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    if page == 11:
        st.subheader("📄 FMCSA Clearinghouse Release")
        st.caption(
            "This is a separate disclosure document. Please review it carefully before acknowledging."
        )

        st.markdown(
            f"""
    ### FMCSA Drug & Alcohol Clearinghouse Consent

    In accordance with 49 CFR Part 382, Subpart G, {company.name} is required to conduct
    a query of the FMCSA Drug and Alcohol Clearinghouse prior to contracting with any
    commercial motor vehicle (CMV) driver.

    ### What This Means

    By providing consent below, you authorize {company.name} to conduct:

    1. A **full query** of the FMCSA Clearinghouse to determine whether any drug or alcohol
       violation information exists about you.
    2. **Annual limited queries** for the duration of your independent contractor agreement
    with {company.name}.

    ### Your Responsibilities

    - You must register with the FMCSA Clearinghouse at https://clearinghouse.fmcsa.dot.gov
      and grant electronic consent for the full query.
    - A full query requires your separate electronic consent through the Clearinghouse system.

    ### Employment Verification Acknowledgment and Release (DOT Drug and Alcohol)

    I authorize the release of information from my Department of Transportation regulated
    drug and alcohol testing records by my previous employers/contractors listed in this
    application to {company.name} or its designated agents.
    """
        )

        clearinghouse_acknowledge = st.checkbox(
            "I acknowledge that I have read and understand the Clearinghouse Release, "
            "and I consent to the full and limited queries of the FMCSA Clearinghouse. *"
        )

        st.markdown(
            "**Investigative Consumer Report**\n\n"
            "In addition to the standard background check disclosures above, an *investigative consumer report* may include "
            "information about your character, general reputation, personal characteristics, and mode of living, gathered "
            "through interviews with people who know you (such as previous employers, neighbors, or references). "
            "You have the right to request additional details about the nature and scope of any such report."
        )
        inv_consumer_report = st.checkbox("I understand and agree to the Investigative Consumer Report Disclosure. *")

        st.markdown("---")

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button("← Back", key="p11_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            render_save_draft_button("p11_save_draft")
        with bcol3:
            if st.button("Next → (Review & Submit)", key="p11_next", use_container_width=True, type="primary"):
                if not clearinghouse_acknowledge or not inv_consumer_report:
                    missing = []
                    if not clearinghouse_acknowledge:
                        missing.append("Clearinghouse Release acknowledgment")
                    if not inv_consumer_report:
                        missing.append("Investigative Consumer Report Disclosure acknowledgment")
                    show_user_error(
                        "Please complete the required final acknowledgments: " + ", ".join(missing),
                        code="validation_final_ack_required",
                        severity="warning",
                        extra={"missing_acknowledgments": missing},
                    )
                else:
                    st.session_state.form_data["clearinghouse_acknowledge"] = True
                    st.session_state.form_data["clearinghouse_timestamp"] = datetime.now().isoformat()
                    st.session_state.form_data["inv_consumer_report"] = inv_consumer_report
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    return False
