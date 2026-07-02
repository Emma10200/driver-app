"""Read-only sampler for the rate-confirmation mailbox.

Purpose
-------
Before building the real ingester, this script connects to the rate-con inbox in
**read-only** mode and dumps a representative sample so we can see the actual
formats we are dealing with:

* real ``From:`` domains (to design the sender -> division map),
* real ``Subject:`` lines (to tune the truck-number regex),
* attachment filenames / sizes / MIME types,
* whether each PDF has an extractable text layer (drives OCR strategy).

It NEVER modifies the mailbox. The IMAP folder is opened with ``readonly=True``,
so nothing is marked read, moved, flagged, or deleted.

Secrets are loaded the same way as the other scripts in this repo:
``.streamlit/secrets.toml`` -> ``.env`` -> process environment.

Required secret keys::

    RATE_CONF_EMAIL          the mailbox address
    RATE_CONF_APP_PASSWORD   Gmail app password (16 chars, no spaces)

Optional::

    RATE_CONF_IMAP_HOST      default imap.gmail.com
    RATE_CONF_IMAP_PORT      default 993
    RATE_CONF_MAILBOX_FOLDER default INBOX

Usage::

    python scripts/sample_rate_conf_inbox.py --limit 40
    python scripts/sample_rate_conf_inbox.py --limit 100 --save-pdfs

Output goes to ``rate_conf_samples/`` (gitignored):
    summary_<timestamp>.json   one record per sampled message
    pdfs/<message>/<file>.pdf   saved attachments (only with --save-pdfs)
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "rate_conf_samples"

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_FOLDER = "INBOX"

# Candidate truck numbers in a subject line: bare integers, 2-5 digits.
# (Deliberately loose for sampling; the real parser will be tightened against
# the live unit roster.)
_TRUCK_CANDIDATE_RE = re.compile(r"\b(\d{2,5})\b")


def _load_secrets() -> dict[str, str]:
    """Mirror the secret-loading convention used by the other repo scripts."""
    secrets: dict[str, str] = {}
    for path in (ROOT / ".streamlit" / "secrets.toml", ROOT / ".env"):
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
        except Exception as exc:  # noqa: BLE001 - surface but continue
            print(f"WARNING: could not read {path.name}: {exc}", file=sys.stderr)
    for key in (
        "RATE_CONF_EMAIL",
        "RATE_CONF_APP_PASSWORD",
        "RATE_CONF_IMAP_HOST",
        "RATE_CONF_IMAP_PORT",
        "RATE_CONF_MAILBOX_FOLDER",
    ):
        if os.environ.get(key):
            secrets[key] = os.environ[key]
    return secrets


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _received_at(msg: Message) -> str:
    raw = msg.get("Date")
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return raw


def _pdf_has_text_layer(data: bytes) -> bool | None:
    """Best-effort check for an extractable text layer.

    Returns True/False if a PDF library is available, else None (unknown).
    Only used to estimate how many confirmations will need OCR.
    """
    try:
        from pypdf import PdfReader  # type: ignore
        import io

        reader = PdfReader(io.BytesIO(data))
        text = ""
        for page in reader.pages[:3]:
            text += page.extract_text() or ""
        return len(text.strip()) > 40
    except ModuleNotFoundError:
        return None
    except Exception:
        return False


def _analyze_attachments(msg: Message, *, save_dir: Path | None) -> list[dict]:
    results: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = _decode(part.get_filename())
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))
        is_attachment = "attachment" in disposition.lower() or bool(filename)
        if not is_attachment:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        record = {
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(payload),
        }
        if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
            record["pdf_has_text_layer"] = _pdf_has_text_layer(payload)
            if save_dir is not None and payload:
                save_dir.mkdir(parents=True, exist_ok=True)
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "attachment.pdf")
                (save_dir / safe_name).write_bytes(payload)
                record["saved_to"] = str((save_dir / safe_name).relative_to(ROOT))
        results.append(record)
    return results


def sample(limit: int, save_pdfs: bool) -> None:
    secrets = _load_secrets()
    username = secrets.get("RATE_CONF_EMAIL", "").strip()
    # Gmail shows app passwords in 4-char groups for readability, but IMAP login
    # needs the 16 chars with no spaces.
    password = re.sub(r"\s+", "", secrets.get("RATE_CONF_APP_PASSWORD", ""))
    if not username or not password:
        raise SystemExit(
            "RATE_CONF_EMAIL and RATE_CONF_APP_PASSWORD are required in .env, "
            ".streamlit/secrets.toml, or environment."
        )
    host = secrets.get("RATE_CONF_IMAP_HOST", DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST
    try:
        port = int(secrets.get("RATE_CONF_IMAP_PORT") or DEFAULT_IMAP_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_IMAP_PORT
    folder = secrets.get("RATE_CONF_MAILBOX_FOLDER", DEFAULT_FOLDER).strip() or DEFAULT_FOLDER

    print(f"Connecting to {host}:{port} as {username} (READ-ONLY) ...")
    client = imaplib.IMAP4_SSL(host, port)
    try:
        client.login(username, password)
        # readonly=True guarantees we never change message state.
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise SystemExit(f"Could not select folder {folder!r}: {status}")

        status, data = client.search(None, "ALL")
        if status != "OK":
            raise SystemExit(f"IMAP search failed: {status}")
        ids = data[0].split()
        if not ids:
            print("Mailbox is empty (or folder has no messages).")
            return
        sample_ids = ids[-limit:][::-1]  # newest first
        print(f"Found {len(ids)} messages; sampling newest {len(sample_ids)}.")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        records: list[dict] = []
        domain_counts: dict[str, int] = {}
        text_layer_yes = text_layer_no = text_layer_unknown = 0

        for n, msg_id in enumerate(sample_ids, start=1):
            status, msg_data = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            from_name, from_addr = parseaddr(msg.get("From", ""))
            domain = from_addr.split("@", 1)[-1].lower() if "@" in from_addr else ""
            subject = _decode(msg.get("Subject"))
            truck_candidates = _TRUCK_CANDIDATE_RE.findall(subject)

            save_dir = None
            if save_pdfs:
                save_dir = OUTPUT_DIR / "pdfs" / f"msg_{n:03d}"
            attachments = _analyze_attachments(msg, save_dir=save_dir)

            for att in attachments:
                tl = att.get("pdf_has_text_layer")
                if tl is True:
                    text_layer_yes += 1
                elif tl is False:
                    text_layer_no += 1
                elif "pdf_has_text_layer" in att:
                    text_layer_unknown += 1

            if domain:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1

            records.append(
                {
                    "index": n,
                    "received_at": _received_at(msg),
                    "from_name": _decode(from_name),
                    "from_addr": from_addr,
                    "from_domain": domain,
                    "subject": subject,
                    "truck_candidates": truck_candidates,
                    "message_id": str(msg.get("Message-ID", "")).strip(),
                    "attachments": attachments,
                }
            )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        summary_path = OUTPUT_DIR / f"summary_{stamp}.json"
        summary = {
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "mailbox": username,
            "folder": folder,
            "message_count_total": len(ids),
            "message_count_sampled": len(records),
            "sender_domain_counts": dict(
                sorted(domain_counts.items(), key=lambda kv: kv[1], reverse=True)
            ),
            "pdf_text_layer": {
                "has_text": text_layer_yes,
                "no_text_needs_ocr": text_layer_no,
                "unknown_no_pdf_lib": text_layer_unknown,
            },
            "records": records,
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        print("\n=== Sender domains ===")
        for dom, count in summary["sender_domain_counts"].items():
            print(f"  {count:>4}  {dom}")
        print("\n=== PDF text layer (OCR estimate) ===")
        print(f"  has text layer : {text_layer_yes}")
        print(f"  needs OCR      : {text_layer_no}")
        print(f"  unknown        : {text_layer_unknown}  (install pypdf for this stat)")
        print(f"\nSummary written to {summary_path.relative_to(ROOT)}")
        if save_pdfs:
            print(f"PDFs saved under {(OUTPUT_DIR / 'pdfs').relative_to(ROOT)}")
    finally:
        try:
            client.close()
        except Exception:
            pass
        client.logout()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only rate-confirmation inbox sampler.")
    parser.add_argument("--limit", type=int, default=40, help="How many newest messages to sample.")
    parser.add_argument(
        "--save-pdfs",
        action="store_true",
        help="Save PDF attachments to rate_conf_samples/pdfs/ for inspection.",
    )
    args = parser.parse_args()
    sample(limit=args.limit, save_pdfs=args.save_pdfs)


if __name__ == "__main__":
    main()
