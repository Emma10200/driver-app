from __future__ import annotations

import logging
import importlib.util
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

import streamlit as st
try:
    from streamlit.errors import StreamlitAuthError as _StreamlitAuthError
except ImportError:  # pragma: no cover - older Streamlit fallback
    _streamlit_auth_exceptions: tuple[type[BaseException], ...] = (RuntimeError,)
else:
    _streamlit_auth_exceptions = (_StreamlitAuthError, RuntimeError)
try:
    from streamlit.errors import StreamlitSecretNotFoundError as _StreamlitSecretNotFoundError
except ImportError:  # pragma: no cover - older Streamlit fallback
    _streamlit_secret_exceptions: tuple[type[BaseException], ...] = (
        FileNotFoundError,
        AttributeError,
        KeyError,
    )
else:
    _streamlit_secret_exceptions = (
        _StreamlitSecretNotFoundError,
        FileNotFoundError,
        AttributeError,
        KeyError,
    )

from qbo.api_client import QboClient
from qbo.company_directory import CompanyDirectory
from qbo.duplicate_check import DuplicateChecker
from qbo.file_loader import FileLoader
from qbo.import_service import ImportService
from qbo.lookups import EntityLookupService
from qbo.models import ConnectedRealm, PreviewResult
from services.qbo_driver_statement_preview import (
    QBO_DRIVER_EDIT_NOTICE_KEY,
    QBO_DRIVER_PENDING_KEY,
    QBO_DRIVER_RESET_KEY,
    QBO_DRIVER_SELECTION_KEY,
    QBO_DRIVER_UNCHECK_KEY,
    _apply_driver_statement_preview_edits,
    _driver_pending_for,
    _driver_statement_preview_rows_from_drafts,
    _render_editable_driver_statement_preview,
)
from qbo.parking_pk import ParkingApplyResult, ParkingMatch, ParkingPkService, ParkingScanResult
from qbo.parsers import DriverStatementParser, InvoiceParser, MoneyCodeParser
from qbo.catalog import EXPENSE_ACCOUNT_CLASSIFICATIONS, QboCatalog
from services.qbo_audit import SupabaseAuditLog, source_file_hash
from services.qbo_auth import QboAuthService, QboTokenRepository, qbo_allowed_emails
from services.qbo_sheets_log import GoogleSheetsImportLog
from services.qbo_supabase import SupabaseQboError, SupabaseRestClient

logger = logging.getLogger(__name__)

GOOGLE_AUTH_PROVIDER = "google"
QBO_OAUTH_STATE_KEY = "qbo_oauth_state"
QBO_PREVIEW_KEY = "qbo_import_preview"
QBO_UPLOAD_HASH_KEY = "qbo_upload_hash"
QBO_VIEW_KEY = "qbo_view"
QBO_TEMPLATE_KEY = "qbo_selected_template"
QBO_PARKING_SCAN_KEY = "qbo_parking_scan"
QBO_PARKING_SELECTION_KEY = "qbo_parking_selected_ids"
QBO_CATALOG_CACHE_KEY = "qbo_catalog_cache"
QBO_MISSING_CUSTOMERS_KEY = "qbo_missing_customers"
QBO_MISSING_VENDORS_KEY = "qbo_missing_vendors"
QBO_RETRY_FILTER_KEY = "qbo_retry_filter"
QBO_INVOICE_AUDIT_KEY = "qbo_invoice_audit"
QBO_INVOICE_TARGET_COMPANY_KEY = "qbo_invoice_target_company"
_TEMPLATE_OPTIONS = {
    "invoices": "Invoices",
    "driver_statements": "Driver Statements / Checks",
    "money_codes": "Money Codes / EFS Fuel Card",
}
_TEMPLATE_CARDS = [
    (
        "invoices",
        "🧾 Invoices",
        "Customer invoices → QBO Accounts Receivable.",
        "Use for the weekly customer invoice file from dispatch.",
    ),
    (
        "driver_statements",
        "🚚 Driver Statements",
        "Driver pay statements → QBO Checks (Vendor Payments).",
        "Use for the ProTransport driver settlement export.",
    ),
    (
        "money_codes",
        "⛽ Money Codes",
        "EFS fuel-card codes → CreditCard Purchases.",
        "Use for the EFS Money Code Use Report (Fuel Card - EFS only).",
    ),
]


def _mapping_get(mapping: Any, key: str) -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(key)
    try:
        return mapping[key]
    except Exception:
        return getattr(mapping, key, None)


def _google_user_is_logged_in() -> bool:
    user = getattr(st, "user", None)
    return bool(user and getattr(user, "is_logged_in", False))


def _google_user_email() -> str:
    user = getattr(st, "user", None)
    if not user:
        return ""
    return str(_mapping_get(user, "email") or getattr(user, "email", "") or "").strip().lower()


def _qbo_access_granted() -> bool:
    email = _google_user_email()
    return bool(_google_user_is_logged_in() and email and email in qbo_allowed_emails())


def _streamlit_auth_login_provider() -> tuple[bool, str | None, str]:
    """Return whether Streamlit OIDC auth is configured and which provider to use.

    Streamlit supports either a flat ``[auth]`` config (called with
    ``st.login()``) or named provider sections like ``[auth.google]`` (called
    with ``st.login("google")``). The QBO page accepts both so it does not
    crash if the existing driver app uses the flat Google tutorial format.
    """
    if not hasattr(st, "login") or not hasattr(st, "user"):
        return False, None, "This Streamlit version does not support native login."
    try:
        auth_config = _mapping_get(st.secrets, "auth")
    except _streamlit_secret_exceptions:
        return False, None, "The [auth] Streamlit Secrets block is missing."
    if not auth_config:
        return False, None, "The [auth] Streamlit Secrets block is empty."

    redirect_uri = str(_mapping_get(auth_config, "redirect_uri") or "").strip()
    cookie_secret = str(_mapping_get(auth_config, "cookie_secret") or "").strip()
    provider_config = _mapping_get(auth_config, GOOGLE_AUTH_PROVIDER)
    provider_name: str | None = GOOGLE_AUTH_PROVIDER if provider_config else None
    provider_config = provider_config or auth_config

    client_id = str(_mapping_get(provider_config, "client_id") or "").strip()
    client_secret = str(_mapping_get(provider_config, "client_secret") or "").strip()
    metadata_url = str(_mapping_get(provider_config, "server_metadata_url") or "").strip()
    missing = [
        label
        for label, value in (
            ("redirect_uri", redirect_uri),
            ("cookie_secret", cookie_secret),
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("server_metadata_url", metadata_url),
        )
        if not value
    ]
    if missing:
        return False, provider_name, "Missing Streamlit auth setting(s): " + ", ".join(missing)
    return True, provider_name, ""


def _streamlit_auth_diagnostics() -> dict[str, Any]:
    """Return non-secret auth diagnostics for troubleshooting deployed config."""
    diagnostics: dict[str, Any] = {
        "Authlib installed": importlib.util.find_spec("authlib") is not None,
        "st.login available": hasattr(st, "login"),
        "st.user available": hasattr(st, "user"),
    }
    try:
        auth_config = _mapping_get(st.secrets, "auth")
    except _streamlit_secret_exceptions:
        diagnostics["[auth] present"] = False
        return diagnostics

    diagnostics["[auth] present"] = bool(auth_config)
    if not auth_config:
        return diagnostics

    redirect_uri = str(_mapping_get(auth_config, "redirect_uri") or "").strip()
    cookie_secret = str(_mapping_get(auth_config, "cookie_secret") or "")
    provider_config = _mapping_get(auth_config, GOOGLE_AUTH_PROVIDER)
    diagnostics["[auth.google] present"] = isinstance(provider_config, Mapping)
    diagnostics["redirect_uri"] = redirect_uri or "missing"
    diagnostics["cookie_secret length"] = len(cookie_secret)

    if isinstance(provider_config, Mapping):
        client_id = str(_mapping_get(provider_config, "client_id") or "").strip()
        client_secret = str(_mapping_get(provider_config, "client_secret") or "")
        metadata_url = str(_mapping_get(provider_config, "server_metadata_url") or "").strip()
        diagnostics["client_id present"] = bool(client_id)
        diagnostics["client_id ends correctly"] = client_id.endswith(".apps.googleusercontent.com")
        diagnostics["client_secret length"] = len(client_secret)
        diagnostics["server_metadata_url"] = metadata_url or "missing"
    return diagnostics


def _render_streamlit_auth_help(reason: str) -> None:
    st.error("Google SSO is not configured correctly for this Streamlit app yet.")
    if reason:
        st.caption(reason)
    st.info(
        "In Streamlit Cloud, open Manage app → Settings → Secrets and make sure the "
        "[auth] / [auth.google] block is present. The Google OAuth redirect URI is "
        "different from the QuickBooks redirect URI."
    )
    st.code(
        '''[auth]
redirect_uri = "https://driver-application.streamlit.app/oauth2callback"
cookie_secret = "generate-a-long-random-string"

[auth.google]
client_id = "your-google-oauth-client-id.apps.googleusercontent.com"
client_secret = "your-google-oauth-client-secret"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"'''.strip(),
        language="toml",
    )
    st.caption(
        "Also add https://driver-application.streamlit.app/oauth2callback to the "
        "Google Cloud OAuth client's Authorized redirect URIs."
    )
    with st.expander("Safe auth diagnostics", expanded=False):
        st.json(_streamlit_auth_diagnostics())


def _default_driver_check_date(today: date | None = None) -> date:
    """Default driver-statement check date to the most recent Friday."""
    today = today or date.today()
    days_since_friday = (today.weekday() - 4) % 7
    return today - timedelta(days=days_since_friday)


