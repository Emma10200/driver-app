from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from services.qbo_supabase import SupabaseRestClient

logger = logging.getLogger(__name__)


def source_file_hash(content: bytes) -> str:
    return hashlib.sha256(content or b"").hexdigest()


class SupabaseAuditLog:
    def __init__(
        self,
        client: SupabaseRestClient,
        *,
        imported_by_email: str = "",
        source_file_name: str = "",
        source_hash: str = "",
        app_version: str = "qbo-streamlit-v1",
    ) -> None:
        self._client = client
        self._imported_by_email = imported_by_email
        self._source_file_name = source_file_name
        self._source_hash = source_hash
        self._app_version = app_version

    def record(
        self,
        *,
        txn_type: str,
        realm_id: str,
        status: str,
        doc_number: str = "",
        txn_date: str = "",
        entity_name: str = "",
        division: str = "",
        amount: float | None = None,
        qbo_id: str = "",
        message: str = "",
        raw_response: Any = None,
    ) -> int:
        raw_json = None
        if raw_response is not None:
            try:
                raw_json = raw_response if isinstance(raw_response, (dict, list)) else {"raw": str(raw_response)[:4000]}
            except Exception:  # pragma: no cover - defensive serialization fallback
                raw_json = {"raw": str(raw_response)[:4000]}

        idempotency_key = self._idempotency_key(
            txn_type=txn_type,
            realm_id=realm_id,
            doc_number=doc_number,
            txn_date=txn_date,
            entity_name=entity_name,
            amount=amount,
        )
        rows = self._client.insert(
            "qbo_audit_log",
            {
                "imported_by_email": self._imported_by_email,
                "txn_type": txn_type,
                "realm_id": str(realm_id or ""),
                "division": division or "",
                "doc_number": doc_number or "",
                "txn_date": txn_date or None,
                "entity_name": entity_name or "",
                "amount": float(amount) if amount is not None else None,
                "status": status,
                "qbo_id": qbo_id or "",
                "message": message or "",
                "source_file_name": self._source_file_name,
                "source_file_hash": self._source_hash,
                "idempotency_key": idempotency_key,
                "app_version": self._app_version,
                "raw_response": raw_json,
            },
        )
        row_id = int((rows[0] or {}).get("id") or 0) if rows else 0
        if idempotency_key and status in {"success", "duplicate"}:
            self._upsert_idempotency(
                idempotency_key=idempotency_key,
                realm_id=realm_id,
                txn_type=txn_type,
                doc_number=doc_number,
                txn_date=txn_date,
                entity_name=entity_name,
                amount=amount,
                audit_log_id=row_id,
            )
        return row_id

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._client.select(
            "qbo_audit_log",
            select="id,created_at,imported_by_email,txn_type,realm_id,division,doc_number,txn_date,entity_name,amount,status,qbo_id,message,source_file_name",
            order="created_at.desc",
            limit=limit,
        )

    def _upsert_idempotency(
        self,
        *,
        idempotency_key: str,
        realm_id: str,
        txn_type: str,
        doc_number: str,
        txn_date: str,
        entity_name: str,
        amount: float | None,
        audit_log_id: int,
    ) -> None:
        try:
            self._client.upsert(
                "qbo_idempotency",
                {
                    "idempotency_key": idempotency_key,
                    "realm_id": realm_id,
                    "txn_type": txn_type,
                    "doc_number": doc_number or "",
                    "txn_date": txn_date or None,
                    "entity_ref_id": entity_name or "",
                    "amount": float(amount) if amount is not None else None,
                    "source_file_hash": self._source_hash,
                    "audit_log_id": audit_log_id or None,
                    "created_by_email": self._imported_by_email,
                },
                on_conflict="idempotency_key",
            )
        except Exception as exc:  # noqa: BLE001 - do not fail import after QBO post because idempotency side-write failed
            logger.warning("Could not upsert QBO idempotency key %s: %s", idempotency_key, exc)

    @staticmethod
    def _idempotency_key(
        *,
        txn_type: str,
        realm_id: str,
        doc_number: str,
        txn_date: str,
        entity_name: str,
        amount: float | None,
    ) -> str:
        raw = "|".join(
            [
                str(txn_type or "").strip().lower(),
                str(realm_id or "").strip().lower(),
                str(doc_number or "").strip().lower(),
                str(txn_date or "").strip().lower(),
                str(entity_name or "").strip().lower(),
                f"{float(amount):.2f}" if amount is not None else "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw.strip("|") else ""


def serialize_preview_rows(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, default=str)
