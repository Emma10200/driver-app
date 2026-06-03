"""Safety Paperwork Portal — staff page.

Renders the SSO-gated upload form for ProTransport exports and produces
a preview of who would be contacted and which rows need staff review.
The send queue uses checkbox rows so staff can exclude specific items
before sending real emails.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import streamlit as st

from services.notification_service import send_safety_document_request_email
from services.qbo_auth import qbo_allowed_emails
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
    load_drivers,
    load_trucks,
    reference_summary,
    upsert_drivers,
    upsert_trucks,
)


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
                "unit": row.get("Unit"),
                "document": row.get("Document"),
                "expires": row.get("Expires"),
                "status": row.get("Status"),
            }
        )
    return grouped


def _render_send_queue(recipients: list[RecipientBundle], *, preview_version: int) -> None:
    if not recipients:
        st.info("No clean recipients to contact from this import.")
        return

    st.subheader("Email send queue")
    st.caption(
        "Uncheck any specific document rows you do NOT want included in the outgoing email. "
        "For example, uncheck IFTA if it is handled internally. Only checked rows are sent."
    )

    rows = _send_queue_rows(recipients)
    edited = st.data_editor(
        rows,
        key=f"safety_send_queue_editor_{preview_version}",
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
        "to the checked recipients using the statements mailbox."
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
        results: list[dict[str, Any]] = []
        progress = st.progress(0, text="Sending safety paperwork emails...")
        total = max(1, len(grouped))
        for idx, bundle in enumerate(grouped.values(), start=1):
            result = send_safety_document_request_email(
                to_email=bundle["email"],
                recipient_name=bundle["recipient_name"],
                division=bundle["division"],
                items=bundle["items"],
            )
            results.append({"Email": bundle["email"], "Status": result["status"], "Message": result["message"]})
            progress.progress(idx / total, text=f"Sent {idx} of {total} email(s)...")
        progress.empty()
        sent = sum(1 for r in results if r["Status"] == "sent")
        errors = [r for r in results if r["Status"] != "sent"]
        if sent:
            st.success(f"Sent {sent} safety paperwork request email(s).")
        if errors:
            st.error(f"{len(errors)} email(s) did not send. Review the result table below.")
        st.dataframe(results, hide_index=True, use_container_width=True)


def _render_review(review: list[ReviewIssue]) -> None:
    if not review:
        st.success("No review issues found in this import.")
        return

    blockers = [r for r in review if r.severity == "blocker"]
    warnings = [r for r in review if r.severity != "blocker"]

    st.subheader(f"Review queue ({len(review)})")
    st.caption(
        "Blockers are not in the selectable send queue. Warnings are informational — "
        "the row can still go out, but the data is worth a second look."
    )

    if blockers:
        st.markdown("**Blockers (excluded from outbound)**")
        st.dataframe(
            [
                {"Category": r.category, "Message": r.message, "Source": str(r.source)}
                for r in blockers
            ],
            hide_index=True,
            use_container_width=True,
        )
    if warnings:
        st.markdown("**Warnings**")
        st.dataframe(
            [
                {"Category": r.category, "Message": r.message, "Source": str(r.source)}
                for r in warnings
            ],
            hide_index=True,
            use_container_width=True,
        )


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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_safety_portal_page(submissions_dir: Path) -> None:
    if not _render_sso_gate():
        return

    st.title("🛡️ Safety Paperwork Portal")
    st.caption(
        "Phase 1 — staff workspace. The reference database stores drivers and "
        "truck owners over time. Each preview run only needs the two warnings "
        "CSVs from ProTransport. Emails only send after you review the checkbox queue "
        "and confirm the real-send step."
    )

    _render_reference_section(submissions_dir)
    st.divider()

    summary = reference_summary(submissions_dir)
    can_preview = summary["driver_count"] > 0 and summary["truck_count"] > 0

    st.subheader("Run a warnings preview")
    if not can_preview:
        st.info(
            "Add at least one driver details file and one truck owner details "
            "file to the reference database before running a preview."
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
            "data, refresh the reference database first."
        )
        submitted = st.form_submit_button("Build preview", type="primary")

    _render_excluded_doc_types()

    if submitted:
        missing = [
            label
            for label, value in (
                ("Driver warnings CSV", driver_warnings),
                ("Truck warnings CSV", truck_warnings),
            )
            if value is None
        ]
        if missing:
            st.error("Please upload: " + ", ".join(missing))
            return

        try:
            preview = build_preview(
                driver_warnings_csv=driver_warnings.getvalue(),
                truck_warnings_csv=truck_warnings.getvalue(),
                driver_details=load_drivers(submissions_dir),
                truck_details=load_trucks(submissions_dir),
            )
        except Exception as exc:  # noqa: BLE001 - surface parser failures to staff
            st.error(f"Could not build preview: {exc}")
            return

        st.session_state["safety_import_preview"] = preview
        st.session_state["safety_preview_version"] = int(st.session_state.get("safety_preview_version", 0) or 0) + 1
        st.success("Preview built. Review the checkbox queue below, then confirm before sending.")

    preview = st.session_state.get("safety_import_preview")
    if not preview:
        return

    preview_version = int(st.session_state.get("safety_preview_version", 1) or 1)
    _render_summary(preview)
    _render_send_queue(preview.recipients, preview_version=preview_version)
    _render_review(preview.review)
