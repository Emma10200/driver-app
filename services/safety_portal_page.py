"""Safety Paperwork Portal — staff page.

Renders the SSO-gated upload form for ProTransport exports and produces
a preview of who would be contacted and which rows need staff review.
The send queue uses checkbox rows so staff can exclude specific items
before sending real emails.
"""

from __future__ import annotations

import base64
import html
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import streamlit as st

from config import COMPANY_PROFILES
from services.notification_service import send_safety_document_request_email
from services.qbo_auth import qbo_allowed_emails
from services.safety_ledger import (
    annotate_rows_for_send_queue,
    backfill_safety_ledger,
    ledger_history_source_counts,
    ledger_summary,
    list_ledger_records,
    record_send_event,
    upsert_import_rows,
)
from services.safety_inbox import (
    assign_unmatched_reply,
    ingest_all,
    list_unmatched_replies,
    load_inbox_mailboxes,
)
from services.safety_link_store import (
    create_safety_upload_link,
    link_history_source_counts,
    list_safety_upload_links,
    record_outbound_message_id,
)
from services.safety_paperwork import (
    DOC_TYPE_LABELS,
    EXCLUDED_FROM_OUTBOUND,
    ImportPreview,
    RecipientBundle,
    ReviewIssue,
    build_preview,
    load_driver_details,
    load_truck_owner_details,
)
from services.safety_reference_db import (
    UpsertResult,
    delete_drivers,
    delete_trucks,
    list_driver_records,
    list_truck_records,
    load_drivers,
    load_trucks,
    reference_summary,
    upsert_drivers,
    upsert_trucks,
)
from submission_storage import read_supporting_document_bytes


def _mapping_get(mapping: Any, key: str) -> Any:
    try:
        return mapping[key]  # type: ignore[index]
    except Exception:
        return getattr(mapping, key, None)


def _user_logged_in() -> bool:
    user = getattr(st, "user", None)
    return bool(user and getattr(user, "is_logged_in", False))


def _user_email() -> str:
    user = getattr(st, "user", None)
    if not user:
        return ""
    return str(_mapping_get(user, "email") or getattr(user, "email", "") or "").strip().lower()


def _access_granted() -> bool:
    email = _user_email()
    return bool(_user_logged_in() and email and email in qbo_allowed_emails())


def _render_sso_gate() -> bool:
    """Return True if the user is allowed to use the page; otherwise render
    the gate UI and return False."""
    if _access_granted():
        return True

    st.title("🛡️ Safety Paperwork Portal")
    if not hasattr(st, "login") or not hasattr(st, "user"):
        st.error("This Streamlit version does not support native Google login.")
        return False

    if not _user_logged_in():
        st.info("Sign in with your company Google account to continue.")
        if st.button("Sign in with Google", type="primary"):
            try:
                st.login("google")
            except Exception:  # pragma: no cover - exercised in deployed env
                st.login()
        return False

    email = _user_email() or "(unknown)"
    st.error(f"Account {email} is not on the safety portal allowlist.")
    st.caption(
        "Access is granted to the same emails listed in the QBO importer "
        "allowlist (qbo.allowed_emails / QBO_ALLOWED_EMAILS)."
    )
    if st.button("Sign out"):
        try:
            st.logout()
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_STATUS_BADGES = {
    "expired": "🔴 Expired",
    "expiring_soon": "🟡 Expiring soon",
    "missing": "⚪ Missing",
    "ok": "🟢 OK",
}

_PUBLIC_APP_URL = "https://driver-application.streamlit.app"


def _app_link(query: str = "") -> str:
    return f"{_PUBLIC_APP_URL}/{query}" if query else f"{_PUBLIC_APP_URL}/"


def _render_safety_sidebar_links() -> None:
    """Safety-portal-only sidebar shortcuts for staff."""
    with st.sidebar:
        st.header("🛡️ Safety links")
        st.caption("Quick copy/open links for safety workflows.")

        st.markdown("**Uploads & portals**")
        st.link_button("Driver document upload", _app_link("?documents=1"), use_container_width=True)
        st.code(_app_link("?documents=1"), language=None)
        st.link_button("Safety paperwork portal", _app_link("?safety=1"), use_container_width=True)

        st.markdown("**Staff tools**")
        st.link_button("Admin dashboard", _app_link("?dashboard=1"), use_container_width=True)
        st.link_button("QBO importer", _app_link("?qbo=1"), use_container_width=True)

        st.markdown("**Driver application links**")
        for slug, profile in COMPANY_PROFILES.items():
            st.link_button(profile.name, _app_link(f"?company={slug}"), use_container_width=True)

        st.caption(
            "Recipient-specific safety upload links are generated from the Warnings & send tab "
            "after you build a warnings preview."
        )


def _format_item_row(item) -> dict[str, str]:
    return {
        "Unit": item.unit_no or "—",
        "Document": DOC_TYPE_LABELS.get(item.doc_type, item.doc_type),
        "Expires": item.expiration_date.isoformat() if item.expiration_date else "—",
        "Status": _STATUS_BADGES.get(item.status, item.status),
    }


def _editor_records(value: Any) -> list[dict[str, Any]]:
    try:
        records = value.to_dict("records")  # type: ignore[attr-defined]
    except AttributeError:
        records = list(value or [])
    return [dict(row) for row in records if isinstance(row, Mapping)]


