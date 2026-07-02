"""Read-only rate-confirmation email ingest helpers.

First-layer pipeline:
- Gmail/IMAP is the source of truth; the folder is opened read-only.
- The dispatch board is the finite universe of valid truck IDs.
- Each attachment/document gets at most ONE selected truck.
- If multiple plausible trucks are found for one attachment, the row is marked
  ambiguous/red instead of creating multiple assignments.
- PDF text-layer parsing runs first; scanned/image-only PDFs optionally fall
    back to free Tesseract OCR when Poppler/Tesseract are installed.
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
from services.rate_confirmation_ocr import OcrResult, ocr_pdf_text
from submission_storage import get_runtime_secret

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MAILBOX_FOLDER = "INBOX"

RATE_CONF_TABLE = "rate_confirmation_documents"
NOISE_DOMAINS = {"accounts.google.com", "google.com"}
# User-requested sender exclusion: BOL chatter is not rate-confirmation input.
# Do not exclude statements@prestigetransportation.com; statement messages may
# still carry dispatch/accounting context the user wants retained.
EXCLUDED_SENDER_EMAILS = {"bol@prestige.inc", "bol@prestigetransportation.com"}

SOURCE_RANK = {"subject": 0, "filename": 1, "email_body": 2, "quoted_body": 3, "pdf_text": 4}
LABEL_RANK = {"truck_label": 0, "number": 1, "trailer_pairing": 2, "short_number": 3, "trailer_label": 4}
MATCH_RANK = {"exact": 0, "one_digit_off": 1, "two_digits_off": 2}

# Bare numbers must be 3-6 digits: 2-digit standalone tokens (dates, weights,
# quantities) are far too noisy. Trucks with 2-digit IDs (45, 67, 95) can still
# match via an explicit "TRUCK 67" label, or as a dispatcher-scoped rescue when
# the sender's own board has that exact 2-digit truck (label "short_number").
NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(\d{3,6})(?![A-Za-z0-9])")
SHORT_NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(\d{2})(?![A-Za-z0-9])")
TAGGED_TRUCK_RE = re.compile(r"\b(?:truck|trk|unit|tractor)\s*#?\s*(\d{2,6})\b", re.IGNORECASE)
TAGGED_TRAILER_RE = re.compile(r"\btrail?er\s*#?\s*(\d{2,6})\b", re.IGNORECASE)
# Standard company signature/footer blocks. Everything from the first marker on
# is boilerplate (phones, MC numbers, addresses) that poisons number extraction.
SIGNATURE_MARKER_RE = re.compile(
    r"(?im)^\s*\*?\s*(?:"
    r"please verify with us before booking"
    r"|we do not use any gmail"
    r"|award-winning service"
    r"|click here to see truck availability"
    r"|fleet\s*&\s*safety manager"
    r"|hazmat approved"
    r")"
)
# Inline signature/logo images that should not become their own document rows.
INLINE_IMAGE_NAME_RE = re.compile(r"^(?:image\d*|outlook-\w*|icon\w*|logo\w*)\.(?:png|jpe?g|gif)$", re.IGNORECASE)
# Markers that start a quoted reply / forwarded chain inside an email body.
QUOTE_MARKER_RE = re.compile(
    r"(?im)^\s*(?:"
    r">"
    r"|On .{0,150}wrote:"
    r"|-{2,}\s*Original Message\s*-{2,}"
    r"|-{4,}\s*Forwarded message\s*-{4,}"
    r"|Begin forwarded message:"
    r"|From:\s\S.{0,150}"
    r")\s*$"
)
LOAD_HINT_RE = re.compile(
    r"\b(?:load|order|carrier|reference|ref|po|paynumber|bol|ldi\s+load)\s*#?\s*[:\-]?\s*([A-Za-z0-9\-]{4,})",
    re.IGNORECASE,
)
RATEISH_SUBJECT_RE = re.compile(r"rate|confirm|load|truck|cancel|carrier|tender|dispatch", re.IGNORECASE)


def _truthy_secret(name: str, default: str = "true") -> bool:
    return str(get_runtime_secret(name, default) or default).strip().lower() not in {"0", "false", "no", "off"}


def _int_secret(name: str, default: int) -> int:
    try:
        return int(get_runtime_secret(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _str_secret(name: str, default: str = "") -> str:
    return str(get_runtime_secret(name, default) or default).strip()

# ---------------------------------------------------------------------------
# Sender email → board dispatcher resolution
# ---------------------------------------------------------------------------
# Mapping from known sender emails to board dispatcher names. This handles the
# two-Carlos split and generic "Prestige DispatchN" senders whose first name
# doesn't match a board dispatcher.
_EMAIL_TO_DISPATCHER: dict[str, str] = {
    "dispatch1@prestige.inc": "Carlos IL",
    "dispatch1@prestigecalifornia.com": "Carlos CA",
    "dispatch3@prestige.inc": "Brittany",
    "dispatch3@prestigecalifornia.com": "Carlos CA",
    "dispatch4@prestige.inc": "Carlos CA",
    "dispatch4@prestigecalifornia.com": "Carlos CA",
    "dispatch5@prestige.inc": "Anna",
    "dispatch7@prestige.inc": "Felix",
    "dispatch7@prestigecalifornia.com": "Carlos CA",
    "dispatch8@prestige.inc": "Lily",
    "dispatch@prestige.inc": "Felix",
    "dispatch@prestigecalifornia.com": "Carlos CA",
    "dispatch@xpresstransinc.com": "Brittany",
    "brittany@xpresstransinc.com": "Brittany",
    "carlos@xpresstransinc.com": "Carlos IL",
    "matt@prestige.inc": "Matt",
}


def resolve_sender_dispatcher(sender_email: str, sender_name: str, board: dict[str, "BoardTruck"]) -> str:
    """Best-effort resolve a sender email/name to a board dispatcher name.

    Priority:
    1. Exact email→dispatcher lookup table
    2. Sender display name first-name match against board dispatcher first names
    3. Empty string (unknown)
    """
    email_lower = sender_email.lower().strip()
    if email_lower in _EMAIL_TO_DISPATCHER:
        return _EMAIL_TO_DISPATCHER[email_lower]
    # First-name match from sender display name
    sender_first = (sender_name or "").split()[0].lower().strip() if sender_name else ""
    if sender_first:
        dispatcher_first_names: dict[str, str] = {}
        for truck in board.values():
            if truck.dispatcher:
                first = truck.dispatcher.split()[0].lower().strip()
                dispatcher_first_names.setdefault(first, truck.dispatcher)
        if sender_first in dispatcher_first_names:
            return dispatcher_first_names[sender_first]
    return ""


def is_excluded_sender_email(sender_email: str) -> bool:
    """Return True when a sender should never create rate-confirmation docs."""
    return sender_email.lower().strip() in EXCLUDED_SENDER_EMAILS


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
    trailer_id: str = ""


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
                trailer_id=clean_unit(row.get("trailer_id")),
            ),
        )
    return trucks


def board_trailer_map(board: dict[str, BoardTruck]) -> dict[str, str]:
    """Trailer number -> truck number from current board pairings.

    Dispatchers often type "39 / 4907 / name" (truck, trailer). Even when the
    truck token is unusable (2-digit, missing), the trailer uniquely identifies
    the board row. Trailers paired to more than one truck are dropped.
    """
    counts: dict[str, set[str]] = {}
    for truck in board.values():
        if truck.trailer_id:
            counts.setdefault(truck.trailer_id, set()).add(truck.truck_id)
    return {trailer: next(iter(trucks)) for trailer, trucks in counts.items() if len(trucks) == 1}


def split_quoted_reply(body: str) -> tuple[str, str]:
    """Split an email body into (top_part, quoted_part).

    The top part is what the sender actually typed (e.g. "TRUCK 940 DRIVER SAM").
    The quoted part is the forwarded broker email / reply chain, which is full of
    load numbers and other digit noise, so it must be matched at lowest priority.
    """
    if not body:
        return "", ""
    match = QUOTE_MARKER_RE.search(body)
    if not match:
        return body, ""
    return body[: match.start()], body[match.start():]


def strip_signature(text: str) -> str:
    """Cut the dispatcher's text at the first company signature/footer marker.

    The standard footers are full of phone numbers, MC numbers, and addresses
    that would otherwise become bogus truck candidates.
    """
    if not text:
        return ""
    match = SIGNATURE_MARKER_RE.search(text)
    return text[: match.start()] if match else text


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
    for match in SHORT_NUMBER_TOKEN_RE.finditer(text or ""):
        token = match.group(1)
        if token in trailer_only_tokens:
            continue
        key = (token, "short_number")
        if key not in seen:
            mentions.append(NumberMention(token=token, source=source, label="short_number"))
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


def _match_type_allowed(label: str, source: str, match_type: str, token: str) -> bool:
    """Gate which match types are allowed per mention label and source.

    - Exact matches are always allowed (short 2-digit tokens are additionally
      gated by sender dispatcher at selection time).
    - Near matches (one/two digits off) are only trusted for explicitly labeled
      truck tokens or subject/filename tokens — a bare number in an email body
      that is "one digit off" a truck is almost always a load-number fragment,
      not a typo.
    """
    if match_type == "exact":
        return True
    if label in {"short_number", "trailer_pairing"}:
        return False
    if label == "truck_label":
        if match_type == "one_digit_off":
            return len(token) >= 3
        return len(token) >= 4
    if source in {"subject", "filename"}:
        return match_type == "one_digit_off" and len(token) >= 3
    return False


def candidate_matches(
    mentions: Sequence[NumberMention],
    board: dict[str, BoardTruck],
    trailer_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    trailer_map = trailer_map or {}
    matches: list[dict[str, Any]] = []
    for mention in mentions:
        # A trailer/bare number that exactly matches a board trailer identifies
        # the paired truck (dispatchers often type "truck / trailer / name").
        if mention.token in trailer_map and mention.label in {"number", "trailer_label", "short_number"}:
            paired_truck = board.get(trailer_map[mention.token])
            if paired_truck is not None:
                matches.append(
                    {
                        "token": mention.token,
                        "matched_truck": paired_truck.truck_id,
                        "match_type": "exact",
                        "digit_distance": 0,
                        "source": mention.source,
                        "label": "trailer_pairing",
                        "board_dispatcher": paired_truck.dispatcher,
                        "board_driver": paired_truck.driver_name,
                        "board_division": paired_truck.division,
                        "board_status": paired_truck.status,
                        "board_sheet_row": paired_truck.sheet_row,
                    }
                )
        # A number explicitly labeled as a trailer should be retained in extracted
        # numbers but should not assign a truck by itself.
        if mention.label == "trailer_label":
            continue
        for truck_id, truck in board.items():
            kind = _match_kind(mention.token, truck_id)
            if not kind:
                continue
            match_type, distance = kind
            if not _match_type_allowed(mention.label, mention.source, match_type, mention.token):
                continue
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


def select_single_truck(
    matches: Sequence[dict[str, Any]],
    *,
    sender_dispatcher: str = "",
    gps_active_trucks: set[str] | None = None,
) -> dict[str, Any]:
    """Return selected truck metadata plus alert fields.

    Resolution order:
        1. **Global exact evidence**: an exact truck label/subject/filename/body
            match wins before dispatcher ownership. Dispatchers sometimes send
            loads for trucks not assigned to them (e.g. coverage/helping another
            board), so dispatcher is strong context but not a hard gate.
        2. **Dispatcher filter**: if no clear global exact match exists, use the
            sender's dispatcher assignment to resolve noisy/fuzzy candidates.
        3. **Source cascade**: subject > filename > email_body > quoted_body > pdf_text.
        4. **GPS active tie-break**: among remaining ties, prefer trucks with a
         recent GPS ping.
        5. **Ambiguous**: only if all above fail.
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

    active = gps_active_trucks or set()

    # Bare 2-digit tokens are only usable as a dispatcher-scoped rescue: the
    # sender's own board must have that exact truck (e.g. Brittany typing "39").
    disp_lower = sender_dispatcher.lower().strip()
    matches = [
        m for m in matches
        if str(m.get("label")) != "short_number"
        or (disp_lower and str(m.get("board_dispatcher") or "").lower().strip() == disp_lower)
    ]
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

    # Step 1: exact evidence before dispatcher ownership. This prevents a real
    # exact truck (e.g. 9988) from being discarded just because the sender is
    # temporarily covering another dispatcher's unit.
    result = _global_exact_selection(list(matches), active)
    if result:
        return result

    # Step 2: dispatcher pre-filter. If sender is a known dispatcher, try to
    # resolve using ONLY that dispatcher's trucks first.
    dispatcher_filtered = list(matches)
    used_dispatcher_filter = False
    if sender_dispatcher:
        disp_lower = sender_dispatcher.lower().strip()
        disp_matches = [
            m for m in matches
            if str(m.get("board_dispatcher") or "").lower().strip() == disp_lower
        ]
        if disp_matches:
            dispatcher_filtered = disp_matches
            used_dispatcher_filter = True

    # Step 3: source cascade on the (possibly dispatcher-filtered) pool
    result = _source_cascade(dispatcher_filtered, active, is_dispatcher_filtered=used_dispatcher_filter)
    if result:
        return result

    # Step 4: if dispatcher filter produced nothing useful, retry with full pool
    if used_dispatcher_filter:
        result = _source_cascade(list(matches), active, is_dispatcher_filtered=False)
        if result:
            return result

    # Fallback
    best = matches[0]
    return _build_selected(best, [str(best["matched_truck"])])


