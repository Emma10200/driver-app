"""Unique safety upload links for driver/owner recipients.

Each outbound safety email gets a long random token that maps back to the
recipient and the selected paperwork items. The public upload page reads
this token to show the person-specific checklist.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

_PUBLIC_BASE_URL = "https://driver-application.streamlit.app"
_LINKS_FILE = "links.json"
_DEFAULT_TTL_DAYS = 60
_lock = threading.Lock()


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
        "recipient_email": str(recipient_email or "").strip().lower(),
        "recipient_name": str(recipient_name or "Driver/Owner").strip() or "Driver/Owner",
        "division": str(division or "").strip(),
        "items": [dict(item) for item in items if item],
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "url": safety_upload_url(token),
    }
    path = _links_path(submissions_dir)
    with _lock:
        links = _read_links(path)
        links[token] = record
        _write_links(path, links)
    return record


def get_safety_upload_link(*, submissions_dir: Path, token: str) -> dict[str, Any] | None:
    token = str(token or "").strip()
    if not token:
        return None
    record = _read_links(_links_path(submissions_dir)).get(token)
    if not record:
        return None
    expires_at = _parse_dt(str(record.get("expires_at") or ""))
    record = dict(record)
    record["expired"] = bool(expires_at and expires_at < _now())
    return record
