"""Renderers for pages 3 through 11 of the application flow."""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from config import (
    LICENSE_CLASSES,
    TRAILER_LENGTHS,
    TRAILER_TYPES,
    US_STATES,
)
from runtime_context import get_active_company_profile
from services.draft_service import autosave_draft
from state import next_page, prev_page
from ui.common import default_california_applicability, selectbox_with_placeholder, show_missing_fields


def render_remaining_page(page: int) -> bool:
    company = get_active_company_profile()

    if page == 3:
        st.subheader("Licenses & Endorsements")
        st.markdown("List all driver's licenses held in the past 3 years and current endorsements.")

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
                lic_class = selectbox_with_placeholder(
                    "License Class",
                    LICENSE_CLASSES,
                    current_value=existing.get("class"),
                    key=f"lic_class_{i}",
                )
            with lcol2:
                lic_expiration = st.date_input(
                    "License Expiration",
                    key=f"lic_exp_{i}",
                    value=existing.get("expiration", date.today()),
                )
                med_card_exp = st.date_input(
                    "DOT Medical Card Expiration",
                    key=f"med_exp_{i}",
                    value=existing.get("med_card_exp", date.today()),
                )
                is_cdl = selectbox_with_placeholder(
                    "Commercial Driver License?",
                    ["Yes", "No"],
                    current_value=existing.get("is_cdl"),
                    key=f"is_cdl_{i}",
                )
            with lcol3:
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
                        value=existing.get("hazmat_exp", date.today()),
                    )
                    if hazmat_end == "Yes"
                    else None
                )
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

            licenses_input.append(
                {
                    "number": lic_number,
                    "state": lic_state,
                    "class": lic_class,
                    "expiration": lic_expiration,
                    "med_card_exp": med_card_exp,
                    "is_cdl": is_cdl,
                    "tanker": tanker_end,
                    "hazmat": hazmat_end,
                    "hazmat_exp": hazmat_exp,
                    "doubles": doubles_end,
                    "x_endorsement": x_end,
                }
            )
            st.markdown("---")

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p3_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            if st.button("Next →", key="p3_next", use_container_width=True, type="primary"):
                missing: list[str] = []
                for index, license_entry in enumerate(licenses_input, start=1):
                    if not license_entry["number"]:
                        missing.append(f"License #{index} number")
                    if not license_entry["state"]:
                        missing.append(f"License #{index} state")
                    if not license_entry["class"]:
                        missing.append(f"License #{index} class")
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
                next_page()
                autosave_draft()
                st.rerun()
        return True

    if page == 4:
        st.subheader("Employment / Contracting History — Past 10 Years")
        st.markdown(
            """
    **49 CFR § 391.21 requires a complete 10-year work history.**
    List ALL employers/contractors for the past 10 years, starting with the most recent.
    Account for all gaps in employment/contracting.
    """
        )

        num_employers = st.number_input(
            "Number of employers/contractors to add",
            min_value=1,
            max_value=15,
            value=max(1, len(st.session_state.employers) if st.session_state.employers else 1),
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
                    emp_phone = st.text_input("Phone", key=f"emp_phone_{i}", value=existing.get("phone", ""))
                    emp_position = st.text_input("Position Held", key=f"emp_pos_{i}", value=existing.get("position", ""))
                with ecol2:
                    emp_start = st.date_input("Start Date", key=f"emp_start_{i}", value=existing.get("start", date(2020, 1, 1)))
                    emp_end = st.date_input("End Date", key=f"emp_end_{i}", value=existing.get("end", date.today()))
                    emp_reason = st.text_input("Reason for Leaving", key=f"emp_reason_{i}", value=existing.get("reason", ""))
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
                        emp_areas = st.text_input("Areas Driven", key=f"emp_areas_{i}", value=existing.get("areas", ""))
                        emp_miles = st.text_input("Miles Driven Weekly", key=f"emp_miles_{i}", value=existing.get("miles", ""))
                    with mcol2:
                        emp_truck = st.text_input("Most Common Truck Driven", key=f"emp_truck_{i}", value=existing.get("truck", ""))
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
                    emp_areas = emp_miles = emp_truck = emp_trailer = emp_trailer_len = ""

                employers_input.append(
                    {
                        "company": emp_company,
                        "address": emp_address,
                        "city_state": emp_city_state,
                        "phone": emp_phone,
                        "position": emp_position,
                        "start": emp_start,
                        "end": emp_end,
                        "reason": emp_reason,
                        "terminated": emp_terminated,
                        "current": emp_current,
                        "contact_ok": emp_contact_ok,
                        "cmv": emp_cmv,
                        "fmcsa": emp_fmcsa,
                        "dot_testing": emp_dot_testing,
                        "areas": emp_areas,
                        "miles": emp_miles,
                        "truck": emp_truck,
                        "trailer": emp_trailer,
                        "trailer_len": emp_trailer_len,
                    }
                )

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p4_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
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
                ts_start = st.date_input("Start Date", key="ts_start", value=st.session_state.form_data.get("ts_start", date(2020, 1, 1)))
            with tcol2:
                ts_end = st.date_input("End Date", key="ts_end", value=st.session_state.form_data.get("ts_end", date(2020, 6, 1)))
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
        st.markdown("List name, address, city, state, phone number, and relationship.")
        ref1 = st.text_input("Reference #1", value=st.session_state.form_data.get("ref1", ""))
        ref2 = st.text_input("Reference #2", value=st.session_state.form_data.get("ref2", ""))

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p5_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
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
                        "ref1": ref1,
                        "ref2": ref2,
                    }
                )
                next_page()
                autosave_draft()
                st.rerun()
        return True

    if page == 6:
        st.subheader("FMCSR Disqualifications, Accident Record & Violations")

        st.markdown("#### DOT Disqualification Questions (49 CFR 391.15)")
        st.markdown("*These are DOT-specific safety questions required by federal regulation.*")

        disq_391_15 = selectbox_with_placeholder(
            "Under FMCSR 391.15, are you currently disqualified from driving a commercial motor vehicle? [49 CFR 391.15]",
            ["No", "Yes"],
        )
        disq_suspended = selectbox_with_placeholder(
            "Has your license, permit, or privilege to drive ever been suspended or revoked for any reason? [49 CFR 391.21(b)(9)]",
            ["No", "Yes"],
        )
        disq_denied = selectbox_with_placeholder(
            "Have you ever been denied a license, permit, or privilege to operate a motor vehicle? [49 CFR 391.21(b)(9)]",
            ["No", "Yes"],
        )
        disq_drug_test = selectbox_with_placeholder(
            "Within the past two years, have you tested positive, or refused to test, on a pre-employment "
            "drug or alcohol test by an employer to whom you applied, but did not obtain, safety-sensitive "
            "transportation work covered by DOT agency drug and alcohol testing rules? [49 CFR 40.25(j)]",
            ["No", "Yes"],
        )
        disq_convicted = selectbox_with_placeholder(
            "In the past three (3) years, have you been convicted of any of the following offenses? [49 CFR 391.15]:\n"
            "• Driving a CMV with a BAC of .04 or more\n"
            "• Driving under the influence of alcohol as prescribed by state law\n"
            "• Refusal to undergo drug and alcohol testing\n"
            "• Driving a CMV under the influence of a Schedule I controlled substance\n"
            "• Transportation, possession, or unlawful use of controlled substances while driving for a motor carrier\n"
            "• Leaving the scene of an accident while operating a CMV\n"
            "• Any other felony involving the use of a CMV",
            ["No", "Yes"],
        )

        st.markdown("---")
        st.markdown("#### Vehicle Accident Record")
        st.markdown("Were you involved in any accidents/incidents with any vehicle in the last 5 years (even if not at fault)?")

        has_accidents = selectbox_with_placeholder("Any accidents to report?", ["No", "Yes"], key="has_acc")
        if has_accidents == "Yes":
            num_accidents = st.number_input("Number of accidents", min_value=1, max_value=10, value=1)
            accidents_input = []
            for i in range(int(num_accidents)):
                with st.expander(f"Accident #{i+1}"):
                    acol1, acol2 = st.columns(2)
                    with acol1:
                        acc_date = st.date_input("Date of Accident", key=f"acc_date_{i}")
                        acc_location = st.text_input("Location", key=f"acc_loc_{i}")
                        acc_fatalities = st.number_input("Fatalities", min_value=0, key=f"acc_fat_{i}")
                    with acol2:
                        acc_injuries = st.number_input("Injuries", min_value=0, key=f"acc_inj_{i}")
                        acc_hazmat = st.selectbox("Hazmat Spill?", ["No", "Yes"], key=f"acc_haz_{i}")
                        acc_description = st.text_area("Description", key=f"acc_desc_{i}")
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
        st.markdown("Have you had any moving violations or traffic convictions in the past 3 years?")

        has_violations = selectbox_with_placeholder("Any violations to report?", ["No", "Yes"], key="has_viol")
        if has_violations == "Yes":
            num_violations = st.number_input("Number of violations", min_value=1, max_value=10, value=1)
            violations_input = []
            for i in range(int(num_violations)):
                with st.expander(f"Violation #{i+1}"):
                    vcol1, vcol2 = st.columns(2)
                    with vcol1:
                        viol_date = st.date_input("Date", key=f"viol_date_{i}")
                        viol_location = st.text_input("Location", key=f"viol_loc_{i}")
                    with vcol2:
                        viol_charge = st.text_input("Charge", key=f"viol_charge_{i}")
                        viol_penalty = st.text_input("Penalty", key=f"viol_pen_{i}")
                    violations_input.append(
                        {
                            "date": viol_date,
                            "location": viol_location,
                            "charge": viol_charge,
                            "penalty": viol_penalty,
                        }
                    )
            st.session_state.violations = violations_input
        else:
            st.session_state.violations = []

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p6_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            if st.button("Next →", key="p6_next", use_container_width=True, type="primary"):
                missing = []
                if not disq_391_15:
                    missing.append("FMCSR 391.15 disqualification question")
                if not disq_suspended:
                    missing.append("License suspension/revocation question")
                if not disq_denied:
                    missing.append("License denial question")
                if not disq_drug_test:
                    missing.append("Pre-employment drug/alcohol test question")
                if not disq_convicted:
                    missing.append("DOT offense conviction question")
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
                    }
                )
                next_page()
                autosave_draft()
                st.rerun()
        return True

    if page == 7:
        st.subheader("Drug and Alcohol Policy — O/O & Independent Contractor Certification")

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

        st.markdown("---")
        st.subheader("Applicant Certification")
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

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p7_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
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
        st.subheader("📄 Federal FCRA Disclosure — Standalone Document")
        st.warning(
            "**IMPORTANT: Federal law (15 U.S.C. § 1681b) requires this disclosure be presented "
            "as a standalone document, separate from the application.**"
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

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p8_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            if st.button("Next → (California Disclosure)", key="p8_next", use_container_width=True, type="primary"):
                if not fcra_acknowledge:
                    st.error("You must acknowledge the FCRA Disclosure to proceed.")
                else:
                    st.session_state.form_data["fcra_acknowledge"] = True
                    st.session_state.form_data["fcra_timestamp"] = datetime.now().isoformat()
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    if page == 9:
        st.subheader("📄 California Disclosure Regarding Background Checks")
        st.warning("**This disclosure applies if you live or work in California.**")

        ca_applicable_default = st.session_state.form_data.get("ca_applicable", default_california_applicability())
        ca_copy_default = st.session_state.form_data.get("ca_copy", False)

        ca_applicable = st.checkbox(
            "I live or work in California, so this California disclosure applies to me.",
            value=ca_applicable_default,
        )

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
        else:
            st.info("If you do not live or work in California, this California-specific disclosure is not required for this application.")
            ca_disclosure_acknowledge = False

        st.markdown("---")
        st.subheader("Consumer Copy Request")
        ca_copy = st.checkbox(
            "If you live or work in California, Minnesota, or Oklahoma, check this box if you'd like to receive a copy of a consumer report if one is obtained.",
            value=ca_copy_default,
        )

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p9_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            if st.button("Next → (PSP Disclosure)", key="p9_next", use_container_width=True, type="primary"):
                if ca_applicable and not ca_disclosure_acknowledge:
                    st.error("You must acknowledge the California Disclosure to proceed.")
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
        st.subheader("📄 PSP Disclosure and Authorization — Standalone Document")
        st.warning("**This disclosure is presented as a standalone document as required by federal regulation.**")

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

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p10_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            if st.button("Next → (Clearinghouse Release)", key="p10_next", use_container_width=True, type="primary"):
                if not psp_acknowledge:
                    st.error("You must acknowledge the PSP Disclosure to proceed.")
                else:
                    st.session_state.form_data["psp_acknowledge"] = True
                    st.session_state.form_data["psp_timestamp"] = datetime.now().isoformat()
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    if page == 11:
        st.subheader("📄 FMCSA Clearinghouse Release — Standalone Document")
        st.warning("**This disclosure is presented as a standalone document as required by federal regulation.**")

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
        inv_consumer_report = st.checkbox("I understand and agree to the Investigative Consumer Report Disclosure.")

        st.markdown("---")

        bcol1, bcol2 = st.columns(2)
        with bcol1:
            if st.button("← Back", key="p11_back", use_container_width=True):
                prev_page()
                st.rerun()
        with bcol2:
            if st.button("Next → (Review & Submit)", key="p11_next", use_container_width=True, type="primary"):
                if not clearinghouse_acknowledge or not inv_consumer_report:
                    missing = []
                    if not clearinghouse_acknowledge:
                        missing.append("Clearinghouse Release acknowledgment")
                    if not inv_consumer_report:
                        missing.append("Investigative Consumer Report Disclosure acknowledgment")
                    st.error("Please complete the required final acknowledgments: " + ", ".join(missing))
                else:
                    st.session_state.form_data["clearinghouse_acknowledge"] = True
                    st.session_state.form_data["clearinghouse_timestamp"] = datetime.now().isoformat()
                    st.session_state.form_data["inv_consumer_report"] = inv_consumer_report
                    next_page()
                    autosave_draft()
                    st.rerun()
        return True

    return False
