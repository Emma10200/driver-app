from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from services.safety_inbox import (
    EMAIL_DOC_TYPE,
    assign_unmatched_reply,
    extract_attachments,
    ingest_email_message,
    ingest_mailbox,
    list_unmatched_replies,
    load_inbox_mailboxes,
    match_reply,
    MailboxConfig,
)
from services.safety_link_store import (
    create_safety_upload_link,
    record_outbound_message_id,
    ref_code_for_token,
)

_PDF = b"%PDF-1.4 fake pdf bytes"
_PNG = b"\x89PNG\r\n\x1a\n fake png bytes"


def _make_reply(
    *,
    sender: str = "driver@example.com",
    subject: str = "Re: Safety paperwork needed",
    in_reply_to: str | None = None,
    body: str = "Here are my documents.",
    attachments: list[tuple[str, bytes, str, str]] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "statements@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = "<reply-123@example.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    for filename, content, maintype, subtype in attachments or [("license.pdf", _PDF, "application", "pdf")]:
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def _create_link(sub: Path, *, email: str = "driver@example.com", token_msg_id: str | None = None) -> dict:
    link = create_safety_upload_link(
        submissions_dir=sub,
        recipient_email=email,
        recipient_name="JOHN DRIVER",
        division="Prestige Transportation Inc",
        items=[{"unit": "—", "document": "Driver License", "expires": "2026-01-01"}],
    )
    if token_msg_id:
        record_outbound_message_id(submissions_dir=sub, token=link["token"], message_id=token_msg_id)
    return link


# ---------------------------------------------------------------------------
# Attachment extraction
# ---------------------------------------------------------------------------


def test_extract_attachments_keeps_allowed_types_and_dedupes() -> None:
    msg = _make_reply(
        attachments=[
            ("license.pdf", _PDF, "application", "pdf"),
            ("photo.png", _PNG, "image", "png"),
            ("notes.txt", b"ignore me", "text", "plain"),
            ("dupe.pdf", _PDF, "application", "pdf"),  # same bytes -> deduped
        ]
    )
    docs = extract_attachments(msg)
    names = sorted(d["file_name"] for d in docs)
    assert names == ["license.pdf", "photo.png"]
    assert all(d["document_type"] == EMAIL_DOC_TYPE for d in docs)
    assert all(isinstance(d["content"], bytes) and d["content"] for d in docs)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_match_reply_by_recipient_email(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    _create_link(sub, email="driver@example.com")
    msg = _make_reply(sender="DRIVER@example.com")
    match = match_reply(sub, msg)
    assert match.basis == "recipient_email"
    assert match.link is not None
    assert match.link["recipient_name"] == "JOHN DRIVER"


def test_match_reply_by_ref_code(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    link = _create_link(sub, email="someone-else@example.com")
    ref = ref_code_for_token(link["token"])
    msg = _make_reply(sender="lazy-personal@gmail.com", subject=f"Re: Safety paperwork [Ref: {ref}]")
    match = match_reply(sub, msg)
    assert match.basis == "ref_code"
    assert match.link["token"] == link["token"]


def test_match_reply_by_message_id_thread(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    link = _create_link(sub, email="other@example.com", token_msg_id="<outbound-9@example.com>")
    msg = _make_reply(sender="personal@gmail.com", in_reply_to="<outbound-9@example.com>")
    match = match_reply(sub, msg)
    assert match.basis == "message_id"
    assert match.link["token"] == link["token"]


def test_match_reply_unmatched(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    msg = _make_reply(sender="stranger@nowhere.com")
    match = match_reply(sub, msg)
    assert match.basis == "unmatched"
    assert match.link is None


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def test_ingest_matched_reply_saves_and_records(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    _create_link(sub, email="driver@example.com")
    saved: dict = {}
    recorded: dict = {}

    def fake_save(*, form_data, documents, local_base_dir, storage_namespace):
        saved.update(form_data=form_data, documents=documents, namespace=storage_namespace)
        return {"upload_key": "key-1", "documents": [{"document_type": EMAIL_DOC_TYPE, "stored_name": "01-x.pdf"}]}

    def fake_record(submissions_dir, **kwargs):
        recorded.update(kwargs)
        return {"uploaded": 1}

    msg = _make_reply(sender="driver@example.com")
    result = ingest_email_message(sub, msg, save_fn=fake_save, record_fn=fake_record)

    assert result["status"] == "ingested"
    assert result["recipient_email"] == "driver@example.com"
    assert result["document_count"] == 1
    assert saved["form_data"]["upload_type"] == "safety_document_upload"
    assert saved["form_data"]["source"] == "email_reply"
    assert saved["namespace"] == "safety-uploads/live"
    assert recorded["recipient_email"] == "driver@example.com"


def test_ingest_unmatched_reply_queues_for_assignment(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    msg = _make_reply(sender="stranger@nowhere.com")
    result = ingest_email_message(sub, msg)
    assert result["status"] == "unmatched"

    queue = list_unmatched_replies(sub)
    assert len(queue) == 1
    assert queue[0]["sender_email"] == "stranger@nowhere.com"
    assert queue[0]["document_count"] == 1


def test_ingest_reply_with_no_attachments_is_skipped(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    msg = EmailMessage()
    msg["From"] = "driver@example.com"
    msg["Subject"] = "No files here"
    msg["Message-ID"] = "<empty-1@example.com>"
    msg.set_content("I will send later")
    result = ingest_email_message(sub, msg)
    assert result["status"] == "skipped_no_attachments"


def test_assign_unmatched_reply_files_under_chosen_person(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    # Real save so the bytes are persisted locally and can be read back on assign.
    ingest_email_message(sub, _make_reply(sender="stranger@nowhere.com"))
    entry_id = list_unmatched_replies(sub)[0]["entry_id"]

    result = assign_unmatched_reply(
        sub,
        entry_id=entry_id,
        recipient_email="real.driver@example.com",
        recipient_name="REAL DRIVER",
        division="Prestige Transportation Inc",
    )
    assert result["status"] == "assigned"
    assert result["recipient_email"] == "real.driver@example.com"
    assert result["document_count"] == 1
    assert list_unmatched_replies(sub) == []


# ---------------------------------------------------------------------------
# Mailbox polling + idempotency
# ---------------------------------------------------------------------------


class _FakeIMAP:
    def __init__(self, raw_messages: list[bytes]) -> None:
        self._raw = raw_messages
        self.seen_calls = 0

    def select(self, folder):  # noqa: D401
        return ("OK", [b""])

    def search(self, charset, criteria):
        nums = " ".join(str(i + 1) for i in range(len(self._raw))).encode()
        return ("OK", [nums])

    def fetch(self, num, parts):
        idx = int(num) - 1
        return ("OK", [(f"{int(num)} (BODY[]".encode(), self._raw[idx]), b")"])

    def store(self, num, flags, value):
        self.seen_calls += 1
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])


def test_ingest_mailbox_ingests_then_is_idempotent(tmp_path: Path) -> None:
    sub = tmp_path / "submissions"
    _create_link(sub, email="driver@example.com")
    raw = _make_reply(sender="driver@example.com").as_bytes()
    config = MailboxConfig(username="statements@example.com", password="pw", division="Prestige")

    fake = _FakeIMAP([raw])
    first = ingest_mailbox(config, sub, imap_factory=lambda c: fake)
    assert first["ingested"] == 1
    assert first["unmatched"] == 0

    # Second poll: same message fingerprint already processed -> skipped.
    second = ingest_mailbox(config, sub, imap_factory=lambda c: _FakeIMAP([raw]))
    assert second["ingested"] == 0
    assert second["skipped"] == 1


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_load_inbox_mailboxes_parses_json_array() -> None:
    raw = (
        '[{"username": "statements@example.com", "password": "pw1"},'
        ' {"username": "safety@xpresstransinc.com", "password": "pw2", "division": "Xpress Trans, Inc"}]'
    )
    mailboxes = load_inbox_mailboxes(raw)
    assert len(mailboxes) == 2
    assert mailboxes[0].host == "imap.gmail.com"
    assert mailboxes[0].port == 993
    assert mailboxes[1].division == "Xpress Trans, Inc"


def test_load_inbox_mailboxes_skips_incomplete_entries() -> None:
    raw = '[{"username": "only-user@example.com"}, {"password": "orphan"}]'
    assert load_inbox_mailboxes(raw) == []


def test_load_inbox_mailboxes_handles_blank_and_bad_json() -> None:
    assert load_inbox_mailboxes("") == []
    assert load_inbox_mailboxes("not json") == []
