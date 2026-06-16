"""Read-only QBO attachment access for the shop Invoice History screen.

The shop manager scans supporting documents (receipts, photos, signed work
orders) into QuickBooks and links them to an invoice. This module lists those
linked attachments and downloads their bytes so the mobile app can show the
original scanned document next to each invoice.

Per the project's separation of concerns, all QBO API access lives in the
``qbo`` layer; the Streamlit page calls these helpers and renders the result.
This module never writes to QBO - it only reads.

No Supabase table or storage bucket is required: attachments are fetched
on demand straight from QuickBooks using the ``Attachable`` entity and its
short-lived ``TempDownloadUri`` pre-signed URLs.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from qbo.api_client import QboClient
from qbo.shop_inventory_sync import build_services, resolve_shop_realm_id

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT = 45


def _escape_literal(value: str) -> str:
    """Escape a value for safe use inside a QBO query string literal."""
    return str(value or "").replace("'", "''")


def list_invoice_attachments(
    qbo_client: QboClient, realm_id: str, invoice_id: str
) -> list[dict[str, Any]]:
    """List file attachments linked to a single invoice.

    Returns one dict per attachment with the metadata the UI needs plus a
    fresh (~15 minute) ``temp_download_uri`` that can be downloaded directly.
    Notes (attachments without a file) are skipped.
    """
    invoice_id = str(invoice_id or "").strip()
    if not realm_id or not invoice_id:
        return []

    sql = (
        "SELECT * FROM Attachable WHERE "
        f"AttachableRef.EntityRef.Type = 'Invoice' AND "
        f"AttachableRef.EntityRef.value = '{_escape_literal(invoice_id)}'"
    )
    try:
        response = qbo_client.query(sql, realm_id=realm_id)
    except Exception:  # noqa: BLE001 - surface an empty list, the UI shows a notice
        logger.exception("Attachable query failed for invoice %s", invoice_id)
        raise

    rows = (response.get("QueryResponse") or {}).get("Attachable") or []
    out: list[dict[str, Any]] = []
    for row in rows:
        file_name = str(row.get("FileName") or "").strip()
        if not file_name:
            # Standalone note (no file) - nothing to display as a document.
            continue
        out.append(
            {
                "attachable_id": str(row.get("Id") or ""),
                "file_name": file_name,
                "content_type": str(row.get("ContentType") or ""),
                "category": str(row.get("Category") or ""),
                "size": row.get("Size"),
                "note": str(row.get("Note") or ""),
                "temp_download_uri": str(row.get("TempDownloadUri") or ""),
                "thumbnail_temp_download_uri": str(row.get("ThumbnailTempDownloadUri") or ""),
            }
        )
    return out


def fresh_temp_download_uri(
    qbo_client: QboClient, realm_id: str, attachable_id: str
) -> str:
    """Get a fresh temporary download URL for an attachment.

    Used when the URL captured during listing has expired (they live ~15 min).
    """
    attachable_id = str(attachable_id or "").strip()
    if not realm_id or not attachable_id:
        return ""
    try:
        response = qbo_client.get(f"/download/{attachable_id}", realm_id=realm_id)
    except Exception:  # noqa: BLE001
        logger.exception("Download-URL fetch failed for attachable %s", attachable_id)
        return ""
    # The endpoint returns the URL as a (quoted) text/plain body.
    text = (response.text or "").strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        text = text[1:-1]
    return text


def download_attachment_bytes(
    qbo_client: QboClient,
    realm_id: str,
    attachable_id: str,
    *,
    temp_download_uri: str = "",
) -> bytes:
    """Download the raw bytes of an attachment.

    Prefers the supplied ``temp_download_uri``; if that is missing or expired,
    requests a fresh URL from QuickBooks and retries once.
    """
    uri = str(temp_download_uri or "").strip()
    if uri:
        data = _get_bytes(uri)
        if data is not None:
            return data
    # Expired or missing - ask QBO for a new pre-signed URL and retry.
    fresh = fresh_temp_download_uri(qbo_client, realm_id, attachable_id)
    if fresh:
        data = _get_bytes(fresh)
        if data is not None:
            return data
    return b""


def _get_bytes(url: str) -> bytes | None:
    """GET a pre-signed URL. Returns ``None`` on any non-OK response."""
    try:
        response = requests.get(url, timeout=_DOWNLOAD_TIMEOUT)
    except requests.RequestException:
        logger.exception("Attachment download request failed")
        return None
    if not response.ok:
        logger.warning("Attachment download returned HTTP %s", response.status_code)
        return None
    return response.content


def build_shop_qbo_client() -> tuple[QboClient, str]:
    """Construct a QBO client and resolve the shop realm id in one call.

    Convenience wrapper so the Streamlit page doesn't need to know the wiring.
    """
    qbo_client, token_repo, _supabase = build_services()
    realm_id = resolve_shop_realm_id(token_repo)
    return qbo_client, realm_id
