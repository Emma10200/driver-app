from __future__ import annotations

from email.message import EmailMessage

from services.rate_confirmation_ingest import (
    BoardTruck,
    build_document_rows_from_message,
    candidate_matches,
    extract_number_mentions,
    select_single_truck,
)
from services.rate_confirmation_ocr import OcrResult


def _board(*truck_ids: str) -> dict[str, BoardTruck]:
    return {truck_id: BoardTruck(truck_id=truck_id, dispatcher="D", driver_name=f"Driver {truck_id}") for truck_id in truck_ids}


def _select(text: str, board: dict[str, BoardTruck], source: str = "subject") -> dict:
    mentions = extract_number_mentions(text, source)
    matches = candidate_matches(mentions, board)
    return select_single_truck(matches)


def test_exact_truck_label_wins() -> None:
    result = _select("TRUCK 649 DRIVER SAM TRAILER 2015", _board("649", "531"))

    assert result["matched_truck_id"] == "649"
    assert result["match_status"] == "matched"
    assert result["match_type"] == "exact"
    assert result["alert_level"] == ""


def test_one_digit_off_selects_near_match() -> None:
    result = _select("TRUCK 648 DRIVER SAM", _board("649", "531"))

    assert result["matched_truck_id"] == "649"
    assert result["match_status"] == "near_match"
    assert result["match_type"] == "one_digit_off"
    assert result["alert_level"] == "info"
    assert "one_digit_off_truck_match" in result["alert_codes"]


def test_trailer_label_does_not_assign_truck_by_itself() -> None:
    result = _select("DRIVER SAM TRAILER 649", _board("649"))

    assert result["matched_truck_id"] == ""
    assert result["match_status"] == "unmatched"
    assert "no_board_truck_match" in result["alert_codes"]


def test_one_attachment_multiple_equal_trucks_is_ambiguous() -> None:
    mentions = extract_number_mentions("Truck 649 and truck 531", "subject")
    matches = candidate_matches(mentions, _board("649", "531"))
    result = select_single_truck(matches)

    assert result["matched_truck_id"] == ""
    assert result["match_status"] == "ambiguous"
    assert result["alert_level"] == "red"
    assert "multiple_truck_candidates_one_attachment" in result["alert_codes"]


def test_body_noise_bare_numbers_pick_first_not_ambiguous() -> None:
    """Multiple bare numbers in email_body that match board trucks should NOT
    produce a red ambiguous alert. The first one gets picked at lower confidence."""
    mentions = extract_number_mentions("order 333 ref 713 load 973", "email_body")
    matches = candidate_matches(mentions, _board("333", "713", "973"))
    result = select_single_truck(matches)

    # Should pick a truck rather than flagging ambiguous
    assert result["matched_truck_id"] != ""
    assert result["match_status"] != "ambiguous" or result["alert_level"] != "red"


def test_subject_wins_over_body_noise() -> None:
    """A clear truck in the subject should override random body numbers."""
    subj_mentions = extract_number_mentions("TRUCK 649", "subject")
    body_mentions = extract_number_mentions("ref 333 order 713 load 973", "email_body")
    matches = candidate_matches(subj_mentions + body_mentions, _board("649", "333", "713", "973"))
    result = select_single_truck(matches)

    assert result["matched_truck_id"] == "649"
    assert result["match_status"] == "matched"
    assert result["alert_level"] == ""


def test_dispatcher_tiebreak_picks_dispatchers_truck() -> None:
    """When multiple trucks tie, the sender's dispatcher's truck wins cleanly."""
    board = {
        "333": BoardTruck(truck_id="333", dispatcher="Anna"),
        "713": BoardTruck(truck_id="713", dispatcher="Brittany"),
        "973": BoardTruck(truck_id="973", dispatcher="Carlos CA"),
    }
    mentions = extract_number_mentions("ref 333 order 713 load 973", "email_body")
    matches = candidate_matches(mentions, board)
    result = select_single_truck(matches, sender_dispatcher="Anna")

    assert result["matched_truck_id"] == "333"
    assert result["match_status"] == "matched"
    # Dispatcher pre-filter narrows to 1 truck → clean match, no alert
    assert result["alert_level"] == ""


