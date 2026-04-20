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
from datetime import date, datetime
from config import (
    COMPANY_NAME, COMPANY_ADDRESS, COMPANY_CITY_STATE_ZIP,
    COMPANY_PHONE, COMPANY_EMAIL, PHASE_LABELS, EQUIPMENT_TYPES,
    TRAILER_TYPES, TRAILER_LENGTHS, LICENSE_CLASSES, US_STATES,
    POSITION_TYPES, REFERRAL_SOURCES,
)
from pdf_generator import (
    generate_application_pdf,
    generate_fcra_pdf,
    generate_psp_pdf,
    generate_clearinghouse_pdf,
    generate_california_disclosure_pdf,
)
from submission_storage import (
    get_submission_destination_summary,
    save_submission_bundle as persist_submission_bundle,
)

# ---------------------------------------------------------------------------
# Session state initialization
# ---------------------------------------------------------------------------
if "current_page" not in st.session_state:
    st.session_state.current_page = 1
if "form_data" not in st.session_state:
    st.session_state.form_data = {}
if "submitted" not in st.session_state:
    st.session_state.submitted = False
if "employers" not in st.session_state:
    st.session_state.employers = []
if "accidents" not in st.session_state:
    st.session_state.accidents = []
if "violations" not in st.session_state:
    st.session_state.violations = []
if "licenses" not in st.session_state:
    st.session_state.licenses = []
if "submission_artifacts" not in st.session_state:
    st.session_state.submission_artifacts = None
if "saved_submission_dir" not in st.session_state:
    st.session_state.saved_submission_dir = None
if "submission_save_error" not in st.session_state:
    st.session_state.submission_save_error = None
if "submission_save_notice" not in st.session_state:
    st.session_state.submission_save_notice = None

SUBMISSIONS_DIR = Path(__file__).resolve().parent / "submissions"


def next_page():
    st.session_state.current_page += 1


def prev_page():
    st.session_state.current_page -= 1


def _display_value(value, default="—"):
    if value is None:
        return default
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    value = str(value).strip()
    return value if value else default


def _summary_item(label, value, default="—"):
    st.markdown(f"- **{label}:** {_display_value(value, default)}")


def _show_missing_fields(missing_fields, header_text="Please complete the required fields:"):
    if not missing_fields:
        return

    bullet_list = "\n".join([f"- {field}" for field in missing_fields])
    st.error(f"{header_text}\n\n{bullet_list}")


def _default_california_applicability():
    state = (st.session_state.form_data.get("state") or "").upper()
    applying_location = (st.session_state.form_data.get("applying_location") or "").lower()
    return state == "CA" or "ca" in applying_location or "fontana" in applying_location


def build_submission_artifacts():
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


def save_submission_bundle(artifacts):
    return persist_submission_bundle(
        form_data=st.session_state.form_data,
        employers=st.session_state.employers,
        licenses=st.session_state.licenses,
        accidents=st.session_state.accidents,
        violations=st.session_state.violations,
        artifacts=artifacts,
        local_base_dir=SUBMISSIONS_DIR,
    )


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title=f"{COMPANY_NAME} - Driver Application",
    page_icon="🚛",
    layout="wide",
)

