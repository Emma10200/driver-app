"""Read-only rate-confirmation email ingest helpers.

First-layer pipeline:
- Gmail/IMAP is the source of truth; the folder is opened read-only.
- The dispatch board is the finite universe of valid truck IDs.
- Each attachment/document gets at most ONE selected truck.
- If multiple plausible trucks are found for one attachment, the row is marked
  ambiguous/red instead of creating multiple assignments.
- PDF parsing/OCR is intentionally second-layer work. This module uses only
  subject, email body, and attachment filenames for fast matching.
"""

from __future__ import annotations

import email
import hashlib
import html
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Iterable, Sequence

from services.dispatch_board_data import load_dispatch_board_rows
from services.qbo_supabase import SupabaseRestClient
from submission_storage import get_runtime_secret

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MAILBOX_FOLDER = "INBOX"

RATE_CONF_TABLE = "rate_confirmation_documents"
NOISE_DOMAINS = {"accounts.google.com", "google.com"}

SOURCE_RANK = {"subject": 0, "filename": 1, "email_body": 2}
LABEL_RANK = {"truck_label": 0, "number": 1, "trailer_label": 2}
MATCH_RANK = {"exact": 0, "one_digit_off": 1, "two_digits_off": 2}

NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(\d{2,6})(?![A-Za-z0-9])")
TAGGED_TRUCK_RE = re.compile(r"\b(?:truck|unit|tractor)\s*#?\s*(\d{2,6})\b", re.IGNORECASE)
TAGGED_TRAILER_RE = re.compile(r"\btrail?er\s*#?\s*(\d{2,6})\b", re.IGNORECASE)
LOAD_HINT_RE = re.compile(
    r"\b(?:load|order|carrier|reference|ref|po|paynumber|bol|ldi\s+load)\s*#?\s*[:\-]?\s*([A-Za-z0-9\-]{4,})",
    re.IGNORECASE,
)
RATEISH_SUBJECT_RE = re.compile(r"rate|confirm|load|truck|cancel|carrier|tender|dispatch", re.IGNORECASE)


@dataclass(frozen=True)
class MailboxConfig:
    username: str
    password: str
    host: str = DEFAULT_IMAP_HOST
    port: int = DEFAULT_IMAP_PORT
    folder: str = DEFAULT_MAILBOX_FOLDER


@dataclass(frozen=True)
class BoardTruck:
    truck_id: str
    dispatcher: str = ""
    driver_name: str = ""
    division: str = ""
    status: str = ""
    sheet_row: int = 0


@dataclass(frozen=True)
class NumberMention:
    token: str
    source: str
    label: str