def _global_exact_selection(matches: list[dict[str, Any]], gps_active: set[str]) -> dict[str, Any] | None:
    """Select clear exact matches before applying dispatcher ownership.

    Dispatcher context is valuable, but exact evidence from the email itself is
    stronger. If exact evidence ties across multiple trucks, return None so the
    dispatcher/GPS/full cascade can resolve it instead of guessing too early.
    """
    exact_matches = [m for m in matches if str(m.get("match_type")) == "exact"]
    if not exact_matches:
        return None

    # Explicit "TRUCK ###" anywhere is the strongest exact signal and should
    # beat bare subject/body numbers.
    labeled = [m for m in exact_matches if str(m.get("label")) == "truck_label"]
    if labeled:
        tied_trucks = sorted({str(m["matched_truck"]) for m in labeled})
        if len(tied_trucks) == 1:
            return _build_selected(labeled[0], tied_trucks)
        active_matches = [m for m in labeled if str(m["matched_truck"]) in gps_active]
        active_trucks = sorted({str(m["matched_truck"]) for m in active_matches})
        if len(active_trucks) == 1:
            return _build_selected(active_matches[0], active_trucks, tiebreak=True)
        return None

    # A board trailer pairing is the next strongest signal: the trailer number
    # uniquely identifies the board row even when the truck token is unusable.
    trailer_paired = [m for m in exact_matches if str(m.get("label")) == "trailer_pairing"]
    if trailer_paired:
        tied_trucks = sorted({str(m["matched_truck"]) for m in trailer_paired})
        if len(tied_trucks) == 1:
            return _build_selected(trailer_paired[0], tied_trucks, trailer_pairing=True)

    # Dispatcher-scoped short numbers (already filtered to the sender's board).
    short = [m for m in exact_matches if str(m.get("label")) == "short_number"]
    if short:
        tied_trucks = sorted({str(m["matched_truck"]) for m in short})
        if len(tied_trucks) == 1:
            return _build_selected(short[0], tied_trucks, short_number=True)

    # Then exact bare numbers by source priority. A unique exact body match can
    # still win globally, but multiple exact body numbers are left for dispatcher
    # narrowing because bodies are noisy.
    for source in ("subject", "filename", "email_body", "quoted_body", "pdf_text"):
        source_matches = [
            m for m in exact_matches
            if str(m.get("source")) == source and str(m.get("label")) == "number"
        ]
        if not source_matches:
            continue
        tied_trucks = sorted({str(m["matched_truck"]) for m in source_matches})
        if len(tied_trucks) == 1:
            return _build_selected(source_matches[0], tied_trucks, body_noise=source in {"email_body", "quoted_body", "pdf_text"})
        active_matches = [m for m in source_matches if str(m["matched_truck"]) in gps_active]
        active_trucks = sorted({str(m["matched_truck"]) for m in active_matches})
        if len(active_trucks) == 1:
            return _build_selected(active_matches[0], active_trucks, tiebreak=True)
        return None
    return None


