"""Read-only dispatch board mirror data from Supabase."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_client():
    from services.qbo_supabase import SupabaseRestClient

    return SupabaseRestClient()


def load_dispatch_board_rows() -> list[dict[str, Any]]:
    """Load the current mirrored DISPATCH sheet rows.

    Apps Script keeps `dispatch_board_rows` as a row-key mirror of the user-facing
    Google Sheet. The table is intentionally small, but use select_all anyway so
    this page never trips over PostgREST's common 1,000-row cap.
    """
    try:
        client = _get_client()
    except Exception as exc:
        logger.warning("Dispatch board unavailable: %s", exc)
        return []

    try:
        rows = client.select_all(
            "dispatch_board_rows",
            select="row_key,snapshot_id,sheet_row,truck_id,trailer_id,driver_name,dispatcher,division,status,origin,destination,pickup_at,delivery_at,updated_timestamp,source_updated_at,raw",
            order="sheet_row.asc",
            page_size=1000,
            hard_cap=10000,
        )
    except Exception as exc:
        logger.warning("Dispatch board rows query failed: %s", exc)
        return []
    return rows
