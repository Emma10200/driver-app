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


def _is_excluded_sender(doc: dict[str, Any]) -> bool:
    """Keep stale excluded senders out of the UI even before DB cleanup runs."""
    try:
        from services.rate_confirmation_ingest import is_excluded_sender_email

        return is_excluded_sender_email(str(doc.get("sender_email") or ""))
    except Exception:
        return False


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
        rows = client.select_all(
            "rate_confirmation_documents",
            select=(
                "document_key,message_id,attachment_index,attachment_filename,attachment_content_type,attachment_sha256,"
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
        return [row for row in rows if not _is_excluded_sender(row)]
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
        "attachment_sha256": str(doc.get("attachment_sha256") or "").strip(),
        "pdf_storage_path": str(doc.get("pdf_storage_path") or "").strip(),
        "alert_level": str(doc.get("alert_level") or "").strip(),
        "alert_codes": alert_codes,
        "alert_notes": str(doc.get("alert_notes") or "").strip(),
        "candidate_matches": candidate_matches,
        "raw": raw,
    }


def dedupe_rate_confirmation_documents(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse repeated forwards/replies of the same rate confirmation for display.

    Supabase intentionally stores one row per email attachment/document for audit
    purposes. The dispatch-board UI should not show five copies of the same PDF
    under a truck, so this groups likely duplicates by truck + load reference,
    PDF hash, or filename/subject fallback. The newest row is kept and annotated
    with ``duplicate_count`` and ``duplicate_documents`` for troubleshooting.
    """
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        grouped[_dedupe_key(doc)].append(doc)

    out: list[dict[str, Any]] = []
    for group_docs in grouped.values():
        group_docs = sorted(group_docs, key=lambda item: str(item.get("received_at") or ""), reverse=True)
        primary = dict(group_docs[0])
        primary["duplicate_count"] = len(group_docs)
        primary["duplicate_documents"] = group_docs[1:]
        out.append(primary)
    out.sort(key=lambda item: str(item.get("received_at") or ""), reverse=True)
    return out


def _dedupe_key(doc: dict[str, Any]) -> str:
    truck = str(doc.get("matched_truck_id") or "no-truck").strip().lower()
    load_ref = str(doc.get("load_reference") or "").strip().lower()
    if load_ref:
        return f"truck:{truck}|load:{load_ref}"
    digest = str(doc.get("attachment_sha256") or "").strip().lower()
    if digest:
        return f"truck:{truck}|sha:{digest}"
    filename = str(doc.get("attachment_filename") or "").strip().lower()
    subject = str(doc.get("subject") or "").strip().lower()
    sender = str(doc.get("sender_domain") or doc.get("sender_email") or "").strip().lower()
    return f"truck:{truck}|fallback:{sender}|{filename}|{subject}"


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
        if _is_excluded_sender(doc):
            continue
        level = str(doc.get("alert_level") or "").strip().lower()
        status = str(doc.get("match_status") or "").strip().lower()
        if level in {"red", "yellow"} or status in {"ambiguous", "unmatched", "cancelled"}:
            out.append(doc)
    return out
