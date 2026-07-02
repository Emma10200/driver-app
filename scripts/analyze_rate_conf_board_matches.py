"""Read-only two-week rate-con email -> dispatch board truck matcher.

This is an analysis/prototyping script, not the production ingester.

It answers the question: if we treat the dispatch board as the finite universe of
valid truck IDs, how often do recent rate-confirmation emails contain an exact or
nearby board truck number somewhere in the subject, attachment filename, email
body, or extractable PDF text?

No mailbox state is modified. IMAP opens the folder with readonly=True.
Output goes to rate_conf_samples/ (gitignored).
"""

from __future__ import annotations

import argparse
import csv
import email
import html
import imaplib
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "rate_conf_samples"

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_FOLDER = "INBOX"

NOISE_DOMAINS = {"accounts.google.com", "google.com"}
SOURCE_RANK = {"subject": 0, "filename": 1, "email_body": 2, "pdf_text": 3}
MATCH_RANK = {"exact": 0, "one_digit_off": 1, "two_digits_off": 2}

# This intentionally only extracts standalone numeric tokens. We do NOT split a
# long load/order number into every possible truck-like substring.
NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(\d{2,6})(?![A-Za-z0-9])")
TAGGED_TRUCK_RE = re.compile(r"\b(?:truck|unit|tractor)\s*#?\s*(\d{2,6})\b", re.IGNORECASE)
TAGGED_TRAILER_RE = re.compile(r"\btrail?er\s*#?\s*(\d{2,6})\b", re.IGNORECASE)
LOAD_HINT_RE = re.compile(
    r"\b(?:load|order|carrier|reference|ref|po|paynumber|bol|ldi\s+load)\s*#?\s*[:\-]?\s*([A-Za-z0-9\-]{4,})",
    re.IGNORECASE,
)


def _load_secrets() -> dict[str, str]:
    secrets: dict[str, str] = {}
    for path in (ROOT / ".streamlit" / "secrets.toml", ROOT / ".env", ROOT.parent / "QBO_App" / ".env"):
        if not path.exists():
            continue
        try:
            if path.suffix == ".toml":
                import tomllib

                data = tomllib.loads(path.read_text(encoding="utf-8"))
                for key, value in data.items():
                    if isinstance(value, dict):
                        for nested_key, nested_value in value.items():
                            secrets[nested_key] = str(nested_value)
                    else:
                        secrets[key] = str(value)
            else:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, _, value = stripped.partition("=")
                    secrets[key.strip()] = value.strip().strip('"').strip("'")
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: could not read {path}: {exc}", file=sys.stderr)
    for key, value in os.environ.items():
        if key.startswith("RATE_CONF_") or key in {"SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY"}:
            secrets[key] = value
    return secrets


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
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def _message_body_text(msg: Message, max_chars: int) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in disposition:
            continue
        if content_type not in {"text/plain", "text/html"}:
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
    out = "\n".join(plain_parts or html_parts)
    return out[:max_chars]


def _extract_pdf_text(data: bytes, max_pages: int, max_chars: int) -> tuple[str, bool | None]:
    if max_pages <= 0 or max_chars <= 0:
        return "", None
    try:
        from pypdf import PdfReader  # type: ignore
    except ModuleNotFoundError:
        return "", None
    try:
        reader = PdfReader(io.BytesIO(data))
        pieces: list[str] = []
        for page in reader.pages[:max_pages]:
            pieces.append(page.extract_text() or "")
            if sum(len(p) for p in pieces) >= max_chars:
                break
        text = "\n".join(pieces)[:max_chars]
        return text, len(text.strip()) > 40
    except Exception:
        return "", False


