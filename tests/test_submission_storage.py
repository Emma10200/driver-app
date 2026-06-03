from __future__ import annotations

import json
from types import SimpleNamespace

import submission_storage


def test_get_runtime_secret_reads_smtp_section(monkeypatch):
    fake_streamlit = SimpleNamespace(
        secrets={
            "app": {"SUBMISSION_STORAGE_BACKEND": "auto"},
            "smtp": {
                "SMTP_HOST": "smtp.gmail.com",
                "SMTP_FROM_EMAIL": "statements@prestigetransportation.com",
            },
        }
    )

    monkeypatch.setattr(submission_storage, "st", fake_streamlit)

    assert submission_storage.get_runtime_secret("SMTP_HOST") == "smtp.gmail.com"
    assert (
        submission_storage.get_runtime_secret("SMTP_FROM_EMAIL")
        == "statements@prestigetransportation.com"
    )


def test_save_draft_bundle_uses_storage_namespace(tmp_path):
    result = submission_storage.save_draft_bundle(
        draft_id="DRAFT-TEST01",
        draft_payload={"draft_id": "DRAFT-TEST01", "current_page": 1},
        local_base_dir=tmp_path,
        storage_namespace="companies/prestige/test-mode",
    )

    expected_file = tmp_path / "companies" / "prestige" / "test-mode" / "drafts" / "DRAFT-TEST01" / "draft.json"
    assert result["draft_id"] == "DRAFT-TEST01"
    assert expected_file.exists()


def test_save_document_upload_bundle_is_company_agnostic(tmp_path, monkeypatch):
    monkeypatch.setattr(submission_storage, "_get_backend", lambda: "local")
    document = {
        "document_type": "CDL",
        "file_name": "license.pdf",
        "content": b"fake pdf bytes",
        "content_type": "application/pdf",
        "size_bytes": 14,
        "content_digest": "abc123def4567890",
    }

    result = submission_storage.save_document_upload_bundle(
        form_data={
            "driver_name": "Jane Driver",
            "final_submission_timestamp": "2026-06-03T10:11:12",
        },
        documents=[document],
        local_base_dir=tmp_path,
        storage_namespace="document-uploads/live",
    )

    expected_dir = tmp_path / "document-uploads" / "live" / "document_uploads" / result["upload_key"]
    manifest_path = expected_dir / "document_upload.json"
    assert result["upload_key"] == "20260603_101112_jane-driver"
    assert manifest_path.exists()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["upload_type"] == "driver_document_upload_only"
    assert payload["form_data"]["driver_name"] == "Jane Driver"
    assert payload["uploaded_documents"][0]["document_type"] == "CDL"
    assert payload["uploaded_documents"][0]["storage_path"].startswith(
        "document-uploads/live/document_uploads/20260603_101112_jane-driver/"
    )
    assert (expected_dir / payload["uploaded_documents"][0]["stored_name"]).read_bytes() == b"fake pdf bytes"