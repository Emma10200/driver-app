from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from scripts.copy_submission_company import generate_company_copy, resolve_company_slug


def _extract_text(pdf_bytes: bytes) -> str:
    return "".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages)


def _sample_payload() -> dict:
    return {
        "submission_key": "20260514_120000_driver-emma",
        "submitted_at": "2026-05-14T12:00:00",
        "form_data": {
            "company_slug": "xpress",
            "company_name": "Xpress Trans, Inc",
            "first_name": "Emma",
            "last_name": "Driver",
            "email": "emma.driver@example.com",
            "position": "Driver",
            "eligible_us": "Yes",
            "read_english": "Yes",
            "currently_employed": "Yes",
            "worked_here_before": "No",
            "relatives_here": "No",
            "known_other_name": "No",
            "twic_card": "No",
            "hazmat_endorsement": "No",
            "attended_trucking_school": "No",
            "disq_391_15": "No",
            "disq_suspended": "No",
            "disq_denied": "No",
            "disq_drug_test": "No",
            "disq_convicted": "No",
            "mvr_suspension_conviction": "No",
            "mvr_no_valid_license": "No",
            "mvr_alcohol_controlled_substance": "No",
            "mvr_illegal_substance_on_duty": "No",
            "mvr_reckless_driving": "No",
            "mvr_any_dot_test_positive": "No",
            "fcra_acknowledge": True,
            "psp_acknowledge": True,
            "clearinghouse_acknowledge": True,
            "ca_applicable": False,
            "ca_copy": False,
            "inv_consumer_report": True,
            "drug_alcohol_cert": True,
            "applicant_cert": True,
            "sig_full_name": "Emma Driver",
            "sig_date": "2026-05-14",
            "final_submission_timestamp": "2026-05-14T12:00:00",
        },
        "employers": [],
        "licenses": [],
        "accidents": [],
        "violations": [],
        "uploaded_documents": [],
    }


def test_resolve_company_slug_accepts_prestig_alias():
    assert resolve_company_slug("prestig") == "pg"


def test_generate_company_copy_writes_target_company_packet(tmp_path):
    source_dir = tmp_path / "source-submission"
    source_dir.mkdir()
    (source_dir / "submission.json").write_text(json.dumps(_sample_payload()), encoding="utf-8")

    result = generate_company_copy(
        source_path=source_dir,
        target_company_slug="prestig",
        output_root=tmp_path / "generated",
        backend="local",
    )

    output_dir = Path(result["location_label"])
    output_payload = json.loads((output_dir / "submission.json").read_text(encoding="utf-8"))
    output_form_data = output_payload["form_data"]

    assert result["target_company_slug"] == "pg"
    assert result["storage_namespace"] == "companies/pg/live"
    assert output_form_data["company_slug"] == "pg"
    assert output_form_data["company_name"] == "Prestig, Inc."
    assert output_form_data["copied_from_company_slug"] == "xpress"
    assert output_form_data["copied_from_submission_key"] == "20260514_120000_driver-emma"

    application_text = _extract_text((output_dir / "application.pdf").read_bytes())
    fcra_text = _extract_text((output_dir / "fcra_disclosure.pdf").read_bytes())

    assert "Prestig, Inc." in application_text
    assert "Stone Park, IL 60165" in application_text
    assert "Xpress Trans, Inc" not in application_text
    assert "Prestig, Inc." in fcra_text