def _render_summary(preview: ImportPreview) -> None:
    cols = st.columns(4)
    cols[0].metric("Driver warnings", preview.driver_warning_rows)
    cols[1].metric("Truck warnings", preview.truck_warning_rows)
    cols[2].metric("Recipients ready", len(preview.recipients))
    blockers = sum(1 for r in preview.review if r.severity == "blocker")
    cols[3].metric("Review queue", blockers, delta=f"{len(preview.review) - blockers} warnings")


def _send_queue_rows(recipients: list[RecipientBundle]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bundle in recipients:
        kind_label = {
            "driver": "Driver",
            "owner": "Owner",
            "driver_owner": "Owner-operator",
        }.get(bundle.kind, bundle.kind)
        for index, item in enumerate(bundle.items):
            formatted = _format_item_row(item)
            rows.append(
                {
                    "Include": True,
                    "Recipient": bundle.display_name,
                    "Kind": kind_label,
                    "Division": bundle.division,
                    "Email": bundle.email,
                    "Unit": formatted["Unit"],
                    "Document": formatted["Document"],
                    "Expires": formatted["Expires"],
                    "Status": formatted["Status"],
                    "_status": item.status,
                    "_recipient_key": bundle.recipient_key,
                    "_row_id": f"{bundle.recipient_key}::{index}::{item.doc_type}::{item.unit_no or 'driver'}",
                }
            )
    return rows


def _group_selected_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not bool(row.get("Include")):
            continue
        email = str(row.get("Email") or "").strip().lower()
        if not email:
            continue
        bundle = grouped.setdefault(
            email,
            {
                "email": email,
                "recipient_name": str(row.get("Recipient") or "Driver/Owner").strip(),
                "division": str(row.get("Division") or "").strip(),
                "items": [],
            },
        )
        bundle["items"].append(
            {
                "item_key": row.get("_item_key"),
                "unit": row.get("Unit"),
                "document": row.get("Document"),
                "expires": row.get("Expires"),
                "status": row.get("Status"),
            }
        )
    return grouped


def _sent_history_rows_from_links(
    links: list[dict[str, Any]],
    *,
    division_filter: str = "All",
    query: str = "",
) -> list[dict[str, Any]]:
    """Build sent-history rows from saved outbound safety upload links."""
    rows: list[dict[str, Any]] = []
    query = query.strip().lower()
    for link in links:
        division = str(link.get("division") or "").strip()
        if division_filter != "All" and (division or "(no division)") != division_filter:
            continue
        base = {
            "Source": "Saved email link",
            "Sent at": link.get("created_at") or "",
            "Sent to": link.get("recipient_email") or "",
            "Recipient": link.get("recipient_name") or "Driver/Owner",
            "Division": division,
            "Ref": link.get("ref_code") or "",
            "Link expires": link.get("expires_at") or "",
            "Expired link?": "Yes" if link.get("expired") else "No",
            "Token": link.get("token") or "",
        }
        items = [dict(item) for item in (link.get("items") or []) if isinstance(item, Mapping)] or [{}]
        for item in items:
            row = {
                **base,
                "Unit": item.get("unit") or item.get("Unit") or "—",
                "Document": item.get("document") or item.get("Document") or "Document",
                "Expires": item.get("expires") or item.get("Expires") or "—",
                "Status when sent": item.get("status") or item.get("Status") or "",
            }
            haystack = " ".join(str(value or "") for value in row.values()).lower()
            if query and query not in haystack:
                continue
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("Sent at") or ""), reverse=True)
    return rows


def _history_query_matches(row: dict[str, Any], query: str) -> bool:
    query = query.strip().lower()
    if not query:
        return True
    haystack = " ".join(str(value or "") for value in row.values()).lower()
    return query in haystack


