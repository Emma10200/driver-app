from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
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
from qbo.parsers import DriverStatementParser, InvoiceParser, MoneyCodeParser
from services.qbo_audit import SupabaseAuditLog, source_file_hash
from services.qbo_auth import QboAuthService, QboTokenRepository, qbo_allowed_emails
from services.qbo_supabase import SupabaseQboError, SupabaseRestClient

logger = logging.getLogger(__name__)

GOOGLE_AUTH_PROVIDER = "google"
QBO_OAUTH_STATE_KEY = "qbo_oauth_state"
QBO_PREVIEW_KEY = "qbo_import_preview"
QBO_UPLOAD_HASH_KEY = "qbo_upload_hash"
_DATE_USE_ROW = "Use row dates (or most recent Friday)"
_TEMPLATE_OPTIONS = {
    "invoices": "Invoices",
    "driver_statements": "Driver Statements / Checks",
    "money_codes": "Money Codes / EFS Fuel Card",
}


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
        """[auth]
redirect_uri = "https://driver-application.streamlit.app/oauth2callback"
cookie_secret = "generate-a-long-random-string"

[auth.google]
client_id = "your-google-oauth-client-id.apps.googleusercontent.com"
client_secret = "your-google-oauth-client-secret"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration""".strip(),
        language="toml",
    )
    st.caption(
        "Also add https://driver-application.streamlit.app/oauth2callback to the "
        "Google Cloud OAuth client's Authorized redirect URIs."
    )


def _date_options() -> list[str]:
    today = datetime.now()
    days_since_friday = (today.weekday() - 4) % 7
    friday = today - timedelta(days=days_since_friday)
    return [_DATE_USE_ROW] + [(friday - timedelta(days=7 * i)).strftime("%Y-%m-%d") for i in range(6)]


def _format_amount(value: Any) -> str:
    try:
        return f"${float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _draft_amount(draft: dict[str, Any]) -> float:
    total = 0.0
    for line in draft.get("Line") or []:
        try:
            total += float((line or {}).get("Amount") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _realm_options(realms: list[ConnectedRealm]) -> dict[str, ConnectedRealm]:
    return {f"{realm.company_name} ({realm.realm_id})": realm for realm in realms}


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
            _render_streamlit_auth_help(
                "Streamlit rejected the current auth configuration. Check the app logs for the "
                f"full provider error. Summary: {type(exc).__name__}"
            )


def render_qbo_dashboard() -> None:
    if not _qbo_access_granted():
        _render_login()
        st.stop()

    st.title("QBO Importer")
    st.caption("Shared accounting import history powered by Supabase + QuickBooks Online.")

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
    col1, col2 = st.columns([0.75, 0.25])
    with col1:
        st.caption(f"Signed in as {email}")
    with col2:
        if st.button("Sign out", use_container_width=True):
            st.logout()

    tabs = st.tabs(["Connect companies", "Import", "History"])
    with tabs[0]:
        _render_connections(auth_service)
    with tabs[1]:
        _render_importer(supabase, token_repo, auth_service, email)
    with tabs[2]:
        _render_history(supabase)


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
) -> None:
    realms = token_repo.list_realms()
    if not realms:
        st.warning("Connect at least one QuickBooks company before importing.")
        return

    template_label = st.radio("Import template", list(_TEMPLATE_OPTIONS.values()), horizontal=True)
    template_key = next(key for key, label in _TEMPLATE_OPTIONS.items() if label == template_label)

    options = _realm_options(realms)
    selected_realm_label = st.selectbox("Target company", list(options.keys()), key="qbo_target_company")
    selected_realm = options[selected_realm_label]

    bank_account_name = ""
    override_date = ""
    if template_key == "driver_statements":
        bank_account_name = st.text_input(
            "Bank account for checks",
            value=selected_realm.default_bank_account_name,
            help="This should be the target company's bank account name in QBO. It will be saved as that company's default.",
        )
        selected_date = st.selectbox("Override check date", _date_options())
        override_date = "" if selected_date == _DATE_USE_ROW else selected_date
    elif template_key == "money_codes":
        st.info("Money Codes post as CreditCard Purchases to the per-row CC Account. Only Fuel Card - EFS rows are imported.")

    uploaded = st.file_uploader("Upload CSV or Excel file", type=["csv", "xlsx", "xlsm", "xls"])
    if not uploaded:
        st.caption("Choose a file to preview before posting anything to QBO.")
        return

    content = uploaded.getvalue()
    upload_hash = source_file_hash(content)
    if st.session_state.get(QBO_UPLOAD_HASH_KEY) != upload_hash:
        st.session_state.pop(QBO_PREVIEW_KEY, None)
        st.session_state[QBO_UPLOAD_HASH_KEY] = upload_hash

    preview_col, clear_col = st.columns([0.7, 0.3])
    with preview_col:
        if st.button("Preview file", type="primary", use_container_width=True):
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
                st.session_state[QBO_PREVIEW_KEY] = preview
    with clear_col:
        if st.button("Clear preview", use_container_width=True):
            st.session_state.pop(QBO_PREVIEW_KEY, None)
            st.rerun()

    preview = st.session_state.get(QBO_PREVIEW_KEY)
    if not isinstance(preview, PreviewResult):
        return

    _render_preview(preview)
    if preview.errors:
        st.warning("Fix preview errors before importing.")
        return
    if not preview.drafts:
        st.warning("No importable rows were found.")
        return

    st.divider()
    st.warning("Posting creates real financial records in QuickBooks. Confirm only after reviewing the table above.")
    if st.button("Confirm and post to QBO", type="primary", use_container_width=True):
        if template_key == "driver_statements" and bank_account_name:
            token_repo.save_realm_settings(
                realm_id=selected_realm.realm_id,
                company_name=selected_realm.company_name,
                environment=selected_realm.environment,
                default_bank_account_name=bank_account_name,
                default_money_code_cc_account_name=selected_realm.default_money_code_cc_account_name,
                connected_by_email=selected_realm.connected_by_email,
            )
        audit = SupabaseAuditLog(
            supabase,
            imported_by_email=email,
            source_file_name=uploaded.name,
            source_hash=upload_hash,
        )
        qbo_client = QboClient(auth_service)
        import_service = ImportService(
            qbo_client,
            EntityLookupService(qbo_client),
            DuplicateChecker(qbo_client),
            audit,
        )
        with st.spinner("Posting to QuickBooks… please keep this tab open."):
            if template_key == "invoices":
                stats = import_service.post_invoices(preview.drafts, target_realm_id=selected_realm.realm_id)
            elif template_key == "driver_statements":
                stats = import_service.post_checks(preview.drafts, target_realm_id=selected_realm.realm_id)
            else:
                stats = import_service.post_money_codes(preview.drafts, target_realm_id=selected_realm.realm_id)
        st.success(f"Done: posted {stats.posted}, duplicates {stats.skipped_duplicates}, failed {stats.failed}.")
        _render_import_stats(stats)
        st.session_state.pop(QBO_PREVIEW_KEY, None)


