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
