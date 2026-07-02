"""Read-only rate-confirmation document data for the dispatch-board UI."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def _get_client():
    from services.qbo_supabase import SupabaseRestClient

    return SupabaseRestClient()


def load_rate_confirmation_documents(days: int = 14) -> list[dict[str, Any]]:
    """Load recent rate-confirmation rows mirrored from the Gmail inbox.

    The table may be empty while the ingester is not yet scheduled; callers should
    treat an empty list as a valid state.
    """
    try:
        client = _get_client()
    except Exception as exc:
        logger.warning("Rate confirmations unavailable: %s", exc)
        return []

    since = (datetime.now(UTC) - timedelta(days=max(1, int(days)))).isoformat()
    try:
        return client.select_all(
            "rate_confirmation_documents",
            select=(
                "document_key,message_id,attachment_index,attachment_filename,attachment_content_type,"
                "received_at,sender_name,sender_email,sender_domain,domain_division,subject,"
                "matched_truck_id,match_status,match_type,match_source,match_token,match_confidence,"
                "candidate_matches,board_dispatcher,board_driver_name,board_division,board_sheet_row,"
                "load_reference,broker_name,pickup_summary,delivery_summary,pickup_at,delivery_at,"
                "rate_amount,stops,parse_status,pdf_storage_path,original_available,alert_level,"
                "alert_codes,alert_notes,raw"
            ),
            filters={"received_at": f"gte.{since}"},
            order="received_at.desc.nullslast",
            page_size=1000,
            hard_cap=20000,
        )
    except Exception as exc:
        logger.warning("Rate confirmations query failed: %s", exc)
        return []


def normalize_rate_confirmation_doc(doc: dict[str, Any]) -> dict[str, Any]:
    raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
    alert_codes = doc.get("alert_codes")
    if not isinstance(alert_codes, list):
        alert_codes = []
    candidate_matches = doc.get("candidate_matches")
    if not isinstance(candidate_matches, list):
        candidate_matches = []
    return {
        **doc,
        "document_key": str(doc.get("document_key") or ""),
        "received_at": str(doc.get("received_at") or ""),
        "sender_name": str(doc.get("sender_name") or "").strip(),
        "sender_email": str(doc.get("sender_email") or "").strip(),
        "sender_domain": str(doc.get("sender_domain") or "").strip(),
        "domain_division": str(doc.get("domain_division") or "").strip(),
        "subject": str(doc.get("subject") or "").strip(),
        "matched_truck_id": str(doc.get("matched_truck_id") or "").strip(),
        "match_status": str(doc.get("match_status") or "").strip(),
        "match_type": str(doc.get("match_type") or "").strip(),
        "match_source": str(doc.get("match_source") or "").strip(),
        "match_token": str(doc.get("match_token") or "").strip(),
        "board_dispatcher": str(doc.get("board_dispatcher") or "").strip(),
        "board_driver_name": str(doc.get("board_driver_name") or "").strip(),
        "board_division": str(doc.get("board_division") or "").strip(),
        "load_reference": str(doc.get("load_reference") or "").strip(),
        "broker_name": str(doc.get("broker_name") or "").strip(),
        "pickup_summary": str(doc.get("pickup_summary") or "").strip(),
        "delivery_summary": str(doc.get("delivery_summary") or "").strip(),
        "parse_status": str(doc.get("parse_status") or "").strip(),
        "attachment_filename": str(doc.get("attachment_filename") or "").strip(),
        "pdf_storage_path": str(doc.get("pdf_storage_path") or "").strip(),
        "alert_level": str(doc.get("alert_level") or "").strip(),
        "alert_codes": alert_codes,
        "alert_notes": str(doc.get("alert_notes") or "").strip(),
        "candidate_matches": candidate_matches,
        "raw": raw,
    }


def group_rate_confirmations_by_truck(docs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        truck_id = str(doc.get("matched_truck_id") or "").strip()
        if truck_id:
            grouped[truck_id].append(doc)
    return dict(grouped)


def rate_confirmation_alerts(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return documents that should be surfaced in the dispatch-board alert lane."""
    out: list[dict[str, Any]] = []
    for doc in docs:
        level = str(doc.get("alert_level") or "").strip().lower()
        status = str(doc.get("match_status") or "").strip().lower()
        if level in {"red", "yellow"} or status in {"ambiguous", "unmatched", "cancelled"}:
            out.append(doc)
    return out
