from __future__ import annotations

from types import SimpleNamespace

import services.submission_service as submission_service

class FakeSessionState(dict):
    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - defensive parity with Streamlit session state
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value):
        self[key] = value


def test_build_submission_artifacts_without_ca_disclosure(monkeypatch):
    fake_state = FakeSessionState(
        form_data={"first_name": "John", "ca_applicable": False},
        employers=[{"name": "Old Corp"}],
        licenses=[{"number": "123"}],
        accidents=[],
        violations=[]
    )
    monkeypatch.setattr(submission_service, "st", SimpleNamespace(session_state=fake_state))

    def mock_generate_application_pdf(form_data, employers, licenses, accidents, violations):
        return b"app_pdf"

    def mock_generate_fcra_pdf(form_data):
        return b"fcra_pdf"

    def mock_generate_california_disclosure_pdf(form_data):
        return b"ca_pdf"

    def mock_generate_psp_pdf(form_data):
        return b"psp_pdf"

    def mock_generate_clearinghouse_pdf(form_data):
        return b"clearinghouse_pdf"

    monkeypatch.setattr(submission_service, "generate_application_pdf", mock_generate_application_pdf)
    monkeypatch.setattr(submission_service, "generate_fcra_pdf", mock_generate_fcra_pdf)
    monkeypatch.setattr(submission_service, "generate_california_disclosure_pdf", mock_generate_california_disclosure_pdf)
    monkeypatch.setattr(submission_service, "generate_psp_pdf", mock_generate_psp_pdf)
    monkeypatch.setattr(submission_service, "generate_clearinghouse_pdf", mock_generate_clearinghouse_pdf)

    result = submission_service.build_submission_artifacts()

    assert result["application_pdf"] == b"app_pdf"
    assert result["fcra_pdf"] == b"fcra_pdf"
    assert result["california_pdf"] is None
    assert result["psp_pdf"] == b"psp_pdf"
    assert result["clearinghouse_pdf"] == b"clearinghouse_pdf"


def test_build_submission_artifacts_with_ca_disclosure(monkeypatch):
    fake_state = FakeSessionState(
        form_data={"first_name": "Jane", "ca_applicable": True},
        employers=[],
        licenses=[],
        accidents=[],
        violations=[]
    )
    monkeypatch.setattr(submission_service, "st", SimpleNamespace(session_state=fake_state))

    def mock_generate_application_pdf(form_data, employers, licenses, accidents, violations):
        return b"app_pdf"

    def mock_generate_fcra_pdf(form_data):
        return b"fcra_pdf"

    def mock_generate_california_disclosure_pdf(form_data):
        return b"ca_pdf"

    def mock_generate_psp_pdf(form_data):
        return b"psp_pdf"

    def mock_generate_clearinghouse_pdf(form_data):
        return b"clearinghouse_pdf"

    monkeypatch.setattr(submission_service, "generate_application_pdf", mock_generate_application_pdf)
    monkeypatch.setattr(submission_service, "generate_fcra_pdf", mock_generate_fcra_pdf)
    monkeypatch.setattr(submission_service, "generate_california_disclosure_pdf", mock_generate_california_disclosure_pdf)
    monkeypatch.setattr(submission_service, "generate_psp_pdf", mock_generate_psp_pdf)
    monkeypatch.setattr(submission_service, "generate_clearinghouse_pdf", mock_generate_clearinghouse_pdf)

    result = submission_service.build_submission_artifacts()

    assert result["application_pdf"] == b"app_pdf"
    assert result["fcra_pdf"] == b"fcra_pdf"
    assert result["california_pdf"] == b"ca_pdf"
    assert result["psp_pdf"] == b"psp_pdf"
    assert result["clearinghouse_pdf"] == b"clearinghouse_pdf"