def _source_cascade(
    matches: list[dict[str, Any]],
    gps_active: set[str],
    *,
    is_dispatcher_filtered: bool,
) -> dict[str, Any] | None:
    """Run the priority cascade on a match pool. Returns a selection dict or None.

    Tier 0: explicit "TRUCK ###" labels from ANY source. A dispatcher typing
            "truck 940" in the body beats every bare number in the subject.
    Tiers 1+: bare-number tokens per source: subject > filename > top body >
            quoted reply chain > pdf text.
    """
    # Tier 0: explicit truck labels anywhere (sorted by match quality then source)
    labeled_matches = [m for m in matches if str(m.get("label")) == "truck_label"]
    if labeled_matches:
        best = labeled_matches[0]
        best_rank = MATCH_RANK.get(str(best["match_type"]), 99)
        tied = [m for m in labeled_matches if MATCH_RANK.get(str(m["match_type"]), 99) == best_rank]
        tied_trucks = sorted({str(m["matched_truck"]) for m in tied})
        if len(tied_trucks) == 1:
            return _build_selected(best, tied_trucks)
        if gps_active:
            active_matches = [m for m in tied if str(m["matched_truck"]) in gps_active]
            active_trucks = sorted({str(m["matched_truck"]) for m in active_matches})
            if len(active_trucks) == 1:
                return _build_selected(active_matches[0], active_trucks, tiebreak=True)
        return _build_ambiguous(best, tied_trucks)

    # Tiers 1+: bare numbers by source priority
    source_tiers = ["subject", "filename", "email_body", "quoted_body", "pdf_text"]
    for source in source_tiers:
        source_matches = [
            m for m in matches
            if str(m.get("source")) == source and str(m.get("label")) == "number"
        ]
        if not source_matches:
            continue
        best = source_matches[0]
        best_rank = MATCH_RANK.get(str(best["match_type"]), 99)
        tied = [m for m in source_matches if MATCH_RANK.get(str(m["match_type"]), 99) == best_rank]
        tied_trucks = sorted({str(item["matched_truck"]) for item in tied})
        if len(tied_trucks) == 1:
            return _build_selected(best, tied_trucks)
        # Multiple trucks tied — GPS tiebreak
        if gps_active:
            active_matches = [m for m in tied if str(m["matched_truck"]) in gps_active]
            active_trucks = sorted({str(m["matched_truck"]) for m in active_matches})
            if len(active_trucks) == 1:
                return _build_selected(active_matches[0], active_trucks, tiebreak=True)
        # Bare numbers in body/quoted text are noise — pick newest-first at low
        # confidence rather than red-flagging every forwarded email.
        if source in {"email_body", "quoted_body", "pdf_text"}:
            return _build_selected(best, [str(best["matched_truck"])], body_noise=True)
        return _build_ambiguous(best, tied_trucks)
    return None


