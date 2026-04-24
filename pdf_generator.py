"""PDF generation helpers for company-specific driver application packets."""

from datetime import date
from io import BytesIO

from fpdf import FPDF

from config import EQUIPMENT_TYPES
from runtime_context import get_active_company_profile


class CompanyPDF(FPDF):
    """Base PDF class with runtime company header/footer."""

    def __init__(self, company):
        super().__init__()
        self.company = company

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(26, 60, 110)
        self.cell(0, 8, self.company.name, align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 8)
        self.set_text_color(100, 100, 100)
        contact_line = " | ".join(
            part for part in [self.company.address, self.company.city_state_zip, self.company.phone] if part
        )
        if contact_line:
            self.cell(0, 4, contact_line, align="C", new_x="LMARGIN", new_y="NEXT")
        if self.company.email:
            self.cell(0, 4, self.company.email, align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(26, 60, 110)
        self.line(10, self.get_y() + 2, 200, self.get_y() + 2)
        self.ln(6)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    def section_title(self, title):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(26, 60, 110)
        self.set_fill_color(230, 236, 245)
        self.cell(0, 8, f"  {title}", fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def field_row(self, label, value):
        label_w = 70
        line_h = 6
        val_str = str(value) if value else ""

        x_start = self.l_margin
        y_start = self.get_y()
        val_w = self.w - self.r_margin - (x_start + label_w)

        # Label (bold, may wrap onto multiple lines for long labels)
        self.set_xy(x_start, y_start)
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(60, 60, 60)
        label_lines = self.multi_cell(
            label_w, line_h, label, dry_run=True, output="LINES"
        )
        label_line_count = max(1, len(label_lines))
        self.multi_cell(label_w, line_h, label, new_x="RIGHT", new_y="TOP")
        label_bottom = y_start + line_h * label_line_count

        # Value (positioned to the right of the label, also wraps if long)
        self.set_font("Helvetica", "", 9)
        self.set_text_color(0, 0, 0)
        self.multi_cell(val_w, line_h, val_str, new_x="LMARGIN", new_y="NEXT")
        value_bottom = self.get_y()

        # Advance past whichever side took more vertical space
        self.set_y(max(label_bottom, value_bottom))

    def field_row_wide(self, label, value):
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(60, 60, 60)
        self.cell(0, 6, label, new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(0, 0, 0)
        self.multi_cell(0, 5, str(value) if value else "")
        self.ln(1)


def _safe(data, key, default=""):
    val = data.get(key, default)
    if val is None:
        return default
    if isinstance(val, date):
        return val.isoformat()
    return str(val)


def _bool_text(value, default=""):
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if value in {"Yes", "No"}:
        return value
    return default


def _selected_experience_rows(form_data):
    rows = []
    for eq_type in EQUIPMENT_TYPES:
        key_prefix = f"exp_{eq_type.lower().replace(' ', '_').replace('-', '_')}"
        truck_type = _safe(form_data, f"{key_prefix}_truck_type_other") or _safe(form_data, f"{key_prefix}_truck_type")
        exp_type = _safe(form_data, f"{key_prefix}_equipment_type_other") or _safe(form_data, f"{key_prefix}_equipment_type") or _safe(form_data, f"{key_prefix}_type")
        trailer_length = _safe(form_data, f"{key_prefix}_trailer_length")
        exp_miles = _safe(form_data, f"{key_prefix}_miles")
        exp_dates = _safe(form_data, f"{key_prefix}_dates")
        exp_notes = _safe(form_data, f"{key_prefix}_notes")
        if any([truck_type, exp_type, trailer_length, exp_miles, exp_dates, exp_notes]):
            rows.append(
                {
                    "label": eq_type,
                    "truck_type": truck_type,
                    "detail": exp_type,
                    "trailer_length": trailer_length,
                    "miles": exp_miles,
                    "dates": exp_dates,
                    "notes": exp_notes,
                }
            )
    return rows


def _previous_addresses(form_data):
    stored = form_data.get("previous_addresses")
    if isinstance(stored, list) and stored:
        return [entry for entry in stored if isinstance(entry, dict)]
    legacy = {
        "address": _safe(form_data, "prev_address"),
        "city": _safe(form_data, "prev_city"),
        "state": _safe(form_data, "prev_state"),
        "zip_code": _safe(form_data, "prev_zip"),
        "from_date": "",
        "to_date": "",
    }
    if any([legacy["address"], legacy["city"], legacy["state"], legacy["zip_code"]]):
        return [legacy]
    return []


def _references(form_data):
    stored = form_data.get("references")
    if isinstance(stored, list) and stored:
        return [entry for entry in stored if isinstance(entry, dict)]
    refs = []
    for key in ("ref1", "ref2"):
        raw = _safe(form_data, key)
        if raw:
            refs.append({"name": raw, "phone": "", "relationship": "", "city": "", "state": ""})
    return refs


def _mvr_rows(form_data):
    return [
        ("Driving During Suspension/Revocation:", _safe(form_data, "mvr_suspension_conviction")),
        ("Driving Without Valid License:", _safe(form_data, "mvr_no_valid_license")),
        ("Alcohol/Controlled Substance Offense:", _safe(form_data, "mvr_alcohol_controlled_substance")),
        ("Illegal Substance While On Duty:", _safe(form_data, "mvr_illegal_substance_on_duty")),
        ("Reckless/Careless Driving:", _safe(form_data, "mvr_reckless_driving")),
        ("Any DOT Positive/Refusal:", _safe(form_data, "mvr_any_dot_test_positive")),
    ]


def generate_application_pdf(form_data, employers, licenses, accidents, violations):
    """Generate the main application PDF."""
    company = get_active_company_profile()
    company_name = form_data.get("company_name") or company.name
    pdf = CompanyPDF(company)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(26, 60, 110)
    pdf.cell(0, 10, "Independent Contractor Driver Application", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # --- Personal Information ---
    pdf.section_title("Personal Information")
    pdf.field_row("Full Name:", f"{_safe(form_data, 'first_name')} {_safe(form_data, 'middle_name')} {_safe(form_data, 'last_name')}")
    pdf.field_row("Date of Birth:", _safe(form_data, "dob"))
    # SSN intentionally omitted from the PDF; it lives only in the per-applicant
    # CSV that ships in the same notification email, so it never appears twice
    # in the safety mailbox.
    pdf.field_row("Address:", _safe(form_data, "address"))
    pdf.field_row("City, State, Zip:", f"{_safe(form_data, 'city')}, {_safe(form_data, 'state')} {_safe(form_data, 'zip_code')}")
    pdf.field_row("Country:", _safe(form_data, "country", "United States"))
    pdf.field_row("Primary Phone:", _safe(form_data, "primary_phone"))
    pdf.field_row("Cell Phone / Text Number:", _safe(form_data, "cell_phone"))
    mobile_carrier = _safe(form_data, "mobile_carrier_other") or _safe(form_data, "mobile_carrier")
    pdf.field_row("Mobile Carrier / Provider:", mobile_carrier)
    pdf.field_row("Email:", _safe(form_data, "email"))
    pdf.field_row("Preferred Method of Contact:", _safe(form_data, "preferred_contact"))
    pdf.field_row("Best Time to Contact:", _safe(form_data, "best_time"))
    pdf.field_row("Text Message Consent:", _bool_text(form_data.get("text_consent")))
    pdf.field_row("Emergency Contact:", f"{_safe(form_data, 'emergency_name')} - {_safe(form_data, 'emergency_phone')} ({_safe(form_data, 'emergency_relationship')})")
    pdf.field_row("Emergency Contact Address:", _safe(form_data, "emergency_address"))

    if _safe(form_data, "resided_3_years") == "No":
        for index, previous_address in enumerate(_previous_addresses(form_data), start=1):
            date_range = " to ".join(part for part in [_safe(previous_address, "from_date"), _safe(previous_address, "to_date")] if part)
            suffix = f" ({date_range})" if date_range else ""
            pdf.field_row(
                f"Previous Address #{index}:",
                f"{_safe(previous_address, 'address')}, {_safe(previous_address, 'city')}, {_safe(previous_address, 'state')} {_safe(previous_address, 'zip_code')}{suffix}",
            )

    pdf.ln(4)

    # --- Company Questions ---
    pdf.section_title("Company Questions")
    pdf.field_row("Position Applying For:", _safe(form_data, "position"))
    preferred_office = _safe(form_data, "preferred_office") or _safe(form_data, "applying_location")
    pdf.field_row("Preferred Office for Onboarding:", preferred_office)
    pdf.field_row("Legally Eligible to Provide Contracted Services in the United States:", _safe(form_data, "eligible_us"))
    pdf.field_row("Read, Write, and Speak English:", _safe(form_data, "read_english"))
    pdf.field_row("Currently Employed/Contracted Elsewhere:", _safe(form_data, "currently_employed"))
    if _safe(form_data, "currently_employed") == "No":
        pdf.field_row("Last Employment/Contract End Date:", _safe(form_data, "last_employment_end"))
    pdf.field_row("Previously Contracted Here:", _safe(form_data, "worked_here_before"))
    pdf.field_row("Referral Source:", _safe(form_data, "referral_source"))
    referral_name = _safe(form_data, "referral_name")
    if referral_name:
        pdf.field_row("Referral Details:", referral_name)
    pdf.field_row("Relatives Contracted Here:", _safe(form_data, "relatives_here"))
    if _safe(form_data, "relatives_here") == "Yes":
        pdf.field_row("Relative Names:", _safe(form_data, "relatives_names"))
    if _safe(form_data, "worked_here_before") == "Yes":
        pdf.field_row_wide("Previous Contract Details:", _safe(form_data, "prev_dates"))
    pdf.field_row("Known by Another Name:", _safe(form_data, "known_other_name"))
    if _safe(form_data, "known_other_name") == "Yes":
        pdf.field_row("Other Name(s):", _safe(form_data, "other_name"))
    pdf.field_row("Safe Driving Awards:", _safe(form_data, "safe_driving_awards"))
    if _safe(form_data, "position") == "Owner Operator":
        pdf.field_row("Owner Op Equipment:", _safe(form_data, "equipment_description"))
        pdf.field_row("  Year/Make/Model:", f"{_safe(form_data, 'equipment_year')} {_safe(form_data, 'equipment_make')} {_safe(form_data, 'equipment_model')}")
        pdf.field_row("  Color:", _safe(form_data, "equipment_color"))
        pdf.field_row("  VIN:", _safe(form_data, "equipment_vin"))
        pdf.field_row("  Weight:", _safe(form_data, "equipment_weight"))
        pdf.field_row("  Mileage:", _safe(form_data, "equipment_mileage"))
        pdf.field_row("  Fifth Wheel Height:", _safe(form_data, "fifth_wheel_height"))
    pdf.ln(4)

    # --- Driving Experience ---
    pdf.section_title("Driving Experience")
    experience_rows = _selected_experience_rows(form_data)
    if not experience_rows:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 6, "No driving experience details provided", new_x="LMARGIN", new_y="NEXT")
    else:
        for row in experience_rows:
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 6, row["label"], new_x="LMARGIN", new_y="NEXT")
            pdf.field_row("  Truck Type:", row["truck_type"])
            pdf.field_row("  Equipment Detail:", row["detail"])
            pdf.field_row("  Trailer Length:", row["trailer_length"])
            pdf.field_row("  Total Miles:", row["miles"])
            pdf.field_row("  Date Range:", row["dates"])
            if row["notes"]:
                pdf.field_row("  Notes:", row["notes"])
            pdf.ln(1)
    pdf.ln(3)

    # --- Licenses ---
    pdf.section_title("Licenses & Endorsements")
    pdf.field_row("TWIC Card:", _safe(form_data, "twic_card"))
    if _safe(form_data, "twic_card") == "Yes":
        pdf.field_row("TWIC Expiration:", _safe(form_data, "twic_expiration"))
    pdf.field_row("HazMat Endorsement:", _safe(form_data, "hazmat_endorsement"))
    if _safe(form_data, "hazmat_endorsement") == "Yes":
        pdf.field_row("HazMat Expiration:", _safe(form_data, "hazmat_expiration"))
    pdf.ln(2)
    for i, lic in enumerate(licenses):
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 6, f"License #{i+1}", new_x="LMARGIN", new_y="NEXT")
        pdf.field_row("  License Number:", lic.get("number", ""))
        pdf.field_row("  State:", lic.get("state", ""))
        pdf.field_row("  Country:", lic.get("country", ""))
        pdf.field_row("  Class:", lic.get("class", ""))
        pdf.field_row("  Current License:", lic.get("current_license", ""))
        pdf.field_row("  Expiration:", _safe(lic, "expiration"))
        pdf.field_row("  Med Card Exp:", _safe(lic, "med_card_exp"))
        pdf.field_row("  CDL:", lic.get("is_cdl", ""))
        pdf.field_row("  Tanker:", lic.get("tanker", ""))
        pdf.field_row("  HAZMAT:", lic.get("hazmat", ""))
        if lic.get("hazmat") == "Yes":
            pdf.field_row("  HAZMAT Exp:", _safe(lic, "hazmat_exp"))
        pdf.field_row("  Doubles/Triples:", lic.get("doubles", ""))
        pdf.field_row("  X Endorsement:", lic.get("x_endorsement", ""))
        pdf.field_row("  Other Endorsement:", lic.get("other_endorsement", ""))
        pdf.ln(2)
    pdf.ln(2)

    # --- Employment History ---
    pdf.section_title("Employment / Contracting History (10 Years)")
    for i, emp in enumerate(employers):
        if pdf.get_y() > 230:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(26, 60, 110)
        company_name = emp.get("company", f"Employer #{i+1}")
        pdf.cell(0, 7, f"{i+1}. {company_name}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.field_row("  Address:", f"{emp.get('address', '')}, {emp.get('city_state', '')}")
        pdf.field_row("  Country:", emp.get("country", ""))
        pdf.field_row("  Phone:", emp.get("phone", ""))
        pdf.field_row("  Position:", emp.get("position", ""))
        pdf.field_row("  Start Date:", _safe(emp, "start"))
        pdf.field_row("  End Date:", _safe(emp, "end"))
        pdf.field_row("  Reason for Leaving:", emp.get("reason", ""))
        pdf.field_row("  Pay Range:", emp.get("pay_range", ""))
        pdf.field_row("  Terminated/Discharged:", emp.get("terminated", ""))
        pdf.field_row("  Current:", emp.get("current", ""))
        pdf.field_row("  May Contact:", emp.get("contact_ok", ""))
        pdf.field_row("  Operated CMV:", emp.get("cmv", ""))
        pdf.field_row("  FMCSA Subject:", emp.get("fmcsa", ""))
        pdf.field_row("  DOT Testing:", emp.get("dot_testing", ""))
        if emp.get("cmv") == "Yes":
            pdf.field_row("  Areas Driven:", emp.get("areas", ""))
            pdf.field_row("  Miles/Week:", emp.get("miles", ""))
            pdf.field_row("  Truck Type:", emp.get("truck", ""))
            pdf.field_row("  Trailer Type:", emp.get("trailer", ""))
            pdf.field_row("  Trailer Length:", emp.get("trailer_len", ""))
        pdf.ln(3)

    # --- Education ---
    pdf.section_title("Education & Trucking School")
    pdf.field_row("Highest Grade:", _safe(form_data, "highest_grade"))
    pdf.field_row("Last School:", _safe(form_data, "last_school"))
    pdf.field_row("Attended Trucking School:", _safe(form_data, "attended_trucking_school"))
    if _safe(form_data, "attended_trucking_school") == "Yes":
        pdf.field_row("Trucking School:", _safe(form_data, "ts_name"))
        pdf.field_row("  Location:", _safe(form_data, "ts_city_state"))
        pdf.field_row("  Dates:", f"{_safe(form_data, 'ts_start')} to {_safe(form_data, 'ts_end')}")
        pdf.field_row("  Graduated:", _safe(form_data, "ts_graduated"))
        pdf.field_row("  FMCSA Subject:", _safe(form_data, "ts_fmcsa_subject"))
    for index, reference in enumerate(_references(form_data), start=1):
        pdf.field_row(
            f"Reference #{index}:",
            ", ".join(
                part
                for part in [
                    _safe(reference, "name"),
                    ", ".join(part for part in [_safe(reference, "city"), _safe(reference, "state")] if part),
                    _safe(reference, "phone"),
                    _safe(reference, "relationship"),
                ]
                if part
            ),
        )
    pdf.ln(4)

    # --- FMCSR Disqualifications ---
    pdf.section_title("FMCSR Disqualification Questions")
    pdf.field_row("Currently Disqualified [391.15]:", _safe(form_data, "disq_391_15"))
    pdf.field_row("License Suspended/Revoked:", _safe(form_data, "disq_suspended"))
    pdf.field_row("License Denied:", _safe(form_data, "disq_denied"))
    pdf.field_row("Failed Pre-Employment Test:", _safe(form_data, "disq_drug_test"))
    pdf.field_row("Convicted of DOT Offenses:", _safe(form_data, "disq_convicted"))
    convicted_which = form_data.get("disq_convicted_which") or []
    if convicted_which:
        pdf.field_row_wide("  Offense(s) disclosed:", "; ".join(convicted_which))
    convicted_details = form_data.get("disq_convicted_details") or ""
    if convicted_details:
        pdf.field_row_wide("  Details:", convicted_details)
    pdf.ln(4)

    pdf.section_title("Motor Vehicle Record")
    for label, value in _mvr_rows(form_data):
        pdf.field_row(label, value)
    pdf.ln(4)

    # --- Accident Record ---
    pdf.section_title("Vehicle Accident Record (5 Years)")
    if not accidents:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 6, "No Accidents Reported", new_x="LMARGIN", new_y="NEXT")
    else:
        for i, acc in enumerate(accidents):
            pdf.field_row(f"  Accident #{i+1} Date:", _safe(acc, "date"))
            pdf.field_row("  Location:", acc.get("location", ""))
            pdf.field_row("  Fatalities:", str(acc.get("fatalities", 0)))
            pdf.field_row("  Injuries:", str(acc.get("injuries", 0)))
            pdf.field_row("  Hazmat Spill:", acc.get("hazmat", "No"))
            pdf.field_row_wide("  Description:", acc.get("description", ""))
    pdf.ln(2)

    # --- Violations ---
    pdf.section_title("Traffic Convictions / Violations (3 Years)")
    if not violations:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 6, "No Violations Reported", new_x="LMARGIN", new_y="NEXT")
    else:
        for i, viol in enumerate(violations):
            pdf.field_row(f"  Violation #{i+1} Date:", _safe(viol, "date"))
            pdf.field_row("  Location:", viol.get("location", ""))
            pdf.field_row("  Charge:", viol.get("charge", ""))
            pdf.field_row("  In Commercial Vehicle:", viol.get("in_commercial_vehicle", ""))
            pdf.field_row("  Fined:", viol.get("fined", ""))
            pdf.field_row("  License Suspended:", viol.get("license_suspended", ""))
            pdf.field_row("  License Revoked:", viol.get("license_revoked", ""))
            pdf.field_row("  Fine Amount:", viol.get("fine_amount", ""))
            pdf.field_row("  Penalty:", viol.get("penalty", ""))
            if viol.get("comments"):
                pdf.field_row_wide("  Comments:", viol.get("comments", ""))
    pdf.ln(4)

    # --- Disclosures & Authorizations ---
    pdf.section_title("Disclosures & Authorizations")
    pdf.field_row("FCRA Acknowledged:", _bool_text(form_data.get("fcra_acknowledge")))
    pdf.field_row("FCRA Timestamp:", _safe(form_data, "fcra_timestamp"))
    pdf.field_row("California Disclosure Applies:", _bool_text(form_data.get("ca_applicable")))
    if form_data.get("ca_applicable"):
        pdf.field_row("California Disclosure Acknowledged:", _bool_text(form_data.get("ca_disclosure_acknowledge")))
        pdf.field_row("California Timestamp:", _safe(form_data, "ca_disclosure_timestamp"))
    pdf.field_row("Consumer Copy Requested:", _bool_text(form_data.get("ca_copy")))
    pdf.field_row("PSP Acknowledged:", _bool_text(form_data.get("psp_acknowledge")))
    pdf.field_row("PSP Timestamp:", _safe(form_data, "psp_timestamp"))
    pdf.field_row("Clearinghouse Acknowledged:", _bool_text(form_data.get("clearinghouse_acknowledge")))
    pdf.field_row("Clearinghouse Timestamp:", _safe(form_data, "clearinghouse_timestamp"))
    pdf.field_row("Investigative Consumer Report:", _bool_text(form_data.get("inv_consumer_report")))
    pdf.ln(4)

    # --- Signature Block ---
    pdf.section_title("Applicant Certification & Signature")
    pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(0, 4,
        f"I certify that all information provided in this application is true and complete. "
        f"I authorize {company_name} to make investigations and inquiries as necessary. "
        f"I understand this is not a contract for services. Engagement as an independent contractor "
        f"is based on mutual agreement and allows either party to terminate at any time.")
    pdf.ln(4)

    pdf.field_row("Digital Signature:", _safe(form_data, "sig_full_name"))
    pdf.field_row("Signature Date:", _safe(form_data, "sig_date"))
    pdf.field_row("Timestamp:", _safe(form_data, "sig_timestamp"))
    pdf.field_row("Drug and Alcohol Policy Acknowledged:", _bool_text(form_data.get("drug_alcohol_cert")))
    pdf.field_row("Applicant Certification Acknowledged:", _bool_text(form_data.get("applicant_cert")))

    # Output
    buf = BytesIO()
    buf.write(pdf.output())
    buf.seek(0)
    return buf.getvalue()


