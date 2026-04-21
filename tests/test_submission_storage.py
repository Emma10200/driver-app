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