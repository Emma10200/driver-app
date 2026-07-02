from __future__ import annotations

from services.rate_confirmation_ingest import (
    BoardTruck,
    candidate_matches,
    extract_number_mentions,
    select_single_truck,
)


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