def _build_selected(
    best: dict[str, Any],
    trucks: list[str],
    *,
    body_noise: bool = False,
    tiebreak: bool = False,
    trailer_pairing: bool = False,
    short_number: bool = False,
) -> dict[str, Any]:
    """Build a successful selection result from a single winning truck."""
    match_type = str(best["match_type"])
    truck_id = trucks[0] if trucks else str(best["matched_truck"])
    if match_type == "exact":
        if trailer_pairing:
            return {
                "matched_truck_id": truck_id,
                "match_status": "matched",
                "match_type": match_type,
                "match_source": str(best["source"]),
                "match_token": str(best["token"]),
                "match_confidence": 0.93,
                "alert_level": "info",
                "alert_codes": ["matched_via_board_trailer"],
                "alert_notes": f"Trailer {best['token']} is paired to truck {truck_id} on the current board.",
                "best_match": best,
            }
        if short_number:
            return {
                "matched_truck_id": truck_id,
                "match_status": "matched",
                "match_type": match_type,
                "match_source": str(best["source"]),
                "match_token": str(best["token"]),
                "match_confidence": 0.9,
                "alert_level": "info",
                "alert_codes": ["dispatcher_short_truck_match"],
                "alert_notes": f"2-digit token {best['token']} matches truck {truck_id} on the sender's own board.",
                "best_match": best,
            }
        if tiebreak:
            return {
                "matched_truck_id": truck_id,
                "match_status": "matched",
                "match_type": match_type,
                "match_source": str(best["source"]),
                "match_token": str(best["token"]),
                "match_confidence": 0.95,
                "alert_level": "info",
                "alert_codes": ["dispatcher_or_gps_tiebreak"],
                "alert_notes": f"Resolved tie to truck {truck_id} via dispatcher/GPS match.",
                "best_match": best,
            }
        confidence = 0.85 if body_noise else 1.0
        return {
            "matched_truck_id": truck_id,
            "match_status": "matched" if not body_noise else "near_match",
            "match_type": match_type,
            "match_source": str(best["source"]),
            "match_token": str(best["token"]),
            "match_confidence": confidence,
            "alert_level": "info" if body_noise else "",
            "alert_codes": ["body_noise_single_pick"] if body_noise else [],
            "alert_notes": f"Picked truck {truck_id} from body (multiple bare numbers present, none labeled)." if body_noise else "",
            "best_match": best,
        }
    if match_type == "one_digit_off":
        return {
            "matched_truck_id": truck_id,
            "match_status": "near_match",
            "match_type": match_type,
            "match_source": str(best["source"]),
            "match_token": str(best["token"]),
            "match_confidence": 0.98,
            "alert_level": "info",
            "alert_codes": ["one_digit_off_truck_match"],
            "alert_notes": f"Matched board truck {truck_id} from nearby token {best['token']}.",
            "best_match": best,
        }
    return {
        "matched_truck_id": truck_id,
        "match_status": "near_match",
        "match_type": match_type,
        "match_source": str(best["source"]),
        "match_token": str(best["token"]),
        "match_confidence": 0.65,
        "alert_level": "yellow",
        "alert_codes": ["two_digits_off_truck_match_review"],
        "alert_notes": f"Only a two-digit-off board truck match was found: {best['token']} -> {truck_id}.",
        "best_match": best,
    }


