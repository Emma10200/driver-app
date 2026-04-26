from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader

import pdf_generator
from runtime_context import get_company_profile


def _extract_text(pdf_bytes: bytes) -> str:
    return "".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages)


def test_field_row_cross_page_wrap_does_not_pin_cursor_to_footer(monkeypatch):
    monkeypatch.setattr(pdf_generator, "get_active_company_profile", lambda: get_company_profile("prestige"))

    pdf = pdf_generator.CompanyPDF(get_company_profile("prestige"))
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_y(pdf.h - pdf.b_margin - 8)

    pdf.field_row("Long wrapped value:", "This is a long applicant-provided note. " * 150)

    assert pdf.page_no() > 1
    assert pdf.get_y() < 200


def test_generate_application_pdf_handles_long_wrapped_fields_without_page_explosion(monkeypatch):
    monkeypatch.setattr(pdf_generator, "get_active_company_profile", lambda: get_company_profile("prestige"))

    form_data = {
        "first_name": "Long",
        "last_name": "Fields",
        "email": "long@example.com",
        "position": "Owner Operator",
        "eligible_us": "Yes",
        "read_english": "Yes",
        "currently_employed": "Yes",
        "worked_here_before": "Yes",
        "prev_dates": "Returned after seasonal work. " * 80,
        "relatives_here": "No",
        "known_other_name": "No",
        "safe_driving_awards": "Safe driving recognition. " * 80,
        "twic_card": "No",
        "hazmat_endorsement": "No",
        "attended_trucking_school": "No",
        "disq_391_15": "No",
        "disq_suspended": "No",
        "disq_denied": "No",
        "disq_drug_test": "No",
        "disq_convicted": "Yes",
        "disq_convicted_details": "Detailed explanation. " * 120,
        "mvr_suspension_conviction": "No",
        "mvr_no_valid_license": "No",
        "mvr_alcohol_controlled_substance": "No",
        "mvr_illegal_substance_on_duty": "No",
        "mvr_reckless_driving": "No",
        "mvr_any_dot_test_positive": "No",
        "sig_full_name": "Long Fields",
        "sig_date": "2026-04-26",
    }
    employers = [
        {
            "company": f"Carrier {index}",
            "address": f"{index} Fleet Way",
            "city_state": "Fontana, CA",
            "country": "United States",
            "phone": "5551234567",
            "position": "Owner Operator",
            "start": "2020-01-01",
            "end": "2025-01-01",
            "reason": "Long reason for leaving. " * 80,
            "pay_range": "Percentage",
            "terminated": "No",
            "current": "No",
            "contact_ok": "Yes",
            "cmv": "Yes",
            "fmcsa": "Yes",
            "dot_testing": "Yes",
            "areas": "Regional lanes with occasional long-haul assignments. " * 30,
            "miles": "2500",
            "truck": "Sleeper tractor",
            "trailer": "Dry Van",
            "trailer_len": "53 feet or more",
        }
        for index in range(1, 4)
    ]

    pdf_bytes = pdf_generator.generate_application_pdf(form_data, employers, [], [], [])
    reader = PdfReader(BytesIO(pdf_bytes))

    assert len(reader.pages) <= 12


