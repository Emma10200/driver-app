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


def test_list_supabase_document_upload_manifests_filters_by_upload_type(monkeypatch):
    safety_manifest = {
        "upload_key": "20260603_101112_owner-one",
        "submitted_at": "2026-06-03T10:11:12",
        "form_data": {"upload_type": "safety_document_upload"},
        "uploaded_documents": [{"file_name": "insurance.pdf"}],
    }
    generic_manifest = {
        "upload_key": "20260603_101113_driver-two",
        "submitted_at": "2026-06-03T10:11:13",
        "form_data": {"upload_type": "driver_document_upload_only"},
        "uploaded_documents": [{"file_name": "license.pdf"}],
    }

    def fake_read(path: str) -> bytes:
        if path.endswith("20260603_101112_owner-one/document_upload.json"):
            return json.dumps(safety_manifest).encode("utf-8")
        if path.endswith("20260603_101113_driver-two/document_upload.json"):
            return json.dumps(generic_manifest).encode("utf-8")
        raise FileNotFoundError(path)

    monkeypatch.setattr(submission_storage, "_supabase_enabled", lambda: True)
    monkeypatch.setattr(
        submission_storage,
        "_supabase_list",
        lambda prefix, limit=1000: [
            {"name": "20260603_101112_owner-one", "id": None},
            {"name": "20260603_101113_driver-two", "id": None},
        ],
    )
    monkeypatch.setattr(submission_storage, "_read_supabase_bytes", fake_read)

    manifests = submission_storage.list_supabase_document_upload_manifests(
        storage_namespace="safety-uploads/live",
        upload_type="safety_document_upload",
    )

    assert len(manifests) == 1
    assert manifests[0]["upload_key"] == "20260603_101112_owner-one"
    assert manifests[0]["_remote_prefix"] == "safety-uploads/live/document_uploads/20260603_101112_owner-one"