def _check_date_override(value: date | datetime | str | None) -> str:
    """Convert the calendar widget value to the QBO parser's YYYY-MM-DD override."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()


def _format_amount(value: Any) -> str:
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _editable_amount(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    cleaned = "".join(ch for ch in str(value) if ch.isdigit() or ch in {".", "-"})
    try:
        return float(cleaned)
    except ValueError:
        return default


def _editable_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _editor_records(value: Any) -> list[dict[str, Any]]:
    try:
        records = value.to_dict("records")  # type: ignore[attr-defined]
    except AttributeError:
        records = list(value or [])
    return [dict(row) for row in records if isinstance(row, Mapping)]


def _draft_amount(draft: dict[str, Any]) -> float:
    total = 0.0
    for line in draft.get("Line") or []:
        try:
            total += float((line or {}).get("Amount") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _invoice_group_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("realm_id") or ""),
        str(row.get("doc_number") or ""),
        str(row.get("customer_name") or ""),
        str(row.get("txn_date") or ""),
        str(row.get("due_date") or ""),
    )


def _invoice_group_totals(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str, str, str], float]:
    totals: dict[tuple[str, str, str, str, str], float] = {}
    for row in rows:
        key = _invoice_group_key(row)
        try:
            amount = float(row.get("amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        totals[key] = totals.get(key, 0.0) + amount
    return totals


def _inspect_invoice_upload(file_name: str, content: bytes) -> dict[str, Any]:
    rows = FileLoader().load_rows_from_bytes(file_name, content)
    return InvoiceParser.inspect_source(rows)


def _realm_options(realms: list[ConnectedRealm]) -> dict[str, ConnectedRealm]:
    return {f"{realm.company_name} ({realm.realm_id})": realm for realm in realms}


def _realm_name_for_id(realms: list[ConnectedRealm], realm_id: str) -> str:
    for realm in realms:
        if realm.realm_id == realm_id:
            return realm.company_name
    return ""


def _render_qbo_upload_styles() -> None:
    """Make the QBO file uploader read as an intentional upload target."""
    st.markdown(
        """
        <style>
        div[data-testid="stFileUploader"] {
            background: linear-gradient(135deg, #eef6ff 0%, #f8fbff 55%, #ffffff 100%);
            border: 1px solid rgba(37, 99, 235, 0.24);
            border-radius: 16px;
            padding: 1rem 1.15rem 1.15rem;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
        }
        div[data-testid="stFileUploader"] label p {
            color: #0f2f57;
            font-size: 1rem;
            font-weight: 750;
        }
        div[data-testid="stFileUploaderDropzone"] {
            background: rgba(255, 255, 255, 0.82);
            border: 2px dashed rgba(37, 99, 235, 0.5);
            border-radius: 14px;
            min-height: 4.5rem;
            transition: border-color 120ms ease, background 120ms ease, box-shadow 120ms ease;
        }
        div[data-testid="stFileUploaderDropzone"]:hover {
            background: #ffffff;
            border-color: #2563eb;
            box-shadow: inset 0 0 0 1px rgba(37, 99, 235, 0.12);
        }
        div[data-testid="stFileUploaderDropzone"] button,
        div[data-testid="stFileUploader"] button[kind="secondary"],
        div[data-testid="stFileUploader"] button[data-testid="baseButton-secondary"] {
            background: linear-gradient(135deg, #2563eb 0%, #0ea5e9 100%);
            border: 1px solid rgba(37, 99, 235, 0.78);
            border-radius: 10px;
            color: #ffffff;
            font-weight: 700;
            box-shadow: 0 6px 16px rgba(37, 99, 235, 0.26);
        }
        div[data-testid="stFileUploaderDropzone"] button:hover,
        div[data-testid="stFileUploader"] button[kind="secondary"]:hover,
        div[data-testid="stFileUploader"] button[data-testid="baseButton-secondary"]:hover {
            background: linear-gradient(135deg, #1d4ed8 0%, #0284c7 100%);
            border-color: #1d4ed8;
            color: #ffffff;
        }
        div[data-testid="stFileUploader"] button[title*="Remove"],
        div[data-testid="stFileUploader"] button[title*="Delete"],
        div[data-testid="stFileUploader"] button[aria-label*="Remove"],
        div[data-testid="stFileUploader"] button[aria-label*="Delete"] {
            background: transparent;
            border: 0;
            box-shadow: none;
            color: #64748b;
        }
        div[data-testid="stFileUploaderDropzone"] small,
        div[data-testid="stFileUploaderDropzone"] [data-testid="stMarkdownContainer"] p {
            color: #475569;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_login() -> None:
    st.title("🔒 QBO Importer")
    st.caption("Accounting-only access. Sign in with an approved Google account.")
    allowed = qbo_allowed_emails()
    if not allowed:
        st.error("QBO_ALLOWED_EMAILS / [qbo].allowed_emails is not configured yet.")
        st.info("Add accounts@prestige.inc to the qbo allowed_emails secret before using this page.")
        return
    if _google_user_is_logged_in():
        email = _google_user_email() or "unknown account"
        if email in allowed:
            st.success(f"Signed in as {email}.")
            st.rerun()
        else:
            st.error(f"{email} is signed in but is not allowed for the QBO importer.")
            if st.button("Sign out of Google", use_container_width=True):
                st.logout()
        return
    if not hasattr(st, "login"):
        st.error("This Streamlit version does not support st.login().")
        return
    auth_ready, provider_name, auth_reason = _streamlit_auth_login_provider()
    if not auth_ready:
        _render_streamlit_auth_help(auth_reason)
        return
    if st.button("Continue with Google", use_container_width=True):
        try:
            if provider_name:
                st.login(provider_name)
            else:
                st.login()
        except _streamlit_auth_exceptions as exc:
            logger.exception("Streamlit Google login failed to start")
            detail = str(exc).strip()
            _render_streamlit_auth_help(
                "Streamlit rejected the current auth configuration. Check the app logs for the "
                f"full provider error. Summary: {type(exc).__name__}"
                + (f" — {detail}" if detail else "")
            )


def render_qbo_dashboard() -> None:
    if not _qbo_access_granted():
        _render_login()
        st.stop()

    try:
        supabase = SupabaseRestClient()
        token_repo = QboTokenRepository(supabase)
        auth_service = QboAuthService(token_repo)
    except SupabaseQboError as exc:
        st.error(str(exc))
        st.stop()

    if _is_qbo_callback():
        _handle_oauth_callback(auth_service)
        st.stop()

    email = _google_user_email()
    _render_sidebar(auth_service, email)

    view = st.session_state.get(QBO_VIEW_KEY) or "templates"

    if view == "history":
        _render_history(supabase)
        return

    if view == "import":
        template_key = st.session_state.get(QBO_TEMPLATE_KEY)
        if template_key not in _TEMPLATE_OPTIONS:
            st.session_state[QBO_VIEW_KEY] = "templates"
            st.rerun()
            return
        _render_importer(supabase, token_repo, auth_service, email, template_key)
        return

    if view == "parking_pk":
        _render_parking_pk(supabase, token_repo, auth_service, email)
        return

    _render_template_picker(token_repo)


def _go_to(view: str, *, template_key: str | None = None) -> None:
    st.session_state[QBO_VIEW_KEY] = view
    if template_key is not None:
        st.session_state[QBO_TEMPLATE_KEY] = template_key
    if view != "import":
        st.session_state.pop(QBO_PREVIEW_KEY, None)
        st.session_state.pop(QBO_UPLOAD_HASH_KEY, None)
        st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
        st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
        st.session_state.pop(QBO_RETRY_FILTER_KEY, None)
        st.session_state.pop(QBO_DRIVER_EDIT_NOTICE_KEY, None)
    st.rerun()


def _render_sidebar(auth_service: QboAuthService, email: str) -> None:
    with st.sidebar:
        st.markdown("### 📘 QBO Importer")
        st.caption(email or "signed in")
        st.divider()
        if st.button("🏠 Import templates", use_container_width=True, key="qbo_nav_templates"):
            _go_to("templates")
        if st.button("📜 Import history", use_container_width=True, key="qbo_nav_history"):
            _go_to("history")
        st.divider()
        st.markdown("#### 🔧 Maintenance")
        if st.button("🅿️ Parking PK (Prestig)", use_container_width=True, key="qbo_nav_parking_pk"):
            _go_to("parking_pk")
        st.divider()
        with st.expander("⚙️ Settings — Companies", expanded=False):
            _render_connections(auth_service)
        st.divider()
        if st.button("Sign out", use_container_width=True, key="qbo_nav_signout"):
            st.logout()


def _render_template_picker(token_repo: QboTokenRepository) -> None:
    st.title("What do you want to import?")
    realms = token_repo.list_realms()
    if not realms:
        st.warning(
            "No QuickBooks companies are connected yet. Open **⚙️ Settings — Companies** in the "
            "sidebar and connect each company once."
        )
        return
    st.caption(
        f"{len(realms)} connected QuickBooks compan{'y' if len(realms) == 1 else 'ies'}. "
        "Choose a template to begin."
    )
    cols = st.columns(len(_TEMPLATE_CARDS))
    for col, (key, title, summary, hint) in zip(cols, _TEMPLATE_CARDS):
        with col:
            with st.container(border=True):
                st.markdown(f"#### {title}")
                st.write(summary)
                st.caption(hint)
                if st.button(
                    "Start import",
                    key=f"qbo_start_{key}",
                    use_container_width=True,
                    type="primary",
                ):
                    _go_to("import", template_key=key)


def _is_qbo_callback() -> bool:
    try:
        return bool(st.query_params.get("qbo_oauth_callback")) or bool(st.query_params.get("code"))
    except Exception:
        return False


def _handle_oauth_callback(auth_service: QboAuthService) -> None:
    params = st.query_params
    error = str(params.get("error") or "").strip()
    if error:
        st.error(f"QuickBooks connection failed: {params.get('error_description') or error}")
        return

    code = str(params.get("code") or "").strip()
    realm_id = str(params.get("realmId") or "").strip()
    returned_state = str(params.get("state") or "").strip()
    expected_state = str(st.session_state.get(QBO_OAUTH_STATE_KEY) or "").strip()
    if expected_state and returned_state and returned_state != expected_state:
        st.error("QuickBooks OAuth state mismatch. Please try connecting again.")
        return
    if not code or not realm_id:
        st.error("QuickBooks callback is missing code or realmId.")
        return

    with st.spinner("Finishing QuickBooks connection…"):
        try:
            bundle = auth_service.exchange_code(code=code, realm_id=realm_id, connected_by_email=_google_user_email())
        except Exception as exc:  # noqa: BLE001 - display provider/API errors safely
            logger.exception("QBO OAuth callback failed")
            st.error(f"QuickBooks connection failed: {exc}")
            return
    st.session_state.pop(QBO_OAUTH_STATE_KEY, None)
    st.success(f"Connected {bundle.get('company_name') or realm_id}.")
    st.query_params.clear()
    st.query_params["qbo"] = "1"
    st.rerun()


def _render_connections(auth_service: QboAuthService) -> None:
    st.subheader("Connected QuickBooks companies")
    if not auth_service.has_credentials():
        st.error("QBO secrets are missing. Add the [qbo] block to Streamlit Secrets before connecting.")
        return

    realms = auth_service.token_repo.list_realms()
    if realms:
        st.dataframe(
            [
                {
                    "Company": realm.company_name,
                    "Realm ID": realm.realm_id,
                    "Environment": realm.environment,
                    "Default Bank": realm.default_bank_account_name,
                    "Connected By": realm.connected_by_email,
                    "Updated": realm.updated_at,
                }
                for realm in realms
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No QuickBooks companies connected yet. Connect each company once below.")

    st.divider()
    st.markdown("**Connect or reconnect a company**")
    st.caption("Option X is enabled: anyone in QBO_ALLOWED_EMAILS can connect/reconnect any company.")
    auth_url, state = auth_service.build_authorization_url()
    st.session_state[QBO_OAUTH_STATE_KEY] = state
    st.link_button("Open QuickBooks consent screen", auth_url, use_container_width=True)
    st.caption("After Intuit redirects back, the connection will be saved centrally in Supabase Vault.")

    if realms:
        with st.expander("Disconnect a company", expanded=False):
            options = _realm_options(realms)
            selected = st.selectbox("Company", list(options.keys()), key="qbo_disconnect_company")
            if st.button("Disconnect selected company", type="secondary"):
                auth_service.disconnect(options[selected].realm_id)
                st.success("Disconnected. Refreshing…")
                st.rerun()


def _render_importer(
    supabase: SupabaseRestClient,
    token_repo: QboTokenRepository,
    auth_service: QboAuthService,
    email: str,
    template_key: str,
) -> None:
    template_label = _TEMPLATE_OPTIONS.get(template_key, template_key)

    back_col, title_col = st.columns([0.18, 0.82])
    with back_col:
        if st.button("\u2190 Templates", use_container_width=True, key="qbo_back_to_templates"):
            _go_to("templates")
            return
    with title_col:
        st.title(f"Import \u2014 {template_label}")

    realms = token_repo.list_realms()
    if not realms:
        st.warning(
            "Connect at least one QuickBooks company before importing. "
            "Open \u2699\ufe0f Settings \u2014 Companies in the sidebar."
        )
        return

    _render_qbo_upload_styles()

    options = _realm_options(realms)
    bank_account_name = ""
    override_date = ""
    selected_realm: ConnectedRealm | None = None
    invoice_requires_company = False
    if template_key == "invoices":
        uploaded = st.file_uploader("Upload invoice file", type=["csv", "xlsx", "xlsm", "xls"])
    else:
        with st.container(border=True):
            st.markdown("**Setup**")
            setup_cols = st.columns(2)
            with setup_cols[0]:
                selected_realm_label = st.selectbox(
                    "Company",
                    list(options.keys()),
                    key="qbo_target_company",
                    help="QuickBooks company to receive this import.",
                )
            selected_realm = options[selected_realm_label]

            with setup_cols[1]:
                if template_key == "driver_statements":
                    bank_account_name = st.text_input(
                        "Bank account",
                        value=selected_realm.default_bank_account_name,
                        help="QBO bank account for the checks. Saved as this company's default after posting.",
                    )
                elif template_key == "money_codes":
                    st.caption("Posts as CreditCard purchases. Only Fuel Card - EFS rows are imported.")

            if template_key == "driver_statements":
                selected_date = st.date_input(
                    "Check date",
                    value=_default_driver_check_date(),
                    key="qbo_driver_statement_check_date",
                    help="Calendar date to use for every check in this driver statement import.",
                )
                override_date = _check_date_override(selected_date)

            uploaded = st.file_uploader("Source file", type=["csv", "xlsx", "xlsm", "xls"])
    retry_filter = _active_retry_filter(template_key)
    if retry_filter:
        doc_numbers = list(retry_filter.get("doc_numbers") or [])
        st.info(
            "Retry mode: upload the original source file "
            f"`{retry_filter.get('source_file_name') or 'for the failed import'}`. "
            f"Preview will keep only {len(doc_numbers)} failed doc(s): "
            + ", ".join(doc_numbers[:8])
            + ("…" if len(doc_numbers) > 8 else "")
        )
        if st.button("Cancel retry mode", key="qbo_cancel_retry_mode"):
            st.session_state.pop(QBO_RETRY_FILTER_KEY, None)
            st.rerun()
    if not uploaded:
        st.caption("Choose a file to preview before posting anything to QBO.")
        return

    content = uploaded.getvalue()
    if template_key == "invoices":
        try:
            invoice_source_info = _inspect_invoice_upload(uploaded.name, content)
        except Exception as exc:  # noqa: BLE001 - user-facing upload validation
            logger.exception("Invoice source inspection failed")
            st.error(f"Could not read this invoice file: {exc}")
            return
        invoice_requires_company = bool(invoice_source_info.get("requires_target_realm"))
        if invoice_requires_company:
            with st.container(border=True):
                st.markdown("**Company required for this invoice file**")
                st.caption(
                    "Detected the QBO invoice import layout (`RefNumber`, `LineItem`, `LineAmount`) with no Division column. "
                    "Choose the QuickBooks company/division that should receive every invoice in this file."
                )
                selected_realm_label = st.selectbox(
                    "Company / division",
                    list(options.keys()),
                    index=None,
                    placeholder="Select target company…",
                    key=QBO_INVOICE_TARGET_COMPANY_KEY,
                    help="Required because this source format does not contain a per-row Division column.",
                )
                selected_realm = options.get(selected_realm_label) if selected_realm_label else None
    upload_hash = source_file_hash(content)
    preview_scope_hash = upload_hash
    if template_key == "invoices" and invoice_requires_company:
        preview_scope_hash = f"{upload_hash}:{selected_realm.realm_id if selected_realm else ''}"
    if st.session_state.get(QBO_UPLOAD_HASH_KEY) != preview_scope_hash:
        st.session_state.pop(QBO_PREVIEW_KEY, None)
        st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
        st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
        st.session_state.pop(QBO_INVOICE_AUDIT_KEY, None)
        st.session_state.pop(QBO_DRIVER_EDIT_NOTICE_KEY, None)
        st.session_state.pop(QBO_DRIVER_PENDING_KEY, None)
        st.session_state.pop(QBO_DRIVER_RESET_KEY, None)
        st.session_state.pop(QBO_DRIVER_UNCHECK_KEY, None)
        st.session_state.pop(QBO_DRIVER_SELECTION_KEY, None)
        st.session_state[QBO_UPLOAD_HASH_KEY] = preview_scope_hash

    preview = st.session_state.get(QBO_PREVIEW_KEY)
    preview_ready = isinstance(preview, PreviewResult)
    has_pending_customer_prompt = isinstance(st.session_state.get(QBO_MISSING_CUSTOMERS_KEY), dict)
    has_pending_vendor_prompt = isinstance(st.session_state.get(QBO_MISSING_VENDORS_KEY), dict)
    has_pending_driver_edits = bool(_driver_pending_for(preview))
    post_disabled = not preview_ready or bool(preview.errors) or not bool(preview.drafts) or has_pending_customer_prompt or has_pending_vendor_prompt or has_pending_driver_edits
    if template_key != "invoices" and selected_realm is None:
        post_disabled = True
    if template_key == "invoices" and invoice_requires_company and selected_realm is None:
        post_disabled = True

    if template_key == "invoices":
        preview_col, clear_col, post_col, audit_col, hint_col = st.columns(
            [0.16, 0.12, 0.16, 0.16, 0.40]
        )
    else:
        preview_col, clear_col, post_col, hint_col = st.columns([0.16, 0.12, 0.16, 0.56])
        audit_col = None
    with preview_col:
        preview_disabled = template_key == "invoices" and invoice_requires_company and selected_realm is None
        if st.button("Preview", type="primary", use_container_width=True, disabled=preview_disabled):
            try:
                preview = _build_preview(
                    template_key=template_key,
                    file_name=uploaded.name,
                    content=content,
                    realms=realms,
                    selected_realm=selected_realm,
                    bank_account_name=bank_account_name,
                    override_date=override_date,
                )
            except Exception as exc:  # noqa: BLE001 - user-facing preview validation
                logger.exception("QBO preview failed")
                st.error(f"Preview failed: {exc}")
            else:
                preview = _apply_retry_filter(preview, retry_filter)
                st.session_state[QBO_PREVIEW_KEY] = preview
                st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
                st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
                st.rerun()
    with clear_col:
        if st.button("Clear", use_container_width=True):
            st.session_state.pop(QBO_PREVIEW_KEY, None)
            st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
            st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
            st.session_state.pop(QBO_INVOICE_AUDIT_KEY, None)
            st.session_state.pop(QBO_RETRY_FILTER_KEY, None)
            st.session_state.pop(QBO_DRIVER_EDIT_NOTICE_KEY, None)
            st.session_state.pop(QBO_DRIVER_PENDING_KEY, None)
            st.session_state.pop(QBO_DRIVER_RESET_KEY, None)
            st.session_state.pop(QBO_DRIVER_UNCHECK_KEY, None)
            st.session_state.pop(QBO_DRIVER_SELECTION_KEY, None)
            st.rerun()
    with post_col:
        post_clicked = st.button(
            "Post to QBO",
            type="primary",
            key="qbo_post_to_qbo",
            disabled=post_disabled,
            use_container_width=True,
        )
    audit_clicked = False
    if audit_col is not None:
        with audit_col:
            audit_clicked = st.button(
                "Audit",
                key="qbo_audit_invoices",
                disabled=not preview_ready or not bool(preview.drafts),
                use_container_width=True,
                help=(
                    "Compare this file against QuickBooks: confirms every row is "
                    "imported and lands in the company its Division resolves to."
                ),
            )
    with hint_col:
        if template_key == "invoices" and invoice_requires_company and selected_realm is None:
            st.caption("Select a target company/division to preview this no-division invoice file.")
        elif template_key == "invoices":
            st.caption("Ready to post: customers are checked first; missing customers can be created before posting.")
        elif has_pending_driver_edits:
            st.caption("Lock in pending preview edits below before posting.")
        else:
            st.caption("Ready to post after preview review.")

    preview = st.session_state.get(QBO_PREVIEW_KEY)
    if not isinstance(preview, PreviewResult):
        return

    if post_clicked:
        _handle_post_to_qbo(
            preview=preview,
            target_realm=selected_realm,
            realms=realms,
            supabase=supabase,
            token_repo=token_repo,
            auth_service=auth_service,
            email=email,
            template_key=template_key,
            template_label=template_label,
            uploaded_name=uploaded.name,
            upload_hash=upload_hash,
            bank_account_name=bank_account_name,
        )
        return

    if template_key == "invoices" and _render_missing_customers_prompt(
        preview=preview,
        realms=realms,
        supabase=supabase,
        token_repo=token_repo,
        auth_service=auth_service,
        email=email,
        template_key=template_key,
        template_label=template_label,
        uploaded_name=uploaded.name,
        upload_hash=upload_hash,
        bank_account_name=bank_account_name,
    ):
        return

    if template_key == "driver_statements" and _render_missing_vendors_prompt(
        preview=preview,
        target_realm=selected_realm,
        supabase=supabase,
        token_repo=token_repo,
        auth_service=auth_service,
        email=email,
        template_key=template_key,
        template_label=template_label,
        uploaded_name=uploaded.name,
        upload_hash=upload_hash,
        bank_account_name=bank_account_name,
    ):
        return

    if template_key == "invoices":
        if audit_clicked:
            with st.spinner("Auditing this file against QuickBooks…"):
                st.session_state[QBO_INVOICE_AUDIT_KEY] = _audit_invoice_import(
                    preview=preview,
                    realms=realms,
                    auth_service=auth_service,
                )
        _render_invoice_routing_summary(preview, realms)
        _render_invoice_audit_result(preview)

    _render_preview(preview)

    # Optional in-line correction of money-code expense account assignments.
    # Driver Statements are edited directly in the full preview table above,
    # which allows free-text cleanup such as removing "Statement Deductions:".
    if template_key == "money_codes":
        if selected_realm is None:
            st.error("Choose a QuickBooks company before importing this file type.")
            return
        with st.expander(
            "\u270f\ufe0f Edit expense account on individual lines (loads QBO chart of accounts)",
            expanded=False,
        ):
            _render_editable_expense_lines(
                preview=preview,
                target_realm=selected_realm,
                auth_service=auth_service,
            )

    if preview.errors:
        st.warning("Fix preview errors before importing.")
        return
    if not preview.drafts:
        st.warning("No importable rows were found.")
        return


def _handle_post_to_qbo(
    *,
    preview: PreviewResult,
    target_realm: ConnectedRealm | None,
    realms: list[ConnectedRealm],
    supabase: SupabaseRestClient,
    token_repo: QboTokenRepository,
    auth_service: QboAuthService,
    email: str,
    template_key: str,
    template_label: str,
    uploaded_name: str,
    upload_hash: str,
    bank_account_name: str,
) -> None:
    """Run the post flow after the top-row Post button is clicked."""
    if preview.errors:
        st.warning("Fix preview errors before importing.")
        return
    if not preview.drafts:
        st.warning("No importable rows were found.")
        return

    if template_key == "invoices":
        with st.spinner("Checking customers in QuickBooks…"):
            check = _find_missing_invoice_customers(
                preview=preview,
                realms=realms,
                auth_service=auth_service,
            )
        if check.get("lookup_errors") or check.get("missing"):
            st.session_state[QBO_MISSING_CUSTOMERS_KEY] = check
            st.rerun()
        st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
    elif template_key == "driver_statements":
        if target_realm is None:
            st.error("Choose a QuickBooks company before posting.")
            return
        with st.spinner("Checking vendors in QuickBooks…"):
            check = _find_missing_driver_statement_vendors(
                preview=preview,
                target_realm=target_realm,
                auth_service=auth_service,
            )
        if check.get("lookup_errors") or check.get("missing"):
            st.session_state[QBO_MISSING_VENDORS_KEY] = check
            st.rerun()
        st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)

    _post_preview_to_qbo(
        preview=preview,
        target_realm=target_realm,
        supabase=supabase,
        token_repo=token_repo,
        auth_service=auth_service,
        email=email,
        template_key=template_key,
        template_label=template_label,
        uploaded_name=uploaded_name,
        upload_hash=upload_hash,
        bank_account_name=bank_account_name,
    )


def _post_preview_to_qbo(
    *,
    preview: PreviewResult,
    target_realm: ConnectedRealm | None,
    supabase: SupabaseRestClient,
    token_repo: QboTokenRepository,
    auth_service: QboAuthService,
    email: str,
    template_key: str,
    template_label: str,
    uploaded_name: str,
    upload_hash: str,
    bank_account_name: str,
) -> None:
    """Post the already-reviewed preview to QBO and render the result."""
    if template_key != "invoices" and target_realm is None:
        st.error("Choose a QuickBooks company before posting.")
        return

    if template_key == "driver_statements" and bank_account_name:
        assert target_realm is not None
        token_repo.save_realm_settings(
            realm_id=target_realm.realm_id,
            company_name=target_realm.company_name,
            environment=target_realm.environment,
            default_bank_account_name=bank_account_name,
            default_money_code_cc_account_name=target_realm.default_money_code_cc_account_name,
            connected_by_email=target_realm.connected_by_email,
        )

    audit = SupabaseAuditLog(
        supabase,
        imported_by_email=email,
        source_file_name=uploaded_name,
        source_hash=upload_hash,
    )
    qbo_client = QboClient(auth_service)
    progress_bar = st.progress(0.0, text=f"Posting to QuickBooks: 0/{len(preview.drafts)} — starting…")

    def _update_import_progress(done: int, total: int, message: str) -> None:
        fraction = min(1.0, max(0.0, (done / total) if total else 0.0))
        progress_bar.progress(fraction, text=message)

    import_service = ImportService(
        qbo_client,
        EntityLookupService(qbo_client),
        DuplicateChecker(qbo_client),
        audit,
        progress_callback=_update_import_progress,
    )
    with st.spinner("Posting to QuickBooks… please keep this tab open."):
        start_ts = datetime.now(tz=timezone.utc)
        if template_key == "invoices":
            # Invoices are fully Division-routed. Preview validation blocks
            # blank/unmatched divisions before this point.
            stats = import_service.post_invoices(
                preview.drafts,
                target_realm_id="",
            )
        elif template_key == "driver_statements":
            assert target_realm is not None
            stats = import_service.post_checks(preview.drafts, target_realm_id=target_realm.realm_id)
        else:
            assert target_realm is not None
            stats = import_service.post_money_codes(preview.drafts, target_realm_id=target_realm.realm_id)
        duration_ms = int((datetime.now(tz=timezone.utc) - start_ts).total_seconds() * 1000)
    completed = int(getattr(stats, "posted", 0) or 0) + int(getattr(stats, "skipped_duplicates", 0) or 0) + int(getattr(stats, "failed", 0) or 0)
    progress_bar.progress(1.0, text=f"Posting complete: {completed}/{len(preview.drafts)} processed.")

    held = int(getattr(stats, "held_for_retry", 0) or 0)
    st.success(f"Done: posted {stats.posted}, duplicates {stats.skipped_duplicates}, failed {stats.failed}.")
    if held:
        st.warning(
            f"QuickBooks throttled part of the import. {held} row(s) were held as retryable failures. "
            "Open Import history → Retry failed rows after waiting about a minute."
        )
    _render_import_stats(stats)

    try:
        sheets_log = GoogleSheetsImportLog()
        wrote = sheets_log.append_summary(
            user_email=email,
            action="IMPORT",
            template=template_label,
            company=target_realm.company_name if target_realm else "Division-routed invoices",
            realm_id=target_realm.realm_id if target_realm else "",
            source_sheet=uploaded_name,
            source_count=len(preview.drafts),
            success=getattr(stats, "posted", 0),
            failed=getattr(stats, "failed", 0),
            skipped=getattr(stats, "skipped_duplicates", 0),
            duration_ms=duration_ms,
            execution_id=upload_hash[:12] if upload_hash else "",
            errors=[getattr(item, "error", "") for item in getattr(stats, "failures", []) or []],
        )
        if not wrote and sheets_log.unavailable_reason:
            logger.info("ImportLog sheet not written: %s", sheets_log.unavailable_reason)
    except Exception as exc:  # noqa: BLE001 - never break the import flow
        logger.warning("ImportLog sheet write failed: %s", exc)

    st.session_state.pop(QBO_PREVIEW_KEY, None)
    st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
    st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
    st.session_state.pop(QBO_RETRY_FILTER_KEY, None)


def _invoice_customer_refs(
    *,
    preview: PreviewResult,
    realms: list[ConnectedRealm],
) -> list[dict[str, Any]]:
    """Return unique invoice Customer/realm targets, resolved by Division."""
    realm_names = {realm.realm_id: realm.company_name for realm in realms}
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for draft in preview.drafts or []:
        realm_id = str(draft.get("_realmId") or "").strip()
        customer_name = str(draft.get("_tempCustomerName") or "").strip()
        if not realm_id or not customer_name:
            continue
        division = str(draft.get("_division") or "").strip()
        key = (realm_id, customer_name)
        if key not in unique:
            unique[key] = {
                "customer_name": customer_name,
                "realm_id": realm_id,
                "target_company": realm_names.get(realm_id) or realm_id,
                "division": division or "(blank)",
                "invoice_count": 0,
            }
        unique[key]["invoice_count"] += 1
    return sorted(
        unique.values(),
        key=lambda row: (str(row.get("target_company") or ""), str(row.get("customer_name") or "")),
    )


def _find_missing_invoice_customers(
    *,
    preview: PreviewResult,
    realms: list[ConnectedRealm],
    auth_service: QboAuthService,
) -> dict[str, Any]:
    refs = _invoice_customer_refs(preview=preview, realms=realms)
    qbo_client = QboClient(auth_service)
    lookups = EntityLookupService(qbo_client)
    missing: list[dict[str, Any]] = []
    lookup_errors: list[dict[str, Any]] = []
    found_count = 0
    for ref in refs:
        try:
            customer_id = lookups.resolve_entity("Customer", ref["customer_name"], ref["realm_id"])
        except Exception as exc:  # noqa: BLE001 - block posting, do not guess
            logger.exception("Customer lookup failed for %s", ref["customer_name"])
            lookup_errors.append({**ref, "error": str(exc)})
            continue
        if customer_id:
            found_count += 1
        else:
            missing.append(ref)
    return {
        "source_hash": preview.source_hash,
        "checked_count": len(refs),
        "found_count": found_count,
        "missing": missing,
        "lookup_errors": lookup_errors,
    }


def _driver_statement_vendor_refs(
    *,
    preview: PreviewResult,
    target_realm: ConnectedRealm | None,
) -> list[dict[str, Any]]:
    """Return unique driver-statement Vendor targets for the selected QBO company."""
    if target_realm is None:
        return []
    unique: dict[str, dict[str, Any]] = {}
    for draft in preview.drafts or []:
        vendor_name = str(draft.get("_tempVendorName") or "").strip()
        if not vendor_name:
            continue
        key = vendor_name.lower()
        if key not in unique:
            unique[key] = {
                "vendor_name": vendor_name,
                "realm_id": target_realm.realm_id,
                "target_company": target_realm.company_name,
                "check_count": 0,
            }
        unique[key]["check_count"] += 1
    return sorted(unique.values(), key=lambda row: str(row.get("vendor_name") or ""))


def _find_missing_driver_statement_vendors(
    *,
    preview: PreviewResult,
    target_realm: ConnectedRealm | None,
    auth_service: QboAuthService,
) -> dict[str, Any]:
    refs = _driver_statement_vendor_refs(preview=preview, target_realm=target_realm)
    qbo_client = QboClient(auth_service)
    lookups = EntityLookupService(qbo_client)
    missing: list[dict[str, Any]] = []
    lookup_errors: list[dict[str, Any]] = []
    found_count = 0
    for ref in refs:
        try:
            vendor_id = lookups.resolve_entity("Vendor", ref["vendor_name"], ref["realm_id"])
        except Exception as exc:  # noqa: BLE001 - block posting, do not guess
            logger.exception("Vendor lookup failed for %s", ref["vendor_name"])
            lookup_errors.append({**ref, "error": str(exc)})
            continue
        if vendor_id:
            found_count += 1
        else:
            missing.append(ref)
    return {
        "source_hash": preview.source_hash,
        "checked_count": len(refs),
        "found_count": found_count,
        "missing": missing,
        "lookup_errors": lookup_errors,
    }


def _render_missing_customers_prompt(
    *,
    preview: PreviewResult,
    realms: list[ConnectedRealm],
    supabase: SupabaseRestClient,
    token_repo: QboTokenRepository,
    auth_service: QboAuthService,
    email: str,
    template_key: str,
    template_label: str,
    uploaded_name: str,
    upload_hash: str,
    bank_account_name: str,
) -> bool:
    """Render the approval prompt after Post discovers missing customers.

    Returns True while the prompt is active so the normal Post button stays
    hidden until the user creates customers, re-checks, or cancels.
    """
    state = st.session_state.get(QBO_MISSING_CUSTOMERS_KEY)
    if not isinstance(state, dict):
        return False
    if state.get("source_hash") != preview.source_hash:
        st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
        return False

    missing = list(state.get("missing") or [])
    lookup_errors = list(state.get("lookup_errors") or [])
    creation_errors = list(state.get("creation_errors") or [])
    if not missing and not lookup_errors and not creation_errors:
        st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
        return False

    with st.container(border=True):
        st.markdown("**Customers needed before posting**")
        st.caption(
            "No invoices have been posted yet. Customers are matched to the QBO company resolved from each row's Division."
        )

        metric_cols = st.columns(3)
        metric_cols[0].metric("Checked", int(state.get("checked_count") or 0))
        metric_cols[1].metric("Found", int(state.get("found_count") or 0))
        metric_cols[2].metric("Missing", len(missing))

        if lookup_errors:
            st.error("Customer lookup failed, so posting is paused. Re-check after fixing the connection or QBO access.")
            with st.expander("Lookup errors", expanded=False):
                st.dataframe(
                    [
                        {
                            "Customer": item.get("customer_name"),
                            "Target company": item.get("target_company"),
                            "Division": item.get("division"),
                            "Error": item.get("error"),
                        }
                        for item in lookup_errors
                    ],
                    hide_index=True,
                    use_container_width=True,
                )

        if creation_errors:
            st.error("Some customers could not be created. Nothing was posted.")
            with st.expander("Create errors", expanded=True):
                st.dataframe(creation_errors, hide_index=True, use_container_width=True)

        if missing:
            table_rows = [
                {
                    "Customer": item.get("customer_name"),
                    "Target company": item.get("target_company"),
                    "Division": item.get("division"),
                    "Invoices": item.get("invoice_count"),
                }
                for item in missing
            ]
            with st.expander("Missing customer details", expanded=len(missing) <= 5):
                st.dataframe(table_rows, hide_index=True, use_container_width=True)

        action_cols = st.columns([0.18, 0.16, 0.13, 0.53])
        with action_cols[0]:
            create_clicked = st.button(
                "Create + post",
                type="primary",
                key="qbo_create_missing_and_post",
                disabled=not missing or bool(lookup_errors),
                use_container_width=True,
            )
        with action_cols[1]:
            recheck_clicked = st.button("Check again", key="qbo_missing_customers_recheck", use_container_width=True)
        with action_cols[2]:
            cancel_clicked = st.button("Cancel", key="qbo_missing_customers_cancel", use_container_width=True)

    if cancel_clicked:
        st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
        st.info("Import cancelled. Nothing was posted.")
        st.rerun()

    if recheck_clicked:
        with st.spinner("Re-checking customers in QuickBooks…"):
            st.session_state[QBO_MISSING_CUSTOMERS_KEY] = _find_missing_invoice_customers(
                preview=preview,
                realms=realms,
                auth_service=auth_service,
            )
        st.rerun()

    if create_clicked:
        created, failed = _create_missing_customers(missing, auth_service)
        if failed:
            st.session_state[QBO_MISSING_CUSTOMERS_KEY] = {
                **state,
                "missing": [item.get("record") or item for item in failed],
                "creation_errors": [
                    {
                        "Customer": (item.get("record") or {}).get("customer_name"),
                        "Target company": (item.get("record") or {}).get("target_company"),
                        "Error": item.get("error"),
                    }
                    for item in failed
                ],
            }
            st.rerun()

        st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
        st.success(f"Created {len(created)} customer(s). Continuing to post…")
        _post_preview_to_qbo(
            preview=preview,
            target_realm=None,
            supabase=supabase,
            token_repo=token_repo,
            auth_service=auth_service,
            email=email,
            template_key=template_key,
            template_label=template_label,
            uploaded_name=uploaded_name,
            upload_hash=upload_hash,
            bank_account_name=bank_account_name,
        )

    return True


def _render_missing_vendors_prompt(
    *,
    preview: PreviewResult,
    target_realm: ConnectedRealm | None,
    supabase: SupabaseRestClient,
    token_repo: QboTokenRepository,
    auth_service: QboAuthService,
    email: str,
    template_key: str,
    template_label: str,
    uploaded_name: str,
    upload_hash: str,
    bank_account_name: str,
) -> bool:
    """Render the approval prompt after Post discovers missing driver vendors.

    Returns True while the prompt is active so the normal Post button stays
    hidden until the user creates vendors, re-checks, or cancels.
    """
    state = st.session_state.get(QBO_MISSING_VENDORS_KEY)
    if not isinstance(state, dict):
        return False
    if state.get("source_hash") != preview.source_hash:
        st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
        return False

    missing = list(state.get("missing") or [])
    lookup_errors = list(state.get("lookup_errors") or [])
    creation_errors = list(state.get("creation_errors") or [])
    if not missing and not lookup_errors and not creation_errors:
        st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
        return False

    with st.container(border=True):
        st.markdown("**Vendors needed before posting checks**")
        st.caption(
            "No driver statement checks have been posted yet. Missing drivers/owners can be created as QBO Vendors, then posting continues."
        )

        metric_cols = st.columns(3)
        metric_cols[0].metric("Checked", int(state.get("checked_count") or 0))
        metric_cols[1].metric("Found", int(state.get("found_count") or 0))
        metric_cols[2].metric("Missing", len(missing))

        if lookup_errors:
            st.error("Vendor lookup failed, so posting is paused. Re-check after fixing the connection or QBO access.")
            with st.expander("Lookup errors", expanded=False):
                st.dataframe(
                    [
                        {
                            "Vendor": item.get("vendor_name"),
                            "Target company": item.get("target_company"),
                            "Error": item.get("error"),
                        }
                        for item in lookup_errors
                    ],
                    hide_index=True,
                    use_container_width=True,
                )

        if creation_errors:
            st.error("Some vendors could not be created. Nothing was posted.")
            with st.expander("Create errors", expanded=True):
                st.dataframe(creation_errors, hide_index=True, use_container_width=True)

        if missing:
            table_rows = [
                {
                    "Vendor": item.get("vendor_name"),
                    "Target company": item.get("target_company"),
                    "Checks": item.get("check_count"),
                }
                for item in missing
            ]
            with st.expander("Missing vendor details", expanded=len(missing) <= 5):
                st.dataframe(table_rows, hide_index=True, use_container_width=True)

        action_cols = st.columns([0.18, 0.16, 0.13, 0.53])
        with action_cols[0]:
            create_clicked = st.button(
                "Create + post",
                type="primary",
                key="qbo_create_missing_vendors_and_post",
                disabled=not missing or bool(lookup_errors),
                use_container_width=True,
            )
        with action_cols[1]:
            recheck_clicked = st.button("Check again", key="qbo_missing_vendors_recheck", use_container_width=True)
        with action_cols[2]:
            cancel_clicked = st.button("Cancel", key="qbo_missing_vendors_cancel", use_container_width=True)

    if cancel_clicked:
        st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
        st.info("Import cancelled. Nothing was posted.")
        st.rerun()

    if recheck_clicked:
        with st.spinner("Re-checking vendors in QuickBooks…"):
            st.session_state[QBO_MISSING_VENDORS_KEY] = _find_missing_driver_statement_vendors(
                preview=preview,
                target_realm=target_realm,
                auth_service=auth_service,
            )
        st.rerun()

    if create_clicked:
        created, failed = _create_missing_vendors(missing, auth_service)
        if failed:
            st.session_state[QBO_MISSING_VENDORS_KEY] = {
                **state,
                "missing": [item.get("record") or item for item in failed],
                "creation_errors": [
                    {
                        "Vendor": (item.get("record") or {}).get("vendor_name"),
                        "Target company": (item.get("record") or {}).get("target_company"),
                        "Error": item.get("error"),
                    }
                    for item in failed
                ],
            }
            st.rerun()

        st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
        st.success(f"Created {len(created)} vendor(s). Continuing to post…")
        _post_preview_to_qbo(
            preview=preview,
            target_realm=target_realm,
            supabase=supabase,
            token_repo=token_repo,
            auth_service=auth_service,
            email=email,
            template_key=template_key,
            template_label=template_label,
            uploaded_name=uploaded_name,
            upload_hash=upload_hash,
            bank_account_name=bank_account_name,
        )

    return True


def _create_missing_customers(
    missing: list[dict[str, Any]], auth_service: QboAuthService
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    qbo_client = QboClient(auth_service)
    lookups = EntityLookupService(qbo_client)
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    progress = st.progress(0.0, text="Creating customers in QuickBooks…")
    total = len(missing)
    for idx, item in enumerate(missing, start=1):
        customer_name = str(item.get("customer_name") or "").strip()
        realm_id = str(item.get("realm_id") or "").strip()
        try:
            existing_id = lookups.resolve_entity("Customer", customer_name, realm_id)
            new_id = existing_id or lookups.create_entity("Customer", customer_name, realm_id)
        except Exception as exc:  # noqa: BLE001 - keep the rest safe
            logger.exception("Customer create failed for %s", customer_name)
            failed.append({"record": item, "error": str(exc)})
        else:
            if new_id:
                created.append({**item, "qbo_id": new_id})
            else:
                failed.append({"record": item, "error": "QBO returned no customer Id."})
        progress.progress(idx / total, text=f"Created {idx}/{total}…")
    progress.empty()
    return created, failed


def _create_missing_vendors(
    missing: list[dict[str, Any]], auth_service: QboAuthService
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    qbo_client = QboClient(auth_service)
    lookups = EntityLookupService(qbo_client)
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    progress = st.progress(0.0, text="Creating vendors in QuickBooks…")
    total = len(missing)
    for idx, item in enumerate(missing, start=1):
        vendor_name = str(item.get("vendor_name") or "").strip()
        realm_id = str(item.get("realm_id") or "").strip()
        try:
            existing_id = lookups.resolve_entity("Vendor", vendor_name, realm_id)
            new_id = existing_id or lookups.create_entity("Vendor", vendor_name, realm_id)
        except Exception as exc:  # noqa: BLE001 - keep the rest safe
            logger.exception("Vendor create failed for %s", vendor_name)
            failed.append({"record": item, "error": str(exc)})
        else:
            if new_id:
                created.append({**item, "qbo_id": new_id})
            else:
                failed.append({"record": item, "error": "QBO returned no vendor Id."})
        progress.progress(idx / total, text=f"Created {idx}/{total}…")
    progress.empty()
    return created, failed


def _active_retry_filter(template_key: str) -> dict[str, Any] | None:
    retry_filter = st.session_state.get(QBO_RETRY_FILTER_KEY)
    if not isinstance(retry_filter, dict):
        return None
    if retry_filter.get("template_key") != template_key:
        return None
    doc_numbers = [str(doc).strip() for doc in retry_filter.get("doc_numbers") or [] if str(doc).strip()]
    if not doc_numbers:
        return None
    retry_filter["doc_numbers"] = doc_numbers
    return retry_filter


def _preview_doc_number(row: dict[str, Any]) -> str:
    return str(row.get("Doc #") or row.get("Code / Doc #") or row.get("Code") or "").strip()


def _apply_retry_filter(preview: PreviewResult, retry_filter: dict[str, Any] | None) -> PreviewResult:
    if not retry_filter:
        return preview
    wanted = {str(doc).strip().lower() for doc in retry_filter.get("doc_numbers") or [] if str(doc).strip()}
    if not wanted:
        return preview

    filtered_drafts = [
        draft for draft in (preview.drafts or []) if str(draft.get("DocNumber") or "").strip().lower() in wanted
    ]
    filtered_rows = [row for row in (preview.rows or []) if _preview_doc_number(row).lower() in wanted]
    found = {str(draft.get("DocNumber") or "").strip().lower() for draft in filtered_drafts}
    missing = sorted(wanted - found)
    warnings = list(preview.warnings or [])
    if missing:
        warnings.append(
            "Retry mode: these failed doc number(s) were not found in the uploaded source file: "
            + ", ".join(missing)
        )
    if not filtered_drafts:
        warnings.append("Retry mode did not find any selected failed docs in this upload. Check that this is the original source file.")

    return PreviewResult(
        template_type=preview.template_type,
        source_file=preview.source_file,
        source_hash=preview.source_hash,
        count=len(filtered_drafts),
        source_count=preview.source_count,
        skipped_count=preview.skipped_count,
        rows=filtered_rows,
        errors=list(preview.errors or []),
        warnings=warnings,
        drafts=filtered_drafts,
    )



def _build_preview(
    *,
    template_key: str,
    file_name: str,
    content: bytes,
    realms: list[ConnectedRealm],
    selected_realm: ConnectedRealm | None,
    bank_account_name: str,
    override_date: str,
) -> PreviewResult:
    rows = FileLoader().load_rows_from_bytes(file_name, content)
    if template_key == "invoices":
        parser = InvoiceParser(CompanyDirectory(realms))
        source_info = InvoiceParser.inspect_source(rows)
        if source_info.get("requires_target_realm") and selected_realm is None:
            raise ValueError("Choose a QuickBooks company / division for this no-division invoice file before previewing.")
        parsed = parser.parse(
            rows,
            target_realm_id=selected_realm.realm_id if selected_realm else "",
            target_division=selected_realm.company_name if selected_realm else "",
        )
        drafts = parser.build_qbo_drafts(parsed)
        preview_rows: list[dict[str, Any]] = []
        parsed_rows = list(parsed.get("rows") or [])
        invoice_totals = _invoice_group_totals(parsed_rows)
        for row in parsed_rows:
            line_amount = row.get("amount") or 0
            line_qty = row.get("line_qty") or 1
            line_unit_price = row.get("line_unit_price") or line_amount
            broker_load_number = row.get("broker_load_number") or ""
            preview_rows.append(
                {
                    "QBO Txn Type": "Invoice",
                    "Doc #": row["doc_number"],
                    "Txn Date": row["txn_date"],
                    "Due Date": row["due_date"],
                    "Customer": row["customer_name"],
                    "Division": row["division_name"] or "(blank)",
                    "QBO Company": _realm_name_for_id(realms, row.get("realm_id") or "") or "(unmatched)",
                    "Realm ID": row.get("realm_id") or "",
                    "PO / Broker Load #": broker_load_number,
                    "QBO Terms": "Net 30",
                    "QBO Item": row.get("line_item") or "Freight Income",
                    "Line Qty": line_qty,
                    "Line Rate": line_unit_price,
                    "Line Amount": line_amount,
                    "Invoice Amount": invoice_totals.get(_invoice_group_key(row), float(line_amount or 0)),
                    "Line Description": row.get("line_description") or f"Load {row['doc_number']}",
                    "Private Note": f"Load {row['doc_number']}",
                    "Custom Field #": "1",
                    "Custom Field Value": broker_load_number,
                    "QB Exported": row.get("qb_exported"),
                    "Invoice Last Sent": row.get("invoice_last_sent_date") or "",
                    "Invoice Remarks": row.get("invoice_remarks") or "",
                    "Status": row.get("status") or "",
                }
            )
        warnings = [f"Row {item.get('row_number')}: {item.get('reason')}" for item in parsed.get("skipped_rows") or []]
        route_errors = [
            "Row {row}: invoice {doc} has Division {division!r}, which does not match a connected QuickBooks company.".format(
                row=item.get("row_number"),
                doc=item.get("doc_number"),
                division=item.get("division_name") or "(blank)",
            )
            for item in parsed.get("rows") or []
            if not item.get("realm_id")
        ]
        return PreviewResult(
            template_type=template_key,
            source_file=file_name,
            source_hash=source_file_hash(content),
            count=len(drafts),
            source_count=int(parsed.get("source_count") or max(len(rows) - 1, 0)),
            skipped_count=len(parsed.get("skipped_rows") or []),
            rows=preview_rows,
            errors=route_errors,
            warnings=warnings,
            drafts=drafts,
        )

    if selected_realm is None:
        raise ValueError("Choose a QuickBooks company before previewing this file type.")

    if template_key == "driver_statements":
        parsed = DriverStatementParser().parse(
            rows,
            target_realm_id=selected_realm.realm_id,
            target_division=selected_realm.company_name,
            bank_account_name=bank_account_name,
            override_txn_date=override_date,
        )
        drafts = parsed.get("checks") or []
        return PreviewResult(
            template_type=template_key,
            source_file=file_name,
            source_hash=source_file_hash(content),
            count=len(drafts),
            source_count=max(len(rows) - 1, 0),
            skipped_count=len(parsed.get("skipped_rows") or []),
            rows=_driver_statement_preview_rows_from_drafts(drafts),
            errors=list(parsed.get("errors") or []),
            warnings=list(parsed.get("warnings") or []),
            drafts=drafts,
        )

    parsed = MoneyCodeParser().parse(rows, target_realm_id=selected_realm.realm_id)
    drafts = parsed.get("expenses") or []
    preview_rows: list[dict[str, Any]] = []
    for draft in drafts:
        total = _draft_amount(draft)
        for line_index, line in enumerate(draft.get("Line") or [], start=1):
            detail = (line or {}).get("AccountBasedExpenseLineDetail") or {}
            account_ref = detail.get("AccountRef") or {}
            preview_rows.append(
                {
                    "QBO Txn Type": "CreditCard Purchase",
                    "Code / Doc #": draft.get("DocNumber"),
                    "Txn Date": draft.get("TxnDate"),
                    "Payment Type": draft.get("PaymentType") or "CreditCard",
                    "Vendor": draft.get("_tempVendorName"),
                    "Realm ID": draft.get("_realmId") or "",
                    "CC Account": draft.get("_tempCcAccountName") or "Fuel Card - EFS",
                    "Memo": draft.get("_memo") or "",
                    "Purchase Total": total,
                    "Line #": line_index,
                    "Line Amount": (line or {}).get("Amount") or 0,
                    "Expense Account": (line or {}).get("_tempAccountName") or account_ref.get("name") or "",
                    "Line Description": (line or {}).get("Description") or "",
                    "Detail Type": (line or {}).get("DetailType") or "",
                }
            )
    return PreviewResult(
        template_type=template_key,
        source_file=file_name,
        source_hash=source_file_hash(content),
        count=len(drafts),
        source_count=max(len(rows) - 1, 0),
        skipped_count=0,
        rows=preview_rows,
        errors=list(parsed.get("errors") or []),
        warnings=list(parsed.get("warnings") or []),
        drafts=drafts,
    )


def _render_preview(preview: PreviewResult) -> None:
    st.subheader("Full preview")
    st.caption(
        f"{_TEMPLATE_OPTIONS.get(preview.template_type, preview.template_type)} — "
        f"{preview.count} importable transaction(s) from {preview.source_file}. "
        "The table includes source fields plus QBO-ready fields used at posting."
    )
    if preview.rows:
        if preview.template_type == "driver_statements":
            _render_editable_driver_statement_preview(preview)
        else:
            st.dataframe(preview.rows, use_container_width=True, hide_index=True)
    if preview.warnings:
        with st.expander(f"Warnings ({len(preview.warnings)})", expanded=True):
            for warning in preview.warnings[:50]:
                st.warning(warning)
    if preview.errors:
        with st.expander(f"Errors ({len(preview.errors)})", expanded=True):
            for error in preview.errors[:50]:
                st.error(error)



def _render_import_stats(stats: Any) -> None:
    rows: list[dict[str, Any]] = []
    for status, items in (("OK", stats.successes), ("Duplicate", stats.duplicates), ("Failed", stats.failures)):
        for item in items:
            rows.append(
                {
                    "Status": status,
                    "Doc #": item.get("doc_number"),
                    "Date": item.get("txn_date"),
                    "Customer / Vendor": item.get("entity_name"),
                    "Amount": _format_amount(item.get("amount")),
                    "Retryable?": "Yes" if item.get("retryable") else "",
                    "QBO Id / Message": item.get("qbo_id") or item.get("message") or "",
                }
            )
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    if stats.by_division:
        st.write("By division")
        st.dataframe(
            [{"Division": division, **counts} for division, counts in stats.by_division.items()],
            use_container_width=True,
            hide_index=True,
        )
    for warning in stats.warnings[:50]:
        st.warning(warning)
    for error in stats.errors[:50]:
        st.error(error)


def _status_label(status: Any) -> str:
    value = str(status or "").strip().lower()
    return {
        "success": "Posted",
        "duplicate": "Duplicate",
        "failed": "Failed",
    }.get(value, value.title() or "Unknown")


def _template_key_for_txn_type(txn_type: Any) -> str:
    value = str(txn_type or "").strip().lower()
    if value == "invoice":
        return "invoices"
    if value == "check":
        return "driver_statements"
    if value in {"moneycode", "money code", "creditcard", "creditcard purchase"}:
        return "money_codes"
    return ""


def _friendly_history_reason(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "").strip().lower()
    message = str(row.get("message") or "").strip()
    if status == "success":
        qbo_id = str(row.get("qbo_id") or "").strip()
        return f"Posted to QuickBooks{f' (QBO ID {qbo_id})' if qbo_id else ''}."
    if status == "duplicate":
        return "Skipped because this transaction already exists in QuickBooks."
    if not message:
        return "QuickBooks rejected this transaction. Open the row details or retry after reviewing the source file."

    patterns = [
        (r"Customer '([^']+)' not found", "Missing customer in QuickBooks: {name}. Retry after creating the customer."),
        (r"Vendor '([^']+)' not found", "Missing vendor in QuickBooks: {name}. Create the vendor, then retry."),
        (r"Item '([^']+)' not found", "Missing QBO item: {name}. Create or map this item, then retry."),
        (r"Expense account '([^']+)' not found", "Missing expense account in QuickBooks: {name}. Fix the account name or create it, then retry."),
        (r"CC account '([^']+)' not found", "Missing credit-card account in QuickBooks: {name}. Fix the CC account mapping, then retry."),
    ]
    for pattern, template in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return template.format(name=match.group(1))
    lower = message.lower()
    if "no bank account" in lower:
        return "No bank account was selected/resolved for this check. Pick the correct bank account and retry."
    if "duplicate" in lower:
        return "QuickBooks says this transaction may already exist. Review before retrying."
    if "http 401" in lower or "authentication" in lower:
        return "QuickBooks authorization failed. Reconnect the company, then retry."
    if "http 429" in lower or "rate limit" in lower:
        return "QuickBooks rate-limited the request. This row is held for retry; wait about a minute, then retry."
    if any(token in lower for token in ("http 500", "http 502", "http 503", "http 504")):
        return "QuickBooks had a temporary server error. Retry is usually safe."
    if "business validation error" in lower:
        return "QuickBooks business validation failed. Check required fields/accounts and retry."
    if "qbo post" in lower and "failed:" in lower:
        return message.split("failed:", 1)[-1].strip()[:240]
    return message[:240]


def _history_display_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    display: list[dict[str, Any]] = []
    for row in rows:
        display.append(
            {
                "Created": row.get("created_at") or "",
                "Status": _status_label(row.get("status")),
                "Type": row.get("txn_type") or "",
                "Source file": row.get("source_file_name") or "",
                "Doc #": row.get("doc_number") or "",
                "Date": row.get("txn_date") or "",
                "Customer / Vendor": row.get("entity_name") or "",
                "Division": row.get("division") or "",
                "Amount": row.get("amount"),
                "QBO ID": row.get("qbo_id") or "",
                "Reason": _friendly_history_reason(row),
                "Realm ID": row.get("realm_id") or "",
                "_status_raw": str(row.get("status") or "").strip().lower(),
                "_source_hash": row.get("source_file_hash") or "",
                "_template_key": _template_key_for_txn_type(row.get("txn_type")),
            }
        )
    return display


def _filtered_history_rows(display_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not display_rows:
        return []
    status_options = sorted({str(row.get("Status") or "") for row in display_rows if row.get("Status")})
    type_options = sorted({str(row.get("Type") or "") for row in display_rows if row.get("Type")})
    filter_cols = st.columns([0.22, 0.22, 0.26, 0.18, 0.12])
    with filter_cols[0]:
        statuses = st.multiselect("Status", status_options, default=status_options, key="qbo_hist_status_filter")
    with filter_cols[1]:
        txn_types = st.multiselect("Type", type_options, default=type_options, key="qbo_hist_type_filter")
    with filter_cols[2]:
        search = st.text_input("Search doc/customer/source/reason", key="qbo_hist_search")
    with filter_cols[3]:
        sort_by = st.selectbox(
            "Sort",
            ["Newest first", "Failed first", "Duplicates first", "Source file", "Doc #"],
            key="qbo_hist_sort",
        )
    with filter_cols[4]:
        failed_only = st.toggle("Failed only", key="qbo_hist_failed_only")

    status_set = set(statuses)
    type_set = set(txn_types)
    terms = [part for part in search.lower().split() if part]
    filtered: list[dict[str, Any]] = []
    for row in display_rows:
        if failed_only and row.get("Status") != "Failed":
            continue
        if status_set and row.get("Status") not in status_set:
            continue
        if type_set and row.get("Type") not in type_set:
            continue
        haystack = " ".join(
            str(row.get(key) or "")
            for key in ("Source file", "Doc #", "Customer / Vendor", "Division", "Reason", "Realm ID")
        ).lower()
        if terms and not all(term in haystack for term in terms):
            continue
        filtered.append(row)

    if sort_by == "Failed first":
        filtered.sort(key=lambda r: (0 if r.get("Status") == "Failed" else 1, str(r.get("Created") or "")), reverse=False)
    elif sort_by == "Duplicates first":
        filtered.sort(key=lambda r: (0 if r.get("Status") == "Duplicate" else 1, str(r.get("Created") or "")), reverse=False)
    elif sort_by == "Source file":
        filtered.sort(key=lambda r: (str(r.get("Source file") or ""), str(r.get("Created") or "")), reverse=True)
    elif sort_by == "Doc #":
        filtered.sort(key=lambda r: str(r.get("Doc #") or ""))
    else:
        filtered.sort(key=lambda r: str(r.get("Created") or ""), reverse=True)
    return filtered


def _public_history_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hidden = {"_status_raw", "_source_hash", "_template_key"}
    return [{key: value for key, value in row.items() if key not in hidden} for row in rows]


def _render_failed_retry_panel(filtered_rows: list[dict[str, Any]]) -> None:
    failed_rows = [row for row in filtered_rows if row.get("Status") == "Failed" and row.get("Doc #")]
    if not failed_rows:
        st.caption("No failed rows in the current filter to retry.")
        return

    with st.expander("Retry failed rows", expanded=False):
        st.caption(
            "Select failed docs, then upload the same source file on the import page. "
            "The importer will preview only those docs before posting again."
        )
        retry_rows = [
            {
                "Retry": True,
                "Type": row.get("Type"),
                "Source file": row.get("Source file"),
                "Doc #": row.get("Doc #"),
                "Customer / Vendor": row.get("Customer / Vendor"),
                "Reason": row.get("Reason"),
                "_template_key": row.get("_template_key"),
                "_source_hash": row.get("_source_hash"),
            }
            for row in failed_rows[:200]
        ]
        edited = st.data_editor(
            retry_rows,
            key="qbo_retry_failed_editor",
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            column_config={
                "_template_key": None,
                "_source_hash": None,
                "Retry": st.column_config.CheckboxColumn("Retry"),
                "Reason": st.column_config.TextColumn(disabled=True),
            },
        )
        try:
            edited_rows = edited.to_dict("records")  # type: ignore[attr-defined]
        except AttributeError:
            edited_rows = list(edited or [])
        selected = [row for row in edited_rows if row.get("Retry")]
        template_keys = {str(row.get("_template_key") or "") for row in selected if row.get("_template_key")}
        source_files = {str(row.get("Source file") or "") for row in selected}
        can_retry = bool(selected) and len(template_keys) == 1 and len(source_files) == 1
        if selected and not can_retry:
            st.warning("Select failed rows from one import type and one source file at a time.")
        if st.button("Retry selected failed docs", type="primary", disabled=not can_retry, key="qbo_retry_selected_failed"):
            template_key = next(iter(template_keys))
            doc_numbers = sorted({str(row.get("Doc #") or "").strip() for row in selected if str(row.get("Doc #") or "").strip()})
            st.session_state[QBO_RETRY_FILTER_KEY] = {
                "template_key": template_key,
                "doc_numbers": doc_numbers,
                "source_file_name": next(iter(source_files)) if source_files else "",
                "source_hash": str((selected[0] or {}).get("_source_hash") or ""),
            }
            st.session_state[QBO_VIEW_KEY] = "import"
            st.session_state[QBO_TEMPLATE_KEY] = template_key
            st.session_state.pop(QBO_PREVIEW_KEY, None)
            st.session_state.pop(QBO_UPLOAD_HASH_KEY, None)
            st.session_state.pop(QBO_MISSING_CUSTOMERS_KEY, None)
            st.session_state.pop(QBO_MISSING_VENDORS_KEY, None)
            st.rerun()


def _render_recent_import_batches(rows: list[dict[str, Any]]) -> None:
    """Summarize transaction-level audit rows into user-friendly import batches."""
    batches: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        source_hash = str(row.get("source_file_hash") or "").strip()
        created_at = str(row.get("created_at") or "")
        source_file = str(row.get("source_file_name") or "")
        fallback_bucket = created_at[:16]
        key = (source_hash or fallback_bucket, source_file)
        bucket = batches.setdefault(
            key,
            {
                "Latest": created_at,
                "Source file": source_file,
                "Hash": source_hash[:12] if source_hash else "",
                "Types": set(),
                "Companies / divisions": set(),
                "Posted": 0,
                "Duplicates": 0,
                "Failed": 0,
                "Rows": 0,
            },
        )
        bucket["Rows"] += 1
        bucket["Latest"] = max(str(bucket.get("Latest") or ""), created_at)
        txn_type = str(row.get("txn_type") or "")
        if txn_type:
            bucket["Types"].add(txn_type)
        division = str(row.get("division") or "")
        realm_id = str(row.get("realm_id") or "")
        if division or realm_id:
            bucket["Companies / divisions"].add(division or realm_id)
        status = str(row.get("status") or "").lower()
        if status == "success":
            bucket["Posted"] += 1
        elif status == "duplicate":
            bucket["Duplicates"] += 1
        elif status == "failed":
            bucket["Failed"] += 1

    summary_rows: list[dict[str, Any]] = []
    for bucket in batches.values():
        summary_rows.append(
            {
                "Latest": bucket["Latest"],
                "Source file": bucket["Source file"],
                "Types": ", ".join(sorted(bucket["Types"])),
                "Companies / divisions": ", ".join(sorted(bucket["Companies / divisions"])),
                "Posted": bucket["Posted"],
                "Duplicates": bucket["Duplicates"],
                "Failed": bucket["Failed"],
                "Audit rows": bucket["Rows"],
                "Hash": bucket["Hash"],
            }
        )
    if not summary_rows:
        return

    st.markdown("**Recent import batches**")
    st.caption("Use this table to quickly see recent import files and outcomes. Expand the transaction audit below for row-level details.")
    st.dataframe(
        sorted(summary_rows, key=lambda item: str(item.get("Latest") or ""), reverse=True),
        use_container_width=True,
        hide_index=True,
    )


def _render_history(supabase: SupabaseRestClient) -> None:
    st.title("Import history")
    st.caption(
        "Two history sources: the live Supabase audit (per-transaction) and the legacy "
        "Google Sheet `ImportLog` (per-import summary, used by the original Apps Script app)."
    )

    live_tab, legacy_tab = st.tabs(["Live (Supabase)", "Legacy import log (Google Sheets)"])

    with live_tab:
        limit = st.slider("Rows", min_value=25, max_value=500, value=100, step=25, key="qbo_hist_live_limit")
        audit = SupabaseAuditLog(supabase)
        try:
            rows = audit.recent(limit=limit)
        except Exception as exc:  # noqa: BLE001 - history should not crash the page
            st.error(f"Could not load QBO history: {exc}")
        else:
            if not rows:
                st.info("No QBO audit rows yet. New imports will appear here.")
            else:
                _render_recent_import_batches(rows)
                st.markdown("**Transaction-level audit**")
                display_rows = _history_display_rows(rows)
                filtered_rows = _filtered_history_rows(display_rows)
                st.caption(f"Showing {len(filtered_rows)} of {len(display_rows)} audit row(s).")
                _render_failed_retry_panel(filtered_rows)
                st.dataframe(_public_history_columns(filtered_rows), use_container_width=True, hide_index=True)

    with legacy_tab:
        legacy_limit = st.slider(
            "Rows",
            min_value=25,
            max_value=500,
            value=100,
            step=25,
            key="qbo_hist_legacy_limit",
        )
        sheets_log = GoogleSheetsImportLog()
        st.caption(
            f"Sheet: `{sheets_log.spreadsheet_id}`  \u00b7  Tab: `{sheets_log.worksheet_name}`"
        )
        legacy_rows = sheets_log.recent(limit=legacy_limit)
        if not legacy_rows:
            reason = sheets_log.unavailable_reason or (
                "No rows returned. Make sure the Streamlit Google service account has Viewer "
                "access to the ImportLog spreadsheet."
            )
            st.info(f"Legacy ImportLog is unavailable: {reason}")
            return
        legacy_display = [{key: value for key, value in row.items() if key.lower() != "user"} for row in legacy_rows]
        st.dataframe(legacy_display, use_container_width=True, hide_index=True)



# =============================================================================
# Editable preview helpers (expense-account dropdowns)
# =============================================================================

def _qbo_account_options(realm_id: str, auth_service: QboAuthService) -> list[str]:
    """Return the cached list of expense-eligible account names for a realm.

    Hits QBO once per session per realm. Use the "Refresh accounts" button to
    force a re-fetch when the chart of accounts changes in QuickBooks.
    """
    if not realm_id:
        return []
    cache = st.session_state.setdefault(QBO_CATALOG_CACHE_KEY, {})
    key = ("accounts:expense", realm_id)
    if key in cache:
        return cache[key]
    try:
        client = QboClient(auth_service)
        accounts = QboCatalog(client).list_accounts(
            realm_id, classifications=EXPENSE_ACCOUNT_CLASSIFICATIONS
        )
    except Exception as exc:  # noqa: BLE001 - UI must not crash
        logger.warning("Failed to load QBO accounts for %s: %s", realm_id, exc)
        st.error(f"Could not load QBO accounts for this company: {exc}")
        accounts = []
    cache[key] = accounts
    return accounts


def _render_editable_expense_lines(
    *,
    preview: PreviewResult,
    target_realm: ConnectedRealm,
    auth_service: QboAuthService,
) -> None:
    """Per-line expense-account editor for driver-statement and money-code drafts.

    The dropdown options come from QBO's chart of accounts (Expense, COGS,
    Other Expense, Other Current Asset, Fixed Asset) for the target company.
    Edits mutate ``preview.drafts`` in place so the subsequent "Confirm and
    post" call uses the corrected accounts.
    """
    drafts = preview.drafts or []
    if not drafts:
        st.caption("No draft rows to edit.")
        return

    accounts = _qbo_account_options(target_realm.realm_id, auth_service)
    refresh_col, count_col = st.columns([0.3, 0.7])
    with refresh_col:
        if st.button("\U0001F501 Refresh accounts", key="qbo_refresh_accounts"):
            cache = st.session_state.setdefault(QBO_CATALOG_CACHE_KEY, {})
            cache.pop(("accounts:expense", target_realm.realm_id), None)
            st.rerun()
    with count_col:
        if accounts:
            st.caption(
                f"Loaded {len(accounts)} expense-eligible accounts from {target_realm.company_name}."
            )
        else:
            st.caption("No accounts loaded yet \u2014 click Refresh, or check QBO connection.")

    if not accounts:
        return

    rows: list[dict[str, Any]] = []
    for ci, draft in enumerate(drafts):
        for li, line in enumerate(draft.get("Line") or []):
            if not isinstance(line, dict):
                continue
            detail = line.get("AccountBasedExpenseLineDetail") or {}
            ref = detail.get("AccountRef") or {}
            current = str(ref.get("name") or line.get("_tempAccountName") or "").strip()
            rows.append(
                {
                    "_ci": ci,
                    "_li": li,
                    "Doc #": draft.get("DocNumber") or "",
                    "Vendor": draft.get("_tempVendorName") or "",
                    "Date": draft.get("TxnDate") or "",
                    "Description": line.get("Description") or "",
                    "Amount": float(line.get("Amount") or 0.0),
                    "Expense Account": current,
                }
            )
    if not rows:
        st.caption("No editable expense-account lines on these drafts.")
        return

    # Ensure every existing value is in the option list (so the editor doesn't
    # silently drop unknown account names that came from the source file).
    option_set = set(accounts)
    for row in rows:
        existing = row["Expense Account"]
        if existing and existing not in option_set:
            accounts.append(existing)
            option_set.add(existing)

    edited = st.data_editor(
        rows,
        key=f"qbo_edit_lines_{preview.template_type}",
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "_ci": None,
            "_li": None,
            "Doc #": st.column_config.TextColumn(disabled=True),
            "Vendor": st.column_config.TextColumn(disabled=True),
            "Date": st.column_config.TextColumn(disabled=True),
            "Description": st.column_config.TextColumn(disabled=True),
            "Amount": st.column_config.NumberColumn(disabled=True, format="$ %.2f"),
            "Expense Account": st.column_config.SelectboxColumn(
                "Expense Account",
                help="Pick the correct QBO account for this line. Applied when you click Confirm and post.",
                options=accounts,
                required=True,
            ),
        },
    )

    edited_rows = _editor_records(edited)

    changed = 0
    for r in edited_rows:
        try:
            ci = int(r.get("_ci"))
            li = int(r.get("_li"))
        except (TypeError, ValueError):
            continue
        new_account = str(r.get("Expense Account") or "").strip()
        if not new_account:
            continue
        if ci < 0 or ci >= len(drafts):
            continue
        draft = drafts[ci]
        lines = draft.get("Line") or []
        if li < 0 or li >= len(lines):
            continue
        line = lines[li]
        detail = line.setdefault(
            "AccountBasedExpenseLineDetail",
            {"AccountRef": {"name": new_account}},
        )
        ref = detail.setdefault("AccountRef", {"name": new_account})
        old_name = str(ref.get("name") or line.get("_tempAccountName") or "").strip()
        if new_account != old_name:
            ref["name"] = new_account
            ref.pop("value", None)  # force re-resolution at post time
            line["_tempAccountName"] = new_account
            changed += 1

    if changed:
        st.success(f"Updated expense account on {changed} line(s). Click Post to QBO when ready.")


# =============================================================================
# Invoice import audit (compare export vs. QuickBooks)
# =============================================================================

# Audit status buckets. "OK" is the only non-anomaly outcome.
_AUDIT_OK = "Imported (correct company)"
_AUDIT_MISSING = "Missing import"
_AUDIT_AMOUNT_MISMATCH = "Amount mismatch"
_AUDIT_WRONG_COMPANY = "Division mismatch (wrong company)"
_AUDIT_DUPLICATE = "In correct + other company"
_AUDIT_UNRESOLVED = "Unresolved Division"
_AUDIT_NOT_CHECKED = "Not checked (lookup failed)"

# Largest absolute dollar difference treated as "same" amount (rounding noise).
_AUDIT_AMOUNT_TOLERANCE = 0.01


def _qbo_total(row: dict[str, Any]) -> float | None:
    try:
        return float(row.get("TotalAmt"))
    except (TypeError, ValueError):
        return None


def _preload_realm_invoices(
    *,
    realm_id: str,
    min_date: str,
    max_date: str,
    qbo_client: QboClient,
) -> dict[str, list[dict[str, Any]]]:
    """Map normalized DocNumber -> invoice rows for one realm over a date window."""
    page_size = 1000
    by_doc: dict[str, list[dict[str, Any]]] = {}
    start = 1
    while True:
        sql = (
            "SELECT DocNumber, TxnDate, CustomerRef, TotalAmt FROM Invoice "
            f"WHERE TxnDate >= '{min_date}' AND TxnDate <= '{max_date}' "
            f"STARTPOSITION {start} MAXRESULTS {page_size}"
        )
        rows = (qbo_client.query(sql, realm_id=realm_id).get("QueryResponse") or {}).get("Invoice") or []
        if not rows:
            break
        for row in rows:
            doc = str(row.get("DocNumber") or "").strip().lower()
            if doc:
                by_doc.setdefault(doc, []).append(row)
        if len(rows) < page_size:
            break
        start += page_size
    return by_doc


def _audit_invoice_import(
    *,
    preview: PreviewResult,
    realms: list[ConnectedRealm],
    auth_service: QboAuthService,
) -> dict[str, Any]:
    """Check every previewed invoice row against QuickBooks.

    For each row resolved to a company by its Division, confirm the invoice
    actually exists in that company (imported) and is not sitting in a
    different connected company instead (division mismatch). Reads only.
    """
    drafts = preview.drafts or []
    realm_names = {realm.realm_id: realm.company_name for realm in realms}
    qbo_client = QboClient(auth_service)

    dates = [str(d.get("TxnDate") or "").strip() for d in drafts if str(d.get("TxnDate") or "").strip()]
    min_date = min(dates) if dates else ""
    max_date = max(dates) if dates else ""

    realm_maps: dict[str, dict[str, list[dict[str, Any]]]] = {}
    query_errors: list[dict[str, Any]] = []
    if min_date and max_date:
        for realm in realms:
            try:
                realm_maps[realm.realm_id] = _preload_realm_invoices(
                    realm_id=realm.realm_id,
                    min_date=min_date,
                    max_date=max_date,
                    qbo_client=qbo_client,
                )
            except Exception as exc:  # noqa: BLE001 - surface per-company, keep auditing the rest
                logger.exception("Invoice audit preload failed for realm %s", realm.realm_id)
                query_errors.append(
                    {
                        "realm_id": realm.realm_id,
                        "company": realm_names.get(realm.realm_id) or realm.realm_id,
                        "error": str(exc),
                    }
                )

    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for draft in drafts:
        doc = str(draft.get("DocNumber") or "").strip()
        doc_key = doc.lower()
        txn_date = str(draft.get("TxnDate") or "").strip()
        division = str(draft.get("_division") or "").strip() or "(blank)"
        customer = str(draft.get("_tempCustomerName") or "").strip()
        amount = _draft_amount(draft)
        expected_realm_id = str(draft.get("_realmId") or "").strip()
        expected_company = realm_names.get(expected_realm_id) or "(unmatched)"

        found_realm_ids = [
            realm_id
            for realm_id, by_doc in realm_maps.items()
            if doc_key and doc_key in by_doc
        ]
        found_companies = [realm_names.get(rid) or rid for rid in found_realm_ids]

        # When the invoice is present in the expected company, compare the QBO
        # total against the export amount. Multiple matches (same DocNumber) are
        # treated as matching if any one is within tolerance.
        qbo_amount: float | None = None
        amount_ok = True
        if expected_realm_id and expected_realm_id in found_realm_ids:
            matches = realm_maps.get(expected_realm_id, {}).get(doc_key) or []
            qbo_totals = [t for t in (_qbo_total(m) for m in matches) if t is not None]
            if qbo_totals:
                qbo_amount = min(
                    qbo_totals, key=lambda total: abs(total - amount)
                )
                amount_ok = abs(qbo_amount - amount) <= _AUDIT_AMOUNT_TOLERANCE

        if not expected_realm_id:
            status = _AUDIT_UNRESOLVED
        elif expected_realm_id not in realm_maps:
            status = _AUDIT_NOT_CHECKED
        elif expected_realm_id in found_realm_ids:
            others = [rid for rid in found_realm_ids if rid != expected_realm_id]
            if not amount_ok:
                status = _AUDIT_AMOUNT_MISMATCH
            elif others:
                status = _AUDIT_DUPLICATE
            else:
                status = _AUDIT_OK
        elif found_realm_ids:
            status = _AUDIT_WRONG_COMPANY
        else:
            status = _AUDIT_MISSING

        counts[status] = counts.get(status, 0) + 1
        rows.append(
            {
                "Doc #": doc,
                "Txn Date": txn_date,
                "Division": division,
                "Customer": customer,
                "Export amount": round(amount, 2),
                "QBO amount": round(qbo_amount, 2) if qbo_amount is not None else None,
                "Expected company": expected_company,
                "Found in": ", ".join(sorted(found_companies)) if found_companies else "(none)",
                "Status": status,
            }
        )

    anomalies = [row for row in rows if row["Status"] != _AUDIT_OK]
    return {
        "source_hash": preview.source_hash,
        "source_file": preview.source_file,
        "checked_count": len(rows),
        "date_window": (min_date, max_date),
        "counts": counts,
        "rows": rows,
        "anomalies": anomalies,
        "query_errors": query_errors,
    }


def _render_invoice_audit_result(preview: PreviewResult) -> None:
    """Render the latest invoice audit, if it matches the current preview."""
    result = st.session_state.get(QBO_INVOICE_AUDIT_KEY)
    if not isinstance(result, dict):
        return
    if result.get("source_hash") != preview.source_hash:
        st.session_state.pop(QBO_INVOICE_AUDIT_KEY, None)
        return

    counts = result.get("counts") or {}
    anomalies = list(result.get("anomalies") or [])
    query_errors = list(result.get("query_errors") or [])
    checked = int(result.get("checked_count") or 0)

    with st.container(border=True):
        header_cols = st.columns([0.4, 0.6])
        with header_cols[0]:
            st.markdown("**Import audit vs. QuickBooks**")
        with header_cols[1]:
            window = result.get("date_window") or ("", "")
            if window[0] and window[1]:
                st.caption(f"Checked TxnDate {window[0]} → {window[1]} across all connected companies.")

        metric_cols = st.columns(6)
        metric_cols[0].metric("Rows", checked)
        metric_cols[1].metric("Imported OK", counts.get(_AUDIT_OK, 0))
        metric_cols[2].metric("Missing", counts.get(_AUDIT_MISSING, 0))
        metric_cols[3].metric("Amount mismatch", counts.get(_AUDIT_AMOUNT_MISMATCH, 0))
        metric_cols[4].metric(
            "Wrong company",
            counts.get(_AUDIT_WRONG_COMPANY, 0) + counts.get(_AUDIT_DUPLICATE, 0),
        )
        metric_cols[5].metric("Anomalies", len(anomalies))

        critical = (
            counts.get(_AUDIT_MISSING, 0)
            + counts.get(_AUDIT_AMOUNT_MISMATCH, 0)
        )
        warn_only = (
            counts.get(_AUDIT_WRONG_COMPANY, 0)
            + counts.get(_AUDIT_DUPLICATE, 0)
        )

        if query_errors:
            st.error(
                "Some companies could not be queried, so their rows show as "
                "\u201cNot checked\u201d. Results below may be incomplete."
            )
            with st.expander("Lookup errors", expanded=False):
                st.dataframe(
                    [{"Company": item.get("company"), "Error": item.get("error")} for item in query_errors],
                    hide_index=True,
                    use_container_width=True,
                )

        if not anomalies and not query_errors:
            st.success(
                "All rows are imported into the company their Division resolves to, "
                "and amounts match QuickBooks. No anomalies found."
            )
            return

        if critical:
            st.error(
                f"\U0001F534 {critical} critical row(s): missing imports or amount "
                "mismatches. These must be fixed."
            )
        if warn_only:
            st.warning(
                f"\U0001F7E0 {warn_only} row(s) imported into a different company "
                "than their Division resolves to."
            )

        if anomalies:
            with st.expander("Anomaly details", expanded=True):
                st.dataframe(anomalies, hide_index=True, use_container_width=True)

        with st.expander("Full audit (all rows)", expanded=False):
            st.dataframe(result.get("rows") or [], hide_index=True, use_container_width=True)


# =============================================================================
# Invoice multi-company routing summary
# =============================================================================

def _render_invoice_routing_summary(preview: PreviewResult, realms: list[ConnectedRealm]) -> bool:
    """Show compact Division routing immediately after setup.

    Invoices are intentionally Division-only: there is no company picker.
    Blank or unmatched divisions are preview errors and must be fixed
    in the source file before posting.
    """
    drafts = preview.drafts or []
    if not drafts:
        return False

    counts: dict[tuple[str, str, str], int] = {}
    unresolved = 0
    for draft in drafts:
        realm_id = str(draft.get("_realmId") or "").strip()
        division = str(draft.get("_division") or "").strip() or "(blank)"
        if not realm_id:
            unresolved += 1
            key = (division, "(unmatched)", "")
        else:
            key = (division, _realm_name_for_id(realms, realm_id) or realm_id, realm_id)
        counts[key] = counts.get(key, 0) + 1

    resolved_realms = {realm_id for (_division, _company, realm_id), _count in counts.items() if realm_id}
    multi_realm = len(resolved_realms) > 1
    with st.container(border=True):
        summary_cols = st.columns([0.28, 0.18, 0.18, 0.36])
        with summary_cols[0]:
            st.markdown("**Division routing**")
        with summary_cols[1]:
            st.metric("Rows", len(drafts))
        with summary_cols[2]:
            st.metric("Companies", len(resolved_realms))
        with summary_cols[3]:
            if unresolved:
                st.error(f"{unresolved} unmatched Division row(s). Fix and re-preview.")
            elif multi_realm:
                st.caption("Multi-company file detected; each row posts to its matched company.")
            else:
                st.caption("All rows resolve to one QBO company.")

        table_rows = [
            {
                "Division": division,
                "QBO company": company,
                "Rows": count,
            }
            for (division, company, _realm_id), count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0]))
        ]
        with st.expander("View routing details", expanded=bool(unresolved)):
            st.dataframe(table_rows, hide_index=True, use_container_width=True)
    return multi_realm


# =============================================================================
# Parking PK view (Prestig Inc only)
# =============================================================================

def _render_parking_pk(
    supabase: SupabaseRestClient,
    token_repo: QboTokenRepository,
    auth_service: QboAuthService,
    email: str,
) -> None:
    back_col, title_col = st.columns([0.18, 0.82])
    with back_col:
        if st.button("\u2190 Templates", use_container_width=True, key="parking_back"):
            _go_to("templates")
            return
    with title_col:
        st.title("\U0001F17F\uFE0F Parking PK \u2014 Prestig Inc only")

    st.caption(
        "Scans posted invoices on **Prestig Inc** for ones that include the "
        "**Parking** line item and appends 'PK' to their DocNumber. "
        "Does **not** touch Prestige Transportation Inc or Xpress Trans Inc."
    )

    qbo_client = QboClient(auth_service)
    lookups = EntityLookupService(qbo_client)
    audit = SupabaseAuditLog(
        supabase,
        imported_by_email=email,
        source_file_name="ParkingPK",
        source_hash="",
    )
    service = ParkingPkService(qbo_client, token_repo, lookups, audit)

    prestig_realm_id = service.resolve_prestig_realm()
    if not prestig_realm_id:
        st.error(
            "No connected QuickBooks company normalizes to 'Prestig Inc'. "
            "Open Settings \u2014 Companies and connect 'Prestig Inc' (note: no 'e' at the end)."
        )
        return
    st.success(f"Resolved Prestig Inc \u2014 realm {prestig_realm_id}")

    today = date.today()
    default_start = today.replace(day=1)
    col_start, col_end = st.columns(2)
    with col_start:
        start_date = st.date_input("Start date", value=default_start, key="parking_start_date")
    with col_end:
        end_date = st.date_input("End date", value=today, key="parking_end_date")

    if st.button("\U0001F50D Scan for Parking invoices", type="primary", key="parking_scan"):
        try:
            with st.spinner("Scanning Prestig Inc invoices\u2026"):
                scan = service.find_matches(start_date.isoformat(), end_date.isoformat())
        except Exception as exc:  # noqa: BLE001 - user-facing
            logger.exception("Parking PK scan failed")
            st.error(f"Scan failed: {exc}")
            return
        st.session_state[QBO_PARKING_SCAN_KEY] = scan
        st.session_state[QBO_PARKING_SELECTION_KEY] = {m.invoice_id for m in scan.matches}

    scan = st.session_state.get(QBO_PARKING_SCAN_KEY)
    if not isinstance(scan, ParkingScanResult):
        return

    if not scan.matches:
        st.info("No invoices found that contain the Parking item and don't already end in 'PK'.")
        return

    st.markdown(f"**{len(scan.matches)}** invoice(s) eligible for DocNumber update.")
    selected_ids: set[str] = set(st.session_state.get(QBO_PARKING_SELECTION_KEY) or set())
    rows = [
        {
            "Apply": match.invoice_id in selected_ids,
            "Doc #": match.doc_number,
            "Proposed": match.proposed_doc_number,
            "Customer": match.customer_name,
            "Date": match.txn_date,
            "Amount": float(match.amount),
            "_id": match.invoice_id,
        }
        for match in scan.matches
    ]
    edited = st.data_editor(
        rows,
        key="parking_table",
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "_id": None,
            "Apply": st.column_config.CheckboxColumn("Apply"),
            "Doc #": st.column_config.TextColumn(disabled=True),
            "Proposed": st.column_config.TextColumn(disabled=True),
            "Customer": st.column_config.TextColumn(disabled=True),
            "Date": st.column_config.TextColumn(disabled=True),
            "Amount": st.column_config.NumberColumn(disabled=True, format="$ %.2f"),
        },
    )
    try:
        edited_rows = edited.to_dict("records")  # type: ignore[attr-defined]
    except AttributeError:
        edited_rows = list(edited or [])
    selected_ids = {str(r.get("_id")) for r in edited_rows if r.get("Apply")}
    st.session_state[QBO_PARKING_SELECTION_KEY] = selected_ids

    st.divider()
    st.warning(
        ":warning: This **mutates posted invoices** on Prestig Inc. "
        "The change cannot be undone from this app \u2014 only by manually editing each "
        "invoice's DocNumber in QuickBooks."
    )

    if st.button(
        f"Apply DocNumber update to {len(selected_ids)} invoice(s)",
        type="primary",
        disabled=not selected_ids,
        key="parking_apply",
    ):
        chosen = [m for m in scan.matches if m.invoice_id in selected_ids]
        try:
            with st.spinner("Updating invoice DocNumbers\u2026"):
                start_ts = datetime.now(tz=timezone.utc)
                result = service.apply_matches(scan.realm_id, chosen)
                duration_ms = int((datetime.now(tz=timezone.utc) - start_ts).total_seconds() * 1000)
        except Exception as exc:  # noqa: BLE001 - user-facing
            logger.exception("Parking PK apply failed")
            st.error(f"Apply failed: {exc}")
            return

        st.success(
            f"Done: updated {result.updated}, skipped {result.skipped}, failed {result.failed}."
        )
        if result.errors:
            with st.expander(f"View {len(result.errors)} error(s)"):
                for line in result.errors:
                    st.code(line)

        # Mirror to Google Sheets ImportLog so it shows up next to regular imports.
        try:
            sheets_log = GoogleSheetsImportLog()
            sheets_log.append_summary(
                user_email=email,
                action="PARKING_PK",
                template="Parking PK",
                company="Prestig Inc",
                realm_id=scan.realm_id,
                source_sheet=f"Parking PK {start_date.isoformat()}..{end_date.isoformat()}",
                source_count=len(chosen),
                success=result.updated,
                failed=result.failed,
                skipped=result.skipped,
                duration_ms=duration_ms,
                execution_id="",
                errors=list(result.errors or []),
            )
        except Exception as exc:  # noqa: BLE001 - never break the flow
            logger.warning("ImportLog (Parking PK) write failed: %s", exc)

        st.session_state.pop(QBO_PARKING_SCAN_KEY, None)
        st.session_state.pop(QBO_PARKING_SELECTION_KEY, None)