# Required field styling
st.markdown("""
<style>
    /* Red asterisk on required labels */
    div[data-testid="stTextInput"] label p:has(~ *),
    div[data-testid="stSelectbox"] label p:has(~ *) {
        font-weight: 600;
    }
    .missing-field {
        background: rgba(255, 75, 75, 0.1);
        border: 2px solid #ff4b4b;
        border-radius: 8px;
        padding: 0.5rem 0.8rem;
        margin-bottom: 0.3rem;
        font-size: 0.9rem;
        color: var(--text-color);
    }
    .missing-field-header {
        color: #ff4b4b;
        font-weight: 700;
        font-size: 1rem;
        margin-bottom: 0.3rem;
    }
    .app-header {
        text-align: center;
        padding: 1.15rem 1rem;
        margin-bottom: 0.5rem;
        border: 1px solid color-mix(in srgb, var(--primary-color) 35%, transparent);
        border-radius: 14px;
        border-bottom-width: 3px;
        background: linear-gradient(
            135deg,
            color-mix(in srgb, var(--primary-color) 12%, transparent),
            color-mix(in srgb, var(--primary-color) 4%, var(--background-color))
        );
    }
    .app-header h1 {
        color: var(--text-color);
        margin-bottom: 0.2rem;
    }
    .app-header p {
        color: color-mix(in srgb, var(--text-color) 82%, transparent);
        margin: 0;
    }
    .app-header h3 {
        color: var(--primary-color);
        margin-top: 0.55rem;
    }
    .eeo-notice {
        background: color-mix(in srgb, var(--primary-color) 8%, var(--secondary-background-color));
        color: var(--text-color);
        padding: 0.8rem;
        border-radius: 8px;
        margin: 0.5rem 0;
        font-size: 0.85rem;
        line-height: 1.5;
        border-left: 4px solid var(--primary-color);
    }
</style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.markdown(f"""
<div class="app-header">
    <h1>{COMPANY_NAME}</h1>
    <p>{COMPANY_ADDRESS} | {COMPANY_CITY_STATE_ZIP}</p>
    <p>Phone: {COMPANY_PHONE} | Email: {COMPANY_EMAIL}</p>
    <h3>Independent Contractor Driver Application</h3>
</div>
""", unsafe_allow_html=True)

# EEO statement - compliant language
st.markdown("""
<div class="eeo-notice">
In compliance with Federal and State equal opportunity laws, qualified applicants are
considered for all positions without regard to race, color, religion, sex, national origin,
age, marital status, veteran status, non-job related disability, or any other protected group status.
</div>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------

total_pages = len(PHASE_LABELS)
if st.session_state.submitted:
    progress = 1.0
    progress_text = "Application complete"
else:
    display_page = min(max(st.session_state.current_page, 1), total_pages)
    progress = display_page / total_pages
    progress_text = f"Step {display_page} of {total_pages}: {PHASE_LABELS.get(display_page, '')}"
st.progress(progress, text=progress_text)

page = 99 if st.session_state.submitted else st.session_state.current_page

# =========================================================================
# PAGE 1: Personal Information
# =========================================================================
if page == 1:
    with st.form("page1_form"):
        st.subheader("Personal Information")

        col1, col2 = st.columns(2)
        with col1:
            first_name = st.text_input("First Name *", value=st.session_state.form_data.get("first_name", ""))
            middle_name = st.text_input("Middle Name", value=st.session_state.form_data.get("middle_name", ""))
            last_name = st.text_input("Last Name *", value=st.session_state.form_data.get("last_name", ""))
            dob = st.date_input("Date of Birth *",
                                value=st.session_state.form_data.get("dob", date(1990, 1, 1)),
                                min_value=date(1940, 1, 1), max_value=date(2008, 1, 1))
            ssn = st.text_input("Social Security Number *", type="password",
                                value=st.session_state.form_data.get("ssn", ""),
                                help="Your SSN is encrypted and stored securely.")

        with col2:
            address = st.text_input("Current Address *", value=st.session_state.form_data.get("address", ""))
            city = st.text_input("City *", value=st.session_state.form_data.get("city", ""))
            state = st.selectbox("State *", options=[""] + US_STATES,
                                 index=0 if not st.session_state.form_data.get("state") else US_STATES.index(st.session_state.form_data["state"]) + 1)
            zip_code = st.text_input("Zip Code *", value=st.session_state.form_data.get("zip_code", ""))
            country = st.text_input("Country", value=st.session_state.form_data.get("country", "United States"))

        st.markdown("---")

        col3, col4 = st.columns(2)
        with col3:
            primary_phone = st.text_input("Primary Phone *", value=st.session_state.form_data.get("primary_phone", ""))
            email = st.text_input("Email Address *", value=st.session_state.form_data.get("email", ""))
            preferred_contact = st.selectbox("Preferred Method of Contact",
                                             ["Cell Phone", "Email", "Either"],
                                             index=["Cell Phone", "Email", "Either"].index(
                                                 st.session_state.form_data.get("preferred_contact", "Cell Phone")))
        with col4:
            cell_phone = st.text_input("Cell Phone", value=st.session_state.form_data.get("cell_phone", ""))
            best_time = st.text_input("Best Time to Contact You", value=st.session_state.form_data.get("best_time", "Any"))
            resided_3_years = st.selectbox("Have you resided at your current address for 3+ years?",
                                            ["Yes", "No"],
                                            index=["Yes", "No"].index(
                                                st.session_state.form_data.get("resided_3_years", "Yes")))

        prev_address = prev_city = prev_state = prev_zip = ""
        if resided_3_years == "No":
            st.markdown("**Previous Address (if less than 3 years at current):**")
            pcol1, pcol2 = st.columns(2)
            with pcol1:
                prev_address = st.text_input("Previous Address", value=st.session_state.form_data.get("prev_address", ""))
                prev_city = st.text_input("Previous City", value=st.session_state.form_data.get("prev_city", ""))
            with pcol2:
                prev_state = st.selectbox("Previous State", options=[""] + US_STATES, key="prev_state_sel")
                prev_zip = st.text_input("Previous Zip", value=st.session_state.form_data.get("prev_zip", ""))

        st.markdown("---")

        emergency_name = st.text_input("Emergency Contact Name *", value=st.session_state.form_data.get("emergency_name", ""))
        ecol1, ecol2 = st.columns(2)
        with ecol1:
            emergency_phone = st.text_input("Emergency Contact Phone *", value=st.session_state.form_data.get("emergency_phone", ""))
        with ecol2:
            emergency_relationship = st.text_input("Relationship", value=st.session_state.form_data.get("emergency_relationship", ""))

        # Text message consent
        st.markdown("---")
        text_consent = st.checkbox(
            "I consent to receive text messages from PRESTIGE TRANSPORTATION INC. "
            "regarding my application and contracting status. I may opt out at any time by texting STOP.",
            value=st.session_state.form_data.get("text_consent", False)
        )

        # Save & navigate
        submitted = st.form_submit_button("Next →", use_container_width=True, type="primary")

    if submitted:
        missing = []
        if not first_name: missing.append("First Name")
        if not last_name: missing.append("Last Name")
        if not ssn: missing.append("Social Security Number")
        if not address: missing.append("Current Address")
        if not city: missing.append("City")
        if not state: missing.append("State")
        if not zip_code: missing.append("Zip Code")
        if not primary_phone: missing.append("Primary Phone")
        if not email: missing.append("Email Address")
        if not emergency_name: missing.append("Emergency Contact Name")
        if not emergency_phone: missing.append("Emergency Contact Phone")
        if missing:
            _show_missing_fields(missing, "The following required fields are missing:")
        else:
            st.session_state.form_data.update({
                "first_name": first_name, "middle_name": middle_name, "last_name": last_name,
                "dob": dob, "ssn": ssn, "address": address, "city": city, "state": state,
                "zip_code": zip_code, "country": country, "primary_phone": primary_phone,
                "cell_phone": cell_phone, "email": email, "preferred_contact": preferred_contact,
                "best_time": best_time, "resided_3_years": resided_3_years,
                "prev_address": prev_address, "prev_city": prev_city,
                "prev_state": prev_state if resided_3_years == "No" else "",
                "prev_zip": prev_zip,
                "emergency_name": emergency_name, "emergency_phone": emergency_phone,
                "emergency_relationship": emergency_relationship,
                "text_consent": text_consent,
            })
            next_page()
            st.rerun()

# =========================================================================
# PAGE 2: Company Questions & Driving Experience
# =========================================================================
elif page == 2:
    st.subheader("Company Questions & Driving Experience")

    position_value = st.session_state.form_data.get("position", POSITION_TYPES[0])
    if position_value not in POSITION_TYPES:
        position_value = POSITION_TYPES[0]

    currently_employed_value = st.session_state.form_data.get("currently_employed", "Yes")
    if currently_employed_value not in ["Yes", "No"]:
        currently_employed_value = "Yes"

    relatives_here_value = st.session_state.form_data.get("relatives_here", "No")
    if relatives_here_value not in ["No", "Yes"]:
        relatives_here_value = "No"

    known_other_name_value = st.session_state.form_data.get("known_other_name", "No")
    if known_other_name_value not in ["No", "Yes"]:
        known_other_name_value = "No"

    col1, col2 = st.columns(2)
    with col1:
        position = st.selectbox("What position are you applying for? *", POSITION_TYPES,
                                index=POSITION_TYPES.index(position_value))
        eligible_us = st.selectbox("Are you legally eligible to provide contracted services in the United States? *",
                                    ["Yes", "No"],
                                    index=["Yes", "No"].index(st.session_state.form_data.get("eligible_us", "Yes")))
        read_english = st.selectbox("Do you read, write, and speak English? *",
                                     ["Yes", "No"],
                                     index=["Yes", "No"].index(st.session_state.form_data.get("read_english", "Yes")))
        currently_employed = st.selectbox("Are you currently employed/contracted elsewhere?",
                                          ["Yes", "No"],
                                          index=["Yes", "No"].index(currently_employed_value))
        worked_here_before = st.selectbox("Have you ever contracted with this company before?",
                                          ["No", "Yes"],
                                          index=["No", "Yes"].index(st.session_state.form_data.get("worked_here_before", "No")))
    with col2:
        applying_location = st.text_input("What location are you applying for?",
                                           value=st.session_state.form_data.get("applying_location", ""))
        twic_card = st.selectbox("Do you have a current TWIC card?", ["No", "Yes"],
                                  index=["No", "Yes"].index(st.session_state.form_data.get("twic_card", "No")))
        if twic_card == "Yes":
            twic_expiration = st.date_input("TWIC Expiration Date",
                                             value=st.session_state.form_data.get("twic_expiration", date.today()))
        else:
            twic_expiration = None
        referral_source = st.selectbox("How did you hear about us?", REFERRAL_SOURCES,
                                        index=REFERRAL_SOURCES.index(st.session_state.form_data.get("referral_source", "Internet Search")))

    if currently_employed == "No":
        last_employment_end = st.date_input(
            "What date did your last employment/contract end?",
            value=st.session_state.form_data.get("last_employment_end", date.today()),
        )
    else:
        last_employment_end = None

    email_marketing_opt_in = st.checkbox(
        f"Yes, I agree to receive information concerning future opportunities or promotions from {COMPANY_NAME} by email or other commercial electronic communications.",
        value=st.session_state.form_data.get("email_marketing_opt_in", False),
    )

    if referral_source == "Driver Referral":
        referral_name = st.text_input("Referral Driver Name", value=st.session_state.form_data.get("referral_name", ""))
    elif referral_source == "Other":
        referral_name = st.text_input("Please explain", value=st.session_state.form_data.get("referral_name", ""))
    else:
        referral_name = ""

    if worked_here_before == "Yes":
        prev_dates = st.text_input("Enter start/end dates, position, and reason for leaving",
                                    value=st.session_state.form_data.get("prev_dates", ""))
    else:
        prev_dates = ""

    # Relatives
    relatives_here = st.selectbox("Do you have any relatives contracted here?", ["No", "Yes"],
                                  index=["No", "Yes"].index(relatives_here_value))
    relatives_names = ""
    if relatives_here == "Yes":
        relatives_names = st.text_input("Names of relatives", value=st.session_state.form_data.get("relatives_names", ""))

    st.markdown("---")
    if position == "Owner Operator":
        st.subheader("Owner Operator Equipment")
        ocol1, ocol2 = st.columns(2)
        with ocol1:
            equipment_description = st.text_input("Equipment Description (Tractor)", value=st.session_state.form_data.get("equipment_description", ""))
            equipment_year = st.text_input("Year", value=st.session_state.form_data.get("equipment_year", ""))
            equipment_make = st.text_input("Make", value=st.session_state.form_data.get("equipment_make", ""))
            equipment_model = st.text_input("Model", value=st.session_state.form_data.get("equipment_model", ""))
            equipment_color = st.text_input("Color", value=st.session_state.form_data.get("equipment_color", ""))
        with ocol2:
            equipment_vin = st.text_input("VIN", value=st.session_state.form_data.get("equipment_vin", ""))
            equipment_weight = st.text_input("Weight", value=st.session_state.form_data.get("equipment_weight", ""))
            equipment_mileage = st.text_input("Mileage", value=st.session_state.form_data.get("equipment_mileage", ""))
            fifth_wheel_height = st.text_input("Fifth Wheel Height", value=st.session_state.form_data.get("fifth_wheel_height", ""))
    else:
        equipment_description = equipment_year = equipment_make = equipment_model = ""
        equipment_color = equipment_vin = equipment_weight = equipment_mileage = fifth_wheel_height = ""

    st.markdown("---")
    st.subheader("Driving Experience")
    st.markdown("For each class of equipment, enter type of equipment, start and end dates, "
                "and approximate number of total miles. If no experience in a class, enter NONE.")

    experience_data = {}
    for eq_type in EQUIPMENT_TYPES:
        with st.expander(eq_type):
            ecol1, ecol2, ecol3 = st.columns(3)
            key_prefix = f"exp_{eq_type.lower().replace(' ', '_').replace('-', '_')}"
            with ecol1:
                exp_type = st.text_input(f"Equipment Detail", key=f"{key_prefix}_type",
                                         value=st.session_state.form_data.get(f"{key_prefix}_type", ""))
            with ecol2:
                exp_miles = st.text_input(f"Total Miles", key=f"{key_prefix}_miles",
                                          value=st.session_state.form_data.get(f"{key_prefix}_miles", ""))
            with ecol3:
                exp_dates = st.text_input(f"Date Range", key=f"{key_prefix}_dates",
                                          value=st.session_state.form_data.get(f"{key_prefix}_dates", ""))
            experience_data[key_prefix] = {"type": exp_type, "miles": exp_miles, "dates": exp_dates}

    st.markdown("---")
    st.subheader("Additional Driving Info")
    dcol1, dcol2 = st.columns(2)
    with dcol1:
        safe_driving_awards = st.text_input("Safe driving awards held (and from whom)?",
                                             value=st.session_state.form_data.get("safe_driving_awards", "None"))
    with dcol2:
        known_other_name = st.selectbox("Have you ever been known by another name?", ["No", "Yes"],
                                        index=["No", "Yes"].index(known_other_name_value))
        other_name = ""
        if known_other_name == "Yes":
            other_name = st.text_input("Enter other name(s)", value=st.session_state.form_data.get("other_name", ""))

    # Navigation
    bcol1, bcol2 = st.columns(2)
    with bcol1:
        if st.button("← Back", key="p2_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        if st.button("Next →", key="p2_next", use_container_width=True, type="primary"):
            st.session_state.form_data.update({
                "position": position, "eligible_us": eligible_us, "read_english": read_english,
                "currently_employed": currently_employed, "last_employment_end": last_employment_end,
                "worked_here_before": worked_here_before, "applying_location": applying_location,
                "twic_card": twic_card, "twic_expiration": twic_expiration,
                "referral_source": referral_source, "referral_name": referral_name,
                "prev_dates": prev_dates, "relatives_here": relatives_here,
                "relatives_names": relatives_names,
                "email_marketing_opt_in": email_marketing_opt_in,
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
                "known_other_name": known_other_name, "other_name": other_name,
            })
            for key_prefix, vals in experience_data.items():
                st.session_state.form_data[f"{key_prefix}_type"] = vals["type"]
                st.session_state.form_data[f"{key_prefix}_miles"] = vals["miles"]
                st.session_state.form_data[f"{key_prefix}_dates"] = vals["dates"]
            next_page()
            st.rerun()

# =========================================================================
# PAGE 3: Licenses & Endorsements
# =========================================================================
elif page == 3:
    st.subheader("Licenses & Endorsements")
    st.markdown("List all driver's licenses held in the past 3 years and current endorsements.")

    num_licenses = st.number_input("How many licenses do you want to add?", min_value=1, max_value=5, value=max(1, len(st.session_state.licenses) if st.session_state.licenses else 1))

    licenses_input = []
    for i in range(int(num_licenses)):
        st.markdown(f"**License #{i+1}**")
        existing = st.session_state.licenses[i] if i < len(st.session_state.licenses) else {}
        lcol1, lcol2, lcol3 = st.columns(3)
        with lcol1:
            lic_number = st.text_input("License Number", key=f"lic_num_{i}",
                                        value=existing.get("number", ""))
            lic_state = st.selectbox("Licensing State", options=[""] + US_STATES, key=f"lic_state_{i}")
            lic_class = st.selectbox("License Class", LICENSE_CLASSES, key=f"lic_class_{i}")
        with lcol2:
            lic_expiration = st.date_input("License Expiration", key=f"lic_exp_{i}",
                                            value=existing.get("expiration", date.today()))
            med_card_exp = st.date_input("DOT Medical Card Expiration", key=f"med_exp_{i}",
                                          value=existing.get("med_card_exp", date.today()))
            is_cdl = st.selectbox("Commercial Driver License?", ["Yes", "No"], key=f"is_cdl_{i}")
        with lcol3:
            tanker_end = st.selectbox("Tanker Endorsement?", ["No", "Yes"], key=f"tanker_{i}")
            hazmat_end = st.selectbox("HAZMAT Endorsement?", ["No", "Yes"], key=f"hazmat_{i}")
            hazmat_exp = st.date_input("HAZMAT Expiration Date", key=f"hazmat_exp_{i}",
                                        value=existing.get("hazmat_exp", date.today())) if hazmat_end == "Yes" else None
            doubles_end = st.selectbox("Doubles/Triples Endorsement?", ["No", "Yes"], key=f"doubles_{i}")
            x_end = st.selectbox("X Endorsement?", ["No", "Yes"], key=f"x_end_{i}")

        licenses_input.append({
            "number": lic_number, "state": lic_state, "class": lic_class,
            "expiration": lic_expiration, "med_card_exp": med_card_exp, "is_cdl": is_cdl,
            "tanker": tanker_end, "hazmat": hazmat_end, "hazmat_exp": hazmat_exp,
            "doubles": doubles_end, "x_endorsement": x_end,
        })
        st.markdown("---")

    bcol1, bcol2 = st.columns(2)
    with bcol1:
        if st.button("← Back", key="p3_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        if st.button("Next →", key="p3_next", use_container_width=True, type="primary"):
            st.session_state.licenses = licenses_input
            next_page()
            st.rerun()

# =========================================================================
# PAGE 4: Employment History (10 Years)
# =========================================================================
elif page == 4:
    st.subheader("Employment / Contracting History — Past 10 Years")
    st.markdown("""
    **49 CFR § 391.21 requires a complete 10-year work history.**
    List ALL employers/contractors for the past 10 years, starting with the most recent.
    Account for all gaps in employment/contracting.
    """)

    num_employers = st.number_input("Number of employers/contractors to add",
                                     min_value=1, max_value=15,
                                     value=max(1, len(st.session_state.employers) if st.session_state.employers else 1))

    employers_input = []
    for i in range(int(num_employers)):
        existing = st.session_state.employers[i] if i < len(st.session_state.employers) else {}
        with st.expander(f"Employer / Contractor #{i+1}", expanded=(i == 0)):
            ecol1, ecol2 = st.columns(2)
            with ecol1:
                emp_company = st.text_input("Company Name *", key=f"emp_company_{i}",
                                             value=existing.get("company", ""))
                emp_address = st.text_input("Address", key=f"emp_addr_{i}",
                                             value=existing.get("address", ""))
                emp_city_state = st.text_input("City, State, Zip", key=f"emp_csz_{i}",
                                                value=existing.get("city_state", ""))
                emp_phone = st.text_input("Phone", key=f"emp_phone_{i}",
                                           value=existing.get("phone", ""))
                emp_position = st.text_input("Position Held", key=f"emp_pos_{i}",
                                              value=existing.get("position", ""))
            with ecol2:
                emp_start = st.date_input("Start Date", key=f"emp_start_{i}",
                                           value=existing.get("start", date(2020, 1, 1)))
                emp_end = st.date_input("End Date", key=f"emp_end_{i}",
                                         value=existing.get("end", date.today()))
                emp_reason = st.text_input("Reason for Leaving", key=f"emp_reason_{i}",
                                            value=existing.get("reason", ""))
                emp_terminated = st.selectbox("Were you terminated/discharged/laid off?",
                                              ["No", "Yes"], key=f"emp_term_{i}")
                emp_current = st.selectbox("Is this your current contractor/employer?",
                                            ["No", "Yes"], key=f"emp_current_{i}")

            ecol3, ecol4 = st.columns(2)
            with ecol3:
                emp_contact_ok = st.selectbox("May we contact this company?",
                                               ["Yes", "No"], key=f"emp_contact_{i}")
                emp_cmv = st.selectbox("Did you operate a commercial motor vehicle?",
                                        ["Yes", "No"], key=f"emp_cmv_{i}")
            with ecol4:
                emp_fmcsa = st.selectbox("Subject to FMCSA/Transport Canada regulations?",
                                          ["Yes", "No"], key=f"emp_fmcsa_{i}")
                emp_dot_testing = st.selectbox("Subject to DOT drug and alcohol testing?",
                                               ["Yes", "No"], key=f"emp_dot_{i}")

            if emp_cmv == "Yes":
                mcol1, mcol2, mcol3 = st.columns(3)
                with mcol1:
                    emp_areas = st.text_input("Areas Driven", key=f"emp_areas_{i}",
                                               value=existing.get("areas", ""))
                    emp_miles = st.text_input("Miles Driven Weekly", key=f"emp_miles_{i}",
                                               value=existing.get("miles", ""))
                with mcol2:
                    emp_truck = st.text_input("Most Common Truck Driven", key=f"emp_truck_{i}",
                                               value=existing.get("truck", ""))
                    emp_trailer = st.selectbox("Most Common Trailer", TRAILER_TYPES, key=f"emp_trailer_{i}")
                with mcol3:
                    emp_trailer_len = st.selectbox("Trailer Length", TRAILER_LENGTHS, key=f"emp_tlen_{i}")
            else:
                emp_areas = emp_miles = emp_truck = emp_trailer = emp_trailer_len = ""

            employers_input.append({
                "company": emp_company, "address": emp_address, "city_state": emp_city_state,
                "phone": emp_phone, "position": emp_position,
                "start": emp_start, "end": emp_end, "reason": emp_reason,
                "terminated": emp_terminated, "current": emp_current,
                "contact_ok": emp_contact_ok, "cmv": emp_cmv, "fmcsa": emp_fmcsa,
                "dot_testing": emp_dot_testing,
                "areas": emp_areas, "miles": emp_miles, "truck": emp_truck,
                "trailer": emp_trailer, "trailer_len": emp_trailer_len,
            })

    bcol1, bcol2 = st.columns(2)
    with bcol1:
        if st.button("← Back", key="p4_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        if st.button("Next →", key="p4_next", use_container_width=True, type="primary"):
            st.session_state.employers = employers_input
            next_page()
            st.rerun()

# =========================================================================
# PAGE 5: Education & Trucking School
# =========================================================================
elif page == 5:
    st.subheader("Education & Trucking School")

    col1, col2 = st.columns(2)
    with col1:
        highest_grade = st.selectbox("Highest Grade Completed",
                                      ["High School", "GED", "Some College", "College - 2 Year",
                                       "College - 4 Year", "Graduate Degree"],
                                      index=["High School", "GED", "Some College", "College - 2 Year",
                                             "College - 4 Year", "Graduate Degree"].index(
                                          st.session_state.form_data.get("highest_grade", "High School")))
    with col2:
        last_school = st.text_input("Last School Attended (Name, City, State)",
                                     value=st.session_state.form_data.get("last_school", ""))

    st.markdown("---")
    st.subheader("Trucking School")
    attended_trucking_school = st.selectbox("Did you attend a trucking school?", ["No", "Yes"])

    if attended_trucking_school == "Yes":
        tcol1, tcol2 = st.columns(2)
        with tcol1:
            ts_name = st.text_input("School Name", value=st.session_state.form_data.get("ts_name", ""))
            ts_city_state = st.text_input("City, State", value=st.session_state.form_data.get("ts_city_state", ""))
            ts_start = st.date_input("Start Date", key="ts_start",
                                      value=st.session_state.form_data.get("ts_start", date(2020, 1, 1)))
        with tcol2:
            ts_end = st.date_input("End Date", key="ts_end",
                                    value=st.session_state.form_data.get("ts_end", date(2020, 6, 1)))
            ts_graduated = st.selectbox("Did you graduate?", ["Yes", "No"], key="ts_grad")
            ts_fmcsa_subject = st.selectbox("Were you subject to FMCSA regulations while attending?",
                                             ["Yes", "No"], key="ts_fmcsa")
    else:
        ts_name = ts_city_state = ""
        ts_start = ts_end = None
        ts_graduated = ts_fmcsa_subject = "No"

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
            st.session_state.form_data.update({
                "highest_grade": highest_grade, "last_school": last_school,
                "attended_trucking_school": attended_trucking_school,
                "ts_name": ts_name, "ts_city_state": ts_city_state,
                "ts_start": ts_start, "ts_end": ts_end,
                "ts_graduated": ts_graduated, "ts_fmcsa_subject": ts_fmcsa_subject,
                "ref1": ref1, "ref2": ref2,
            })
            next_page()
            st.rerun()

# =========================================================================
# PAGE 6: FMCSR Disqualifications & Records
# =========================================================================
elif page == 6:
    st.subheader("FMCSR Disqualifications, Accident Record & Violations")

    st.markdown("#### DOT Disqualification Questions (49 CFR 391.15)")
    st.markdown("*These are DOT-specific safety questions required by federal regulation.*")

    disq_391_15 = st.selectbox(
        "Under FMCSR 391.15, are you currently disqualified from driving a commercial motor vehicle? [49 CFR 391.15]",
        ["No", "Yes"])

    disq_suspended = st.selectbox(
        "Has your license, permit, or privilege to drive ever been suspended or revoked for any reason? [49 CFR 391.21(b)(9)]",
        ["No", "Yes"])

    disq_denied = st.selectbox(
        "Have you ever been denied a license, permit, or privilege to operate a motor vehicle? [49 CFR 391.21(b)(9)]",
        ["No", "Yes"])

    disq_drug_test = st.selectbox(
        "Within the past two years, have you tested positive, or refused to test, on a pre-employment "
        "drug or alcohol test by an employer to whom you applied, but did not obtain, safety-sensitive "
        "transportation work covered by DOT agency drug and alcohol testing rules? [49 CFR 40.25(j)]",
        ["No", "Yes"])

    disq_convicted = st.selectbox(
        "In the past three (3) years, have you been convicted of any of the following offenses? [49 CFR 391.15]:\n"
        "• Driving a CMV with a BAC of .04 or more\n"
        "• Driving under the influence of alcohol as prescribed by state law\n"
        "• Refusal to undergo drug and alcohol testing\n"
        "• Driving a CMV under the influence of a Schedule I controlled substance\n"
        "• Transportation, possession, or unlawful use of controlled substances while driving for a motor carrier\n"
        "• Leaving the scene of an accident while operating a CMV\n"
        "• Any other felony involving the use of a CMV",
        ["No", "Yes"])

    st.markdown("---")
    st.markdown("#### Vehicle Accident Record")
    st.markdown("Were you involved in any accidents/incidents with any vehicle in the last 5 years (even if not at fault)?")

    has_accidents = st.selectbox("Any accidents to report?", ["No", "Yes"], key="has_acc")
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
                accidents_input.append({
                    "date": acc_date, "location": acc_location, "fatalities": acc_fatalities,
                    "injuries": acc_injuries, "hazmat": acc_hazmat, "description": acc_description,
                })
        st.session_state.accidents = accidents_input
    else:
        st.session_state.accidents = []

    st.markdown("---")
    st.markdown("#### Traffic Convictions / Violations")
    st.markdown("Have you had any moving violations or traffic convictions in the past 3 years?")

    has_violations = st.selectbox("Any violations to report?", ["No", "Yes"], key="has_viol")
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
                violations_input.append({
                    "date": viol_date, "location": viol_location,
                    "charge": viol_charge, "penalty": viol_penalty,
                })
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
            st.session_state.form_data.update({
                "disq_391_15": disq_391_15, "disq_suspended": disq_suspended,
                "disq_denied": disq_denied, "disq_drug_test": disq_drug_test,
                "disq_convicted": disq_convicted,
            })
            next_page()
            st.rerun()

# =========================================================================
# PAGE 7: Certifications & Signature
# =========================================================================
elif page == 7:
    st.subheader("Drug and Alcohol Policy — O/O & Independent Contractor Certification")

    st.markdown(f"""
    I certify that I have received a copy of, and have read, the Drug and Alcohol Policy.
    I understand that as a **condition of this independent contractor agreement**, I must comply
    with these guidelines and agree that I will remain medically qualified by these procedures.
    I also acknowledge that if I become disqualified as a driver for any reason, I have
    self-terminated my contract with {COMPANY_NAME}.
    """)

    drug_alcohol_cert = st.checkbox("I certify I have read and understand the Drug and Alcohol Policy *")

    st.markdown("---")
    st.subheader("Applicant Certification")
    st.markdown(f"""
    I certify that all information provided in this application is true and complete to the
    best of my knowledge. I understand that any misrepresentation or omission of facts may
    result in rejection of this application or termination of my independent contractor agreement
    with {COMPANY_NAME}.

    I authorize {COMPANY_NAME} to make such investigations and inquiries of my personal,
    contracting, financial, driving, and other related matters as may be necessary in arriving
    at a contracting decision. I understand that this application is not and is not intended to
    be a contract for services.

    I understand that engagement as an independent contractor with {COMPANY_NAME} is based
    on mutual agreement and allows either party to terminate the contractor relationship at any
    time, with or without cause or advance notice.

    I also understand that {COMPANY_NAME} reserves the right to require all independent
    contractors to submit to substance abuse testing in accordance with applicable federal
    and state regulations.
    """)

    applicant_cert = st.checkbox("I certify that all information in this application is true and complete *")

    st.markdown("---")
    st.subheader("Digital Signature")
    sig_full_name = st.text_input("Full Legal Name (typed signature) *",
                                   value=st.session_state.form_data.get("sig_full_name", ""))
    sig_date = st.date_input("Date", value=date.today(), disabled=True)

    st.info("By typing your full legal name above, you agree that this electronic signature is as "
            "legally binding as an ink signature. A submission timestamp will be recorded.")

    bcol1, bcol2 = st.columns(2)
    with bcol1:
        if st.button("← Back", key="p7_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        if st.button("Next → (FCRA Disclosure)", key="p7_next", use_container_width=True, type="primary"):
            missing = []
            if not drug_alcohol_cert: missing.append("Drug and Alcohol Policy certification")
            if not applicant_cert: missing.append("Applicant certification checkbox")
            if not sig_full_name: missing.append("Full Legal Name (typed signature)")
            if missing:
                _show_missing_fields(missing, "The following are required:")
            else:
                st.session_state.form_data.update({
                    "drug_alcohol_cert": drug_alcohol_cert,
                    "applicant_cert": applicant_cert,
                    "sig_full_name": sig_full_name,
                    "sig_date": sig_date.isoformat(),
                    "sig_timestamp": datetime.now().isoformat(),
                })
                next_page()
                st.rerun()

# =========================================================================
# PAGE 8: FCRA Disclosure (STANDALONE - Federal requirement)
# =========================================================================
elif page == 8:
    st.subheader("📄 Federal FCRA Disclosure — Standalone Document")
    st.warning("**IMPORTANT: Federal law (15 U.S.C. § 1681b) requires this disclosure be presented "
               "as a standalone document, separate from the application.**")

    st.markdown(f"""
    ### Disclosure Regarding Background Investigation

    {COMPANY_NAME} ("the Company") may obtain information about you from a third-party
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

    By acknowledging below, I authorize {COMPANY_NAME} to obtain a consumer report and/or
    investigative consumer report about me for contracting and independent contractor
    qualification purposes.
    """)

    fcra_acknowledge = st.checkbox("I acknowledge that I have read and understand the FCRA Disclosure above, "
                                    "have been given the opportunity to copy/print the Summary of Rights, and "
                                    "authorize the background investigation. *")

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
                st.rerun()

# =========================================================================
# PAGE 9: California Disclosure
# =========================================================================
elif page == 9:
    st.subheader("📄 California Disclosure Regarding Background Checks")
    st.warning("**This disclosure applies if you live or work in California.**")

    ca_applicable_default = st.session_state.form_data.get("ca_applicable", _default_california_applicability())
    ca_copy_default = st.session_state.form_data.get("ca_copy", False)

    ca_applicable = st.checkbox(
        "I live or work in California, so this California disclosure applies to me.",
        value=ca_applicable_default,
    )

    if ca_applicable:
        st.markdown(f"""
        ### California Disclosure

        {COMPANY_NAME} may obtain information about you from a consumer reporting agency
        for contracting purposes. Thus, you may be the subject of a consumer report and/or
        an investigative consumer report under California law. These reports may include
        information about your character, general reputation, personal characteristics,
        and mode of living.

        You may request the nature and scope of any investigative consumer report and may
        request a copy of any report obtained about you, where permitted by law.
        """)

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
                st.session_state.form_data["ca_disclosure_timestamp"] = datetime.now().isoformat() if ca_applicable and ca_disclosure_acknowledge else None
                st.session_state.form_data["ca_copy"] = ca_copy
                next_page()
                st.rerun()

# =========================================================================
# PAGE 10: PSP Disclosure (STANDALONE)
# =========================================================================
elif page == 10:
    st.subheader("📄 PSP Disclosure and Authorization — Standalone Document")
    st.warning("**This disclosure is presented as a standalone document as required by federal regulation.**")

    st.markdown(f"""
    ### Pre-Employment Screening Program (PSP) Disclosure

    In connection with your application for contracting with {COMPANY_NAME}, we may obtain
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

    By acknowledging below, I authorize {COMPANY_NAME} and its agents to access my
    PSP record from FMCSA in connection with my application to contract as an
    independent contractor driver.
    """)

    psp_acknowledge = st.checkbox("I acknowledge that I have read and understand the PSP Disclosure "
                                   "and Authorization above, and authorize access to my PSP record. *")

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
                st.rerun()

# =========================================================================
# PAGE 11: Clearinghouse Release (STANDALONE)
# =========================================================================
elif page == 11:
    st.subheader("📄 FMCSA Clearinghouse Release — Standalone Document")
    st.warning("**This disclosure is presented as a standalone document as required by federal regulation.**")

    st.markdown(f"""
    ### FMCSA Drug & Alcohol Clearinghouse Consent

    In accordance with 49 CFR Part 382, Subpart G, {COMPANY_NAME} is required to conduct
    a query of the FMCSA Drug and Alcohol Clearinghouse prior to contracting with any
    commercial motor vehicle (CMV) driver.

    ### What This Means

    By providing consent below, you authorize {COMPANY_NAME} to conduct:

    1. A **full query** of the FMCSA Clearinghouse to determine whether any drug or alcohol
       violation information exists about you.
    2. **Annual limited queries** for the duration of your independent contractor agreement
       with {COMPANY_NAME}.

    ### Your Responsibilities

    - You must register with the FMCSA Clearinghouse at https://clearinghouse.fmcsa.dot.gov
      and grant electronic consent for the full query.
    - A full query requires your separate electronic consent through the Clearinghouse system.

    ### Employment Verification Acknowledgment and Release (DOT Drug and Alcohol)

    I authorize the release of information from my Department of Transportation regulated
    drug and alcohol testing records by my previous employers/contractors listed in this
    application to {COMPANY_NAME} or its designated agents.
    """)

    clearinghouse_acknowledge = st.checkbox(
        "I acknowledge that I have read and understand the Clearinghouse Release, "
        "and I consent to the full and limited queries of the FMCSA Clearinghouse. *")

    inv_consumer_report = st.checkbox(
        "I understand and agree to the Investigative Consumer Report Disclosure.")

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
                st.rerun()

# =========================================================================
# PAGE 12: Review & Submit
# =========================================================================
elif page == 12:
    st.subheader("🧾 Review & Submit")
    submission_destination = get_submission_destination_summary(SUBMISSIONS_DIR)
    st.info(
        f"When you submit, a company copy will be saved to {submission_destination}. "
        "This MVP does not automatically email or send the application anywhere else yet."
    )

    with st.expander("Personal Information", expanded=True):
        _summary_item("Applicant", f"{st.session_state.form_data.get('first_name', '')} {st.session_state.form_data.get('last_name', '')}".strip())
        _summary_item("Date of birth", st.session_state.form_data.get("dob"))
        _summary_item("Address", f"{st.session_state.form_data.get('address', '')}, {st.session_state.form_data.get('city', '')}, {st.session_state.form_data.get('state', '')} {st.session_state.form_data.get('zip_code', '')}".strip(", "))
        _summary_item("Primary phone", st.session_state.form_data.get("primary_phone"))
        _summary_item("Email", st.session_state.form_data.get("email"))
        _summary_item("Emergency contact", f"{st.session_state.form_data.get('emergency_name', '')} / {st.session_state.form_data.get('emergency_phone', '')}")

    with st.expander("Company Questions & Experience"):
        _summary_item("Position", st.session_state.form_data.get("position"))
        _summary_item("Applying location", st.session_state.form_data.get("applying_location"))
        _summary_item("Currently employed/contracted elsewhere", st.session_state.form_data.get("currently_employed"))
        _summary_item("Previously contracted here", st.session_state.form_data.get("worked_here_before"))
        _summary_item("TWIC card", st.session_state.form_data.get("twic_card"))
        _summary_item("Referral source", st.session_state.form_data.get("referral_source"))
        _summary_item("License entries", len(st.session_state.licenses), default="0")
        _summary_item("Employment history entries", len(st.session_state.employers), default="0")

    with st.expander("Education, Safety, and Records"):
        _summary_item("Highest grade completed", st.session_state.form_data.get("highest_grade"))
        _summary_item("Attended trucking school", st.session_state.form_data.get("attended_trucking_school"))
        _summary_item("Accidents reported", len(st.session_state.accidents), default="0")
        _summary_item("Violations reported", len(st.session_state.violations), default="0")
        _summary_item("Currently disqualified", st.session_state.form_data.get("disq_391_15"))
        _summary_item("Suspended or revoked license history", st.session_state.form_data.get("disq_suspended"))

    with st.expander("Disclosures & Acknowledgments"):
        _summary_item("Drug & alcohol policy", st.session_state.form_data.get("drug_alcohol_cert"))
        _summary_item("Applicant certification", st.session_state.form_data.get("applicant_cert"))
        _summary_item("FCRA acknowledged", st.session_state.form_data.get("fcra_acknowledge"))
        if st.session_state.form_data.get("ca_applicable"):
            _summary_item("California disclosure acknowledged", st.session_state.form_data.get("ca_disclosure_acknowledge"))
        else:
            _summary_item("California disclosure", "Not applicable")
        _summary_item("Consumer copy requested", st.session_state.form_data.get("ca_copy"))
        _summary_item("PSP acknowledged", st.session_state.form_data.get("psp_acknowledge"))
        _summary_item("Clearinghouse acknowledged", st.session_state.form_data.get("clearinghouse_acknowledge"))
        _summary_item("Investigative consumer report acknowledged", st.session_state.form_data.get("inv_consumer_report"))

    st.markdown("---")
    st.markdown("### What happens when you submit")
    st.markdown(
        "1. A timestamped submission bundle is created for this applicant.\n"
        f"2. The app saves `submission.json` plus PDF copies of the application and disclosures to {submission_destination}.\n"
        "3. The applicant can manually download copies from the confirmation page.\n"
        "4. **Nothing is automatically emailed or sent anywhere else yet.**"
    )

    review_confirm = st.checkbox(
        "I reviewed the information above and I am ready to submit this application.",
        value=st.session_state.form_data.get("review_confirm", False),
    )

    bcol1, bcol2 = st.columns(2)
    with bcol1:
        if st.button("← Back", key="p12_back", use_container_width=True):
            prev_page()
            st.rerun()
    with bcol2:
        if st.button("✅ Submit Application", key="p12_submit", use_container_width=True, type="primary"):
            if not review_confirm:
                st.error("Please confirm that you reviewed the application before submitting.")
            else:
                st.session_state.form_data["review_confirm"] = True
                st.session_state.form_data["final_submission_timestamp"] = datetime.now().isoformat()
                st.session_state.submitted = True
                st.rerun()

# =========================================================================
# SUBMISSION COMPLETE
# =========================================================================
if st.session_state.submitted:
    st.session_state.current_page = 99  # prevent form pages from showing

    if st.session_state.submission_artifacts is None:
        try:
            st.session_state.submission_artifacts = build_submission_artifacts()
        except Exception as e:
            st.session_state.submission_save_error = f"Could not generate submission PDFs: {e}"

    if st.session_state.submission_artifacts is not None and st.session_state.saved_submission_dir is None:
        try:
            saved_result = save_submission_bundle(st.session_state.submission_artifacts)
            st.session_state.saved_submission_dir = saved_result.get("location_label")
            warnings = saved_result.get("warnings", [])
            if warnings:
                st.session_state.submission_save_notice = "\n".join(warnings)
        except Exception as e:
            st.session_state.submission_save_error = f"Could not save submission files: {e}"

    st.balloons()
    st.success("### ✅ Application Submitted Successfully!")
    st.markdown(f"""
    Thank you, **{st.session_state.form_data.get('first_name', '')} {st.session_state.form_data.get('last_name', '')}**!

    Your application to {COMPANY_NAME} has been received.
    A confirmation has been created with the following details:

    - **Submission Timestamp:** {st.session_state.form_data.get('final_submission_timestamp', datetime.now().isoformat())}
    - **Application Signature:** {st.session_state.form_data.get('sig_full_name', 'N/A')}
    """)

    if st.session_state.saved_submission_dir:
        st.info(f"A company copy was saved to: `{st.session_state.saved_submission_dir}`")
    elif st.session_state.submission_save_error:
        st.warning(st.session_state.submission_save_error)

    if st.session_state.submission_save_notice:
        st.warning(st.session_state.submission_save_notice)

    st.caption("This app stores the submission using the configured storage backend and offers manual downloads below. It does not auto-email or send the application anywhere else yet.")

    st.markdown("---")
    st.subheader("Download Your Application PDF")

    try:
        pdf_bytes = st.session_state.submission_artifacts["application_pdf"] if st.session_state.submission_artifacts else generate_application_pdf(
            st.session_state.form_data,
            st.session_state.employers,
            st.session_state.licenses,
            st.session_state.accidents,
            st.session_state.violations,
        )
        st.download_button(
            label="📥 Download Application PDF",
            data=pdf_bytes,
            file_name=f"prestige_application_{st.session_state.form_data.get('last_name', 'driver')}_{date.today().isoformat()}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
    except Exception as e:
        st.error(f"PDF generation error: {e}")

    # Standalone disclosure PDFs
    st.markdown("---")
    st.subheader("Standalone Disclosure Documents")

    dcol1, dcol2, dcol3 = st.columns(3)
    with dcol1:
        try:
            fcra_pdf = st.session_state.submission_artifacts["fcra_pdf"] if st.session_state.submission_artifacts else generate_fcra_pdf(st.session_state.form_data)
            st.download_button(
                label="📥 FCRA Disclosure PDF",
                data=fcra_pdf,
                file_name="FCRA_Disclosure_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"FCRA PDF error: {e}")

    with dcol2:
        try:
            psp_pdf = st.session_state.submission_artifacts["psp_pdf"] if st.session_state.submission_artifacts else generate_psp_pdf(st.session_state.form_data)
            st.download_button(
                label="📥 PSP Disclosure PDF",
                data=psp_pdf,
                file_name="PSP_Disclosure_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"PSP PDF error: {e}")

    with dcol3:
        try:
            ch_pdf = st.session_state.submission_artifacts["clearinghouse_pdf"] if st.session_state.submission_artifacts else generate_clearinghouse_pdf(st.session_state.form_data)
            st.download_button(
                label="📥 Clearinghouse Release PDF",
                data=ch_pdf,
                file_name="Clearinghouse_Release_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"Clearinghouse PDF error: {e}")

    if st.session_state.submission_artifacts and st.session_state.submission_artifacts.get("california_pdf"):
        try:
            st.download_button(
                label="📥 California Disclosure PDF",
                data=st.session_state.submission_artifacts["california_pdf"],
                file_name="California_Disclosure_Standalone.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as e:
            st.error(f"California PDF error: {e}")

    if st.button("🔄 Start New Application"):
        for key in list(st.session_state.keys()):
            del st.session_state[key]
        st.rerun()
