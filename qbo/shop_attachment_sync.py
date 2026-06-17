"""QBO -> Supabase sync for the shop attachment index.

Caches WHICH transactions have scanned documents (and each file's stable
metadata) so the Invoice/Purchase history lists and part-history rows can show
document badges + open buttons instantly, without scanning the QBO ``Attachable``
entity on every page load.

Only lightweight metadata is cached (file name, content type, size, note). The
document BYTES are never copied: they always stream on demand straight from
QuickBooks' short-lived pre-signed URLs, so this cache stays tiny (KBs per
transaction). This module never writes to QBO - it only reads.

A single manual run via the shop "Sync All" button stores all historical
attachments. The same entry point is cron-ready for later automation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from qbo.api_client import QboClient, QboRateLimitError
from qbo.shop_inventory_sync import build_services, resolve_shop_realm_id
from services.qbo_supabase import SupabaseRestClient

logger = logging.getLogger(__name__)

_CACHE_TABLE = "shop_attachment_index_cache"
_STATE_TABLE = "shop_attachment_index_sync_state"
_PAGE_SIZE = 1000
_UPSERT_CHUNK = 250
_MAX_RATE_LIMIT_RETRIES = 3

# Upper bound on how many Attachable records the scan will pull. Big enough for a
# multi-year shop; paginated 1000 at a time.
_SCAN_CAP = 20000


@dataclass(slots=True)
class AttachmentSyncResult:
    realm_id: str = ""
    files_scanned: int = 0
    links_upserted: int = 0
    status: str = ""
    message: str = ""
    errors: list[str] = field(default_factory=list)


def sync_shop_attachments(realm_id: str | None = None, *, force_full: bool = True) -> AttachmentSyncResult:
    """Rebuild the Supabase attachment index from a full QBO ``Attachable`` scan.

    ``force_full`` is accepted for signature parity with the other shop syncs;
    the attachment index is always a full rebuild (links can be added or removed
    on existing files, so a high-water cursor is not reliable). Rows for the
    realm that disappear from QuickBooks are pruned after the upsert.
    """
    _ = force_full  # always a full rebuild; kept for a consistent call signature
    result = AttachmentSyncResult()
    try:
        qbo_client, token_repo, supabase = build_services()
        result.realm_id = realm_id or resolve_shop_realm_id(token_repo)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Service initialisation failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Attachment index sync could not initialise")
        return result

    if not result.realm_id:
        result.status = "failed"
        result.message = "No shop QuickBooks company is connected yet."
        result.errors.append(result.message)
        return result

    run_started = datetime.now(UTC).isoformat()
    try:
        links, files_scanned = _scan_links(qbo_client, result.realm_id)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"QBO Attachable scan failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Attachment index scan failed")
        _save_state(supabase, result.realm_id, run_started, "failed", result.message, 0)
        return result

    result.files_scanned = files_scanned
    rows = [
        {
            "realm_id": result.realm_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "attachments": metas,
            "attachment_count": len(metas),
            "last_synced": run_started,
        }
        for (entity_type, entity_id), metas in links.items()
    ]

    try:
        upserted = _upsert_rows(supabase, rows)
        _prune_stale(supabase, result.realm_id, run_started)
    except Exception as exc:  # noqa: BLE001
        result.status = "failed"
        result.message = f"Supabase upsert failed: {exc}"
        result.errors.append(str(exc))
        logger.exception("Attachment index upsert failed")
        _save_state(supabase, result.realm_id, run_started, "failed", result.message, 0)
        return result

    result.links_upserted = upserted
    result.status = "success"
    result.message = f"Cached documents for {upserted} transaction(s) from {files_scanned} file(s)."
    _save_state(
        supabase,
        result.realm_id,
        run_started,
        "success",
        result.message,
        upserted,
        full_completed=True,
    )
    return result


def _stable_meta(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Attachable into the stable metadata the UI caches.

    Deliberately omits ``TempDownloadUri`` / ``ThumbnailTempDownloadUri``: those
    pre-signed URLs expire in ~15 minutes, so caching them would be misleading.
    A fresh URL is minted on demand when the user actually opens a document.
    """
    return {
        "attachable_id": str(row.get("Id") or ""),
        "file_name": str(row.get("FileName") or "").strip(),
        "content_type": str(row.get("ContentType") or ""),
        "category": str(row.get("Category") or ""),
        "size": row.get("Size"),
        "note": str(row.get("Note") or ""),
        "temp_download_uri": "",
        "thumbnail_temp_download_uri": "",
    }