def _sent_history_rows_from_ledger_records(
    records: list[dict[str, Any]],
    *,
    division_filter: str = "All",
    query: str = "",
    skip_tokens: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fallback sent-history rows from ledger records.

    Older environments may have upload/submission ledger rows even when the
    original saved link metadata is missing. Show those rows with a clear source
    so staff can still see who was contacted / who used a safety link.
    """
    skip_tokens = skip_tokens or set()
    rows: list[dict[str, Any]] = []
    for record in records:
        division = str(record.get("division") or "").strip()
        if division_filter != "All" and (division or "(no division)") != division_filter:
            continue
        base = {
            "Sent to": record.get("last_sent_to") or record.get("recipient_email") or "",
            "Recipient": record.get("recipient_name") or "Driver/Owner",
            "Division": division,
            "Unit": record.get("unit") or "—",
            "Document": record.get("document") or "Document",
            "Expires": record.get("expires") or "—",
            "Status when sent": record.get("status") or "",
            "Last upload": record.get("last_upload_display") or record.get("last_upload_at") or "",
            "Token": record.get("last_link_token") or record.get("last_upload_token") or "",
        }
        events = [dict(event) for event in (record.get("send_events") or []) if isinstance(event, Mapping)]
        if events:
            for event in events:
                token = str(event.get("token") or base["Token"] or "")
                if token and token in skip_tokens:
                    continue
                row = {
                    **base,
                    "Source": "Ledger send event",
                    "Sent at": event.get("sent_at") or record.get("last_sent_at") or "",
                    "Token": token,
                }
                if _history_query_matches(row, query):
                    rows.append(row)
            continue
        if record.get("last_sent_at") or int(record.get("send_count") or 0) > 0:
            token = str(base["Token"] or "")
            if token and token in skip_tokens:
                continue
            row = {**base, "Source": "Ledger send summary", "Sent at": record.get("last_sent_at") or ""}
            if _history_query_matches(row, query):
                rows.append(row)
            continue
        if record.get("uploads"):
            token = str(base["Token"] or "")
            if token and token in skip_tokens:
                continue
            row = {
                **base,
                "Source": "Submitted upload (original send record unavailable)",
                "Sent at": "",
            }
            if _history_query_matches(row, query):
                rows.append(row)
    rows.sort(key=lambda row: str(row.get("Sent at") or row.get("Last upload") or ""), reverse=True)
    return rows


def _send_recipient_bundles(
    grouped: dict[str, dict[str, Any]], submissions_dir: Path
) -> None:
    """Create upload links, send the request emails, and display the results.

    Shared by the normal send queue and the missing-email recovery section so
    both code paths record links/send events identically.
    """
    results: list[dict[str, Any]] = []
    progress = st.progress(0, text="Sending safety paperwork emails...")
    total = max(1, len(grouped))
    for idx, bundle in enumerate(grouped.values(), start=1):
        link = create_safety_upload_link(
            submissions_dir=submissions_dir,
            recipient_email=bundle["email"],
            recipient_name=bundle["recipient_name"],
            division=bundle["division"],
            items=bundle["items"],
        )
        result = send_safety_document_request_email(
            to_email=bundle["email"],
            recipient_name=bundle["recipient_name"],
            division=bundle["division"],
            items=bundle["items"],
            upload_url=str(link.get("url") or ""),
            token=str(link.get("token") or ""),
            ref_code=str(link.get("ref_code") or ""),
        )
        if result.get("status") == "sent":
            message_id = str(result.get("message_id") or "")
            if message_id:
                record_outbound_message_id(
                    submissions_dir=submissions_dir,
                    token=str(link.get("token") or ""),
                    message_id=message_id,
                )
            record_send_event(
                submissions_dir,
                recipient_email=bundle["email"],
                recipient_name=bundle["recipient_name"],
                division=bundle["division"],
                items=bundle["items"],
                token=str(link.get("token") or ""),
            )
        results.append(
            {
                "Email": bundle["email"],
                "Status": result["status"],
                "Message": result["message"],
                "Unique link": link.get("url"),
            }
        )
        progress.progress(idx / total, text=f"Sent {idx} of {total} email(s)...")
    progress.empty()
    sent = sum(1 for r in results if r["Status"] == "sent")
    errors = [r for r in results if r["Status"] != "sent"]
    if sent:
        st.success(f"Sent {sent} safety paperwork request email(s).")
    if errors:
        st.error(f"{len(errors)} email(s) did not send. Review the result table below.")
    st.dataframe(results, hide_index=True, use_container_width=True)


def _render_send_queue(recipients: list[RecipientBundle], *, preview_version: int, submissions_dir: Path) -> None:
    if not recipients:
        st.info("No clean recipients to contact from this import.")
        return

    st.subheader("Email send queue")
    st.caption(
        "Uncheck any specific document rows you do NOT want included in the outgoing email. "
        "For example, uncheck IFTA if it is handled internally. Only checked rows are sent."
    )

    rows = annotate_rows_for_send_queue(submissions_dir, _send_queue_rows(recipients))

    expiring_soon_total = sum(1 for row in rows if str(row.get("_status") or "") == "expiring_soon")
    include_expiring = st.checkbox(
        "Send reminders for items that are only expiring soon (not yet expired)",
        value=True,
        key=f"safety_include_expiring_{preview_version}",
        help=(
            "When unchecked, rows still inside their grace window (🟡 Expiring soon) are "
            "excluded by default so this round only chases already-expired/missing items. "
            "You can still re-check any individual row below."
        ),
    )
    if expiring_soon_total:
        st.caption(
            f"This import has **{expiring_soon_total}** item(s) that are only expiring soon. "
            + ("They are included below." if include_expiring else "They are excluded by default below.")
        )
    if not include_expiring:
        for row in rows:
            if str(row.get("_status") or "") == "expiring_soon":
                row["Include"] = False

    edited = st.data_editor(
        rows,
        key=f"safety_send_queue_editor_{preview_version}_{int(include_expiring)}",
        hide_index=True,
        use_container_width=True,
        column_order=[
            "Include",
            "Recipient",
            "Kind",
            "Division",
            "Email",
            "Unit",
            "Document",
            "Expires",
            "Status",
            "Ledger status",
            "Last emailed",
            "Sent count",
            "Last upload",
            "Action note",
        ],
        disabled=[
            "Recipient",
            "Kind",
            "Division",
            "Email",
            "Unit",
            "Document",
            "Expires",
            "Status",
            "Ledger status",
            "Last emailed",
            "Sent count",
            "Last upload",
            "Action note",
        ],
        column_config={
            "Include": st.column_config.CheckboxColumn("Include in email?", default=True),
            "Email": st.column_config.TextColumn("Recipient email"),
        },
    )

    selected_rows = _editor_records(edited)
    grouped = _group_selected_rows(selected_rows)
    selected_item_count = sum(len(bundle["items"]) for bundle in grouped.values())
    st.info(
        f"Ready to send **{selected_item_count} selected document item(s)** "
        f"across **{len(grouped)} email(s)**."
    )

    st.warning(
        "This is the real send step. Clicking the send button below will send emails "
        "to the checked recipients using the matching division safety mailbox. "
        "Configured internal recipients are copied automatically."
    )
    confirm = st.checkbox(
        "I understand this will send real safety paperwork request emails.",
        key="safety_send_confirm",
    )
    send_disabled = not confirm or selected_item_count == 0
    if st.button(
        f"🚨 Send {len(grouped)} email(s) now",
        type="primary",
        disabled=send_disabled,
        key="safety_send_selected",
    ):
        _send_recipient_bundles(grouped, submissions_dir)


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(str(value or "").strip()))


def _format_blocker_source(source: Any) -> str:
    """Render a review source dict, hiding internal ``_``-prefixed keys."""
    if isinstance(source, Mapping):
        visible = {k: v for k, v in source.items() if not str(k).startswith("_")}
        return str(visible)
    return str(source)


def _summarize_items(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        unit = str(item.get("unit") or "").strip()
        document = str(item.get("document") or "Document").strip()
        parts.append(f"Unit {unit} · {document}" if unit else document)
    return "; ".join(parts) or "—"


def _render_missing_email_recovery(
    blockers: list[ReviewIssue], submissions_dir: Path, *, preview_version: int
) -> None:
    st.markdown("**Missing email — add an address and send**")
    st.caption(
        "These recipients were blocked only because no email is on file. Type an email "
        "next to each row, check Send, then confirm. Rows that share the same email are "
        "combined into a single message."
    )

    rows: list[dict[str, Any]] = []
    for idx, issue in enumerate(blockers):
        source = issue.source if isinstance(issue.source, Mapping) else {}
        items = [dict(i) for i in (source.get("_items") or []) if isinstance(i, Mapping)]
        kind_label = {"driver": "Driver", "owner": "Owner"}.get(
            str(source.get("_kind") or ""), "Recipient"
        )
        rows.append(
            {
                "Send": False,
                "Recipient": str(source.get("_recipient_name") or "Driver/Owner"),
                "Kind": kind_label,
                "Division": str(source.get("_division") or ""),
                "Custom email": "",
                "Documents": _summarize_items(items),
                "_idx": idx,
            }
        )

    edited = st.data_editor(
        rows,
        key=f"safety_missing_email_editor_{preview_version}",
        hide_index=True,
        use_container_width=True,
        column_order=["Send", "Recipient", "Kind", "Division", "Custom email", "Documents"],
        disabled=["Recipient", "Kind", "Division", "Documents"],
        column_config={
            "Send": st.column_config.CheckboxColumn("Send?", default=False),
            "Custom email": st.column_config.TextColumn(
                "Custom email", help="Type the recipient's email address to send to."
            ),
        },
    )

    grouped: dict[str, dict[str, Any]] = {}
    invalid: list[str] = []
    for row_index, row in enumerate(_editor_records(edited)):
        if not bool(row.get("Send")):
            continue
        name = str(row.get("Recipient") or "Driver/Owner").strip()
        email = str(row.get("Custom email") or "").strip().lower()
        if not _looks_like_email(email):
            invalid.append(name or "(unnamed)")
            continue
        if row_index >= len(blockers):
            continue
        source = blockers[row_index].source if isinstance(blockers[row_index].source, Mapping) else {}
        items = [dict(i) for i in (source.get("_items") or []) if isinstance(i, Mapping)]
        bundle = grouped.setdefault(
            email,
            {
                "email": email,
                "recipient_name": name,
                "division": str(source.get("_division") or "").strip(),
                "items": [],
            },
        )
        bundle["items"].extend(items)

    if invalid:
        st.warning("Enter a valid email address for: " + ", ".join(invalid))

    item_count = sum(len(bundle["items"]) for bundle in grouped.values())
    if grouped:
        st.info(
            f"Ready to send **{item_count} document item(s)** across **{len(grouped)} email(s)** "
            "using the addresses you entered."
        )

    confirm = st.checkbox(
        "I understand this will send real safety paperwork request emails to the addresses I entered.",
        key=f"safety_missing_email_confirm_{preview_version}",
    )
    if st.button(
        f"📧 Send {len(grouped)} recovered email(s) now",
        type="primary",
        disabled=not confirm or not grouped,
        key=f"safety_missing_email_send_{preview_version}",
    ):
        _send_recipient_bundles(grouped, submissions_dir)


def _render_review(review: list[ReviewIssue], submissions_dir: Path, *, preview_version: int) -> None:
    if not review:
        st.success("No review issues found in this import.")
        return

    blockers = [r for r in review if r.severity == "blocker"]
    warnings = [r for r in review if r.severity != "blocker"]
    sendable = [r for r in blockers if isinstance(r.source, Mapping) and r.source.get("_sendable")]
    plain_blockers = [r for r in blockers if r not in sendable]

    st.subheader(f"Review queue ({len(review)})")
    st.caption(
        "Blockers are not in the selectable send queue. Warnings are informational — "
        "the row can still go out, but the data is worth a second look."
    )

    if sendable:
        _render_missing_email_recovery(sendable, submissions_dir, preview_version=preview_version)

    if plain_blockers:
        st.markdown("**Blockers (excluded from outbound)**")
        st.dataframe(
            [
                {"Category": r.category, "Message": r.message, "Source": _format_blocker_source(r.source)}
                for r in plain_blockers
            ],
            hide_index=True,
            use_container_width=True,
        )
    if warnings:
        st.markdown("**Warnings**")
        st.dataframe(
            [
                {"Category": r.category, "Message": r.message, "Source": _format_blocker_source(r.source)}
                for r in warnings
            ],
            hide_index=True,
            use_container_width=True,
        )


def _render_file_preview(file_name: str, content_type: str, content: bytes) -> None:
    if not content:
        st.warning("File bytes are not available from storage.")
        return
    lower_name = file_name.lower()
    if content_type == "application/pdf" or lower_name.endswith(".pdf"):
        encoded = base64.b64encode(content).decode("ascii")
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{encoded}" width="100%" height="520" style="border:1px solid #e5e7eb;border-radius:8px;"></iframe>',
            unsafe_allow_html=True,
        )
    elif content_type.startswith("image/") or lower_name.endswith((".png", ".jpg", ".jpeg")):
        st.image(content, caption=file_name, use_container_width=True)
    else:
        st.caption("Preview is not available for this file type. Use the download button.")


def _render_uploaded_documents(record: dict[str, Any], submissions_dir: Path) -> None:
    uploads = [dict(item) for item in (record.get("uploads") or []) if isinstance(item, dict)]
    if not uploads:
        return
    st.markdown("**Submitted file(s)**")
    for index, upload in enumerate(uploads, start=1):
        file_name = str(upload.get("file_name") or upload.get("stored_name") or f"document-{index}")
        content_type = str(upload.get("content_type") or "application/octet-stream")
        content = read_supporting_document_bytes(upload, local_base_dir=submissions_dir)
        cols = st.columns([3, 1])
        with cols[0]:
            st.markdown(
                f"{index}. **{html.escape(file_name)}**  \n"
                f"Type: `{html.escape(str(upload.get('document_type') or 'Document'))}` · "
                f"Submitted: `{html.escape(str(upload.get('submitted_at') or ''))}`"
            )
        with cols[1]:
            st.download_button(
                "Download",
                data=content or b"",
                file_name=file_name,
                mime=content_type,
                disabled=not bool(content),
                key=f"safety_download_{record.get('item_key')}_{index}_{upload.get('event_id')}",
                use_container_width=True,
            )
        if content:
            with st.expander(f"View {file_name}", expanded=False):
                _render_file_preview(file_name, content_type, content)
        else:
            st.warning(f"Could not load `{file_name}` from storage. It may only exist in a remote backend not currently configured.")


def _render_ledger_dashboard(submissions_dir: Path) -> None:
    st.subheader("Safety dashboard")
    backfill_result = backfill_safety_ledger(submissions_dir)
    summary = ledger_summary(submissions_dir, backfill=False)
    cols = st.columns(5)
    cols[0].metric("Ledger items", summary["total"])
    cols[1].metric("Recently sent", summary["recently_sent"])
    cols[2].metric("Needs nudge", summary["needs_nudge"])
    cols[3].metric("Submitted", summary["submitted"])
    cols[4].metric("Resolved", summary["resolved"])
    if backfill_result.get("sent_events") or backfill_result.get("upload_events"):
        st.caption(
            f"Backfilled {backfill_result.get('sent_events', 0)} historical send event(s) and "
            f"{backfill_result.get('upload_events', 0)} upload event(s) from saved links/manifests."
        )

    records = list_ledger_records(submissions_dir, backfill=False)
    if not records:
        st.info("No safety ledger records yet. Build a warnings preview or send a safety request to start the ledger.")
        return

    divisions = sorted({str(r.get("division") or "(no division)") for r in records})
    states = sorted({str(r.get("ledger_state") or "") for r in records if r.get("ledger_state")})
    c1, c2, c3 = st.columns([1.2, 1.2, 1.6])
    with c1:
        division_filter = st.selectbox("Division", ["All"] + divisions, key="safety_ledger_division_filter")
    with c2:
        state_filter = st.multiselect(
            "Status",
            states,
            default=[s for s in states if s != "Resolved"],
            key="safety_ledger_state_filter",
        )
    with c3:
        search = st.text_input("Search recipient / unit / document", key="safety_ledger_search")

    filtered = []
    query = search.strip().lower()
    for record in records:
        if division_filter != "All" and str(record.get("division") or "(no division)") != division_filter:
            continue
        if state_filter and record.get("ledger_state") not in state_filter:
            continue
        haystack = " ".join(
            str(record.get(key) or "")
            for key in ("recipient_name", "recipient_email", "unit", "document", "status", "item_key")
        ).lower()
        if query and query not in haystack:
            continue
        filtered.append(record)

    st.dataframe(
        [
            {
                "State": r.get("ledger_state"),
                "Recipient": r.get("recipient_name"),
                "Email": r.get("recipient_email"),
                "Division": r.get("division"),
                "Unit": r.get("unit"),
                "Document": r.get("document"),
                "Expires": r.get("expires"),
                "Last emailed": r.get("last_sent_display"),
                "Sent count": r.get("send_count"),
                "Last upload": r.get("last_upload_display"),
                "Suppressed until": r.get("suppressed_until_display"),
            }
            for r in filtered
        ],
        hide_index=True,
        use_container_width=True,
    )

    sent_history = _sent_history_rows_from_links(
        list_safety_upload_links(submissions_dir=submissions_dir),
        division_filter=division_filter,
        query=query,
    )
    seen_tokens = {str(row.get("Token") or "") for row in sent_history if row.get("Token")}
    sent_history.extend(
        _sent_history_rows_from_ledger_records(
            records,
            division_filter=division_filter,
            query=query,
            skip_tokens=seen_tokens,
        )
    )
    sent_history.sort(key=lambda row: str(row.get("Sent at") or row.get("Last upload") or ""), reverse=True)
    with st.expander(f"Sent email history ({len(sent_history)})", expanded=False):
        st.caption(
            "Historical safety requests from saved email links and ledger fallbacks. "
            "This follows the Division and Search filters above, but it is not hidden by the Status filter."
        )
        if sent_history:
            st.dataframe(sent_history[:500], hide_index=True, use_container_width=True)
            if len(sent_history) > 500:
                st.caption(f"Showing the latest 500 of {len(sent_history)} sent item event(s).")
        else:
            st.caption("No sent email events match the current filters yet.")

    with st.expander("History storage diagnostics", expanded=False):
        st.caption(
            "Shows where the app is finding saved safety-send history. If older sends are missing here, "
            "they were likely stored only in the previous Streamlit local filesystem and not mirrored to Supabase."
        )
        st.table(link_history_source_counts(submissions_dir) + ledger_history_source_counts(submissions_dir))

    uploaded = [r for r in filtered if r.get("uploads")]
    with st.expander(f"Submitted documents ({len(uploaded)})", expanded=bool(uploaded)):
        if not uploaded:
            st.caption("No submitted documents match the current filters.")
        for record in uploaded[:50]:
            title = (
                f"{record.get('recipient_name') or 'Recipient'} · "
                f"Unit {record.get('unit') or '—'} · {record.get('document') or 'Document'} · "
                f"{record.get('last_upload_display') or ''}"
            )
            with st.expander(title, expanded=False):
                st.caption(f"Item key: {record.get('item_key')}")
                _render_uploaded_documents(record, submissions_dir)


def _render_excluded_doc_types() -> None:
    with st.expander("Document types intentionally excluded from outbound emails", expanded=False):
        st.caption(
            "These appear on the staff dashboard for visibility but are NOT requested "
            "from drivers/owners in v1 (handled by safety internally or noisy free-text)."
        )
        for label in EXCLUDED_FROM_OUTBOUND:
            st.markdown(f"- {label}")


# ---------------------------------------------------------------------------
# Reference DB section
# ---------------------------------------------------------------------------


def _format_upsert_result(result: UpsertResult) -> str:
    return (
        f"Added **{result.added}**, updated **{result.updated}**, "
        f"unchanged **{result.unchanged}** (of {result.total} rows in the file)."
    )


def _render_reference_section(submissions_dir: Path) -> None:
    summary = reference_summary(submissions_dir)
    st.subheader("Reference data (drivers & truck owners)")
    cols = st.columns(2)
    with cols[0]:
        st.metric("Drivers on file", summary["driver_count"])
        last = summary["drivers_last_updated"] or "never"
        st.caption(f"Last refreshed: {last}")
    with cols[1]:
        st.metric("Trucks on file", summary["truck_count"])
        last = summary["trucks_last_updated"] or "never"
        st.caption(f"Last refreshed: {last}")

    with st.expander("Refresh / grow reference data", expanded=summary["driver_count"] == 0):
        st.caption(
            "Upload either or both detail files to add new drivers/trucks and "
            "refresh existing ones. Re-uploading the same file is safe — "
            "duplicates are detected by Driver Personal Id (or normalized name) "
            "and Unit #. Records that aren't present in the latest file are NOT "
            "deleted; they keep their previous data."
        )
        c1, c2 = st.columns(2)
        with c1:
            driver_details_file = st.file_uploader(
                "Driver details list (XLS/XLSX)",
                type=["xls", "xlsx"],
                key="safety_ref_driver_details",
            )
        with c2:
            truck_owner_file = st.file_uploader(
                "Truck owner details (XLS/XLSX)",
                type=["xls", "xlsx"],
                key="safety_ref_truck_owner",
            )

        if st.button("Save to reference database", type="primary", key="safety_ref_save"):
            if driver_details_file is None and truck_owner_file is None:
                st.warning("Pick at least one file.")
            else:
                if driver_details_file is not None:
                    try:
                        details = load_driver_details(driver_details_file.getvalue())
                        result = upsert_drivers(
                            details,
                            submissions_dir=submissions_dir,
                            source_name=driver_details_file.name,
                        )
                        st.success(f"Drivers — {_format_upsert_result(result)}")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Driver details upload failed: {exc}")
                if truck_owner_file is not None:
                    try:
                        details = load_truck_owner_details(truck_owner_file.getvalue())
                        result = upsert_trucks(
                            details,
                            submissions_dir=submissions_dir,
                            source_name=truck_owner_file.name,
                        )
                        st.success(f"Trucks — {_format_upsert_result(result)}")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Truck owner details upload failed: {exc}")

    _render_reference_search_delete(submissions_dir)


def _render_reference_search_delete(submissions_dir: Path) -> None:
    """Search the reference database and delete selected drivers / trucks."""
    with st.expander("Search & delete reference records", expanded=False):
        st.caption(
            "Find drivers or truck owners already on file and remove the ones you no "
            "longer want. Deleting here only removes the stored reference record; it "
            "does not touch any sent emails or uploaded documents."
        )
        kind = st.radio(
            "Records to manage",
            options=["Drivers", "Truck owners"],
            horizontal=True,
            key="safety_ref_manage_kind",
        )
        query = st.text_input(
            "Search",
            key="safety_ref_manage_search",
            placeholder="Name, email, division, unit, or driver id…",
        ).strip().lower()

        if kind == "Drivers":
            records = list_driver_records(submissions_dir)

            def _row(record: dict[str, Any]) -> dict[str, Any]:
                return {
                    "Delete": False,
                    "Name": str(record.get("display_name") or record.get("full_name") or ""),
                    "Email": str(record.get("email") or ""),
                    "Division": str(record.get("division") or ""),
                    "Driver ID": str(record.get("driver_personal_id") or ""),
                    "_key": str(record.get("key") or ""),
                }
        else:
            records = list_truck_records(submissions_dir)

            def _row(record: dict[str, Any]) -> dict[str, Any]:
                owner = f"{record.get('owner_first') or ''} {record.get('owner_last') or ''}".strip()
                return {
                    "Delete": False,
                    "Unit": str(record.get("unit_no") or ""),
                    "Owner": owner or str(record.get("owner_company") or ""),
                    "Email": str(record.get("owner_email") or ""),
                    "Division": str(record.get("division") or ""),
                    "_key": str(record.get("key") or ""),
                }

        rows = [_row(record) for record in records]
        if query:
            rows = [
                row
                for row in rows
                if any(query in str(value).lower() for key, value in row.items() if key != "Delete")
            ]

        if not records:
            st.info("No reference records on file yet. Upload detail files above first.")
            return

        st.caption(f"Showing {len(rows)} of {len(records)} {kind.lower()} record(s).")
        column_order = (
            ["Delete", "Name", "Email", "Division", "Driver ID"]
            if kind == "Drivers"
            else ["Delete", "Unit", "Owner", "Email", "Division"]
        )
        edited = st.data_editor(
            rows,
            key=f"safety_ref_manage_editor_{kind}",
            hide_index=True,
            use_container_width=True,
            column_order=column_order,
            disabled=[c for c in column_order if c != "Delete"],
            column_config={"Delete": st.column_config.CheckboxColumn("Delete?", default=False)},
        )

        to_delete = [
            str(rows[row_index].get("_key"))
            for row_index, row in enumerate(_editor_records(edited))
            if row_index < len(rows) and bool(row.get("Delete")) and rows[row_index].get("_key")
        ]
        st.warning(
            f"Selected **{len(to_delete)}** {kind.lower()} record(s) for deletion."
            if to_delete
            else "Check the Delete box on any rows you want to remove."
        )
        confirm = st.checkbox(
            "I understand deleting these reference records cannot be undone.",
            key=f"safety_ref_delete_confirm_{kind}",
        )
        if st.button(
            f"🗑️ Delete {len(to_delete)} {kind.lower()} record(s)",
            type="primary",
            disabled=not confirm or not to_delete,
            key=f"safety_ref_delete_button_{kind}",
        ):
            if kind == "Drivers":
                removed = delete_drivers(to_delete, submissions_dir=submissions_dir)
            else:
                removed = delete_trucks(to_delete, submissions_dir=submissions_dir)
            st.success(f"Deleted {removed} {kind.lower()} record(s).")
            st.rerun()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _render_email_replies(submissions_dir: Path) -> None:
    """Pull driver replies (lazy uploads) and triage anything we couldn't match."""
    st.subheader("Email replies")
    st.caption("Email-reply ingestion is installed. Matched attachments are filed into Supabase; unmatched replies wait here for assignment.")
    st.caption(
        "Drivers who reply to the request email with attachments instead of using "
        "their link are ingested here and filed under the matched person. Replies we "
        "can't match wait below for manual assignment. Scheduled ingestion runs "
        "automatically; use the button to pull on demand."
    )

    mailboxes = load_inbox_mailboxes()
    if not mailboxes:
        st.info(
            "No reply mailboxes are configured for this Streamlit app. Add `SAFETY_INBOX_MAILBOXES` "
            "to **Streamlit app secrets** to enable the dashboard pull button. GitHub Actions secrets "
            "only power the scheduled background run. For a division-agnostic statements mailbox, use "
            "a JSON array like `[{\"username\":\"statements@yourcompany.com\",\"password\":\"gmail-app-password\"}]`."
        )
    else:
        st.caption(
            "Polling: " + ", ".join(f"{m.display}" + (f" → {m.division}" if m.division else "") for m in mailboxes)
        )
        if st.button("📥 Pull email replies now", key="safety_pull_replies"):
            with st.spinner("Checking mailboxes for new replies..."):
                try:
                    summary = ingest_all(submissions_dir, mailboxes=mailboxes)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Reply ingestion failed: {exc}")
                else:
                    st.success(
                        f"Ingested {summary['ingested']}, unmatched {summary['unmatched']}, "
                        f"skipped {summary['skipped']} across {summary['mailboxes']} mailbox(es)."
                    )
                    if summary.get("errors"):
                        st.warning("Some mailboxes reported issues: " + "; ".join(summary["errors"][:5]))

    unmatched = list_unmatched_replies(submissions_dir)
    with st.expander(f"Unmatched replies to assign ({len(unmatched)})", expanded=bool(unmatched)):
        if not unmatched:
            st.caption("No unmatched replies. Everything pulled so far was filed automatically.")
            return
        for entry in unmatched:
            entry_id = str(entry.get("entry_id") or "")
            title = (
                f"{entry.get('sender_name') or 'Unknown'} <{entry.get('sender_email') or '?'}> · "
                f"{entry.get('document_count') or 0} file(s) · {entry.get('subject') or '(no subject)'}"
            )
            with st.expander(title, expanded=False):
                st.caption(f"Received: {entry.get('received_at') or '—'} · Mailbox: {entry.get('mailbox') or '—'}")
                with st.form(f"assign_reply_{entry_id}", clear_on_submit=True):
                    a1, a2 = st.columns(2)
                    with a1:
                        assign_email = st.text_input(
                            "Recipient email",
                            value=str(entry.get("sender_email") or ""),
                            key=f"assign_email_{entry_id}",
                        )
                    with a2:
                        assign_name = st.text_input(
                            "Recipient name",
                            value=str(entry.get("sender_name") or ""),
                            key=f"assign_name_{entry_id}",
                        )
                    assign_division = st.text_input(
                        "Division (optional)",
                        value=str(entry.get("division_hint") or ""),
                        key=f"assign_division_{entry_id}",
                    )
                    if st.form_submit_button("Assign to this person", type="primary"):
                        result = assign_unmatched_reply(
                            submissions_dir,
                            entry_id=entry_id,
                            recipient_email=assign_email,
                            recipient_name=assign_name,
                            division=assign_division,
                        )
                        if result.get("status") == "assigned":
                            st.success(
                                f"Filed {result.get('document_count')} document(s) under "
                                f"{result.get('recipient_name')}."
                            )
                            st.rerun()
                        else:
                            st.error(result.get("message") or "Could not assign this reply.")


def _render_warnings_preview(submissions_dir: Path) -> None:
    st.subheader("Run a warnings preview")
    st.caption(
        "Upload the latest ProTransport warning exports, review the checkbox queue, "
        "then send only after the final confirmation."
    )
    summary = reference_summary(submissions_dir)
    can_preview = summary["driver_count"] > 0 or summary["truck_count"] > 0
    if not can_preview:
        st.info(
            "Before you can run warnings, add at least one reference file first: "
            "driver details for driver warnings, or truck owner details for truck warnings. "
            "Use the **Reference data** tab."
        )
        _render_excluded_doc_types()
        return

    with st.form("safety_ingest_form", clear_on_submit=False):
        c1, c2 = st.columns(2)
        with c1:
            driver_warnings = st.file_uploader(
                "Driver warnings (CSV)",
                type=["csv"],
                key="safety_driver_warnings",
            )
        with c2:
            truck_warnings = st.file_uploader(
                "Truck warnings (CSV)",
                type=["csv"],
                key="safety_truck_warnings",
            )
        st.caption(
            "The preview joins these warnings against the stored reference "
            "database above. To use a one-off detail file instead of the stored "
            "data, refresh the reference database first. You can upload driver warnings, "
            "truck warnings, or both."
        )
        submitted = st.form_submit_button("Build preview", type="primary")

    _render_excluded_doc_types()

    if submitted:
        if driver_warnings is None and truck_warnings is None:
            st.error("Please upload at least one warnings CSV: driver warnings, truck warnings, or both.")
            return
        if driver_warnings is not None and summary["driver_count"] == 0:
            st.error("Driver warnings need driver details on file. Add driver details in the Reference data tab, or skip the driver warnings file for now.")
            return
        if truck_warnings is not None and summary["truck_count"] == 0:
            st.error("Truck warnings need truck owner details on file. Add truck owner details in the Reference data tab, or skip the truck warnings file for now.")
            return

        try:
            preview = build_preview(
                driver_warnings_csv=driver_warnings.getvalue() if driver_warnings is not None else None,
                truck_warnings_csv=truck_warnings.getvalue() if truck_warnings is not None else None,
                driver_details=load_drivers(submissions_dir),
                truck_details=load_trucks(submissions_dir),
            )
        except Exception as exc:  # noqa: BLE001 - surface parser failures to staff
            st.error(f"Could not build preview: {exc}")
            return

        ledger_update = upsert_import_rows(
            submissions_dir,
            _send_queue_rows(preview.recipients),
            full_export=True,
            source="warnings_preview",
        )
        st.session_state["safety_import_preview"] = preview
        st.session_state["safety_preview_version"] = int(st.session_state.get("safety_preview_version", 0) or 0) + 1
        st.success(
            "Preview built. Review the checkbox queue below, then confirm before sending. "
            f"Ledger: {ledger_update['added']} new, {ledger_update['updated']} updated, "
            f"{ledger_update['resolved']} resolved from latest full export."
        )

    preview = st.session_state.get("safety_import_preview")
    if not preview:
        return

    preview_version = int(st.session_state.get("safety_preview_version", 1) or 1)
    _render_summary(preview)
    _render_send_queue(preview.recipients, preview_version=preview_version, submissions_dir=submissions_dir)
    _render_review(preview.review, submissions_dir, preview_version=preview_version)


def render_safety_portal_page(submissions_dir: Path) -> None:
    if not _render_sso_gate():
        return

    st.title("🛡️ Safety Paperwork Portal")
    st.caption(
        "Use the tabs left-to-right: keep reference data current, run warning emails, "
        "review sent/submitted items, and handle email replies."
    )

    reference_tab, warnings_tab, dashboard_tab, replies_tab = st.tabs(
        [
            "1️⃣ Reference data",
            "2️⃣ Warnings & send",
            "3️⃣ Dashboard / uploads",
            "4️⃣ Email replies",
        ]
    )

    with reference_tab:
        _render_reference_section(submissions_dir)

    with warnings_tab:
        _render_warnings_preview(submissions_dir)

    with dashboard_tab:
        _render_ledger_dashboard(submissions_dir)

    with replies_tab:
        _render_email_replies(submissions_dir)
