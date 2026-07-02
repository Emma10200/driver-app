from __future__ import annotations

from types import SimpleNamespace

from services import rate_confirmation_ocr as ocr


class FakeImage:
    pass


def test_ocr_pdf_text_renders_pages_and_ocr_text(monkeypatch):
    def fake_import_pdf2image():
        def convert_from_bytes(payload, **kwargs):
            assert payload == b"%PDF fake"
            assert kwargs["first_page"] == 1
            assert kwargs["last_page"] == 2
            assert kwargs["dpi"] == 200
            assert kwargs["grayscale"] is True
            return [FakeImage(), FakeImage()]

        return convert_from_bytes, (ValueError,)

    fake_tesseract = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd=""),
        image_to_string=lambda image, **kwargs: "Truck 1971\nTotal $1,250.00",
    )
    monkeypatch.setattr(ocr, "_import_pdf2image", fake_import_pdf2image)
    monkeypatch.setattr(ocr, "_import_pytesseract", lambda: fake_tesseract)

    result = ocr.ocr_pdf_text(b"%PDF fake", max_pages=2)

    assert result.status == "ocr_text_extracted"
    assert result.pages_rendered == 2
    assert result.pages_ocrd == 2
    assert "Truck 1971" in result.text
    assert result.as_metadata()["chars"] == result.chars


def test_ocr_pdf_text_missing_dependency_is_safe(monkeypatch):
    def missing_pdf2image():
        raise ImportError("no pdf2image")

    monkeypatch.setattr(ocr, "_import_pdf2image", missing_pdf2image)

    result = ocr.ocr_pdf_text(b"%PDF fake")

    assert result.status == "ocr_unavailable"
    assert "pdf2image" in result.error


def test_ocr_pdf_text_partial_on_tesseract_timeout(monkeypatch):
    def fake_import_pdf2image():
        def convert_from_bytes(_payload, **_kwargs):
            return [FakeImage(), FakeImage()]

        return convert_from_bytes, (ValueError,)

    calls = {"count": 0}

    def image_to_string(_image, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return "First page text"
        raise RuntimeError("timeout")

    fake_tesseract = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd=""),
        image_to_string=image_to_string,
    )
    monkeypatch.setattr(ocr, "_import_pdf2image", fake_import_pdf2image)
    monkeypatch.setattr(ocr, "_import_pytesseract", lambda: fake_tesseract)

    result = ocr.ocr_pdf_text(b"%PDF fake")

    assert result.status == "ocr_partial"
    assert result.pages_rendered == 2
    assert result.pages_ocrd == 1
    assert result.text == "First page text"
    assert "timeout" in result.error