def generate_fcra_pdf(form_data):
    """Generate standalone FCRA Disclosure PDF (federal requirement)."""
    company = get_active_company_profile()
    company_name = form_data.get("company_name") or company.name
    pdf = CompanyPDF(company)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(26, 60, 110)
    pdf.cell(0, 10, "STANDALONE DOCUMENT", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, "Federal FCRA Disclosure & Authorization", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(180, 0, 0)
    pdf.cell(0, 5, "This document is presented separately as required by 15 U.S.C. Section 1681b.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Disclosure Regarding Background Investigation", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5,
        f'{company_name} ("the Company") may obtain information about you from a third-party '
        f'consumer reporting agency for contracting purposes. Thus, you may be the subject of a '
        f'"consumer report" and/or an "investigative consumer report" which may include information '
        f'about your character, general reputation, personal characteristics, and/or mode of living, '
        f'and which can involve personal interviews with sources such as your neighbors, friends, or '
        f'associates. These reports may contain information regarding your credit history, criminal '
        f'history, social security verification, motor vehicle records ("driving records"), '
        f'verification of your education or contracting history, or other background checks.')
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Summary of Your Rights Under the Fair Credit Reporting Act", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    rights = [
        "You have the right to request and obtain all information about you in the files of a consumer reporting agency.",
        "You have the right to know if information in your file has been used against you.",
        "You have the right to dispute inaccurate or incomplete information.",
        "You have the right to have inaccurate, incomplete, or unverifiable information corrected or deleted.",
        "You have the right to have outdated negative information excluded from your report.",
        "You have the right to seek damages from violators.",
    ]
    for right in rights:
        pdf.cell(5)
        pdf.cell(0, 5, f"  - {right}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "For more information, visit www.consumerfinance.gov/learnmore", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Authorization", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5,
        f'By signing below, I authorize {company_name} to obtain a consumer report and/or '
        f'investigative consumer report about me for contracting and independent contractor '
        f'qualification purposes.')
    pdf.ln(8)

    # Signature block
    pdf.section_title("Acknowledgment")
    pdf.field_row("Applicant Name:", f"{_safe(form_data, 'first_name')} {_safe(form_data, 'last_name')}")
    pdf.field_row("Digital Signature:", _safe(form_data, "sig_full_name"))
    pdf.field_row("Acknowledged:", "Yes" if form_data.get("fcra_acknowledge") else "No")
    pdf.field_row("Timestamp:", _safe(form_data, "fcra_timestamp"))

    buf = BytesIO()
    buf.write(pdf.output())
    buf.seek(0)
    return buf.getvalue()


