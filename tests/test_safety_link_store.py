from __future__ import annotations

from pathlib import Path

from services.safety_link_store import (
    create_safety_upload_link,
    find_link_by_message_id,
    find_link_by_ref_code,
    find_links_by_recipient_email,
    get_safety_upload_link,
    list_safety_upload_links,
    record_outbound_message_id,
    ref_code_for_token,
    safety_upload_url,
)

import services.safety_link_store as safety_link_store


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


def test_link_has_deterministic_ref_code(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    link = create_safety_upload_link(
        submissions_dir=sub,
        recipient_email="driver@example.com",
        recipient_name="JOHN DRIVER",
        division="Prestige Transportation Inc",
        items=[],
    )
    assert link["ref_code"] == ref_code_for_token(link["token"])
    assert find_link_by_ref_code(submissions_dir=sub, ref_code=link["ref_code"])["token"] == link["token"]
    assert find_link_by_ref_code(submissions_dir=sub, ref_code=link["ref_code"].lower())["token"] == link["token"]


def test_record_and_find_outbound_message_id(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    link = create_safety_upload_link(
        submissions_dir=sub,
        recipient_email="driver@example.com",
        recipient_name="JOHN DRIVER",
        division="Prestige Transportation Inc",
        items=[],
    )
    assert record_outbound_message_id(
        submissions_dir=sub, token=link["token"], message_id="<outbound-1@example.com>"
    )
    found = find_link_by_message_id(submissions_dir=sub, message_id="<outbound-1@example.com>")
    assert found is not None and found["token"] == link["token"]
    # Matching is tolerant of missing angle brackets / case.
    assert find_link_by_message_id(submissions_dir=sub, message_id="OUTBOUND-1@example.com")["token"] == link["token"]


def test_find_links_by_recipient_email_is_case_insensitive(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    link = create_safety_upload_link(
        submissions_dir=sub,
        recipient_email="Driver@Example.com",
        recipient_name="JOHN DRIVER",
        division="Prestige Transportation Inc",
        items=[],
    )
    matches = find_links_by_recipient_email(submissions_dir=sub, email="driver@example.com")
    assert len(matches) == 1
    assert matches[0]["token"] == link["token"]


def test_list_safety_upload_links_merges_legacy_cloud_paths(tmp_path: Path, monkeypatch) -> None:
    sub = tmp_path / "submissions"

    monkeypatch.setattr(safety_link_store, "_read_cloud_state", lambda name: {})
    monkeypatch.setattr(
        safety_link_store,
        "_read_cloud_state_path",
        lambda path: {
            "tok_legacy": {
                "token": "tok_legacy",
                "recipient_email": "owner@example.com",
                "recipient_name": "Owner One",
                "division": "Prestige Transportation Inc",
                "created_at": "2026-06-08T10:00:00+00:00",
                "expires_at": "2026-08-07T10:00:00+00:00",
                "items": [],
            }
        }
        if path == "safety/links/links.json"
        else {},
    )

    links = list_safety_upload_links(submissions_dir=sub)

    assert len(links) == 1
    assert links[0]["token"] == "tok_legacy"
    assert links[0]["recipient_email"] == "owner@example.com"