def load_mailbox_config() -> MailboxConfig | None:
    username = str(get_runtime_secret("RATE_CONF_EMAIL", "") or "").strip()
    password = re.sub(r"\s+", "", str(get_runtime_secret("RATE_CONF_APP_PASSWORD", "") or ""))
    if not username or not password:
        return None
    try:
        port = int(get_runtime_secret("RATE_CONF_IMAP_PORT", str(DEFAULT_IMAP_PORT)) or DEFAULT_IMAP_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_IMAP_PORT
    return MailboxConfig(
        username=username,
        password=password,
        host=str(get_runtime_secret("RATE_CONF_IMAP_HOST", DEFAULT_IMAP_HOST) or DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST,
        port=port,
        folder=str(get_runtime_secret("RATE_CONF_MAILBOX_FOLDER", DEFAULT_MAILBOX_FOLDER) or DEFAULT_MAILBOX_FOLDER).strip()
        or DEFAULT_MAILBOX_FOLDER,
    )


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _received_at(msg: Message) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:
        return None


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def message_body_text(msg: Message, max_chars: int = 20000) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in disposition or content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if content_type == "text/plain":
            plain_parts.append(text)
        else:
            html_parts.append(_strip_html(text))
    return "\n".join(plain_parts or html_parts)[:max_chars]


def sender_domain_to_division(domain: str) -> str:
    domain = domain.lower().strip()
    if domain == "prestige.inc":
        return "pg"
    if domain == "prestigecalifornia.com":
        return "prestige"
    if domain == "xpresstransinc.com":
        return "xpress"
    if domain == "prestigetransportation.com":
        return "internal"
    return ""


def clean_unit(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d+", text)
    return match.group(0) if match else ""


def board_trucks_from_rows(rows: Sequence[dict[str, Any]]) -> dict[str, BoardTruck]:
    trucks: dict[str, BoardTruck] = {}
    for row in rows:
        truck_id = clean_unit(row.get("truck_id"))
        if not truck_id:
            continue
        trucks.setdefault(
            truck_id,
            BoardTruck(
                truck_id=truck_id,
                dispatcher=str(row.get("dispatcher") or "").strip(),
                driver_name=str(row.get("driver_name") or "").strip(),
                division=str(row.get("division") or "").strip(),
                status=str(row.get("status") or "").strip(),
                sheet_row=int(row.get("sheet_row") or 0),
            ),
        )
    return trucks


def extract_number_mentions(text: str, source: str) -> list[NumberMention]:
    mentions: list[NumberMention] = []
    seen: set[tuple[str, str]] = set()
    truck_labeled_tokens = {match.group(1) for match in TAGGED_TRUCK_RE.finditer(text or "")}
    trailer_labeled_tokens = {match.group(1) for match in TAGGED_TRAILER_RE.finditer(text or "")}
    trailer_only_tokens = trailer_labeled_tokens - truck_labeled_tokens
    for pattern, label in ((TAGGED_TRUCK_RE, "truck_label"), (TAGGED_TRAILER_RE, "trailer_label")):
        for match in pattern.finditer(text or ""):
            token = match.group(1)
            key = (token, label)
            if key not in seen:
                mentions.append(NumberMention(token=token, source=source, label=label))
                seen.add(key)
    for match in NUMBER_TOKEN_RE.finditer(text or ""):
        token = match.group(1)
        if token in trailer_only_tokens:
            continue
        key = (token, "number")
        if key not in seen:
            mentions.append(NumberMention(token=token, source=source, label="number"))
            seen.add(key)
    return mentions


def extract_load_references(texts: Iterable[str]) -> list[str]:
    refs: list[str] = []
    for text in texts:
        for match in LOAD_HINT_RE.finditer(text or ""):
            value = match.group(1).strip().strip(".,;:)")
            if value and value not in refs:
                refs.append(value)
    return refs[:10]


def _same_length_digit_distance(a: str, b: str) -> int | None:
    if len(a) != len(b):
        return None
    return sum(1 for left, right in zip(a, b, strict=True) if left != right)


def _match_kind(token: str, truck_id: str) -> tuple[str, int] | None:
    if token == truck_id:
        return "exact", 0
    distance = _same_length_digit_distance(token, truck_id)
    if distance == 1:
        return "one_digit_off", 1
    if distance == 2:
        return "two_digits_off", 2
    return None


def candidate_matches(mentions: Sequence[NumberMention], board: dict[str, BoardTruck]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for mention in mentions:
        # A number explicitly labeled as a trailer should be retained in extracted
        # numbers but should not assign a truck by itself.
        if mention.label == "trailer_label":
            continue
        for truck_id, truck in board.items():
            kind = _match_kind(mention.token, truck_id)
            if not kind:
                continue
            match_type, distance = kind
            matches.append(
                {
                    "token": mention.token,
                    "matched_truck": truck_id,
                    "match_type": match_type,
                    "digit_distance": distance,
                    "source": mention.source,
                    "label": mention.label,
                    "board_dispatcher": truck.dispatcher,
                    "board_driver": truck.driver_name,
                    "board_division": truck.division,
                    "board_status": truck.status,
                    "board_sheet_row": truck.sheet_row,
                }
            )
    matches.sort(
        key=lambda item: (
            MATCH_RANK.get(str(item["match_type"]), 99),
            LABEL_RANK.get(str(item["label"]), 99),
            SOURCE_RANK.get(str(item["source"]), 99),
            len(str(item["token"])),
            str(item["matched_truck"]),
        )
    )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for match in matches:
        key = (
            str(match["matched_truck"]),
            str(match["match_type"]),
            str(match["source"]),
            str(match["token"]),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def select_single_truck(matches: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return selected truck metadata plus alert fields.

    If the best-ranked group contains multiple trucks, do NOT select any truck.
    That implements the business rule: one attachment should never assign to two
    trucks; ambiguous rows go to review instead.
    """
    if not matches:
        return {
            "matched_truck_id": "",
            "match_status": "unmatched",
            "match_type": "",
            "match_source": "",
            "match_token": "",
            "match_confidence": None,
            "alert_level": "red",
            "alert_codes": ["no_board_truck_match"],
            "alert_notes": "No exact/near current dispatch-board truck number found in subject, body, or attachment filename.",
            "best_match": None,
        }

    best = matches[0]
    best_key = (
        MATCH_RANK.get(str(best["match_type"]), 99),
        LABEL_RANK.get(str(best["label"]), 99),
        SOURCE_RANK.get(str(best["source"]), 99),
    )
    tied = [
        item
        for item in matches
        if (
            MATCH_RANK.get(str(item["match_type"]), 99),
            LABEL_RANK.get(str(item["label"]), 99),
            SOURCE_RANK.get(str(item["source"]), 99),
        )
        == best_key
    ]
    tied_trucks = sorted({str(item["matched_truck"]) for item in tied})
    if len(tied_trucks) > 1:
        return {
            "matched_truck_id": "",
            "match_status": "ambiguous",
            "match_type": str(best["match_type"]),
            "match_source": str(best["source"]),
            "match_token": str(best["token"]),
            "match_confidence": 0.0,
            "alert_level": "red",
            "alert_codes": ["multiple_truck_candidates_one_attachment"],
            "alert_notes": f"One attachment produced multiple equally ranked truck candidates: {', '.join(tied_trucks)}.",
            "best_match": best,
        }

    match_type = str(best["match_type"])
    if match_type == "exact":
        return {
            "matched_truck_id": str(best["matched_truck"]),
            "match_status": "matched",
            "match_type": match_type,
            "match_source": str(best["source"]),
            "match_token": str(best["token"]),
            "match_confidence": 1.0,
            "alert_level": "",
            "alert_codes": [],
            "alert_notes": "",
            "best_match": best,
        }
    if match_type == "one_digit_off":
        return {
            "matched_truck_id": str(best["matched_truck"]),
            "match_status": "near_match",
            "match_type": match_type,
            "match_source": str(best["source"]),
            "match_token": str(best["token"]),
            "match_confidence": 0.98,
            "alert_level": "info",
            "alert_codes": ["one_digit_off_truck_match"],
            "alert_notes": f"Matched board truck {best['matched_truck']} from nearby token {best['token']}.",
            "best_match": best,
        }
    return {
        "matched_truck_id": str(best["matched_truck"]),
        "match_status": "ambiguous",
        "match_type": match_type,
        "match_source": str(best["source"]),
        "match_token": str(best["token"]),
        "match_confidence": 0.65,
        "alert_level": "yellow",
        "alert_codes": ["two_digits_off_truck_match_review"],
        "alert_notes": f"Only a two-digit-off board truck match was found: {best['token']} -> {best['matched_truck']}.",
        "best_match": best,
    }


def _attachment_parts(msg: Message) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = _decode(part.get_filename())
        disposition = str(part.get("Content-Disposition", ""))
        content_type = part.get_content_type()
        is_attachment = "attachment" in disposition.lower() or bool(filename)
        if not is_attachment:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        out.append(
            {
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest() if payload else "",
            }
        )
    return out


def _safe_doc_key(message_id: str, attachment_index: int, digest: str) -> str:
    basis = f"{message_id}|{attachment_index}|{digest}".encode("utf-8", "replace")
    return hashlib.sha256(basis).hexdigest()


def build_document_rows_from_message(msg: Message, board: dict[str, BoardTruck]) -> list[dict[str, Any]]:
    from_name, from_addr = parseaddr(msg.get("From", ""))
    domain = from_addr.split("@", 1)[-1].lower() if "@" in from_addr else ""
    subject = _decode(msg.get("Subject"))
    message_id = str(msg.get("Message-ID") or "").strip()
    thread_id = str(msg.get("Thread-Index") or msg.get("References") or msg.get("In-Reply-To") or "").strip()[:500]
    received = _received_at(msg)
    body = message_body_text(msg)
    attachments = _attachment_parts(msg)

    if domain in NOISE_DOMAINS:
        return []

    # Attachment rows are the core unit. If there is no attachment but the message
    # is clearly an assignment/cancel/rate-con control email, create one message row.
    units = attachments or ([{"filename": "", "content_type": "message/rfc822", "size_bytes": 0, "sha256": ""}] if RATEISH_SUBJECT_RE.search(subject) else [])
    rows: list[dict[str, Any]] = []
    for index, attachment in enumerate(units, start=1):
        filename = str(attachment.get("filename") or "")
        texts = [subject, body, filename]
        mentions: list[NumberMention] = []
        for source, text in (("subject", subject), ("email_body", body), ("filename", filename)):
            mentions.extend(extract_number_mentions(text, source))
        matches = candidate_matches(mentions, board)
        selection = select_single_truck(matches)
        best = selection.get("best_match") or {}
        alert_codes = list(selection["alert_codes"] or [])
        alert_level = str(selection["alert_level"] or "")
        alert_notes = str(selection["alert_notes"] or "")

        if re.search(r"\bcancel(?:led|lation)?\b", subject, re.IGNORECASE):
            alert_codes.append("cancel_notice")
            if not alert_level:
                alert_level = "yellow"
            if selection["matched_truck_id"]:
                selection["match_status"] = "cancelled"

        selected_truck = board.get(str(selection["matched_truck_id"] or ""))
        load_refs = extract_load_references(texts)
        digest = str(attachment.get("sha256") or "")
        row = {
            "document_key": _safe_doc_key(message_id or subject, index, digest or filename),
            "message_id": message_id,
            "thread_id": thread_id,
            "attachment_index": index,
            "attachment_filename": filename,
            "attachment_content_type": str(attachment.get("content_type") or ""),
            "attachment_size_bytes": int(attachment.get("size_bytes") or 0),
            "attachment_sha256": digest,
            "received_at": received.isoformat() if received else None,
            "sender_name": _decode(from_name),
            "sender_email": from_addr,
            "sender_domain": domain,
            "domain_division": sender_domain_to_division(domain),
            "subject": subject,
            "matched_truck_id": selection["matched_truck_id"],
            "match_status": selection["match_status"],
            "match_type": selection["match_type"],
            "match_source": selection["match_source"],
            "match_token": selection["match_token"],
            "match_confidence": selection["match_confidence"],
            "candidate_matches": matches[:25],
            "extracted_numbers": [mention.__dict__ for mention in mentions[:100]],
            "board_dispatcher": selected_truck.dispatcher if selected_truck else str(best.get("board_dispatcher") or ""),
            "board_driver_name": selected_truck.driver_name if selected_truck else str(best.get("board_driver") or ""),
            "board_division": selected_truck.division if selected_truck else str(best.get("board_division") or ""),
            "board_sheet_row": selected_truck.sheet_row if selected_truck else best.get("board_sheet_row"),
            "load_reference": load_refs[0] if load_refs else "",
            "alert_level": alert_level,
            "alert_codes": alert_codes,
            "alert_notes": alert_notes,
            "raw": {
                "load_references": load_refs,
                "body_preview": body[:2000],
                "all_attachment_count": len(attachments),
            },
        }
        rows.append(row)
    return rows


def _imap_since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%d-%b-%Y")


def fetch_recent_document_rows(mailbox: MailboxConfig, board: dict[str, BoardTruck], *, days: int = 14, limit: int = 0) -> list[dict[str, Any]]:
    client = imaplib.IMAP4_SSL(mailbox.host, mailbox.port)
    try:
        client.login(mailbox.username, mailbox.password)
        status, _ = client.select(mailbox.folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not select IMAP folder {mailbox.folder!r}: {status}")
        status, data = client.search(None, "SINCE", _imap_since(days))
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()
        if limit and limit > 0:
            ids = ids[-limit:]
        rows: list[dict[str, Any]] = []
        for msg_id in ids:
            status, msg_data = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            rows.extend(build_document_rows_from_message(msg, board))
        return rows
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass


def ingest_recent_rate_confirmations(*, days: int = 14, limit: int = 0, dry_run: bool = False) -> dict[str, Any]:
    mailbox = load_mailbox_config()
    if mailbox is None:
        raise RuntimeError("RATE_CONF_EMAIL and RATE_CONF_APP_PASSWORD are required.")
    board = board_trucks_from_rows(load_dispatch_board_rows())
    if not board:
        raise RuntimeError("No current dispatch-board truck IDs found.")
    rows = fetch_recent_document_rows(mailbox, board, days=days, limit=limit)
    summary = _summarize_rows(rows)
    summary.update({"days": days, "dry_run": dry_run, "dispatch_board_trucks": len(board)})
    if dry_run or not rows:
        return summary
    client = SupabaseRestClient()
    # Upsert in modest chunks to avoid huge PostgREST payloads.
    upserted = 0
    for start in range(0, len(rows), 250):
        batch = rows[start : start + 250]
        upserted += len(client.upsert(RATE_CONF_TABLE, batch, on_conflict="document_key"))
    summary["upserted"] = upserted
    return summary


def _summarize_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_alert: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        by_status[str(row.get("match_status") or "")] = by_status.get(str(row.get("match_status") or ""), 0) + 1
        level = str(row.get("alert_level") or "none") or "none"
        by_alert[level] = by_alert.get(level, 0) + 1
        source = str(row.get("match_source") or "unmatched") or "unmatched"
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "documents": len(rows),
        "match_status_counts": dict(sorted(by_status.items())),
        "alert_level_counts": dict(sorted(by_alert.items())),
        "match_source_counts": dict(sorted(by_source.items())),
    }