def _build_ambiguous(best: dict[str, Any], tied_trucks: list[str]) -> dict[str, Any]:
    """Build an ambiguous result when multiple trucks genuinely tie."""
    return {
        "matched_truck_id": "",
        "match_status": "ambiguous",
        "match_type": str(best["match_type"]),
        "match_source": str(best["source"]),
        "match_token": str(best["token"]),
        "match_confidence": 0.0,
        "alert_level": "red",
        "alert_codes": ["multiple_truck_candidates_one_attachment"],
        "alert_notes": f"Multiple equally ranked truck candidates: {', '.join(tied_trucks)}.",
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
                "payload": payload if content_type == "application/pdf" else b"",
            }
        )
    return out


def _is_inline_signature_image(attachment: dict[str, Any]) -> bool:
    """Logo/signature images (image001.jpg, Outlook-*.png) are not documents."""
    content_type = str(attachment.get("content_type") or "")
    if not content_type.startswith("image/"):
        return False
    filename = str(attachment.get("filename") or "")
    return bool(INLINE_IMAGE_NAME_RE.match(filename.strip()))


def extract_pdf_text(payload: bytes, *, max_pages: int = 2, max_chars: int = 6000) -> str:
    """Best-effort text-layer extraction for second-chance truck matching."""
    if not payload:
        return ""
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(payload))
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(chunks)[:max_chars]
    except Exception:
        return ""


