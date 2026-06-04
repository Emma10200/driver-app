"""Persistent safety paperwork ledger.

The ledger tracks each specific requested item (driver CDL, unit insurance,
etc.) across imports, sends, nudges, and uploads. It is intentionally JSON
backed for the proof-of-concept so it can live next to the existing local
submission/reference data. The public functions are pure-ish and easy to move
to Supabase later.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from services.safety_link_store import list_safety_upload_links

_LEDGER_FILE = "ledger.json"
_DEFAULT_COOLDOWN_DAYS = 7
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Basic file helpers
# ---------------------------------------------------------------------------


def _ledger_dir(submissions_dir: Path) -> Path:
    path = submissions_dir / "safety" / "ledger"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ledger_path(submissions_dir: Path) -> Path:
    return _ledger_dir(submissions_dir) / _LEDGER_FILE


def _read_ledger(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def _write_ledger(path: Path, records: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


def _parse_dt(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _safe(value: Any) -> str:
    return str(value or "").strip()


# ---------------------------------------------------------------------------
# Stable item keys
# ---------------------------------------------------------------------------


def normalize_doc_key(document: Any, doc_type: Any = "") -> str:
    raw = _safe(doc_type) or _safe(document)
    normalized = "".join(ch.upper() if ch.isalnum() else "_" for ch in raw)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    normalized = normalized.strip("_")
    label = _safe(document).lower()
    if "insurance" in label:
        return "INSURANCE"
    if "ifta" in label:
        return "IFTA"
    if "inspection" in label or normalized in {"DOT", "DOT_INSPECTION"}:
        return "DOT_INSPECTION"
    if "plate" in label or "registration" in label:
        return "PLATES"
    if "medical" in label:
        return "MEDICAL_CARD"
    if "cdl" in label or "license" in label:
        return "CDL"
    return normalized or "DOCUMENT"


def item_key_from_parts(*, recipient_email: str, unit: Any, document: Any, doc_type: Any = "") -> str:
    unit_text = _safe(unit)
    doc_key = normalize_doc_key(document, doc_type)
    if unit_text and unit_text != "—":
        return f"unit:{unit_text}:{doc_key}"
    email = _safe(recipient_email).lower() or "unknown"
    return f"driver:{email}:{doc_key}"


def item_key_from_row(row: dict[str, Any]) -> str:
    return item_key_from_parts(
        recipient_email=_safe(row.get("Email") or row.get("email")),
        unit=row.get("Unit") or row.get("unit"),
        document=row.get("Document") or row.get("document"),
        doc_type=row.get("_doc_type") or row.get("doc_type"),
    )


def _item_label(item: dict[str, Any]) -> str:
    unit = _safe(item.get("unit") or item.get("Unit"))
    document = _safe(item.get("document") or item.get("Document") or "Document")
    return f"Unit {unit} - {document}" if unit and unit != "—" else document


# ---------------------------------------------------------------------------
# Record state / display helpers
# ---------------------------------------------------------------------------


def ledger_state(record: dict[str, Any], *, now: datetime | None = None) -> str:
    now = now or _now()
    if record.get("resolved_at"):
        return "Resolved"
    if record.get("last_upload_at"):
        return "Submitted"
    suppressed_until = _parse_dt(record.get("suppressed_until"))
    if suppressed_until and suppressed_until > now:
        return "Recently sent"
    if record.get("last_sent_at"):
        return "Needs nudge"
    return "New"


def should_default_include(record: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if not record:
        return True
    return ledger_state(record, now=now) in {"New", "Needs nudge"}


def _display_dt(raw: Any) -> str:
    parsed = _parse_dt(raw)
    if not parsed:
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Import / preview updates
# ---------------------------------------------------------------------------


def upsert_import_rows(
    submissions_dir: Path,
    rows: Iterable[dict[str, Any]],
    *,
    full_export: bool = False,
    source: str = "preview",
) -> dict[str, int]:
    rows = [dict(row) for row in rows]
    now_iso = _iso()
    seen_keys: set[str] = set()
    path = _ledger_path(submissions_dir)
    with _lock:
        records = _read_ledger(path)
        added = updated = unchanged = resolved = 0
        for row in rows:
            item_key = item_key_from_row(row)
            seen_keys.add(item_key)
            existing = records.get(item_key)
            payload = {
                "item_key": item_key,
                "recipient_email": _safe(row.get("Email") or row.get("email")).lower(),
                "recipient_name": _safe(row.get("Recipient") or row.get("recipient_name") or "Driver/Owner"),
                "division": _safe(row.get("Division") or row.get("division")),
                "unit": _safe(row.get("Unit") or row.get("unit") or "—"),
                "document": _safe(row.get("Document") or row.get("document") or "Document"),
                "doc_key": normalize_doc_key(row.get("Document") or row.get("document"), row.get("_doc_type")),
                "expires": _safe(row.get("Expires") or row.get("expires")),
                "status": _safe(row.get("Status") or row.get("status")),
                "last_seen_at": now_iso,
                "last_seen_source": source,
            }
            if existing is None:
                records[item_key] = {**payload, "first_seen_at": now_iso, "send_count": 0, "uploads": [], "send_events": []}
                added += 1
                continue
            before = {k: existing.get(k) for k in payload}
            existing.update(payload)
            # If a previously-resolved item appears in a new import, reopen it.
            if existing.get("resolved_at"):
                existing.pop("resolved_at", None)
                existing.pop("resolved_reason", None)
            if before == {k: existing.get(k) for k in payload}:
                unchanged += 1
            else:
                updated += 1

        if full_export:
            for key, record in records.items():
                if key in seen_keys or record.get("resolved_at"):
                    continue
                if record.get("last_upload_at"):
                    continue
                record["resolved_at"] = now_iso
                record["resolved_reason"] = "not_present_in_latest_full_export"
                resolved += 1
        _write_ledger(path, records)
    return {"added": added, "updated": updated, "unchanged": unchanged, "resolved": resolved, "seen": len(seen_keys)}


def annotate_rows_for_send_queue(
    submissions_dir: Path,
    rows: Iterable[dict[str, Any]],
    *,
    cooldown_days: int = _DEFAULT_COOLDOWN_DAYS,
) -> list[dict[str, Any]]:
    backfill_safety_ledger(submissions_dir, cooldown_days=cooldown_days)
    records = _read_ledger(_ledger_path(submissions_dir))
    now = _now()
    annotated: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        item_key = item_key_from_row(out)
        record = records.get(item_key)
        state = ledger_state(record or {}, now=now) if record else "New"
        out["_item_key"] = item_key
        out["Include"] = should_default_include(record, now=now)
        out["Ledger status"] = state
        out["Last emailed"] = _display_dt((record or {}).get("last_sent_at"))
        out["Sent count"] = int((record or {}).get("send_count") or 0)
        out["Last upload"] = _display_dt((record or {}).get("last_upload_at"))
        out["Suppressed until"] = _display_dt((record or {}).get("suppressed_until"))
        if state == "Recently sent":
            out["Action note"] = "Unchecked by default: emailed in last 7 days. Re-check to nudge."
        elif state == "Submitted":
            out["Action note"] = "Unchecked by default: documents submitted, pending review."
        elif state == "Needs nudge":
            out["Action note"] = "Eligible for nudge."
        else:
            out["Action note"] = "Ready."
        annotated.append(out)
    return annotated


# ---------------------------------------------------------------------------
# Send / upload events
# ---------------------------------------------------------------------------


def record_send_event(
    submissions_dir: Path,
    *,
    recipient_email: str,
    recipient_name: str,
    division: str,
    items: Iterable[dict[str, Any]],
    token: str,
    sent_at: str | None = None,
    cooldown_days: int = _DEFAULT_COOLDOWN_DAYS,
) -> dict[str, int]:
    sent_dt = _parse_dt(sent_at) or _now()
    sent_iso = sent_dt.isoformat()
    suppressed_until = (sent_dt + timedelta(days=max(1, int(cooldown_days or _DEFAULT_COOLDOWN_DAYS)))).isoformat()
    token = _safe(token)
    path = _ledger_path(submissions_dir)
    with _lock:
        records = _read_ledger(path)
        changed = 0
        for item in [dict(i) for i in items if i]:
            item_key = _safe(item.get("item_key") or item.get("_item_key")) or item_key_from_parts(
                recipient_email=recipient_email,
                unit=item.get("unit") or item.get("Unit"),
                document=item.get("document") or item.get("Document"),
                doc_type=item.get("doc_type") or item.get("_doc_type"),
            )
            record = records.setdefault(
                item_key,
                {
                    "item_key": item_key,
                    "first_seen_at": sent_iso,
                    "send_count": 0,
                    "uploads": [],
                    "send_events": [],
                },
            )
            record.update(
                {
                    "recipient_email": _safe(recipient_email).lower(),
                    "recipient_name": _safe(recipient_name or item.get("recipient_name") or "Driver/Owner"),
                    "division": _safe(division),
                    "unit": _safe(item.get("unit") or item.get("Unit") or "—"),
                    "document": _safe(item.get("document") or item.get("Document") or "Document"),
                    "doc_key": normalize_doc_key(item.get("document") or item.get("Document"), item.get("doc_type")),
                    "expires": _safe(item.get("expires") or item.get("Expires")),
                    "status": _safe(item.get("status") or item.get("Status")),
                    "last_sent_at": sent_iso,
                    "last_sent_to": _safe(recipient_email).lower(),
                    "last_link_token": token,
                    "suppressed_until": suppressed_until,
                }
            )
            event_id = f"send:{token}:{item_key}" if token else f"send:{sent_iso}:{item_key}"
            events = list(record.get("send_events") or [])
            if not any(e.get("event_id") == event_id for e in events if isinstance(e, dict)):
                events.append({"event_id": event_id, "token": token, "sent_at": sent_iso, "to": _safe(recipient_email).lower()})
                record["send_events"] = events
                record["send_count"] = int(record.get("send_count") or 0) + 1
                changed += 1
        _write_ledger(path, records)
    return {"updated": changed}


def record_upload_event(
    submissions_dir: Path,
    *,
    token: str,
    recipient_email: str,
    recipient_name: str,
    division: str,
    requested_items: Iterable[dict[str, Any]],
    uploaded_documents: Iterable[dict[str, Any]],
    submitted_at: str | None = None,
    upload_key: str = "",
) -> dict[str, int]:
    submitted_dt = _parse_dt(submitted_at) or _now()
    submitted_iso = submitted_dt.isoformat()
    requested = [dict(item) for item in requested_items if isinstance(item, dict)]
    docs = [dict(doc) for doc in uploaded_documents if isinstance(doc, dict)]
    path = _ledger_path(submissions_dir)
    with _lock:
        records = _read_ledger(path)
        changed = 0
        for item in requested or [{}]:
            item_key = item_key_from_parts(
                recipient_email=recipient_email,
                unit=item.get("unit") or item.get("Unit"),
                document=item.get("document") or item.get("Document") or (docs[0].get("document_type") if docs else "Document"),
                doc_type=item.get("doc_type") or item.get("_doc_type"),
            )
            expected_label = _item_label(item) if item else ""
            matching_docs = [doc for doc in docs if not expected_label or _safe(doc.get("document_type")) == expected_label]
            if not matching_docs and len(requested) == 1:
                matching_docs = docs
            if not matching_docs:
                continue
            record = records.setdefault(
                item_key,
                {
                    "item_key": item_key,
                    "first_seen_at": submitted_iso,
                    "send_count": 0,
                    "uploads": [],
                    "send_events": [],
                },
            )
            record.update(
                {
                    "recipient_email": _safe(recipient_email).lower(),
                    "recipient_name": _safe(recipient_name or "Driver/Owner"),
                    "division": _safe(division),
                    "unit": _safe(item.get("unit") or item.get("Unit") or "—"),
                    "document": _safe(item.get("document") or item.get("Document") or expected_label or "Document"),
                    "doc_key": normalize_doc_key(item.get("document") or item.get("Document") or expected_label),
                    "last_upload_at": submitted_iso,
                    "last_upload_token": _safe(token),
                }
            )
            uploads = list(record.get("uploads") or [])
            for doc in matching_docs:
                event_id = f"upload:{upload_key}:{doc.get('stored_name') or doc.get('storage_path') or doc.get('file_name')}"
                if any(u.get("event_id") == event_id for u in uploads if isinstance(u, dict)):
                    continue
                uploads.append(
                    {
                        "event_id": event_id,
                        "token": _safe(token),
                        "upload_key": _safe(upload_key),
                        "submitted_at": submitted_iso,
                        "document_type": _safe(doc.get("document_type")),
                        "file_name": _safe(doc.get("file_name")),
                        "stored_name": _safe(doc.get("stored_name")),
                        "content_type": _safe(doc.get("content_type")),
                        "size_bytes": int(doc.get("size_bytes") or 0),
                        "storage_path": _safe(doc.get("storage_path")),
                    }
                )
                changed += 1
            record["uploads"] = uploads
        _write_ledger(path, records)
    return {"uploaded": changed}


# ---------------------------------------------------------------------------
# Backfill helpers
# ---------------------------------------------------------------------------


def _iter_safety_upload_manifests(submissions_dir: Path) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    for path in submissions_dir.rglob("document_upload.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        form_data = payload.get("form_data") if isinstance(payload, dict) else None
        if not isinstance(form_data, dict):
            continue
        if form_data.get("upload_type") != "safety_document_upload":
            continue
        manifests.append(payload)
    return manifests


def backfill_safety_ledger(submissions_dir: Path, *, cooldown_days: int = _DEFAULT_COOLDOWN_DAYS) -> dict[str, int]:
    sent = uploaded = 0
    for link in list_safety_upload_links(submissions_dir=submissions_dir):
        result = record_send_event(
            submissions_dir,
            recipient_email=_safe(link.get("recipient_email")),
            recipient_name=_safe(link.get("recipient_name")),
            division=_safe(link.get("division")),
            items=link.get("items") or [],
            token=_safe(link.get("token")),
            sent_at=_safe(link.get("created_at")),
            cooldown_days=cooldown_days,
        )
        sent += result.get("updated", 0)

    for manifest in _iter_safety_upload_manifests(submissions_dir):
        form_data = manifest.get("form_data") or {}
        result = record_upload_event(
            submissions_dir,
            token=_safe(form_data.get("safety_link_token")),
            recipient_email=_safe(form_data.get("email")),
            recipient_name=_safe(form_data.get("driver_name") or form_data.get("recipient_name")),
            division=_safe(form_data.get("division")),
            requested_items=form_data.get("requested_items") or [],
            uploaded_documents=manifest.get("uploaded_documents") or [],
            submitted_at=_safe(manifest.get("submitted_at") or form_data.get("final_submission_timestamp")),
            upload_key=_safe(manifest.get("upload_key")),
        )
        uploaded += result.get("uploaded", 0)
    return {"sent_events": sent, "upload_events": uploaded}


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------


def list_ledger_records(submissions_dir: Path, *, backfill: bool = True) -> list[dict[str, Any]]:
    if backfill:
        backfill_safety_ledger(submissions_dir)
    records = _read_ledger(_ledger_path(submissions_dir))
    now = _now()
    out: list[dict[str, Any]] = []
    for record in records.values():
        item = dict(record)
        item["ledger_state"] = ledger_state(item, now=now)
        item["last_sent_display"] = _display_dt(item.get("last_sent_at"))
        item["last_upload_display"] = _display_dt(item.get("last_upload_at"))
        item["suppressed_until_display"] = _display_dt(item.get("suppressed_until"))
        out.append(item)
    out.sort(key=lambda r: (r.get("last_upload_at") or r.get("last_sent_at") or r.get("last_seen_at") or ""), reverse=True)
    return out


def ledger_summary(submissions_dir: Path, *, backfill: bool = True) -> dict[str, int]:
    rows = list_ledger_records(submissions_dir, backfill=backfill)
    summary = {"total": len(rows), "new": 0, "recently_sent": 0, "needs_nudge": 0, "submitted": 0, "resolved": 0}
    for row in rows:
        key = str(row.get("ledger_state") or "").lower().replace(" ", "_")
        if key in summary:
            summary[key] += 1
    return summary
