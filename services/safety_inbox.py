"""Ingest safety paperwork that drivers send as EMAIL REPLIES.

Many drivers ignore the recipient-specific upload link and simply *reply* to the
safety request email with their documents attached. This module pulls those
replies from the safety mailbox(es) over IMAP, classifies each one under the
correct person, and stores the attachments in Supabase exactly like a portal
upload — so they show up on the staff ``?safety=1`` dashboard.

Document-type parsing is intentionally out of scope for now: every attachment is
labelled ``"Email reply attachment"`` and the human reviews it later.

Design notes
------------
* Streamlit Cloud cannot receive inbound email and has no scheduler, so this runs
  from a *separate* process (a GitHub Action cron) and/or a manual "pull now"
  button. All shared state therefore lives in Supabase:
    - link directory (``services.safety_link_store`` mirror) for matching, and
    - inbox state here (processed Message-IDs + the unmatched queue).
* The ledger is **not** written by this module. Saving the upload bundle to
  Supabase with ``upload_type == "safety_document_upload"`` is enough — the
  dashboard's ``backfill_safety_ledger`` reconstructs the ledger entry from the
  Supabase manifest.
* IMAP and the persistence functions are dependency-injected so the matching and
  ingestion logic is unit-testable without a live mailbox or Supabase.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from services.document_upload_page import (
    ALLOWED_DOCUMENT_EXTENSIONS,
    MAX_DOCUMENT_UPLOAD_FILES,
    MAX_DOCUMENT_UPLOAD_SIZE_BYTES,
)
from services.safety_cloud_state import read_state as _read_cloud_state
from services.safety_cloud_state import write_state as _write_cloud_state
from services.safety_ledger import record_upload_event
from services.safety_link_store import (
    find_link_by_message_id,
    find_link_by_ref_code,
    find_links_by_recipient_email,
)
from submission_storage import (
    get_runtime_secret,
    read_supporting_document_bytes,
    save_document_upload_bundle,
)

# Mirrors services.safety_upload_page._STORAGE_NAMESPACE so email-sourced and
# portal-sourced documents land in the same place and the same backfill sees them.
_STORAGE_NAMESPACE = "safety-uploads/live"
_INBOX_STATE_NAME = "inbox"
_LOCAL_INBOX_FILE = "inbox.json"

EMAIL_DOC_TYPE = "Email reply attachment"
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MAILBOX_FOLDER = "INBOX"

_REF_TAG_RE = re.compile(r"\[Ref:\s*([A-Za-z0-9]{6,})\s*\]", re.IGNORECASE)
_SAFETY_REF_LINE_RE = re.compile(r"Safety-Ref:\s*([A-Za-z0-9]{6,})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MailboxConfig:
    """A single Gmail/IMAP inbox the ingester should poll."""

    username: str
    password: str
    host: str = DEFAULT_IMAP_HOST
    port: int = DEFAULT_IMAP_PORT
    division: str = ""
    folder: str = DEFAULT_MAILBOX_FOLDER
    label: str = ""

    @property
    def display(self) -> str:
        return self.label or self.username


def _coerce_mailbox(entry: dict[str, Any]) -> MailboxConfig | None:
    username = str(entry.get("username") or entry.get("user") or entry.get("email") or "").strip()
    password = str(entry.get("password") or entry.get("app_password") or "").strip()
    if not username or not password:
        return None
    try:
        port = int(entry.get("port") or DEFAULT_IMAP_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_IMAP_PORT
    return MailboxConfig(
        username=username,
        password=password,
        host=str(entry.get("host") or DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST,
        port=port,
        division=str(entry.get("division") or "").strip(),
        folder=str(entry.get("folder") or entry.get("mailbox") or DEFAULT_MAILBOX_FOLDER).strip()
        or DEFAULT_MAILBOX_FOLDER,
        label=str(entry.get("label") or "").strip(),
    )


def load_inbox_mailboxes(raw_config: str | None = None) -> list[MailboxConfig]:
    """Parse the ``SAFETY_INBOX_MAILBOXES`` secret into mailbox configs.

    The secret is a JSON array (or single object) of mailbox entries, e.g.::

        [
                    {"username": "statements@example.com", "password": "app-pw"},
          {"username": "safety@xpresstransinc.com", "password": "app-pw",
           "division": "Xpress Trans, Inc"}
        ]

        ``division`` is optional. Omitting it is the recommended division-agnostic
        mode for today's single ``statements`` mailbox; matched replies still use
        the division from the original safety upload link. Per-division safety
        inboxes can be added later with no code change.
    """
    import json

    raw = raw_config if raw_config is not None else (get_runtime_secret("SAFETY_INBOX_MAILBOXES", "") or "")
    raw = str(raw).strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    mailboxes: list[MailboxConfig] = []
    seen: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        mailbox = _coerce_mailbox(entry)
        if mailbox is None:
            continue
        key = mailbox.username.lower()
        if key in seen:
            continue
        seen.add(key)
        mailboxes.append(mailbox)
    return mailboxes


# ---------------------------------------------------------------------------
# Email parsing
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sender_email_of(msg: Message) -> str:
    _, addr = parseaddr(str(msg.get("From", "") or ""))
    return addr.strip().lower()


def sender_name_of(msg: Message) -> str:
    name, addr = parseaddr(str(msg.get("From", "") or ""))
    name = str(name or "").strip()
    return name or (addr.split("@")[0] if addr else "Email reply")


def message_id_of(msg: Message) -> str:
    return str(msg.get("Message-ID", "") or "").strip()


def referenced_message_ids(msg: Message) -> list[str]:
    ids: list[str] = []
    for header in ("In-Reply-To", "References"):
        raw = str(msg.get(header, "") or "")
        ids.extend(re.findall(r"<[^>]+>", raw))
    seen: set[str] = set()
    ordered: list[str] = []
    for mid in ids:
        key = mid.strip().strip("<>").lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(mid.strip())
    return ordered


def _plain_text_body(msg: Message) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "text" and part.get_content_subtype() == "plain":
                try:
                    payload = part.get_payload(decode=True)
                except Exception:
                    payload = None
                if payload:
                    parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    else:
        try:
            payload = msg.get_payload(decode=True)
        except Exception:
            payload = None
        if payload:
            parts.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
    return "\n".join(parts)


def ref_code_from_email(msg: Message) -> str:
    subject = str(msg.get("Subject", "") or "")
    match = _REF_TAG_RE.search(subject)
    if match:
        return match.group(1).strip().upper()
    body = _plain_text_body(msg)
    match = _SAFETY_REF_LINE_RE.search(body) or _REF_TAG_RE.search(body)
    if match:
        return match.group(1).strip().upper()
    return ""


def extract_attachments(msg: Message) -> list[dict[str, Any]]:
    """Return allowed PDF/JPG/PNG attachments as normalized document dicts."""
    documents: list[dict[str, Any]] = []
    seen_digests: set[str] = set()
    for index, part in enumerate(msg.walk(), start=1):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = str(part.get("Content-Disposition", "") or "").lower()
        if not filename and "attachment" not in disposition:
            continue
        filename = email.utils.collapse_rfc2231_value(filename) if filename else ""
        filename = Path(str(filename or "")).name.strip() or f"attachment-{index}"
        extension = Path(filename).suffix.lower().lstrip(".")
        if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
            continue
        try:
            content = part.get_payload(decode=True)
        except Exception:
            content = None
        if not content:
            continue
        if len(content) > MAX_DOCUMENT_UPLOAD_SIZE_BYTES:
            continue
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen_digests:
            continue
        seen_digests.add(digest)
        documents.append(
            {
                "document_type": EMAIL_DOC_TYPE,
                "file_name": filename,
                "content": content,
                "content_type": part.get_content_type() or "application/octet-stream",
                "size_bytes": len(content),
                "content_digest": digest,
            }
        )
        if len(documents) >= MAX_DOCUMENT_UPLOAD_FILES:
            break
    return documents


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class ReplyMatch:
    link: dict[str, Any] | None
    basis: str  # "message_id" | "ref_code" | "recipient_email" | "unmatched"


def match_reply(submissions_dir: Path, msg: Message) -> ReplyMatch:
    """Resolve an email reply to the recipient it was originally sent to."""
    for mid in referenced_message_ids(msg):
        link = find_link_by_message_id(submissions_dir=submissions_dir, message_id=mid)
        if link:
            return ReplyMatch(link=link, basis="message_id")

    ref_code = ref_code_from_email(msg)
    if ref_code:
        link = find_link_by_ref_code(submissions_dir=submissions_dir, ref_code=ref_code)
        if link:
            return ReplyMatch(link=link, basis="ref_code")

    sender = sender_email_of(msg)
    if sender:
        links = find_links_by_recipient_email(submissions_dir=submissions_dir, email=sender)
        if links:
            return ReplyMatch(link=links[0], basis="recipient_email")

    return ReplyMatch(link=None, basis="unmatched")


# ---------------------------------------------------------------------------
# Inbox state (processed Message-IDs + unmatched queue)
# ---------------------------------------------------------------------------


def _local_inbox_path(submissions_dir: Path) -> Path:
    path = submissions_dir / "safety" / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path / _LOCAL_INBOX_FILE


def _read_inbox_state(submissions_dir: Path) -> dict[str, Any]:
    cloud = _read_cloud_state(_INBOX_STATE_NAME)
    local: dict[str, Any] = {}
    path = _local_inbox_path(submissions_dir)
    if path.exists():
        import json

        try:
            local = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            local = {}
    state = {"processed": {}, "unmatched": {}}
    for source in (cloud, local):
        if not isinstance(source, dict):
            continue
        processed = source.get("processed")
        if isinstance(processed, dict):
            state["processed"].update({str(k): v for k, v in processed.items()})
        unmatched = source.get("unmatched")
        if isinstance(unmatched, dict):
            state["unmatched"].update({str(k): v for k, v in unmatched.items()})
    return state


def _write_inbox_state(submissions_dir: Path, state: dict[str, Any]) -> None:
    import json

    path = _local_inbox_path(submissions_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)
    _write_cloud_state(_INBOX_STATE_NAME, state)


def _message_fingerprint(message_id: str, sender: str, attachments: Sequence[dict[str, Any]]) -> str:
    if message_id:
        return message_id.strip().strip("<>").lower()
    digest = hashlib.sha256()
    digest.update(sender.encode("utf-8"))
    for doc in attachments:
        digest.update(str(doc.get("content_digest") or "").encode("utf-8"))
    return f"nomid:{digest.hexdigest()[:24]}"


def list_unmatched_replies(submissions_dir: Path) -> list[dict[str, Any]]:
    """Return queued replies awaiting manual assignment, newest first."""
    state = _read_inbox_state(submissions_dir)
    rows = [dict(v, entry_id=k) for k, v in (state.get("unmatched") or {}).items() if isinstance(v, dict)]
    rows.sort(key=lambda row: str(row.get("received_at") or ""), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

SaveFn = Callable[..., dict[str, Any]]
RecordFn = Callable[..., dict[str, int]]


def _build_form_data(
    *,
    upload_type: str,
    recipient_name: str,
    recipient_email: str,
    division: str,
    sender_email: str,
    subject: str,
    message_id: str,
    match_basis: str,
    token: str,
    items: list[dict[str, Any]],
    submitted_at: str,
) -> dict[str, Any]:
    name_parts = recipient_name.split()
    return {
        "upload_type": upload_type,
        "source": "email_reply",
        "driver_name": recipient_name,
        "first_name": name_parts[0] if name_parts else recipient_name,
        "last_name": " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
        "email": recipient_email,
        "notes": f"Ingested from email reply (sender: {sender_email}; subject: {subject})".strip(),
        "document_types": [EMAIL_DOC_TYPE],
        "requested_items": items,
        "safety_link_token": token,
        "division": division,
        "email_reply_sender": sender_email,
        "email_reply_message_id": message_id,
        "email_reply_match_basis": match_basis,
        "final_submission_timestamp": submitted_at,
    }


def ingest_email_message(
    submissions_dir: Path,
    msg: Message,
    *,
    mailbox: MailboxConfig | None = None,
    save_fn: SaveFn = save_document_upload_bundle,
    record_fn: RecordFn = record_upload_event,
    submitted_at: str | None = None,
) -> dict[str, Any]:
    """Classify and persist one reply. Returns a result dict with ``status``.

    ``status`` is one of ``ingested``, ``unmatched``, ``skipped_no_attachments``.
    """
    submitted_at = submitted_at or _now_iso()
    message_id = message_id_of(msg)
    sender_email = sender_email_of(msg)
    subject = str(msg.get("Subject", "") or "").strip()
    attachments = extract_attachments(msg)
    if not attachments:
        return {
            "status": "skipped_no_attachments",
            "message_id": message_id,
            "sender": sender_email,
            "document_count": 0,
        }

    match = match_reply(submissions_dir, msg)
    division_hint = (mailbox.division if mailbox else "") or ""

    if match.link is not None:
        link = match.link
        recipient_email = str(link.get("recipient_email") or sender_email)
        recipient_name = str(link.get("recipient_name") or "Driver/Owner")
        division = str(link.get("division") or division_hint)
        token = str(link.get("token") or "")
        items = [dict(item) for item in (link.get("items") or []) if isinstance(item, dict)]
        form_data = _build_form_data(
            upload_type="safety_document_upload",
            recipient_name=recipient_name,
            recipient_email=recipient_email,
            division=division,
            sender_email=sender_email,
            subject=subject,
            message_id=message_id,
            match_basis=match.basis,
            token=token,
            items=items,
            submitted_at=submitted_at,
        )
        upload_result = save_fn(
            form_data=form_data,
            documents=attachments,
            local_base_dir=submissions_dir,
            storage_namespace=_STORAGE_NAMESPACE,
        )
        try:
            record_fn(
                submissions_dir,
                token=token,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                division=division,
                requested_items=items,
                uploaded_documents=upload_result.get("documents", []),
                submitted_at=submitted_at,
                upload_key=str(upload_result.get("upload_key") or ""),
            )
        except Exception:
            # The bundle is safely in Supabase; the dashboard backfill will
            # still reconstruct the ledger row even if this live write fails.
            pass
        return {
            "status": "ingested",
            "message_id": message_id,
            "sender": sender_email,
            "recipient_email": recipient_email,
            "recipient_name": recipient_name,
            "division": division,
            "match_basis": match.basis,
            "document_count": len(attachments),
            "upload_key": str(upload_result.get("upload_key") or ""),
        }

    # Unmatched -> hold for manual assignment, but still persist the bytes to
    # Supabase so nothing is lost if the IMAP message is later deleted.
    recipient_name = sender_name_of(msg)
    form_data = _build_form_data(
        upload_type="safety_email_reply_unmatched",
        recipient_name=recipient_name,
        recipient_email=sender_email,
        division=division_hint,
        sender_email=sender_email,
        subject=subject,
        message_id=message_id,
        match_basis=match.basis,
        token="",
        items=[],
        submitted_at=submitted_at,
    )
    upload_result = save_fn(
        form_data=form_data,
        documents=attachments,
        local_base_dir=submissions_dir,
        storage_namespace=_STORAGE_NAMESPACE,
    )
    entry_id = _message_fingerprint(message_id, sender_email, attachments)
    state = _read_inbox_state(submissions_dir)
    state.setdefault("unmatched", {})[entry_id] = {
        "received_at": submitted_at,
        "sender_email": sender_email,
        "sender_name": recipient_name,
        "subject": subject,
        "message_id": message_id,
        "mailbox": mailbox.username if mailbox else "",
        "division_hint": division_hint,
        "upload_key": str(upload_result.get("upload_key") or ""),
        "documents": upload_result.get("documents", []),
        "document_count": len(attachments),
    }
    _write_inbox_state(submissions_dir, state)
    return {
        "status": "unmatched",
        "message_id": message_id,
        "sender": sender_email,
        "entry_id": entry_id,
        "document_count": len(attachments),
    }


def assign_unmatched_reply(
    submissions_dir: Path,
    *,
    entry_id: str,
    recipient_email: str,
    recipient_name: str,
    division: str = "",
    token: str = "",
    save_fn: SaveFn = save_document_upload_bundle,
    record_fn: RecordFn = record_upload_event,
) -> dict[str, Any]:
    """Re-file a queued unmatched reply under a chosen person."""
    state = _read_inbox_state(submissions_dir)
    entry = (state.get("unmatched") or {}).get(entry_id)
    if not isinstance(entry, dict):
        return {"status": "error", "message": "Unmatched reply not found."}

    documents: list[dict[str, Any]] = []
    for doc in entry.get("documents") or []:
        if not isinstance(doc, dict):
            continue
        content = read_supporting_document_bytes(doc, local_base_dir=submissions_dir)
        if not content:
            continue
        documents.append(
            {
                "document_type": EMAIL_DOC_TYPE,
                "file_name": doc.get("file_name") or "attachment",
                "content": content,
                "content_type": doc.get("content_type") or "application/octet-stream",
                "size_bytes": int(doc.get("size_bytes") or len(content)),
                "content_digest": doc.get("content_digest") or hashlib.sha256(content).hexdigest(),
            }
        )
    if not documents:
        return {"status": "error", "message": "Could not load the stored attachments for this reply."}

    submitted_at = _now_iso()
    recipient_name = str(recipient_name or entry.get("sender_name") or "Driver/Owner").strip() or "Driver/Owner"
    recipient_email = str(recipient_email or entry.get("sender_email") or "").strip().lower()
    division = str(division or entry.get("division_hint") or "").strip()
    form_data = _build_form_data(
        upload_type="safety_document_upload",
        recipient_name=recipient_name,
        recipient_email=recipient_email,
        division=division,
        sender_email=str(entry.get("sender_email") or ""),
        subject=str(entry.get("subject") or ""),
        message_id=str(entry.get("message_id") or ""),
        match_basis="manual_assignment",
        token=token,
        items=[],
        submitted_at=submitted_at,
    )
    upload_result = save_fn(
        form_data=form_data,
        documents=documents,
        local_base_dir=submissions_dir,
        storage_namespace=_STORAGE_NAMESPACE,
    )
    try:
        record_fn(
            submissions_dir,
            token=token,
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            division=division,
            requested_items=[],
            uploaded_documents=upload_result.get("documents", []),
            submitted_at=submitted_at,
            upload_key=str(upload_result.get("upload_key") or ""),
        )
    except Exception:
        pass

    state.get("unmatched", {}).pop(entry_id, None)
    _write_inbox_state(submissions_dir, state)
    return {
        "status": "assigned",
        "recipient_email": recipient_email,
        "recipient_name": recipient_name,
        "document_count": len(documents),
        "upload_key": str(upload_result.get("upload_key") or ""),
    }


# ---------------------------------------------------------------------------
# IMAP polling
# ---------------------------------------------------------------------------

ImapFactory = Callable[[MailboxConfig], "imaplib.IMAP4"]


def _default_imap_factory(config: MailboxConfig) -> imaplib.IMAP4:
    connection = imaplib.IMAP4_SSL(config.host, config.port)
    connection.login(config.username, config.password)
    return connection


def ingest_mailbox(
    config: MailboxConfig,
    submissions_dir: Path,
    *,
    imap_factory: ImapFactory = _default_imap_factory,
    max_messages: int = 50,
) -> dict[str, Any]:
    """Poll one mailbox for unseen replies and ingest their attachments."""
    summary = {
        "mailbox": config.username,
        "ingested": 0,
        "unmatched": 0,
        "skipped": 0,
        "errors": [],
        "results": [],
    }
    try:
        connection = imap_factory(config)
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(f"connect:{exc}")
        return summary

    try:
        connection.select(config.folder)
        status, data = connection.search(None, "UNSEEN")
        if status != "OK":
            summary["errors"].append("search_failed")
            return summary
        message_numbers = (data[0].split() if data and data[0] else [])[:max_messages]
        processed_state = _read_inbox_state(submissions_dir).get("processed", {})
        for num in message_numbers:
            try:
                status, fetched = connection.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                    summary["errors"].append("fetch_failed")
                    continue
                raw_bytes = fetched[0][1]
                msg = email.message_from_bytes(raw_bytes)
                fingerprint = _message_fingerprint(
                    message_id_of(msg), sender_email_of(msg), extract_attachments(msg)
                )
                if fingerprint in processed_state:
                    summary["skipped"] += 1
                    _mark_seen(connection, num)
                    continue
                result = ingest_email_message(submissions_dir, msg, mailbox=config)
                summary["results"].append(result)
                if result["status"] == "ingested":
                    summary["ingested"] += 1
                elif result["status"] == "unmatched":
                    summary["unmatched"] += 1
                else:
                    summary["skipped"] += 1
                _record_processed(submissions_dir, fingerprint, config, result)
                _mark_seen(connection, num)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(str(exc))
    finally:
        try:
            connection.logout()
        except Exception:
            pass
    return summary


def _mark_seen(connection: "imaplib.IMAP4", num: bytes) -> None:
    try:
        connection.store(num, "+FLAGS", "\\Seen")
    except Exception:
        pass


def _record_processed(
    submissions_dir: Path,
    fingerprint: str,
    config: MailboxConfig,
    result: dict[str, Any],
) -> None:
    state = _read_inbox_state(submissions_dir)
    state.setdefault("processed", {})[fingerprint] = {
        "processed_at": _now_iso(),
        "mailbox": config.username,
        "status": result.get("status"),
    }
    _write_inbox_state(submissions_dir, state)


def ingest_all(
    submissions_dir: Path,
    *,
    mailboxes: Iterable[MailboxConfig] | None = None,
    imap_factory: ImapFactory = _default_imap_factory,
) -> dict[str, Any]:
    """Poll every configured mailbox and return an aggregate summary."""
    configs = list(mailboxes) if mailboxes is not None else load_inbox_mailboxes()
    overall = {
        "mailboxes": len(configs),
        "ingested": 0,
        "unmatched": 0,
        "skipped": 0,
        "errors": [],
        "by_mailbox": [],
    }
    if not configs:
        overall["errors"].append("no_mailboxes_configured")
        return overall
    for config in configs:
        result = ingest_mailbox(config, submissions_dir, imap_factory=imap_factory)
        overall["ingested"] += result["ingested"]
        overall["unmatched"] += result["unmatched"]
        overall["skipped"] += result["skipped"]
        overall["errors"].extend(f"{config.username}:{err}" for err in result["errors"])
        overall["by_mailbox"].append(result)
    return overall