def _safe_doc_key(message_id: str, attachment_index: int, digest: str) -> str:
    basis = f"{message_id}|{attachment_index}|{digest}".encode("utf-8", "replace")
    return hashlib.sha256(basis).hexdigest()


def build_document_rows_from_message(
    msg: Message,
    board: dict[str, BoardTruck],
    *,
    gps_active_trucks: set[str] | None = None,
) -> list[dict[str, Any]]:
    from_name, from_addr = parseaddr(msg.get("From", ""))
    from_addr_lower = from_addr.lower().strip()
    domain = from_addr.split("@", 1)[-1].lower() if "@" in from_addr else ""
    subject = _decode(msg.get("Subject"))
    message_id = str(msg.get("Message-ID") or "").strip()
    thread_id = str(msg.get("Thread-Index") or msg.get("References") or msg.get("In-Reply-To") or "").strip()[:500]
    received = _received_at(msg)
    body = message_body_text(msg)
    attachments = _attachment_parts(msg)

    if is_excluded_sender_email(from_addr_lower) or domain in NOISE_DOMAINS:
        return []

    sender_dispatcher = resolve_sender_dispatcher(from_addr, _decode(from_name), board)
    trailer_map = board_trailer_map(board)

    # Inline signature/logo images should not become their own document rows —
    # they just duplicate the message as junk "unmatched" entries.
    document_attachments = [a for a in attachments if not _is_inline_signature_image(a)]
    if not document_attachments and attachments:
        document_attachments = attachments[:1]

    # Attachment rows are the core unit. If there is no attachment but the message
    # is clearly an assignment/cancel/rate-con control email, create one message row.
    units = document_attachments or ([{"filename": "", "content_type": "message/rfc822", "size_bytes": 0, "sha256": ""}] if RATEISH_SUBJECT_RE.search(subject) else [])
    top_body, quoted_body = split_quoted_reply(body)
    top_body = strip_signature(top_body)
    rows: list[dict[str, Any]] = []
    for index, attachment in enumerate(units, start=1):
        filename = str(attachment.get("filename") or "")
        texts = [subject, body, filename]
        mentions: list[NumberMention] = []
        for source, text in (
            ("subject", subject),
            ("email_body", top_body),
            ("quoted_body", quoted_body),
            ("filename", filename),
        ):
            mentions.extend(extract_number_mentions(text, source))
        matches = candidate_matches(mentions, board, trailer_map)
        selection = select_single_truck(
            matches,
            sender_dispatcher=sender_dispatcher,
            gps_active_trucks=gps_active_trucks,
        )

        # One text extraction per PDF, reused for parsing AND match fallback.
        # If the PDF has no text layer, Layer 2 uses free OCR (Tesseract via
        # pytesseract + pdf2image/Poppler) when available.
        pdf_text_full = ""
        ocr_result = OcrResult()
        if attachment.get("payload"):
            pdf_text_full = extract_pdf_text(bytes(attachment.get("payload") or b""), max_pages=8, max_chars=20000)
            if not pdf_text_full.strip() and _truthy_secret("RATE_CONF_OCR_ENABLED", "true"):
                ocr_result = ocr_pdf_text(
                    bytes(attachment.get("payload") or b""),
                    max_pages=_int_secret("RATE_CONF_OCR_MAX_PAGES", 4),
                    dpi=_int_secret("RATE_CONF_OCR_DPI", 200),
                    max_chars=_int_secret("RATE_CONF_OCR_MAX_CHARS", 20000),
                    lang=_str_secret("RATE_CONF_OCR_LANG", "eng") or "eng",
                    tesseract_config=_str_secret("RATE_CONF_OCR_CONFIG", "--oem 3 --psm 6") or "--oem 3 --psm 6",
                    poppler_path=_str_secret("POPPLER_PATH", ""),
                    tesseract_cmd=_str_secret("TESSERACT_CMD", ""),
                    pdf_timeout=_int_secret("RATE_CONF_OCR_PDF_TIMEOUT", 60),
                    tesseract_timeout=_int_secret("RATE_CONF_OCR_TESSERACT_TIMEOUT", 30),
                )
                if ocr_result.text.strip():
                    pdf_text_full = ocr_result.text

        # Second chance: if subject/body/filename produced nothing, look inside
        # the PDF text layer for board trucks/trailers before giving up.
        if selection["match_status"] == "unmatched" and pdf_text_full:
            pdf_mentions = extract_number_mentions(pdf_text_full[:6000], "pdf_text")
            if pdf_mentions:
                mentions.extend(pdf_mentions)
                matches = candidate_matches(mentions, board, trailer_map)
                selection = select_single_truck(
                    matches,
                    sender_dispatcher=sender_dispatcher,
                    gps_active_trucks=gps_active_trucks,
                )

        # Layer-1 field parsing (broker/pickup/delivery/rate) from the text
        # layer. Image-only documents come back as needs_ocr for layer 2.
        from services.rate_confirmation_parser import parse_rate_confirmation

        parsed = parse_rate_confirmation(pdf_text_full, subject=subject, quoted_body=quoted_body)
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
            "broker_name": str(parsed.get("broker_name") or "")[:120],
            "pickup_summary": str(parsed.get("pickup_summary") or "")[:200],
            "delivery_summary": str(parsed.get("delivery_summary") or "")[:200],
            "pickup_at": parsed.get("pickup_at"),
            "delivery_at": parsed.get("delivery_at"),
            "rate_amount": parsed.get("rate_amount"),
            "parsed_fields": {
                "rate_items": parsed.get("rate_items") or [],
                "text_source": "ocr" if ocr_result.text.strip() else ("pdf_text" if pdf_text_full.strip() else ""),
                "ocr": ocr_result.as_metadata() if ocr_result.status != "not_started" else {},
            },
            "parse_status": str(parsed.get("parse_status") or "not_started"),
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