def test_global_exact_match_beats_dispatcher_assignment() -> None:
    """An exact truck match should win even if sender maps to another dispatcher."""
    board = {
        "333": BoardTruck(truck_id="333", dispatcher="Anna"),
        "9988": BoardTruck(truck_id="9988", dispatcher="Matt"),
    }
    subject = extract_number_mentions("TRUCK 9988", "subject")
    body = extract_number_mentions("ref 333", "email_body")
    matches = candidate_matches(subject + body, board)
    result = select_single_truck(matches, sender_dispatcher="Anna")

    assert result["matched_truck_id"] == "9988"
    assert result["match_status"] == "matched"
    assert result["match_type"] == "exact"


def test_gps_active_tiebreak() -> None:
    """When dispatcher can't break the tie, prefer the GPS-active truck."""
    board = {
        "333": BoardTruck(truck_id="333", dispatcher="Anna"),
        "713": BoardTruck(truck_id="713", dispatcher="Anna"),
    }
    mentions = extract_number_mentions("ref 333 order 713", "email_body")
    matches = candidate_matches(mentions, board)
    result = select_single_truck(matches, sender_dispatcher="Anna", gps_active_trucks={"713"})

    assert result["matched_truck_id"] == "713"
    assert result["match_status"] == "matched"


def test_truck_label_in_body_beats_subject_bare_number() -> None:
    """Explicit 'truck 940' typed in the body must beat a bare subject number."""
    board = {
        "940": BoardTruck(truck_id="940", dispatcher="Felix"),
        "467": BoardTruck(truck_id="467", dispatcher="Anna"),
    }
    subj = extract_number_mentions("Fwd: Load 467 pickup", "subject")
    body = extract_number_mentions("truck 940 driver sam", "email_body")
    matches = candidate_matches(subj + body, board)
    result = select_single_truck(matches)

    assert result["matched_truck_id"] == "940"
    assert result["match_status"] == "matched"


def test_quoted_reply_numbers_are_deprioritized() -> None:
    """Numbers in the quoted/forwarded chain must not beat the dispatcher's text."""
    from services.rate_confirmation_ingest import split_quoted_reply

    body = "TRUCK 940\n\n---------- Forwarded message ---------\nLoad 333 rate $1500 ref 973"
    top, quoted = split_quoted_reply(body)
    assert "TRUCK 940" in top
    assert "333" in quoted

    board = {
        "940": BoardTruck(truck_id="940", dispatcher="Felix"),
        "333": BoardTruck(truck_id="333", dispatcher="Anna"),
        "973": BoardTruck(truck_id="973", dispatcher="Carlos CA"),
    }
    mentions = extract_number_mentions(top, "email_body") + extract_number_mentions(quoted, "quoted_body")
    matches = candidate_matches(mentions, board)
    result = select_single_truck(matches)

    assert result["matched_truck_id"] == "940"


def test_bare_two_digit_numbers_are_ignored() -> None:
    """Standalone 2-digit numbers (weights, dates) must not match 2-digit trucks."""
    board = {"67": BoardTruck(truck_id="67", dispatcher="Felix"), "45": BoardTruck(truck_id="45", dispatcher="Felix")}
    mentions = extract_number_mentions("45 pallets weight 67 lbs pickup 07 01", "email_body")
    matches = candidate_matches(mentions, board)
    result = select_single_truck(matches)

    assert result["matched_truck_id"] == ""
    assert result["match_status"] == "unmatched"


