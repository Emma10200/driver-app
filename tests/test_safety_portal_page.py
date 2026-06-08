from __future__ import annotations

from services.safety_portal_page import (
    _group_selected_rows,
    _sent_history_rows_from_ledger_records,
    _sent_history_rows_from_links,
)


def test_group_selected_rows_excludes_unchecked_items() -> None:
    grouped = _group_selected_rows(
        [
            {
                "Include": True,
                "Recipient": "ABRAHAM PEREZ",
                "Division": "Prestige Transportation Inc",
                "Email": "owner@example.com",
                "Unit": "802",
                "Document": "Insurance Certificate",
                "Expires": "2026-06-01",
                "Status": "🔴 Expired",
            },
            {
                "Include": False,
                "Recipient": "ABRAHAM PEREZ",
                "Division": "Prestige Transportation Inc",
                "Email": "owner@example.com",
                "Unit": "802",
                "Document": "IFTA Sticker",
                "Expires": "2021-12-31",
                "Status": "🔴 Expired",
            },
        ]
    )

    assert list(grouped) == ["owner@example.com"]
    items = grouped["owner@example.com"]["items"]
    assert len(items) == 1
    assert items[0]["document"] == "Insurance Certificate"


def test_sent_history_rows_are_built_from_saved_links() -> None:
    rows = _sent_history_rows_from_links(
        [
            {
                "token": "tok_1",
                "ref_code": "ABC12345",
                "recipient_email": "owner@example.com",
                "recipient_name": "Owner One",
                "division": "Prestige Transportation Inc",
                "created_at": "2026-06-08T10:00:00+00:00",
                "expires_at": "2026-08-07T10:00:00+00:00",
                "expired": False,
                "items": [
                    {
                        "unit": "802",
                        "document": "Insurance Certificate",
                        "expires": "2026-06-01",
                        "status": "🔴 Expired",
                    }
                ],
            }
        ],
        division_filter="All",
        query="insurance",
    )

    assert rows == [
        {
                "Source": "Saved email link",
            "Sent at": "2026-06-08T10:00:00+00:00",
            "Sent to": "owner@example.com",
            "Recipient": "Owner One",
            "Division": "Prestige Transportation Inc",
            "Ref": "ABC12345",
            "Link expires": "2026-08-07T10:00:00+00:00",
            "Expired link?": "No",
            "Token": "tok_1",
            "Unit": "802",
            "Document": "Insurance Certificate",
            "Expires": "2026-06-01",
            "Status when sent": "🔴 Expired",
        }
    ]


def test_sent_history_rows_include_submitted_upload_fallback() -> None:
    rows = _sent_history_rows_from_ledger_records(
        [
            {
                "recipient_email": "driver@example.com",
                "recipient_name": "Driver One",
                "division": "Xpress Trans Inc",
                "unit": "7001",
                "document": "Insurance Certificate",
                "expires": "2026-06-01",
                "status": "🔴 Expired",
                "last_upload_display": "2026-06-03 23:15",
                "last_upload_token": "tok_upload",
                "uploads": [{"file_name": "insurance.pdf"}],
            }
        ],
        division_filter="All",
        query="driver one",
    )

    assert rows[0]["Source"] == "Submitted upload (original send record unavailable)"
    assert rows[0]["Sent to"] == "driver@example.com"
    assert rows[0]["Last upload"] == "2026-06-03 23:15"