def _build_preview(
    *,
    template_key: str,
    file_name: str,
    content: bytes,
    realms: list[ConnectedRealm],
    selected_realm: ConnectedRealm,
    bank_account_name: str,
    override_date: str,
) -> PreviewResult:
    rows = FileLoader().load_rows_from_bytes(file_name, content)
    if template_key == "invoices":
        parser = InvoiceParser(CompanyDirectory(realms))
        parsed = parser.parse(rows)
        drafts = parser.build_qbo_drafts(parsed)
        preview_rows = [
            {
                "Doc #": row["doc_number"],
                "Date": row["txn_date"],
                "Customer": row["customer_name"],
                "Amount": row["amount"],
                "Division": row["division_name"] or selected_realm.company_name,
                "Realm ID": row["realm_id"] or selected_realm.realm_id,
            }
            for row in parsed.get("rows") or []
        ]
        warnings = [f"Row {item.get('row_number')}: {item.get('reason')}" for item in parsed.get("skipped_rows") or []]
        return PreviewResult(
            template_type=template_key,
            source_file=file_name,
            source_hash=source_file_hash(content),
            count=len(drafts),
            source_count=max(len(rows) - 1, 0),
            skipped_count=len(parsed.get("skipped_rows") or []),
            rows=preview_rows,
            warnings=warnings,
            drafts=drafts,
        )

    if template_key == "driver_statements":
        parsed = DriverStatementParser().parse(
            rows,
            target_realm_id=selected_realm.realm_id,
            target_division=selected_realm.company_name,
            bank_account_name=bank_account_name,
            override_txn_date=override_date,
        )
        drafts = parsed.get("checks") or []
        preview_rows = [
            {
                "Doc #": draft.get("DocNumber"),
                "Date": draft.get("TxnDate"),
                "Vendor": draft.get("_tempVendorName"),
                "Lines": len(draft.get("Line") or []),
                "Total": _draft_amount(draft),
                "Bank Account": (draft.get("AccountRef") or {}).get("name") or "",
            }
            for draft in drafts
        ]
        return PreviewResult(
            template_type=template_key,
            source_file=file_name,
            source_hash=source_file_hash(content),
            count=len(drafts),
            source_count=max(len(rows) - 1, 0),
            skipped_count=len(parsed.get("skipped_rows") or []),
            rows=preview_rows,
            errors=list(parsed.get("errors") or []),
            warnings=list(parsed.get("warnings") or []),
            drafts=drafts,
        )

    parsed = MoneyCodeParser().parse(rows, target_realm_id=selected_realm.realm_id)
    drafts = parsed.get("expenses") or []
    preview_rows = [
        {
            "Code": draft.get("DocNumber"),
            "Date": draft.get("TxnDate"),
            "Vendor": draft.get("_tempVendorName"),
            "CC Account": draft.get("_tempCcAccountName"),
            "Amount": _draft_amount(draft),
            "Expense Account": ((draft.get("Line") or [{}])[0] or {}).get("_tempAccountName"),
        }
        for draft in drafts
    ]
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
    st.subheader("Preview")
    st.caption(
        f"{_TEMPLATE_OPTIONS.get(preview.template_type, preview.template_type)} — "
        f"{preview.count} importable rows from {preview.source_file}"
    )
    if preview.rows:
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


def _render_history(supabase: SupabaseRestClient) -> None:
    st.subheader("Recent QBO import history")
    limit = st.slider("Rows", min_value=25, max_value=500, value=100, step=25)
    audit = SupabaseAuditLog(supabase, imported_by_email=_google_user_email())
    try:
        rows = audit.recent(limit=limit)
    except Exception as exc:  # noqa: BLE001 - history should not crash the page
        st.error(f"Could not load QBO history: {exc}")
        return
    if not rows:
        st.info("No QBO audit rows yet.")
        return
    st.dataframe(rows, use_container_width=True, hide_index=True)