def test_generate_application_pdf_includes_aligned_credentials_and_disclosures(monkeypatch):
    monkeypatch.setattr(pdf_generator, "get_active_company_profile", lambda: get_company_profile("prestige"))

    form_data = {
        "first_name": "Emma",
        "middle_name": "J",
        "last_name": "Driver",
        "dob": "1990-01-01",
        "ssn": "123456789",
        "address": "123 Main St",
        "city": "Fontana",
        "state": "CA",
        "zip_code": "92335",
        "country": "United States",
        "primary_phone": "5551234567",
        "cell_phone": "5551239999",
        "mobile_carrier": "Other",
        "mobile_carrier_other": "Visible Wireless",
        "email": "emma@example.com",
        "preferred_contact": "Either",
        "best_time": "Afternoons",
        "resided_3_years": "No",
        "previous_addresses": [
            {
                "address": "9 Old Rd",
                "city": "Ontario",
                "state": "CA",
                "zip_code": "91761",
                "from_date": "2019-01-01",
                "to_date": "2021-01-01",
            }
        ],
        "prev_address": "9 Old Rd",
        "prev_city": "Ontario",
        "prev_state": "CA",
        "prev_zip": "91761",
        "emergency_name": "John Driver",
        "emergency_phone": "5557654321",
        "emergency_relationship": "Brother",
        "emergency_address": "321 Family Way, Fontana, CA",
        "text_consent": True,
        "position": "Owner Operator",
        "preferred_office": "California Office",
        "eligible_us": "Yes",
        "read_english": "Yes",
        "currently_employed": "No",
        "last_employment_end": "2026-04-01",
        "worked_here_before": "Yes",
        "referral_source": "Driver Referral",
        "referral_name": "Alex Driver",
        "prev_dates": "2021-2022, leased on, moved out of state",
        "relatives_here": "Yes",
        "relatives_names": "Sam Driver",
        "equipment_description": "Peterbilt 579",
        "equipment_year": "2022",
        "equipment_make": "Peterbilt",
        "equipment_model": "579",
        "equipment_color": "Blue",
        "equipment_vin": "VIN123",
        "equipment_weight": "18000",
        "equipment_mileage": "250000",
        "fifth_wheel_height": "47 in",
        "safe_driving_awards": "Million Mile Award",
        "known_other_name": "Yes",
        "other_name": "Emma Smith",
        "exp_straight_truck_type": "Box truck",
        "exp_straight_truck_truck_type": "Straight Truck",
        "exp_straight_truck_equipment_type": "Box",
        "exp_straight_truck_trailer_length": "28 feet or less",
        "exp_straight_truck_miles": "50000",
        "exp_straight_truck_dates": "2018-2020",
        "exp_tractor_and_semi_trailer_type": "53 dry van",
        "exp_tractor_and_semi_trailer_truck_type": "Sleeper",
        "exp_tractor_and_semi_trailer_equipment_type": "Dry Van",
        "exp_tractor_and_semi_trailer_trailer_length": "53 feet or more",
        "exp_tractor_and_semi_trailer_miles": "300000",
        "exp_tractor_and_semi_trailer_dates": "2020-present",
        "twic_card": "Yes",
        "twic_expiration": "2027-08-01",
        "hazmat_endorsement": "Yes",
        "hazmat_expiration": "2026-12-01",
        "highest_grade": "High School",
        "last_school": "Fontana High, Fontana, CA",
        "attended_trucking_school": "Yes",
        "ts_name": "Roadmaster",
        "ts_city_state": "Fontana, CA",
        "ts_start": "2019-01-01",
        "ts_end": "2019-03-01",
        "ts_graduated": "Yes",
        "ts_fmcsa_subject": "Yes",
        "references": [
            {"name": "Jane Doe", "city": "Fontana", "state": "CA", "phone": "5551112222", "relationship": "Friend"},
            {"name": "Mike Roe", "city": "Ontario", "state": "CA", "phone": "5553334444", "relationship": "Former Dispatcher"},
        ],
        "ref1": "Jane Doe, Fontana, CA, 5551112222, Friend",
        "ref2": "Mike Roe, Ontario, CA, 5553334444, Former Dispatcher",
        "disq_391_15": "No",
        "disq_suspended": "No",
        "disq_denied": "No",
        "disq_drug_test": "No",
        "disq_convicted": "Yes",
        "disq_convicted_which": ["Driving under the influence of alcohol (state law)"],
        "disq_convicted_details": "2018, CA, completed program",
        "mvr_suspension_conviction": "No",
        "mvr_no_valid_license": "No",
        "mvr_alcohol_controlled_substance": "No",
        "mvr_illegal_substance_on_duty": "No",
        "mvr_reckless_driving": "No",
        "mvr_any_dot_test_positive": "No",
        "fcra_acknowledge": True,
        "fcra_timestamp": "2026-04-21T10:00:00",
        "ca_applicable": True,
        "ca_disclosure_acknowledge": True,
        "ca_disclosure_timestamp": "2026-04-21T10:01:00",
        "ca_copy": True,
        "psp_acknowledge": True,
        "psp_timestamp": "2026-04-21T10:02:00",
        "clearinghouse_acknowledge": True,
        "clearinghouse_timestamp": "2026-04-21T10:03:00",
        "inv_consumer_report": True,
        "sig_full_name": "Emma Driver",
        "sig_date": "2026-04-21",
        "sig_timestamp": "2026-04-21T10:04:00",
        "drug_alcohol_cert": True,
        "applicant_cert": True,
    }
    licenses = [
        {
            "number": "A1234567",
            "state": "CA",
            "country": "US",
            "class": "Class A",
            "current_license": "Yes",
            "expiration": "2027-01-01",
            "med_card_exp": "2026-09-01",
            "is_cdl": "Yes",
            "tanker": "Yes",
            "hazmat": "Yes",
            "hazmat_exp": "2026-12-01",
            "doubles": "No",
            "x_endorsement": "Yes",
            "other_endorsement": "Passenger",
        }
    ]
    employers = [
        {
            "company": "ABC Logistics",
            "address": "1 Fleet Way",
            "city_state": "Ontario, CA 91761",
            "country": "United States",
            "phone": "5557778888",
            "position": "Owner Operator",
            "start": "2022-01-01",
            "end": "2026-04-01",
            "reason": "Better opportunity",
            "pay_range": "75 cpm",
            "terminated": "No",
            "current": "No",
            "contact_ok": "Yes",
            "cmv": "Yes",
            "fmcsa": "Yes",
            "dot_testing": "Yes",
            "areas_type": "Regional",
            "areas_other": "",
            "areas": "CA/NV/AZ",
            "miles": "2500",
            "truck_type": "Sleeper",
            "truck_other": "",
            "truck": "Peterbilt",
            "trailer": "Van",
            "trailer_len": "53 feet or more",
        }
    ]
    accidents = [
        {
            "date": "2023-01-01",
            "location": "Barstow, CA",
            "fatalities": 0,
            "injuries": 0,
            "hazmat": "No",
            "description": "Minor weather-related slide",
        }
    ]
    violations = [
        {
            "date": "2024-02-02",
            "location": "Needles, CA",
            "charge": "Speeding",
            "in_commercial_vehicle": "Yes",
            "fined": "Yes",
            "license_suspended": "No",
            "license_revoked": "No",
            "fine_amount": "$250",
            "penalty": "Fine",
            "comments": "9 MPH over the limit",
        }
    ]

    pdf_bytes = pdf_generator.generate_application_pdf(form_data, employers, licenses, accidents, violations)
    text = _extract_text(pdf_bytes)

    for expected_text in [
        "Visible Wireless",
        "Emergency Contact Address:",
        "Previous Address #1:",
        "Best Time to Contact:",
        "Referral Details:",
        "Relative Names:",
        "Known by Another Name:",
        "Driving Experience",
        "Truck Type:",
            "Dry Van",
        "TWIC Card:",
        "TWIC Expiration:",
        "Pay Range:",
        "Reference #1:",
        "Motor Vehicle Record",
        "In Commercial Vehicle:",
        "Fine Amount:",
        "Disclosures & Authorizations",
        "California Disclosure Acknowledged:",
        "Applicant Certification Acknowledged:",
    ]:
        assert expected_text in text