def test_labeled_two_digit_truck_still_matches() -> None:
    """'TRUCK 67' explicitly labeled must still match the 2-digit truck."""
    board = {"67": BoardTruck(truck_id="67", dispatcher="Felix")}
    mentions = extract_number_mentions("TRUCK 67 DRIVER BOB", "subject")
    matches = candidate_matches(mentions, board)
    result = select_single_truck(matches)

    assert result["matched_truck_id"] == "67"
    assert result["match_status"] == "matched"


def test_body_bare_number_near_match_not_allowed() -> None:
    """A bare body number one digit off a truck is a load fragment, not a typo."""
    board = {"940": BoardTruck(truck_id="940", dispatcher="Felix")}
    mentions = extract_number_mentions("ref 943 total 1500", "email_body")
    matches = candidate_matches(mentions, board)

    assert not [m for m in matches if m["match_type"] == "one_digit_off"]


def test_bol_sender_is_excluded_but_statements_sender_is_retained() -> None:
    """Exclude only BOL chatter; do not broadly exclude statements emails."""
    board = {"649": BoardTruck(truck_id="649", dispatcher="Carlos CA")}

    bol_msg = EmailMessage()
    bol_msg["From"] = "Bol Department <bol@prestige.inc>"
    bol_msg["Subject"] = "TRUCK 649"
    bol_msg.set_content("TRUCK 649")

    statements_msg = EmailMessage()
    statements_msg["From"] = "Statements <statements@prestigetransportation.com>"
    statements_msg["Subject"] = "TRUCK 649"
    statements_msg.set_content("TRUCK 649")

    assert build_document_rows_from_message(bol_msg, board) == []
    rows = build_document_rows_from_message(statements_msg, board)
    assert len(rows) == 1
    assert rows[0]["matched_truck_id"] == "649"


def test_stale_bol_rows_are_not_ui_alerts() -> None:
    """Old BOL rows already in Supabase should be hidden from alert lane."""
    from services.rate_confirmation_data import rate_confirmation_alerts

    docs = [
        {
            "sender_email": "bol@prestige.inc",
            "match_status": "unmatched",
            "alert_level": "red",
        },
        {
            "sender_email": "statements@prestigetransportation.com",
            "match_status": "unmatched",
            "alert_level": "red",
        },
    ]

    alerts = rate_confirmation_alerts(docs)
    assert len(alerts) == 1
    assert alerts[0]["sender_email"] == "statements@prestigetransportation.com"


def test_trailer_pairing_rescues_dispatcher_shorthand() -> None:
    """'39 / 4907 / Brittany' should match truck 39 via the board trailer 4907."""
    from services.rate_confirmation_ingest import board_trailer_map

    board = {
        "39": BoardTruck(truck_id="39", dispatcher="Brittany", trailer_id="4907"),
        "713": BoardTruck(truck_id="713", dispatcher="Brittany", trailer_id="5001"),
    }
    trailer_map = board_trailer_map(board)
    assert trailer_map == {"4907": "39", "5001": "713"}

    mentions = extract_number_mentions("39\n4907\nBrittany", "email_body")
    matches = candidate_matches(mentions, board, trailer_map)
    result = select_single_truck(matches, sender_dispatcher="Brittany")

    assert result["matched_truck_id"] == "39"
    assert result["match_status"] == "matched"
    assert "matched_via_board_trailer" in result["alert_codes"] or "dispatcher_short_truck_match" in result["alert_codes"]


def test_short_number_only_matches_senders_own_board() -> None:
    """Bare 2-digit tokens only count when the sender's own board has that truck."""
    board = {"39": BoardTruck(truck_id="39", dispatcher="Brittany")}
    mentions = extract_number_mentions("39 loaded and rolling", "email_body")
    matches = candidate_matches(mentions, board)

    own = select_single_truck(matches, sender_dispatcher="Brittany")
    assert own["matched_truck_id"] == "39"
    assert "dispatcher_short_truck_match" in own["alert_codes"]

    unknown = select_single_truck(matches, sender_dispatcher="")
    assert unknown["matched_truck_id"] == ""
    assert unknown["match_status"] == "unmatched"


