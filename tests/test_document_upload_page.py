from __future__ import annotations

import services.document_upload_page as document_upload_page


class MockUploadedFile:
    def __init__(self, name: str, size: int, type_: str, content: bytes = b""):
        self.name = name
        self.size = size
        self.type = type_
        self._content = content or b"fake content"

    def getvalue(self) -> bytes:
        return self._content


def test_normalize_document_uploads_keeps_document_type_and_dedupes():
    uploads_by_type = {
        "CDL": [MockUploadedFile("cdl.pdf", 123, "application/pdf", b"same")],
        "W9": [MockUploadedFile("w9.pdf", 123, "application/pdf", b"same")],
        "Owner registration": [MockUploadedFile("registration.png", 456, "image/png", b"different")],
    }

    normalized, errors = document_upload_page._normalize_document_uploads(uploads_by_type)

    assert errors == []
    assert len(normalized) == 2
    assert normalized[0]["document_type"] == "CDL"
    assert normalized[0]["file_name"] == "cdl.pdf"
    assert normalized[1]["document_type"] == "Owner registration"
    assert normalized[1]["file_name"] == "registration.png"


def test_document_upload_options_include_dot_inspections():
    assert "DOT inspections (truck and trailer if applicable)" in document_upload_page.DOCUMENT_UPLOAD_OPTIONS


def test_normalize_document_uploads_rejects_invalid_extension():
    normalized, errors = document_upload_page._normalize_document_uploads(
        {"CDL": [MockUploadedFile("cdl.exe", 123, "application/octet-stream", b"bad")]}
    )

    assert normalized == []
    assert len(errors) == 1
    assert "cdl.exe" in errors[0]
    assert "PDF, JPG, or PNG" in errors[0]


def test_split_driver_name_preserves_multi_part_last_name():
    assert document_upload_page._split_driver_name("Jane Maria Driver Smith") == (
        "Jane",
        "Maria Driver Smith",
    )