def _clean_unit(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d+", text)
    return match.group(0) if match else ""


def _load_board_rows(secrets: dict[str, str]) -> list[dict[str, Any]]:
    supabase_url = (secrets.get("SUPABASE_URL") or "").rstrip("/")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY") or secrets.get("SUPABASE_KEY") or ""
    if not supabase_url or not supabase_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SERVICE_KEY are required.")
    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while True:
        params = {
            "select": "row_key,sheet_row,truck_id,trailer_id,driver_name,dispatcher,division,status,raw",
            "order": "sheet_row.asc",
            "limit": str(page_size),
            "offset": str(offset),
        }
        response = requests.get(
            f"{supabase_url}/rest/v1/dispatch_board_rows?{urlencode(params)}",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(f"dispatch_board_rows query failed: HTTP {response.status_code} {response.text[:500]}")
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"dispatch_board_rows returned unexpected payload: {str(batch)[:500]}")
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


@dataclass(frozen=True)
class BoardTruck:
    truck_id: str
    dispatcher: str
    driver_name: str
    division: str
    status: str
    sheet_row: int


def _board_trucks(rows: list[dict[str, Any]]) -> dict[str, BoardTruck]:
    out: dict[str, BoardTruck] = {}
    for row in rows:
        truck = _clean_unit(row.get("truck_id"))
        if not truck:
            continue
        out.setdefault(
            truck,
            BoardTruck(
                truck_id=truck,
                dispatcher=str(row.get("dispatcher") or "").strip(),
                driver_name=str(row.get("driver_name") or "").strip(),
                division=str(row.get("division") or "").strip(),
                status=str(row.get("status") or "").strip(),
                sheet_row=int(row.get("sheet_row") or 0),
            ),
        )
    return out


def _same_length_digit_distance(a: str, b: str) -> int | None:
    if len(a) != len(b):
        return None
    return sum(1 for x, y in zip(a, b, strict=True) if x != y)


def _source_mentions(text: str, source: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in TAGGED_TRUCK_RE.finditer(text):
        token = match.group(1)
        key = (token, "truck_label")
        if key not in seen:
            mentions.append({"token": token, "source": source, "label": "truck_label"})
            seen.add(key)
    for match in TAGGED_TRAILER_RE.finditer(text):
        token = match.group(1)
        key = (token, "trailer_label")
        if key not in seen:
            mentions.append({"token": token, "source": source, "label": "trailer_label"})
            seen.add(key)
    for match in NUMBER_TOKEN_RE.finditer(text):
        token = match.group(1)
        key = (token, "number")
        if key not in seen:
            mentions.append({"token": token, "source": source, "label": "number"})
            seen.add(key)
    return mentions


def _rank_mentions(mentions: list[dict[str, Any]], board: dict[str, BoardTruck]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for mention in mentions:
        token = str(mention["token"])
        for truck_id, truck in board.items():
            kind = ""
            distance = 0
            if token == truck_id:
                kind = "exact"
            else:
                digit_distance = _same_length_digit_distance(token, truck_id)
                if digit_distance == 1:
                    kind = "one_digit_off"
                    distance = 1
                elif digit_distance == 2:
                    kind = "two_digits_off"
                    distance = 2
            if not kind:
                continue
            matches.append(
                {
                    "token": token,
                    "matched_truck": truck_id,
                    "match_type": kind,
                    "digit_distance": distance,
                    "source": mention["source"],
                    "label": mention["label"],
                    "board_dispatcher": truck.dispatcher,
                    "board_driver": truck.driver_name,
                    "board_division": truck.division,
                    "board_status": truck.status,
                    "board_sheet_row": truck.sheet_row,
                }
            )
    matches.sort(
        key=lambda m: (
            MATCH_RANK.get(str(m["match_type"]), 99),
            0 if m["label"] == "truck_label" else 1,
            SOURCE_RANK.get(str(m["source"]), 99),
            len(str(m["token"])),
            str(m["matched_truck"]),
        )
    )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for match in matches:
        key = (str(match["matched_truck"]), str(match["match_type"]), str(match["source"]), str(match["token"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def _load_hints(texts: list[str]) -> list[str]:
    hints: list[str] = []
    for text in texts:
        for match in LOAD_HINT_RE.finditer(text):
            value = match.group(1).strip().strip(".,;:)")
            if value and value not in hints:
                hints.append(value)
    return hints[:10]


def _domain_to_division(domain: str) -> str:
    if domain == "prestige.inc":
        return "pg"
    if domain == "prestigecalifornia.com":
        return "prestige"
    if domain == "xpresstransinc.com":
        return "xpress"
    return ""


def _search_since(days: int) -> str:
    since = datetime.now(UTC) - timedelta(days=days)
    return since.strftime("%d-%b-%Y")


def analyze(days: int, pdf_text_pages: int, body_chars: int, pdf_chars: int, hard_limit: int | None) -> None:
    secrets = _load_secrets()
    board_rows = _load_board_rows(secrets)
    board = _board_trucks(board_rows)
    if not board:
        raise SystemExit("No truck IDs found in dispatch_board_rows.")

    username = (secrets.get("RATE_CONF_EMAIL") or "").strip()
    password = re.sub(r"\s+", "", secrets.get("RATE_CONF_APP_PASSWORD") or "")
    if not username or not password:
        raise SystemExit("RATE_CONF_EMAIL and RATE_CONF_APP_PASSWORD are required.")
    host = (secrets.get("RATE_CONF_IMAP_HOST") or DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST
    port = int(secrets.get("RATE_CONF_IMAP_PORT") or DEFAULT_IMAP_PORT)
    folder = (secrets.get("RATE_CONF_MAILBOX_FOLDER") or DEFAULT_FOLDER).strip() or DEFAULT_FOLDER

    client = imaplib.IMAP4_SSL(host, port)
    records: list[dict[str, Any]] = []
    try:
        print(f"Loaded {len(board_rows)} dispatch rows / {len(board)} truck IDs.")
        print(f"Connecting to {host}:{port} as {username} (READ-ONLY) ...")
        client.login(username, password)
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise SystemExit(f"Could not select folder {folder!r}: {status}")
        since = _search_since(days)
        status, data = client.search(None, "SINCE", since)
        if status != "OK":
            raise SystemExit(f"IMAP search failed: {status}")
        ids = data[0].split()
        ids = ids[-hard_limit:] if hard_limit else ids
        print(f"Found {len(ids)} messages since {since}; analyzing all fetched messages.")

        for n, msg_id in enumerate(ids, start=1):
            if n % 100 == 0:
                print(f"  analyzed {n}/{len(ids)} ...")
            status, msg_data = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            from_name, from_addr = parseaddr(msg.get("From", ""))
            domain = from_addr.split("@", 1)[-1].lower() if "@" in from_addr else ""
            subject = _decode(msg.get("Subject"))
            received = _received_at(msg)
            body = _message_body_text(msg, body_chars)

            texts_by_source: list[tuple[str, str]] = [("subject", subject), ("email_body", body)]
            attachment_records: list[dict[str, Any]] = []
            pdf_text_layer_yes = 0
            pdf_text_layer_no = 0
            image_count = 0
            for part in msg.walk():
                if part.is_multipart():
                    continue
                filename = _decode(part.get_filename())
                disposition = str(part.get("Content-Disposition", ""))
                content_type = part.get_content_type()
                is_attachment = "attachment" in disposition.lower() or bool(filename)
                if not is_attachment:
                    continue
                if filename:
                    texts_by_source.append(("filename", filename))
                try:
                    payload = part.get_payload(decode=True) or b""
                except Exception:
                    payload = b""
                att = {"filename": filename, "content_type": content_type, "size_bytes": len(payload)}
                if content_type.startswith("image/"):
                    image_count += 1
                if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
                    pdf_text, has_text = _extract_pdf_text(payload, pdf_text_pages, pdf_chars)
                    att["pdf_has_text_layer"] = has_text
                    if has_text is True:
                        pdf_text_layer_yes += 1
                        texts_by_source.append(("pdf_text", pdf_text))
                    elif has_text is False:
                        pdf_text_layer_no += 1
                attachment_records.append(att)

            mentions: list[dict[str, Any]] = []
            for source, text in texts_by_source:
                if text:
                    mentions.extend(_source_mentions(text, source))
            matches = _rank_mentions(mentions, board)
            best = matches[0] if matches else None
            records.append(
                {
                    "index": n,
                    "received_at": received.isoformat() if received else "",
                    "from_name": _decode(from_name),
                    "from_addr": from_addr,
                    "from_domain": domain,
                    "domain_division": _domain_to_division(domain),
                    "subject": subject,
                    "message_id": str(msg.get("Message-ID", "")).strip(),
                    "is_noise_domain": domain in NOISE_DOMAINS,
                    "attachment_count": len(attachment_records),
                    "pdf_text_layer_yes": pdf_text_layer_yes,
                    "pdf_text_layer_no": pdf_text_layer_no,
                    "image_count": image_count,
                    "load_hints": _load_hints([text for _, text in texts_by_source]),
                    "number_tokens": sorted({m["token"] for m in mentions}, key=lambda x: (len(str(x)), str(x)))[:80],
                    "match_count": len(matches),
                    "best_match": best,
                    "matches": matches[:20],
                    "attachments": attachment_records,
                }
            )
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass

    non_noise = [r for r in records if not r["is_noise_domain"]]
    with_attach_or_rateish = [
        r
        for r in non_noise
        if r["attachment_count"] or re.search(r"rate|confirm|load|truck|cancel", str(r["subject"]), re.IGNORECASE)
    ]
    exact = [r for r in with_attach_or_rateish if (r.get("best_match") or {}).get("match_type") == "exact"]
    one = [r for r in with_attach_or_rateish if (r.get("best_match") or {}).get("match_type") == "one_digit_off"]
    two = [r for r in with_attach_or_rateish if (r.get("best_match") or {}).get("match_type") == "two_digits_off"]
    unmatched = [r for r in with_attach_or_rateish if not r.get("best_match")]
    source_counts: dict[str, int] = {}
    for r in with_attach_or_rateish:
        best = r.get("best_match") or {}
        source = str(best.get("source") or "unmatched")
        source_counts[source] = source_counts.get(source, 0) + 1

    summary = {
        "analyzed_at": datetime.now(UTC).isoformat(),
        "days": days,
        "message_count": len(records),
        "candidate_rate_conf_message_count": len(with_attach_or_rateish),
        "dispatch_board_rows": len(board_rows),
        "dispatch_board_trucks": len(board),
        "best_match_counts": {
            "exact": len(exact),
            "one_digit_off": len(one),
            "two_digits_off": len(two),
            "unmatched": len(unmatched),
        },
        "best_match_source_counts": dict(sorted(source_counts.items())),
        "pdf_text_layer": {
            "has_text": sum(int(r["pdf_text_layer_yes"] or 0) for r in records),
            "needs_ocr": sum(int(r["pdf_text_layer_no"] or 0) for r in records),
        },
        "domain_counts": {},
        "records": records,
    }
    for r in records:
        domain = str(r.get("from_domain") or "")
        summary["domain_counts"][domain] = summary["domain_counts"].get(domain, 0) + 1
    summary["domain_counts"] = dict(sorted(summary["domain_counts"].items(), key=lambda kv: kv[1], reverse=True))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"two_week_board_match_{stamp}.json"
    csv_path = OUTPUT_DIR / f"two_week_board_match_{stamp}.csv"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "received_at",
                "from_domain",
                "from_name",
                "subject",
                "best_truck",
                "match_type",
                "match_source",
                "match_token",
                "board_dispatcher",
                "board_driver",
                "board_division",
                "load_hints",
                "attachment_count",
                "pdf_text_layer_yes",
                "pdf_text_layer_no",
            ],
        )
        writer.writeheader()
        for r in records:
            best = r.get("best_match") or {}
            writer.writerow(
                {
                    "received_at": r.get("received_at", ""),
                    "from_domain": r.get("from_domain", ""),
                    "from_name": r.get("from_name", ""),
                    "subject": r.get("subject", ""),
                    "best_truck": best.get("matched_truck", ""),
                    "match_type": best.get("match_type", ""),
                    "match_source": best.get("source", ""),
                    "match_token": best.get("token", ""),
                    "board_dispatcher": best.get("board_dispatcher", ""),
                    "board_driver": best.get("board_driver", ""),
                    "board_division": best.get("board_division", ""),
                    "load_hints": "; ".join(r.get("load_hints") or []),
                    "attachment_count": r.get("attachment_count", 0),
                    "pdf_text_layer_yes": r.get("pdf_text_layer_yes", 0),
                    "pdf_text_layer_no": r.get("pdf_text_layer_no", 0),
                }
            )

    print("\n=== Two-week board match summary ===")
    print(f"Messages analyzed: {len(records)}")
    print(f"Candidate rate-con-ish messages: {len(with_attach_or_rateish)}")
    print(f"Board rows/trucks: {len(board_rows)} / {len(board)}")
    print("Best match counts:")
    for key, value in summary["best_match_counts"].items():
        pct = (value / len(with_attach_or_rateish) * 100) if with_attach_or_rateish else 0
        print(f"  {key:>14}: {value:>4} ({pct:5.1f}%)")
    print("Best source counts:")
    for key, value in summary["best_match_source_counts"].items():
        print(f"  {key:>14}: {value:>4}")
    print("PDF text layer:")
    print(f"  has text : {summary['pdf_text_layer']['has_text']}")
    print(f"  OCR need : {summary['pdf_text_layer']['needs_ocr']}")
    print(f"JSON: {json_path.relative_to(ROOT)}")
    print(f"CSV : {csv_path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze recent rate-con emails against dispatch board truck IDs.")
    parser.add_argument("--days", type=int, default=14, help="How many recent days to analyze.")
    parser.add_argument(
        "--pdf-text-pages",
        type=int,
        default=2,
        help="Pages of extractable PDF text to scan. Use 0 for a fast subject/body/filename-only board match.",
    )
    parser.add_argument("--body-chars", type=int, default=20000, help="Max email body chars to scan per message.")
    parser.add_argument("--pdf-chars", type=int, default=30000, help="Max PDF text chars to scan per attachment.")
    parser.add_argument("--hard-limit", type=int, default=0, help="Optional cap on messages fetched from IMAP.")
    args = parser.parse_args()
    analyze(
        days=args.days,
        pdf_text_pages=args.pdf_text_pages,
        body_chars=args.body_chars,
        pdf_chars=args.pdf_chars,
        hard_limit=args.hard_limit or None,
    )


if __name__ == "__main__":
    main()