def test_signature_footer_numbers_are_stripped() -> None:
    """Company footer phones/MC numbers must not become truck candidates."""
    from services.rate_confirmation_ingest import strip_signature

    body = (
        "TRUCK 940 DRIVER SAM\n"
        "*Please verify with us before booking any loads by calling 224-715-1371*\n"
        "*We do not use any GMAIL.com, we only use PRESTIGE.INC*\n"
        "MC 553373 3810 North Ave 60165 773 303 4616"
    )
    stripped = strip_signature(body)
    assert "TRUCK 940" in stripped
    assert "553373" not in stripped
    assert "1371" not in stripped


def test_inline_signature_images_do_not_create_rows() -> None:
    """image001.jpg logo attachments must not become separate unmatched docs."""
    from services.rate_confirmation_ingest import _is_inline_signature_image

    assert _is_inline_signature_image({"content_type": "image/jpeg", "filename": "image001.jpg"})
    assert _is_inline_signature_image({"content_type": "image/png", "filename": "Outlook-abc123.png"})
    assert not _is_inline_signature_image({"content_type": "image/png", "filename": "signed_rate_con_photo.png"})
    assert not _is_inline_signature_image({"content_type": "application/pdf", "filename": "image001.pdf"})


def test_pt_bol_sender_is_also_excluded() -> None:
    board = {"649": BoardTruck(truck_id="649", dispatcher="Carlos CA")}
    msg = EmailMessage()
    msg["From"] = "Proof of Delivery Department <bol@prestigetransportation.com>"
    msg["Subject"] = "TRUCK 649"
    msg.set_content("TRUCK 649")

    assert build_document_rows_from_message(msg, board) == []


def test_pdf_text_fallback_source_can_match() -> None:
    """pdf_text mentions should resolve when nothing else matched."""
    board = {"1971": BoardTruck(truck_id="1971", dispatcher="Carlos IL")}
    mentions = extract_number_mentions("TRUCK 1971 PICKUP MONDAY", "pdf_text")
    matches = candidate_matches(mentions, board)
    result = select_single_truck(matches)

    assert result["matched_truck_id"] == "1971"
    assert result["match_status"] == "matched"


def test_scanned_pdf_ocr_fallback_matches_and_parses(monkeypatch) -> None:
    """Blank text-layer PDFs should use OCR text for matching and parsing."""
    from services import rate_confirmation_ingest as ingest

    monkeypatch.setattr(ingest, "extract_pdf_text", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        ingest,
        "ocr_pdf_text",
        lambda *_args, **_kwargs: OcrResult(
            text="""
            RXO Load Confirmation
            TRUCK 1971
            Total Carrier Pay $1,250.00
            STOP DETAIL
            Pick 07/02/26 SHIPPER
            Chicago, IL 60601
            Drop 07/03/26 RECEIVER
            Fontana, CA 92335
            """,
            status="ocr_text_extracted",
            pages_rendered=1,
            pages_ocrd=1,
            chars=180,
        ),
    )

    msg = EmailMessage()
    msg["From"] = "Dispatcher <dispatch1@prestige.inc>"
    msg["Subject"] = "Load confirmation"
    msg["Message-ID"] = "<ocr-test@example.com>"
    msg.set_content("Please see attached.")
    msg.add_attachment(b"%PDF fake scanned", maintype="application", subtype="pdf", filename="scan.pdf")

    rows = build_document_rows_from_message(msg, {"1971": BoardTruck(truck_id="1971", dispatcher="Carlos IL")})

    assert len(rows) == 1
    row = rows[0]
    assert row["matched_truck_id"] == "1971"
    assert row["match_source"] == "pdf_text"
    assert row["broker_name"] == "RXO"
    assert row["rate_amount"] == 1250.0
    assert row["parse_status"] == "parsed"
    assert row["parsed_fields"]["text_source"] == "ocr"
    assert row["parsed_fields"]["ocr"]["status"] == "ocr_text_extracted"
