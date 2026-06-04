from __future__ import annotations

from pathlib import Path

from services.safety_link_store import (
    create_safety_upload_link,
    get_safety_upload_link,
    list_safety_upload_links,
    safety_upload_url,
)


def test_safety_upload_url_uses_stable_driver_application_domain() -> None:
    assert safety_upload_url("abc123") == "https://driver-application.streamlit.app/?safety_upload=abc123"


def test_create_and_get_safety_upload_link(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    link = create_safety_upload_link(
        submissions_dir=sub,
        recipient_email="OWNER@EXAMPLE.COM",
        recipient_name="ABRAHAM PEREZ",
        division="Prestige Transportation Inc",
        items=[
            {
                "unit": "802",
                "document": "Insurance Certificate",
                "expires": "2026-06-01",
                "status": "🔴 Expired",
            }
        ],
    )

    assert link["url"].startswith("https://driver-application.streamlit.app/?safety_upload=")
    token = link["token"]
    loaded = get_safety_upload_link(submissions_dir=sub, token=token)
    assert loaded is not None
    assert loaded["recipient_email"] == "owner@example.com"
    assert loaded["recipient_name"] == "ABRAHAM PEREZ"
    assert loaded["items"][0]["document"] == "Insurance Certificate"
    assert loaded["expired"] is False

    links = list_safety_upload_links(submissions_dir=sub)
    assert len(links) == 1
    assert links[0]["token"] == token
    assert links[0]["expired"] is False


def test_missing_safety_upload_link_returns_none(tmp_path: Path) -> None:
    assert get_safety_upload_link(submissions_dir=tmp_path, token="missing") is None
