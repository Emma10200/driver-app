from __future__ import annotations

from services.safety_portal_page import _group_selected_rows


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
