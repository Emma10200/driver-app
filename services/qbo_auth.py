from __future__ import annotations

import base64
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests

from qbo.models import ConnectedRealm
from services.qbo_supabase import SupabaseRestClient
from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)

_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_QBO_BASE_PROD = "https://quickbooks.api.intuit.com"
_QBO_BASE_SANDBOX = "https://sandbox-quickbooks.api.intuit.com"
_ACCESS_REFRESH_LEEWAY_SECONDS = 120


class QboAuthError(RuntimeError):
    pass


def _mapping_get(mapping: Any, key: str) -> Any:
    try:
        if hasattr(mapping, "get"):
            return mapping.get(key)
        return mapping[key]
    except Exception:
        return getattr(mapping, key, None)


def _streamlit_qbo_secret(name: str) -> str:
    try:
        import streamlit as st
        from streamlit.errors import StreamlitSecretNotFoundError
    except Exception:
        return ""

    try:
        qbo_section = _mapping_get(st.secrets, "qbo")
    except StreamlitSecretNotFoundError:
        return ""
    except Exception:
        return ""
    if not qbo_section:
        return ""
    return str(_mapping_get(qbo_section, name) or "").strip()


def get_qbo_secret(name: str, env_name: str, default: str = "") -> str:
    value = _streamlit_qbo_secret(name)
    if value:
        return value
    return (get_runtime_secret(env_name, default) or default or "").strip()


def parse_email_list(raw_value: str | None) -> set[str]:
    if not raw_value:
        return set()
    normalized = raw_value.replace(";", ",").replace("\n", ",")
    return {item.strip().lower() for item in normalized.split(",") if item.strip()}


def qbo_allowed_emails() -> set[str]:
    return parse_email_list(get_qbo_secret("allowed_emails", "QBO_ALLOWED_EMAILS", ""))


