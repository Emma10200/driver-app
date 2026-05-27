from __future__ import annotations

from typing import Any

import requests

from services.qbo_auth import QboAuthService, get_qbo_secret


class QboClient:
    def __init__(self, auth_service: QboAuthService) -> None:
        self._auth_service = auth_service

    def get_company_info(self, realm_id: str) -> dict[str, Any]:
        return self.get(f"/companyinfo/{realm_id}", realm_id=realm_id).json()

    def query(self, sql: str, realm_id: str) -> dict[str, Any]:
        return self.get("/query", realm_id=realm_id, params={"query": sql}).json()

    def get(self, path: str, realm_id: str, params: dict[str, Any] | None = None) -> requests.Response:
        return self._request("GET", path=path, realm_id=realm_id, params=params)

    def post(
        self,
        path: str,
        realm_id: str,
        payload: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> requests.Response:
        return self._request("POST", path=path, realm_id=realm_id, json=payload, params=params)

    def _request(self, method: str, path: str, realm_id: str, **kwargs: Any) -> requests.Response:
        if not realm_id:
            raise ValueError("realm_id is required for every QBO API call.")

        access_token = self._auth_service.get_valid_access_token(realm_id)
        url = self._build_url(realm_id=realm_id, path=path)

        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {access_token}"
        headers.setdefault("Accept", "application/json")
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")

        params = dict(kwargs.pop("params", None) or {})
        params.setdefault("minorversion", get_qbo_secret("minor_version", "QBO_MINOR_VERSION", "70"))

        response = requests.request(method, url, headers=headers, params=params, timeout=45, **kwargs)
        if not response.ok:
            raise RuntimeError(f"QBO {method} {path} failed: HTTP {response.status_code} {response.text[:500]}")
        return response

    def _build_url(self, realm_id: str, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        realm = self._auth_service.token_repo.get_realm(realm_id)
        environment = str((realm.environment if realm else "") or self._auth_service.environment()).lower()
        base = "https://quickbooks.api.intuit.com" if environment == "production" else "https://sandbox-quickbooks.api.intuit.com"
        return f"{base}/v3/company/{realm_id}{normalized_path}"
