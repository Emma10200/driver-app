"""Unique safety upload links for driver/owner recipients.

Each outbound safety email gets a long random token that maps back to the
recipient and the selected paperwork items. The public upload page reads
this token to show the person-specific checklist.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from services.safety_cloud_state import read_state as _read_cloud_state
from services.safety_cloud_state import write_state as _write_cloud_state

_PUBLIC_BASE_URL = "https://driver-application.streamlit.app"
_LINKS_FILE = "links.json"
# Logical name of the Supabase-backed mirror so the scheduled email-reply
# ingester (a separate process) can resolve a reply back to its recipient.
_CLOUD_STATE_NAME = "links"
_DEFAULT_TTL_DAYS = 60
_lock = threading.Lock()


def ref_code_for_token(token: str) -> str:
    """Return the short, human-friendly reference stamped into outbound emails.

    Deterministically derived from the link token so a reply quoting the
    ``[Ref: XXXXXXXX]`` tag can be mapped back to the exact recipient without
    storing a second lookup table.
    """
    token = str(token or "").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8].upper()


def _links_dir(submissions_dir: Path) -> Path:
    path = submissions_dir / "safety" / "links"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _links_path(submissions_dir: Path) -> Path:
    return _links_dir(submissions_dir) / _LINKS_FILE


def _read_links(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def _write_links(path: Path, payload: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_cloud_links() -> dict[str, dict[str, Any]]:
    data = _read_cloud_state(_CLOUD_STATE_NAME)
    if not isinstance(data, dict):
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def _merged_links(path: Path) -> dict[str, dict[str, Any]]:
    """Union of the local and Supabase link directories.

    Local entries win on conflict because the in-process session is the most
    likely to hold the freshest copy, but cloud-only entries (e.g. created by a
    different deployment/container) are still surfaced so links survive restarts
    and are visible to the scheduled ingester.
    """
    merged = _read_cloud_links()
    for token, record in _read_links(path).items():
        merged[token] = record
    return merged


def _persist_links(path: Path, payload: dict[str, dict[str, Any]]) -> None:
    _write_links(path, payload)
    # Best-effort mirror; never block link creation on Supabase availability.
    _write_cloud_state(_CLOUD_STATE_NAME, payload)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def safety_upload_url(token: str, *, base_url: str = _PUBLIC_BASE_URL) -> str:
    """Build the public recipient URL.

    Intentionally defaults to the stable app URL and does NOT read APP_BASE_URL;
    the generic app base has pointed at the old hashed Streamlit domain before.
    """
    return f"{base_url.rstrip('/')}/?{urlencode({'safety_upload': token})}"


def create_safety_upload_link(
    *,
    submissions_dir: Path,
    recipient_email: str,
    recipient_name: str,
    division: str,
    items: list[dict[str, Any]],
    ttl_days: int = _DEFAULT_TTL_DAYS,
) -> dict[str, Any]:
    """Create and persist a recipient-specific upload link."""
    token = secrets.token_urlsafe(32)
    now = _now()
    expires_at = now + timedelta(days=max(1, int(ttl_days or _DEFAULT_TTL_DAYS)))
    record = {
        "token": token,
        "ref_code": ref_code_for_token(token),
        "recipient_email": str(recipient_email or "").strip().lower(),
        "recipient_name": str(recipient_name or "Driver/Owner").strip() or "Driver/Owner",
        "division": str(division or "").strip(),
        "items": [dict(item) for item in items if item],
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "url": safety_upload_url(token),
        "outbound_message_ids": [],
    }
    path = _links_path(submissions_dir)
    with _lock:
        links = _merged_links(path)
        links[token] = record
        _persist_links(path, links)
    return record


def get_safety_upload_link(*, submissions_dir: Path, token: str) -> dict[str, Any] | None:
    token = str(token or "").strip()
    if not token:
        return None
    record = _merged_links(_links_path(submissions_dir)).get(token)
    if not record:
        return None
    expires_at = _parse_dt(str(record.get("expires_at") or ""))
    record = dict(record)
    record["expired"] = bool(expires_at and expires_at < _now())
    return record


def list_safety_upload_links(*, submissions_dir: Path) -> list[dict[str, Any]]:
    """Return all generated safety upload links, newest first."""
    rows = []
    for token, record in _merged_links(_links_path(submissions_dir)).items():
        item = dict(record)
        item.setdefault("token", token)
        expires_at = _parse_dt(str(item.get("expires_at") or ""))
        item["expired"] = bool(expires_at and expires_at < _now())
        rows.append(item)
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def record_outbound_message_id(*, submissions_dir: Path, token: str, message_id: str) -> bool:
    """Associate an outbound email Message-ID with a link for thread matching."""
    token = str(token or "").strip()
    message_id = str(message_id or "").strip()
    if not token or not message_id:
        return False
    path = _links_path(submissions_dir)
    with _lock:
        links = _merged_links(path)
        record = links.get(token)
        if not record:
            return False
        existing = [str(m) for m in (record.get("outbound_message_ids") or []) if m]
        if message_id not in existing:
            existing.append(message_id)
        record["outbound_message_ids"] = existing
        links[token] = record
        _persist_links(path, links)
    return True


def find_link_by_message_id(*, submissions_dir: Path, message_id: str) -> dict[str, Any] | None:
    """Find the link whose outbound Message-ID matches a reply's In-Reply-To."""
    target = str(message_id or "").strip().strip("<>").lower()
    if not target:
        return None
    for record in list_safety_upload_links(submissions_dir=submissions_dir):
        for raw in record.get("outbound_message_ids") or []:
            if str(raw or "").strip().strip("<>").lower() == target:
                return record
    return None


def find_link_by_ref_code(*, submissions_dir: Path, ref_code: str) -> dict[str, Any] | None:
    """Find the link whose stamped ``[Ref: XXXXXXXX]`` tag matches."""
    target = str(ref_code or "").strip().upper()
    if not target:
        return None
    for record in list_safety_upload_links(submissions_dir=submissions_dir):
        code = str(record.get("ref_code") or "").strip().upper()
        if not code:
            code = ref_code_for_token(str(record.get("token") or ""))
        if code and code == target:
            return record
    return None


def find_links_by_recipient_email(*, submissions_dir: Path, email: str) -> list[dict[str, Any]]:
    """Return links sent to a recipient address, newest first."""
    target = str(email or "").strip().lower()
    if not target:
        return []
    return [
        record
        for record in list_safety_upload_links(submissions_dir=submissions_dir)
        if str(record.get("recipient_email") or "").strip().lower() == target
    ]
