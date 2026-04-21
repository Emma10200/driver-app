from __future__ import annotations

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