def fetch_recent_document_rows(mailbox: MailboxConfig, board: dict[str, BoardTruck], *, days: int = 14, limit: int = 0, gps_active_trucks: set[str] | None = None) -> list[dict[str, Any]]:
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
            rows.extend(build_document_rows_from_message(msg, board, gps_active_trucks=gps_active_trucks))
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
    # Load GPS-active trucks for tie-breaking
    gps_active: set[str] = set()
    try:
        from services.gps_data import load_current_assets
        from datetime import timezone
        now = datetime.now(timezone.utc)
        for asset in load_current_assets():
            if asset.asset_type == "truck" and asset.last_ping:
                if (now - asset.last_ping).total_seconds() < 86400:
                    gps_active.add(str(asset.asset_id).strip())
    except Exception:
        pass  # GPS is a nice-to-have tie-breaker, not critical
    rows = fetch_recent_document_rows(mailbox, board, days=days, limit=limit, gps_active_trucks=gps_active)
    summary = _summarize_rows(rows)
    summary.update({"days": days, "dry_run": dry_run, "dispatch_board_trucks": len(board)})
    if dry_run or not rows:
        return summary
    client = SupabaseRestClient()
    # Upsert will not remove rows that are now intentionally excluded. Clean the
    # active ingest window so stale BOL alerts disappear after the next refresh.
    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
    for sender_email in EXCLUDED_SENDER_EMAILS:
        client.delete(
            RATE_CONF_TABLE,
            filters={"sender_email": f"eq.{sender_email}", "received_at": f"gte.{since}"},
        )
    # Delete-then-insert: matching-rule changes can renumber attachments within
    # a message (e.g. inline images no longer get rows), which collides with the
    # unique (message_id, attachment_index) index if stale rows linger.
    message_ids = sorted({str(row.get("message_id") or "") for row in rows if row.get("message_id")})
    for start in range(0, len(message_ids), 50):
        batch_ids = message_ids[start : start + 50]
        quoted = ",".join('"' + mid.replace('"', "") + '"' for mid in batch_ids)
        client.delete(RATE_CONF_TABLE, filters={"message_id": f"in.({quoted})"})
    # Insert in modest chunks to avoid huge PostgREST payloads.
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
