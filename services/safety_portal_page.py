"""Safety Paperwork Portal — staff page (Phase 1: ingest + preview only).

Renders the SSO-gated upload form for ProTransport exports and produces
a read-only preview of who would be contacted and which rows need staff
review. No emails or DB writes happen here yet — that lands in later
phases.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from services.qbo_auth import qbo_allowed_emails
from services.safety_paperwork import (
    DOC_TYPE_LABELS,
    EXCLUDED_FROM_OUTBOUND,
    ImportPreview,
    RecipientBundle,
    ReviewIssue,
    build_preview,
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


def _render_summary(preview: ImportPreview) -> None:
    cols = st.columns(4)
    cols[0].metric("Driver warnings", preview.driver_warning_rows)
    cols[1].metric("Truck warnings", preview.truck_warning_rows)
    cols[2].metric("Recipients ready", len(preview.recipients))
    blockers = sum(1 for r in preview.review if r.severity == "blocker")
    cols[3].metric("Review queue", blockers, delta=f"{len(preview.review) - blockers} warnings")


def _render_recipients(recipients: list[RecipientBundle]) -> None:
    if not recipients:
        st.info("No clean recipients to contact from this import.")
        return

    st.subheader(f"Recipients ready to contact ({len(recipients)})")

    by_division: dict[str, list[RecipientBundle]] = {}
    for bundle in recipients:
        by_division.setdefault(bundle.division or "(no division)", []).append(bundle)

    for division in sorted(by_division):
        with st.expander(f"{division} — {len(by_division[division])} recipient(s)", expanded=False):
            for bundle in by_division[division]:
                kind_label = {
                    "driver": "Driver",
                    "owner": "Owner",
                    "driver_owner": "Owner-operator (driver + owner)",
                }.get(bundle.kind, bundle.kind)
                units = ", ".join(bundle.units) if bundle.units else "—"
                st.markdown(
                    f"**{bundle.display_name}** &nbsp; · &nbsp; {kind_label} &nbsp; · &nbsp; "
                    f"{bundle.email} &nbsp; · &nbsp; Units: {units}"
                )
                rows = [_format_item_row(item) for item in bundle.items]
                st.dataframe(rows, hide_index=True, use_container_width=True)
                st.divider()


def _render_review(review: list[ReviewIssue]) -> None:
    if not review:
        st.success("No review issues found in this import.")
        return

    blockers = [r for r in review if r.severity == "blocker"]
    warnings = [r for r in review if r.severity != "blocker"]

    st.subheader(f"Review queue ({len(review)})")
    st.caption(
        "Blockers will NOT be auto-emailed once notifications are turned on. "
        "Warnings are informational — the row still goes out but the data is "
        "worth a second look."
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
# Entry point
# ---------------------------------------------------------------------------


def render_safety_portal_page() -> None:
    if not _render_sso_gate():
        return

    st.title("🛡️ Safety Paperwork Portal")
    st.caption(
        "Phase 1 — upload the four ProTransport exports to preview who would "
        "be contacted. No emails are sent and nothing is saved yet."
    )

    with st.form("safety_ingest_form", clear_on_submit=False):
        c1, c2 = st.columns(2)
        with c1:
            driver_warnings = st.file_uploader(
                "Driver warnings (CSV)",
                type=["csv"],
                key="safety_driver_warnings",
            )
            driver_details = st.file_uploader(
                "Driver details list (XLS/XLSX)",
                type=["xls", "xlsx"],
                key="safety_driver_details",
            )
        with c2:
            truck_warnings = st.file_uploader(
                "Truck warnings (CSV)",
                type=["csv"],
                key="safety_truck_warnings",
            )
            truck_owner = st.file_uploader(
                "Truck owner details (XLS/XLSX)",
                type=["xls", "xlsx"],
                key="safety_truck_owner",
            )
        submitted = st.form_submit_button("Build preview", type="primary")

    _render_excluded_doc_types()

    if not submitted:
        return

    missing = [
        label
        for label, value in (
            ("Driver warnings CSV", driver_warnings),
            ("Truck warnings CSV", truck_warnings),
            ("Driver details list", driver_details),
            ("Truck owner details", truck_owner),
        )
        if value is None
    ]
    if missing:
        st.error("Please upload all four files: " + ", ".join(missing))
        return

    try:
        preview = build_preview(
            driver_warnings_csv=driver_warnings.getvalue(),
            truck_warnings_csv=truck_warnings.getvalue(),
            driver_details_xls=driver_details.getvalue(),
            truck_owner_xls=truck_owner.getvalue(),
        )
    except Exception as exc:  # noqa: BLE001 - surface parser failures to staff
        st.error(f"Could not build preview: {exc}")
        return

    _render_summary(preview)
    _render_recipients(preview.recipients)
    _render_review(preview.review)