def generate_psp_pdf(form_data):
    """Generate standalone PSP Disclosure PDF."""
    company = get_active_company_profile()
    company_name = form_data.get("company_name") or company.name
    pdf = CompanyPDF(company)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(26, 60, 110)
    pdf.cell(0, 10, "STANDALONE DOCUMENT", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, "PSP Disclosure and Authorization", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(180, 0, 0)
    pdf.cell(0, 5, "This document is presented separately as required by federal regulation.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Pre-Employment Screening Program (PSP) Disclosure", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5,
        f'In connection with your application for contracting with {company_name}, we may obtain '
        f'one or more reports from the Federal Motor Carrier Safety Administration (FMCSA) '
        f'Pre-Employment Screening Program (PSP) regarding your safety record.\n\n'
        f'The PSP report will contain your crash and inspection history from the FMCSA\'s '
        f'Motor Carrier Management Information System (MCMIS) for the preceding five (5) years '
        f'of crash data and three (3) years of inspection data.')
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Your Rights", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    psp_rights = [
        "You have the right to review the PSP report before any adverse action is taken.",
        "You may obtain a copy of the report by contacting FMCSA.",
        "You may challenge the accuracy of any information contained in the report.",
        "You may request correction of inaccurate information through the DataQs system at https://dataqs.fmcsa.dot.gov.",
    ]
    for right in psp_rights:
        pdf.cell(5)
        pdf.cell(0, 5, f"  - {right}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Authorization", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5,
        f'By signing below, I authorize {company_name} and its agents to access my PSP record '
        f'from FMCSA in connection with my application to contract as an independent contractor driver.')
    pdf.ln(8)

    pdf.section_title("Acknowledgment")
    pdf.field_row("Applicant Name:", f"{_safe(form_data, 'first_name')} {_safe(form_data, 'last_name')}")
    pdf.field_row("Digital Signature:", _safe(form_data, "sig_full_name"))
    pdf.field_row("Acknowledged:", "Yes" if form_data.get("psp_acknowledge") else "No")
    pdf.field_row("Timestamp:", _safe(form_data, "psp_timestamp"))

    buf = BytesIO()
    buf.write(pdf.output())
    buf.seek(0)
    return buf.getvalue()


