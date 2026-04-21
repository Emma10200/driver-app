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