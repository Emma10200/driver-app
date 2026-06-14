from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

import requests

from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)


class SupabaseQboError(RuntimeError):
    pass


class SupabaseRestClient:
    """Small PostgREST client matching the repo's raw-requests Supabase style."""

    def __init__(self) -> None:
        self._url = (get_runtime_secret("SUPABASE_URL", "") or "").rstrip("/")
        self._key = (
            get_runtime_secret("SUPABASE_SERVICE_KEY")
            or get_runtime_secret("SUPABASE_KEY", "")
            or ""
        ).strip()
        if not self._url or not self._key:
            raise SupabaseQboError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required for QBO importer.")

    def select(
        self,
        table: str,
        *,
        select: str = "*",
        filters: dict[str, Any] | None = None,
        order: str = "",
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"select": select}
        for key, value in (filters or {}).items():
            params[key] = value
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = int(limit)
        if offset is not None:
            params["offset"] = int(offset)
        response = self._request("GET", f"/{table}?{urlencode(params)}")
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def select_all(
        self,
        table: str,
        *,
        select: str = "*",
        filters: dict[str, Any] | None = None,
        order: str = "",
        page_size: int = 1000,
        hard_cap: int = 100000,
    ) -> list[dict[str, Any]]:
        """Fetch EVERY matching row, paging past PostgREST's server max-rows cap.

        Supabase/PostgREST enforces a server-side ``max-rows`` limit (commonly
        1000). A single ``select(limit=5000)`` is silently truncated to that cap,
        so large tables (e.g. a 4000-part catalog) come back incomplete. This
        loops with ``offset`` until a short page (or the hard cap) is reached.
        """
        out: list[dict[str, Any]] = []
        offset = 0
        page_size = max(1, int(page_size))
        while offset < hard_cap:
            page = self.select(
                table,
                select=select,
                filters=filters,
                order=order,
                limit=page_size,
                offset=offset,
            )
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return out

    def insert(self, table: str, rows: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
        response = self._request(
            "POST",
            f"/{table}",
            json=rows,
            extra_headers={"Prefer": "return=representation"},
        )
        if not response.content:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def upsert(
        self,
        table: str,
        rows: dict[str, Any] | list[dict[str, Any]],
        *,
        on_conflict: str = "",
    ) -> list[dict[str, Any]]:
        path = f"/{table}"
        if on_conflict:
            path += "?" + urlencode({"on_conflict": on_conflict})
        response = self._request(
            "POST",
            path,
            json=rows,
            extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        if not response.content:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def patch(
        self,
        table: str,
        row: dict[str, Any],
        *,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        params = urlencode(filters)
        response = self._request(
            "PATCH",
            f"/{table}?{params}",
            json=row,
            extra_headers={"Prefer": "return=representation"},
        )
        if not response.content:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def rpc(self, function_name: str, payload: dict[str, Any] | None = None) -> Any:
        response = self._request("POST", f"/rpc/{function_name}", json=payload or {})
        if not response.content:
            return None
        return response.json()

    def delete(self, table: str, *, filters: dict[str, Any]) -> None:
        params = urlencode(filters)
        self._request("DELETE", f"/{table}?{params}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Response:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        if json is not None:
            headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        response = requests.request(
            method,
            f"{self._url}/rest/v1{path}",
            headers=headers,
            json=json,
            timeout=45,
        )
        if not response.ok:
            logger.error("Supabase QBO %s %s failed: %s %s", method, path, response.status_code, response.text[:500])
            raise SupabaseQboError(f"Supabase request failed: HTTP {response.status_code} {response.text[:500]}")
        return response
