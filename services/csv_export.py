"""Build a flat CSV of an applicant's full submission for the safety team.

The CSV is intentionally a simple two-column ``Field,Value`` shape so it opens
cleanly in Excel / Google Sheets and individual rows can be copied into the
company's master spreadsheet without further parsing. Repeating sections
(employers, licenses, accidents, violations) are flattened with numbered
prefixes such as ``Employer 1 - Company name``.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from io import StringIO
from typing import Any, Iterable


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={_stringify(v)}" for k, v in value.items())
    return str(value)


def _section_rows(section_label: str, items: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            label = str(key).replace("_", " ").strip().title()
            rows.append((f"{section_label} {index} - {label}", _stringify(value)))
    return rows


def build_application_csv(
    *,
    form_data: dict[str, Any] | None,
    employers: list[dict[str, Any]] | None = None,
    licenses: list[dict[str, Any]] | None = None,
    accidents: list[dict[str, Any]] | None = None,
    violations: list[dict[str, Any]] | None = None,
    uploaded_documents: list[dict[str, Any]] | None = None,
) -> bytes:
    """Return UTF-8 encoded CSV bytes representing the full application."""

    form_data = form_data or {}
    rows: list[tuple[str, str]] = []

    # Top-level form fields, sorted for stable output.
    for key in sorted(form_data.keys()):
        label = str(key).replace("_", " ").strip().title()
        rows.append((label, _stringify(form_data[key])))

    rows.extend(_section_rows("Employer", employers or []))
    rows.extend(_section_rows("License", licenses or []))
    rows.extend(_section_rows("Accident", accidents or []))
    rows.extend(_section_rows("Violation", violations or []))

    for index, document in enumerate(uploaded_documents or [], start=1):
        if not isinstance(document, dict):
            continue
        rows.append((f"Supporting Document {index} - File Name", _stringify(document.get("file_name"))))
        rows.append((f"Supporting Document {index} - Size Bytes", _stringify(document.get("size_bytes"))))
        rows.append((f"Supporting Document {index} - Storage Path", _stringify(document.get("storage_path"))))

    buffer = StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["Field", "Value"])
    writer.writerows(rows)
    # Prepend a UTF-8 BOM so Excel opens it with the right encoding by default.
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")
