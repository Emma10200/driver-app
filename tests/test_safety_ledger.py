from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.safety_ledger import (
    annotate_rows_for_send_queue,
    backfill_safety_ledger,
    item_key_from_parts,
    ledger_summary,
    list_ledger_records,
    record_send_event,
    record_upload_event,
    upsert_import_rows,
)
from services.safety_link_store import create_safety_upload_link


def test_item_key_from_parts_uses_unit_for_truck_documents_and_email_for_driver_documents() -> None:
    assert (
        item_key_from_parts(
            recipient_email="owner@example.com",
            unit="802",
            document="Insurance Certificate",
        )
        == "unit:802:INSURANCE"
    )
    assert (
        item_key_from_parts(
            recipient_email="DRIVER@EXAMPLE.COM",
            unit="—",
            document="Medical Card",
        )
        == "driver:driver@example.com:MEDICAL_CARD"
    )


def test_upsert_import_rows_creates_records_and_resolves_missing_rows(tmp_path: Path) -> None:
    submissions_dir = tmp_path / "submissions"
    rows = [
        {
            "Recipient": "Owner One",
            "Division": "Prestige Transportation Inc",
            "Email": "owner@example.com",
            "Unit": "802",
            "Document": "Insurance Certificate",
            "Expires": "2026-06-01",
            "Status": "🔴 Expired",
        },
        {
            "Recipient": "Driver One",
            "Division": "Xpress Trans Inc",
            "Email": "driver@example.com",
            "Unit": "—",
            "Document": "CDL License",
            "Expires": "2026-01-01",
            "Status": "🟡 Missing",
        },
    ]

    result = upsert_import_rows(submissions_dir, rows, full_export=True, source="test")
    assert result["added"] == 2

    result = upsert_import_rows(submissions_dir, rows[:1], full_export=True, source="test")
    assert result["resolved"] == 1

    records = list_ledger_records(submissions_dir, backfill=False)
    resolved = [record for record in records if record["ledger_state"] == "Resolved"]
    assert len(resolved) == 1
    assert resolved[0]["document"] == "CDL License"


def test_record_send_event_suppresses_recently_emailed_rows(tmp_path: Path) -> None:
    submissions_dir = tmp_path / "submissions"
    row = {
        "Recipient": "Owner One",
        "Division": "Prestige Transportation Inc",
        "Email": "owner@example.com",
        "Unit": "802",
        "Document": "Insurance Certificate",
        "Expires": "2026-06-01",
        "Status": "🔴 Expired",
    }
    upsert_import_rows(submissions_dir, [row], full_export=True)

    sent_at = datetime.now(UTC).isoformat()
    result = record_send_event(
        submissions_dir,
        recipient_email="owner@example.com",
        recipient_name="Owner One",
        division="Prestige Transportation Inc",
        items=[{"unit": "802", "document": "Insurance Certificate"}],
        token="tok_1",
        sent_at=sent_at,
    )

    assert result == {"updated": 1}
    annotated = annotate_rows_for_send_queue(submissions_dir, [row])
    assert annotated[0]["Include"] is False
    assert annotated[0]["Ledger status"] == "Recently sent"
    assert annotated[0]["Sent count"] == 1


def test_record_upload_event_marks_row_submitted_and_keeps_download_metadata(tmp_path: Path) -> None:
    submissions_dir = tmp_path / "submissions"
    result = record_upload_event(
        submissions_dir,
        token="tok_1",
        recipient_email="owner@example.com",
        recipient_name="Owner One",
        division="Prestige Transportation Inc",
        requested_items=[{"unit": "802", "document": "Insurance Certificate"}],
        uploaded_documents=[
            {
                "document_type": "Unit 802 - Insurance Certificate",
                "file_name": "insurance.pdf",
                "stored_name": "insurance.pdf",
                "content_type": "application/pdf",
                "size_bytes": 12,
                "storage_path": "safety_document_uploads/2026/insurance.pdf",
            }
        ],
        submitted_at="2026-04-20T12:00:00+00:00",
        upload_key="upload_1",
    )

    assert result == {"uploaded": 1}
    records = list_ledger_records(submissions_dir, backfill=False)
    assert records[0]["ledger_state"] == "Submitted"
    assert records[0]["uploads"][0]["file_name"] == "insurance.pdf"
    assert ledger_summary(submissions_dir)["submitted"] == 1


def test_backfill_safety_ledger_from_links_and_upload_manifests(tmp_path: Path) -> None:
    submissions_dir = tmp_path / "submissions"
    link = create_safety_upload_link(
        submissions_dir=submissions_dir,
        recipient_email="owner@example.com",
        recipient_name="Owner One",
        division="Prestige Transportation Inc",
        items=[{"unit": "802", "document": "Insurance Certificate"}],
    )

    manifest_dir = submissions_dir / "safety_document_uploads" / "2026" / "upload_1"
    manifest_dir.mkdir(parents=True)
    manifest = {
        "upload_key": "upload_1",
        "submitted_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "form_data": {
            "upload_type": "safety_document_upload",
            "safety_link_token": link["token"],
            "email": "owner@example.com",
            "driver_name": "Owner One",
            "division": "Prestige Transportation Inc",
            "requested_items": [{"unit": "802", "document": "Insurance Certificate"}],
        },
        "uploaded_documents": [
            {
                "document_type": "Unit 802 - Insurance Certificate",
                "file_name": "insurance.pdf",
                "stored_name": "insurance.pdf",
                "content_type": "application/pdf",
                "size_bytes": 12,
                "storage_path": "safety_document_uploads/2026/upload_1/insurance.pdf",
            }
        ],
    }
    (manifest_dir / "document_upload.json").write_text(json.dumps(manifest), encoding="utf-8")

    result = backfill_safety_ledger(submissions_dir)

    assert result["sent_events"] == 1
    assert result["upload_events"] == 1
    records = list_ledger_records(submissions_dir, backfill=False)
    assert len(records) == 1
    assert records[0]["send_count"] == 1
    assert records[0]["ledger_state"] == "Submitted"
    assert records[0]["uploads"][0]["storage_path"].endswith("insurance.pdf")