class QboTokenRepository:
    def __init__(self, client: SupabaseRestClient) -> None:
        self._client = client

    def list_realms(self) -> list[ConnectedRealm]:
        rows = self._client.select(
            "qbo_realms",
            select="realm_id,company_name,environment,default_bank_account_name,default_money_code_cc_account_name,connected_by_email,connected_at,updated_at",
            order="company_name.asc",
        )
        return [self._row_to_realm(row) for row in rows]

    def get_realm(self, realm_id: str) -> ConnectedRealm | None:
        rows = self._client.select(
            "qbo_realms",
            select="realm_id,company_name,environment,default_bank_account_name,default_money_code_cc_account_name,connected_by_email,connected_at,updated_at",
            filters={"realm_id": f"eq.{realm_id}"},
            limit=1,
        )
        return self._row_to_realm(rows[0]) if rows else None

    def save_realm_settings(
        self,
        *,
        realm_id: str,
        company_name: str,
        environment: str,
        default_bank_account_name: str = "",
        default_money_code_cc_account_name: str = "Fuel Card - EFS",
        connected_by_email: str = "",
    ) -> None:
        self._client.upsert(
            "qbo_realms",
            {
                "realm_id": realm_id,
                "company_name": company_name or realm_id,
                "environment": environment or "production",
                "default_bank_account_name": default_bank_account_name or "",
                "default_money_code_cc_account_name": default_money_code_cc_account_name or "Fuel Card - EFS",
                "connected_by_email": connected_by_email or "",
            },
            on_conflict="realm_id",
        )

    def save_token_bundle(self, bundle: dict[str, Any]) -> None:
        self._client.rpc(
            "qbo_upsert_token_bundle",
            {
                "p_realm_id": bundle["realm_id"],
                "p_company_name": bundle.get("company_name") or bundle["realm_id"],
                "p_environment": bundle.get("environment") or "production",
                "p_access_token": bundle.get("access_token") or "",
                "p_refresh_token": bundle.get("refresh_token") or "",
                "p_access_expires_at": bundle.get("access_expires_at"),
                "p_refresh_expires_at": bundle.get("refresh_expires_at"),
                "p_connected_by_email": bundle.get("connected_by_email") or "",
            },
        )

    def get_token_bundle(self, realm_id: str) -> dict[str, Any] | None:
        payload = self._client.rpc("qbo_get_token_bundle", {"p_realm_id": realm_id})
        rows = payload if isinstance(payload, list) else []
        return rows[0] if rows else None

    def disconnect(self, realm_id: str) -> None:
        self._client.rpc("qbo_disconnect_realm", {"p_realm_id": realm_id})

    @staticmethod
    def _row_to_realm(row: dict[str, Any]) -> ConnectedRealm:
        return ConnectedRealm(
            realm_id=str(row.get("realm_id") or ""),
            company_name=str(row.get("company_name") or row.get("realm_id") or ""),
            environment=str(row.get("environment") or "production"),
            default_bank_account_name=str(row.get("default_bank_account_name") or ""),
            default_money_code_cc_account_name=str(row.get("default_money_code_cc_account_name") or "Fuel Card - EFS"),
            connected_by_email=str(row.get("connected_by_email") or ""),
            connected_at=str(row.get("connected_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )


class QboAuthService:
    def __init__(self, token_repo: QboTokenRepository) -> None:
        self._token_repo = token_repo

    @property
    def token_repo(self) -> QboTokenRepository:
        return self._token_repo

    def environment(self) -> str:
        value = get_qbo_secret("environment", "QBO_ENVIRONMENT", "production").lower()
        return "sandbox" if value == "sandbox" else "production"

    def redirect_uri(self) -> str:
        return get_qbo_secret("redirect_uri", "QBO_REDIRECT_URI", "").strip()

    def client_id(self) -> str:
        return get_qbo_secret("client_id", "QBO_CLIENT_ID", "").strip()

    def client_secret(self) -> str:
        return get_qbo_secret("client_secret", "QBO_CLIENT_SECRET", "").strip()

    def has_credentials(self) -> bool:
        return bool(self.client_id() and self.client_secret() and self.redirect_uri())

    def build_authorization_url(self, state: str | None = None) -> tuple[str, str]:
        if not self.has_credentials():
            raise QboAuthError("QBO client_id, client_secret, and redirect_uri are required.")
        state_value = state or secrets.token_urlsafe(32)
        query = urlencode(
            {
                "client_id": self.client_id(),
                "response_type": "code",
                "scope": "com.intuit.quickbooks.accounting",
                "redirect_uri": self.redirect_uri(),
                "state": state_value,
            }
        )
        return f"{_AUTH_URL}?{query}", state_value

    def exchange_code(self, *, code: str, realm_id: str, connected_by_email: str) -> dict[str, Any]:
        if not code or not realm_id:
            raise QboAuthError("QBO callback is missing code or realmId.")
        token_payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(),
            }
        )
        company_name = self._fetch_company_name(
            realm_id=realm_id,
            access_token=str(token_payload.get("access_token") or ""),
            environment=self.environment(),
        )
        bundle = self._bundle_from_token_payload(
            token_payload,
            realm_id=realm_id,
            environment=self.environment(),
            company_name=company_name or realm_id,
            connected_by_email=connected_by_email,
        )
        self._token_repo.save_token_bundle(bundle)
        return bundle

    def get_valid_access_token(self, realm_id: str) -> str:
        bundle = self._token_repo.get_token_bundle(realm_id)
        if not bundle:
            raise QboAuthError(f"No saved QuickBooks connection for realm {realm_id}.")
        access_token = str(bundle.get("access_token") or "")
        access_expires_at = _parse_dt(bundle.get("access_expires_at"))
        if access_token and access_expires_at and access_expires_at - timedelta(seconds=_ACCESS_REFRESH_LEEWAY_SECONDS) > _now():
            return access_token
        return self.refresh_tokens(realm_id)["access_token"]

    def refresh_tokens(self, realm_id: str) -> dict[str, Any]:
        bundle = self._token_repo.get_token_bundle(realm_id)
        if not bundle:
            raise QboAuthError(f"No saved QuickBooks connection for realm {realm_id}.")
        refresh_token = str(bundle.get("refresh_token") or "")
        if not refresh_token:
            raise QboAuthError(f"No refresh token stored for realm {realm_id}. Reconnect this company.")
        refresh_expires_at = _parse_dt(bundle.get("refresh_expires_at"))
        if refresh_expires_at and refresh_expires_at <= _now():
            raise QboAuthError(f"Refresh token expired for realm {realm_id}. Reconnect this company.")

        token_payload = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        new_bundle = self._bundle_from_token_payload(
            token_payload,
            realm_id=realm_id,
            environment=str(bundle.get("environment") or self.environment()),
            company_name=str(bundle.get("company_name") or realm_id),
            connected_by_email=str(bundle.get("connected_by_email") or ""),
        )
        self._token_repo.save_token_bundle(new_bundle)
        return new_bundle

    def disconnect(self, realm_id: str) -> None:
        self._token_repo.disconnect(realm_id)

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        raw = f"{self.client_id()}:{self.client_secret()}".encode("utf-8")
        auth_header = base64.b64encode(raw).decode("ascii")
        response = requests.post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=data,
            timeout=45,
        )
        if not response.ok:
            raise QboAuthError(f"QuickBooks token request failed: HTTP {response.status_code} {response.text[:500]}")
        payload = response.json()
        if "access_token" not in payload or "refresh_token" not in payload:
            raise QboAuthError("QuickBooks token response did not include access_token and refresh_token.")
        return payload

    @staticmethod
    def _fetch_company_name(*, realm_id: str, access_token: str, environment: str) -> str:
        if not access_token:
            return ""
        base = _QBO_BASE_PROD if environment == "production" else _QBO_BASE_SANDBOX
        response = requests.get(
            f"{base}/v3/company/{realm_id}/companyinfo/{realm_id}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            params={"minorversion": get_qbo_secret("minor_version", "QBO_MINOR_VERSION", "70")},
            timeout=45,
        )
        if not response.ok:
            logger.warning("Could not fetch QBO company name for realm %s: %s", realm_id, response.text[:300])
            return ""
        company = (response.json().get("CompanyInfo") or {})
        return str(company.get("CompanyName") or company.get("LegalName") or "")

    @staticmethod
    def _bundle_from_token_payload(
        payload: dict[str, Any],
        *,
        realm_id: str,
        environment: str,
        company_name: str,
        connected_by_email: str,
    ) -> dict[str, Any]:
        now = _now()
        expires_in = int(payload.get("expires_in") or 0)
        refresh_expires_in = int(payload.get("x_refresh_token_expires_in") or 0)
        return {
            "realm_id": realm_id,
            "environment": environment,
            "company_name": company_name or realm_id,
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token") or ""),
            "access_expires_at": (now + timedelta(seconds=expires_in)).isoformat() if expires_in else None,
            "refresh_expires_at": (now + timedelta(seconds=refresh_expires_in)).isoformat() if refresh_expires_in else None,
            "connected_by_email": connected_by_email,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