def generate_california_disclosure_pdf(form_data):
    """Generate California background check disclosure PDF."""
    company = get_active_company_profile()
    company_name = form_data.get("company_name") or company.name
    pdf = CompanyPDF(company)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(26, 60, 110)
    pdf.cell(0, 10, "STANDALONE DOCUMENT", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, "California Disclosure Regarding Background Checks", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(180, 0, 0)
    pdf.cell(0, 5, "This document is presented separately for California applicants and workers.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(
        0,
        5,
        f"{company_name} may obtain information about you from a consumer reporting agency for contracting purposes. "
        f"Thus, you may be the subject of a consumer report or investigative consumer report under California law. "
        f"These reports may include information regarding your character, general reputation, personal characteristics, "
        f"and mode of living. The Company may use such information to evaluate your qualifications to provide contracted services.",
    )
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Your California Rights", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    rights = [
        "You may request a copy of any consumer report obtained about you.",
        "You may request the nature and scope of any investigative consumer report.",
        "You may dispute inaccurate information with the consumer reporting agency.",
        "You may receive a summary of your rights under California Civil Code section 1786.22.",
    ]
    for right in rights:
        pdf.cell(5)
        pdf.cell(0, 5, f"  - {right}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.section_title("Acknowledgment")
    pdf.field_row("Applicant Name:", f"{_safe(form_data, 'first_name')} {_safe(form_data, 'last_name')}")
    pdf.field_row("California Disclosure Applies:", "Yes" if form_data.get("ca_applicable") else "No")
    pdf.field_row("Digital Signature:", _safe(form_data, "sig_full_name"))
    pdf.field_row("Acknowledged:", "Yes" if form_data.get("ca_disclosure_acknowledge") else "No")
    pdf.field_row("Timestamp:", _safe(form_data, "ca_disclosure_timestamp"))
    pdf.field_row("Consumer Copy Requested:", "Yes" if form_data.get("ca_copy") else "No")

    buf = BytesIO()
    buf.write(pdf.output())
    buf.seek(0)
    return buf.getvalue()


def generate_clearinghouse_pdf(form_data):
    """Generate standalone Clearinghouse Release PDF."""
    company = get_active_company_profile()
    company_name = form_data.get("company_name") or company.name
    pdf = CompanyPDF(company)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(26, 60, 110)
    pdf.cell(0, 10, "STANDALONE DOCUMENT", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 10, "FMCSA Clearinghouse Release", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(180, 0, 0)
    pdf.cell(0, 5, "This document is presented separately as required by federal regulation.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "FMCSA Drug & Alcohol Clearinghouse Consent", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5,
        f'In accordance with 49 CFR Part 382, Subpart G, {company_name} is required to conduct '
        f'a query of the FMCSA Drug and Alcohol Clearinghouse prior to contracting with any '
        f'commercial motor vehicle (CMV) driver.\n\n'
        f'By providing consent, the applicant authorizes {company_name} to conduct:\n'
        f'1. A full query of the FMCSA Clearinghouse to determine whether any drug or alcohol '
        f'violation information exists.\n'
        f'2. Annual limited queries for the duration of the independent contractor agreement.')
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Applicant Responsibilities", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5,
        'The applicant must register with the FMCSA Clearinghouse at '
        'https://clearinghouse.fmcsa.dot.gov and grant electronic consent for the full query. '
        'A full query requires separate electronic consent through the Clearinghouse system.')
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 8, "Employment Verification & Release (DOT Drug and Alcohol)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5,
        f'I authorize the release of information from my Department of Transportation regulated '
        f'drug and alcohol testing records by my previous employers/contractors listed in my '
        f'application to {company_name} or its designated agents.')
    pdf.ln(8)

    pdf.section_title("Acknowledgment")
    pdf.field_row("Applicant Name:", f"{_safe(form_data, 'first_name')} {_safe(form_data, 'last_name')}")
    pdf.field_row("Digital Signature:", _safe(form_data, "sig_full_name"))
    pdf.field_row("Acknowledged:", "Yes" if form_data.get("clearinghouse_acknowledge") else "No")
    pdf.field_row("Timestamp:", _safe(form_data, "clearinghouse_timestamp"))
    pdf.field_row("CA Consumer Copy Requested:", "Yes" if form_data.get("ca_copy") else "No")

    buf = BytesIO()
    buf.write(pdf.output())
    buf.seek(0)
    return buf.getvalue()