def _scan_links(
    qbo_client: QboClient, realm_id: str
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], int]:
    """Scan ALL file attachments once, grouped by linked entity.

    Returns ``({(entity_type_lower, entity_id): [meta, ...]}, files_scanned)``.
    One Attachable can be linked to several entities, so every
    ``AttachableRef.EntityRef`` is recorded.
    """
    links: dict[tuple[str, str], list[dict[str, Any]]] = {}
    files_scanned = 0
    start = 1
    fetched = 0
    while fetched < _SCAN_CAP:
        sql = f"SELECT * FROM Attachable STARTPOSITION {start} MAXRESULTS {_PAGE_SIZE}"
        response = _query_with_retry(qbo_client, sql, realm_id)
        rows = (response.get("QueryResponse") or {}).get("Attachable") or []
        if not rows:
            break
        for row in rows:
            if not str(row.get("FileName") or "").strip():
                continue  # standalone note, no file
            files_scanned += 1
            meta = _stable_meta(row)
            for ref in row.get("AttachableRef") or []:
                if not isinstance(ref, dict):
                    continue
                ent = ref.get("EntityRef") or {}
                etype = str(ent.get("type") or "").strip().lower()
                eid = str(ent.get("value") or "").strip()
                if etype and eid:
                    links.setdefault((etype, eid), []).append(meta)
        fetched += len(rows)
        if len(rows) < _PAGE_SIZE:
            break
        start += _PAGE_SIZE
    return links, files_scanned


def _query_with_retry(qbo_client: QboClient, sql: str, realm_id: str) -> dict[str, Any]:
    attempt = 0
    while True:
        try:
            return qbo_client.query(sql, realm_id=realm_id)
        except QboRateLimitError as exc:
            attempt += 1
            if attempt > _MAX_RATE_LIMIT_RETRIES:
                raise
            time.sleep(exc.retry_after_seconds or 60)


def _upsert_rows(supabase: SupabaseRestClient, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    count = 0
    for start in range(0, len(rows), _UPSERT_CHUNK):
        chunk = rows[start : start + _UPSERT_CHUNK]
        supabase.upsert(_CACHE_TABLE, chunk, on_conflict="realm_id,entity_type,entity_id")
        count += len(chunk)
    return count


def _prune_stale(supabase: SupabaseRestClient, realm_id: str, run_started: str) -> None:
    """Delete cache rows not refreshed this run (attachments removed in QBO)."""
    supabase.delete(
        _CACHE_TABLE,
        filters={"realm_id": f"eq.{realm_id}", "last_synced": f"lt.{run_started}"},
    )


def _save_state(
    supabase: SupabaseRestClient,
    realm_id: str,
    run_at: str,
    status: str,
    message: str,
    links_upserted: int,
    *,
    full_completed: bool = False,
) -> None:
    row: dict[str, Any] = {
        "realm_id": realm_id,
        "last_run_at": run_at,
        "last_run_status": status,
        "last_run_message": message[:1000],
        "links_upserted": int(links_upserted),
    }
    if full_completed:
        row["full_sync_completed_at"] = run_at
    try:
        supabase.upsert(_STATE_TABLE, row, on_conflict="realm_id")
    except Exception:  # noqa: BLE001 - state is best-effort telemetry
        logger.exception("Attachment index sync-state save failed")
