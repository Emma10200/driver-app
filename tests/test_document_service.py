from __future__ import annotations

from types import SimpleNamespace

import services.document_service as document_service


class FakeSessionState(dict):
    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError as exc:  # pragma: no cover - defensive parity with Streamlit session state
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value):
        self[key] = value


def test_render_supporting_documents_section_shows_requested_uploads(monkeypatch):
    markdown_calls: list[str] = []
    caption_calls: list[str] = []
    checkbox_calls: list[dict[str, object]] = []
    uploader_calls: list[dict[str, object]] = []

    def fake_markdown(value: str) -> None:
        markdown_calls.append(value)

    def fake_caption(value: str) -> None:
        caption_calls.append(value)

    def fake_subheader(value: str) -> None:
        markdown_calls.append(value)

    def fake_checkbox(label: str, key: str):
        checkbox_calls.append({"label": label, "key": key})
        return False

    def fake_file_uploader(label: str, **kwargs):
        uploader_calls.append({"label": label, **kwargs})
        return []

    fake_st = SimpleNamespace(
        session_state=FakeSessionState(uploaded_documents=[], form_data={}),
        markdown=fake_markdown,
        subheader=fake_subheader,
        caption=fake_caption,
        info=lambda _value: None,
        checkbox=fake_checkbox,
        file_uploader=fake_file_uploader,
    )

    monkeypatch.setattr(document_service, "st", fake_st)
    monkeypatch.setattr(document_service, "is_test_mode_active", lambda: False)

    document_service.render_supporting_documents_section()

    assert checkbox_calls
    checkbox_labels = [str(call["label"]) for call in checkbox_calls]
    for document in document_service.REQUESTED_SUPPORTING_DOCUMENTS:
        assert any(str(document["label"]) in label for label in checkbox_labels)

    assert any("Required before onboarding" in str(call["label"]) for call in checkbox_calls)
    assert any("Optional:" in caption for caption in caption_calls)
    assert fake_st.session_state.form_data["supporting_doc_direct_deposit"] is False

    assert uploader_calls
    assert uploader_calls[0]["label"] == "Upload supporting documents"
    assert "direct deposit form" in str(uploader_calls[0]["help"]).lower()
class MockUploadedFile:
    def __init__(self, name: str, size: int, type_: str, content: bytes = b""):
        self.name = name
        self.size = size
        self.type = type_
        self._content = content or b"fake content"

    def getvalue(self) -> bytes:
        return self._content

def test_normalize_pending_uploads_empty(monkeypatch):
    monkeypatch.setattr(document_service, "get_pending_uploads", lambda: [])
    normalized, errors = document_service._normalize_pending_uploads()
    assert normalized == []
    assert errors == []

def test_normalize_pending_uploads_valid(monkeypatch):
    mock_file1 = MockUploadedFile(name="doc1.pdf", size=1024, type_="application/pdf", content=b"fake pdf")
    mock_file2 = MockUploadedFile(name="image.png", size=2048, type_="image/png", content=b"fake png")

    monkeypatch.setattr(document_service, "get_pending_uploads", lambda: [mock_file1, mock_file2])

    normalized, errors = document_service._normalize_pending_uploads()

    assert errors == []
    assert len(normalized) == 2

    assert normalized[0]["file_name"] == "doc1.pdf"
    assert normalized[0]["content_type"] == "application/pdf"
    assert normalized[0]["size_bytes"] == 1024
    assert normalized[0]["content"] == b"fake pdf"
    assert "content_digest" in normalized[0]

    assert normalized[1]["file_name"] == "image.png"
    assert normalized[1]["content_type"] == "image/png"
    assert normalized[1]["size_bytes"] == 2048
    assert normalized[1]["content"] == b"fake png"
    assert "content_digest" in normalized[1]

def test_normalize_pending_uploads_exceeds_max_files(monkeypatch):
    max_files = document_service.MAX_SUPPORTING_DOCUMENTS
    mock_files = [MockUploadedFile(name=f"doc{i}.pdf", size=1024, type_="application/pdf") for i in range(max_files + 1)]

    monkeypatch.setattr(document_service, "get_pending_uploads", lambda: mock_files)

    normalized, errors = document_service._normalize_pending_uploads()

    assert len(errors) == 1
    assert "Upload no more than" in errors[0]
    assert str(max_files) in errors[0]
    # The current implementation still processes the files even if the max count is exceeded
    assert len(normalized) == max_files + 1

def test_normalize_pending_uploads_invalid_extension(monkeypatch):
    mock_file = MockUploadedFile(name="bad_script.txt", size=1024, type_="text/plain")

    monkeypatch.setattr(document_service, "get_pending_uploads", lambda: [mock_file])

    normalized, errors = document_service._normalize_pending_uploads()

    assert len(errors) == 1
    assert "bad_script.txt" in errors[0]
    assert "must be a PDF, JPG, or PNG file" in errors[0]
    assert len(normalized) == 0

def test_normalize_pending_uploads_exceeds_max_size(monkeypatch):
    max_size = document_service.MAX_SUPPORTING_DOCUMENT_SIZE_BYTES
    mock_file = MockUploadedFile(name="huge.pdf", size=max_size + 1, type_="application/pdf")

    monkeypatch.setattr(document_service, "get_pending_uploads", lambda: [mock_file])

    normalized, errors = document_service._normalize_pending_uploads()

    assert len(errors) == 1
    assert "huge.pdf" in errors[0]
    assert str(document_service.MAX_SUPPORTING_DOCUMENT_SIZE_MB) in errors[0]
    assert "exceeds the" in errors[0]
    assert len(normalized) == 0
