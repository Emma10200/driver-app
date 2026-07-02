"""Dispatcher-friendly read-only web view of the Google Sheet dispatch board."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from html import escape
from io import StringIO
from typing import Any

import streamlit as st

from services.dispatch_board_data import load_dispatch_board_rows
from services.dispatch_contacts import (
    group_contacts_by_dispatcher,
    load_company_info,
    load_dispatcher_contacts,
    save_company_info,
    save_dispatcher_contacts,
)
from services.rate_confirmation_data import (
    dedupe_rate_confirmation_documents,
    group_rate_confirmations_by_truck,
    load_rate_confirmation_documents,
    normalize_rate_confirmation_doc,
    rate_confirmation_alerts,
)
from services.gps_data import load_current_assets


DISPATCH_BOARD_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Bk2p4B8oztxNew1Ei0D7s8UK2gQOsWXftmeRBZnsCII/edit?gid=0#gid=0"


_BOARD_CSS = """
<style>
.dispatch-hero {
    border: 1px solid rgba(37,99,235,.20);
    border-radius: 16px;
    padding: 1rem 1.15rem;
    background: linear-gradient(135deg, #f8fafc, #eef6ff);
    box-shadow: 0 10px 28px rgba(15,23,42,.08);
  margin-bottom: 1rem;
}
.dispatch-hero h1 { margin: 0 0 .25rem; font-size: 1.65rem; color: #0f172a; letter-spacing: -.02em; }
.dispatch-hero p { margin: 0; color: #475569; }
.dispatch-section {
    margin: 1.15rem 0 .45rem;
    padding: .45rem .7rem;
    border-radius: 10px;
    background: #eaf2ff;
    border: 1px solid #bfdbfe;
    color: #1e3a8a;
  font-weight: 800;
  letter-spacing: .02em;
}
.dispatch-card {
  display: grid;
    grid-template-columns: 74px 92px minmax(140px,1.1fr) minmax(130px,1fr) 76px 92px 126px 118px;
  gap: 0;
  align-items: stretch;
    border: 1px solid #dbe3ef;
    border-radius: 10px;
    margin: .26rem 0;
  overflow: hidden;
    background: #ffffff;
    box-shadow: 0 1px 2px rgba(15,23,42,.04);
}
.dispatch-card:not(.dispatch-header-row):hover {
    border-color: #93c5fd;
    box-shadow: 0 6px 18px rgba(37,99,235,.08);
}
.dispatch-cell {
    padding: .42rem .48rem;
    border-right: 1px solid #e5edf7;
    min-height: 38px;
  display: flex;
  align-items: center;
    font-size: .78rem;
    line-height: 1.18;
    color: #0f172a;
}
.dispatch-cell:last-child { border-right: 0; }
.dispatch-header-row {
  position: sticky;
  top: 0;
  z-index: 2;
    background: #1e3a8a;
    color: #eff6ff;
  font-weight: 800;
  text-transform: uppercase;
    font-size: .68rem;
  letter-spacing: .045em;
    box-shadow: none;
}
.dispatch-header-row .dispatch-cell { color: #eff6ff; border-color: rgba(219,234,254,.25); }
.dispatch-unit { font-weight: 900; font-size: .94rem; color: #1d4ed8; }
.dispatch-trailer { font-weight: 850; color: #047857; }
.dispatch-note { font-weight: 700; color: #92400e; background: #fffbeb; }
.dispatch-muted { color: #64748b; }
.status-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 72px;
  padding: .18rem .52rem;
  border-radius: 999px;
  font-weight: 800;
  font-size: .75rem;
  border: 1px solid transparent;
}
.status-active { background: #dcfce7; color: #166534; border-color: #86efac; }
.status-inactive { background: #f1f5f9; color: #475569; border-color: #cbd5e1; }
.status-warning { background: #fef3c7; color: #92400e; border-color: #fcd34d; }
.status-other { background: #dbeafe; color: #1e40af; border-color: #93c5fd; }
.gps-na { color: #b91c1c; font-weight: 800; }
.gps-stack { display: flex; flex-direction: column; gap: .15rem; align-items: flex-start; width: 100%; }
.gps-status-line { display: inline-flex; align-items: center; gap: .35rem; font-weight: 850; color: #334155; }
.gps-dot { width: .62rem; height: .62rem; border-radius: 999px; display: inline-block; box-shadow: 0 0 0 2px #fff; }
.gps-dot-active { background: #22c55e; box-shadow: 0 0 0 2px #dcfce7; }
.gps-dot-stale { background: #94a3b8; box-shadow: 0 0 0 2px #f1f5f9; }
.gps-dot-missing { background: #ef4444; box-shadow: 0 0 0 2px #fee2e2; }
.gps-summary {
    cursor: pointer;
    list-style: none;
    color: #2563eb;
    font-size: .70rem;
    font-weight: 800;
}
.gps-summary::-webkit-details-marker { display: none; }
.gps-details-panel {
    margin-top: .18rem;
    padding: .38rem .45rem;
    border: 1px solid #dbeafe;
    border-radius: 9px;
    background: #f8fbff;
    color: #334155;
    font-size: .70rem;
    line-height: 1.24;
    min-width: 160px;
}
.gps-details-panel a { color: #1d4ed8; text-decoration: none; font-weight: 800; }
.rate-conf-stack { display: flex; flex-direction: column; gap: .18rem; align-items: flex-start; }
.rate-pill {
    display: inline-flex;
    align-items: center;
    gap: .22rem;
    border-radius: 999px;
    padding: .16rem .44rem;
    font-size: .68rem;
    font-weight: 850;
    border: 1px solid #bfdbfe;
    background: #eff6ff;
    color: #1d4ed8;
}
.rate-pill-alert { border-color: #fca5a5; background: #fef2f2; color: #991b1b; }
.rate-pill-warn { border-color: #fcd34d; background: #fffbeb; color: #92400e; }
.rate-line { font-size: .70rem; color: #475569; max-width: 118px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.alert-bell-badge {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: .3rem;
    cursor: pointer;
    font-size: 1.35rem;
    padding: .3rem .5rem;
    border-radius: 10px;
    border: 1px solid #fed7aa;
    background: #fff7ed;
    color: #9a3412;
    font-weight: 850;
    letter-spacing: .02em;
}
.alert-bell-count {
    position: absolute;
    top: -4px;
    right: -6px;
    min-width: 18px;
    height: 18px;
    border-radius: 999px;
    background: #dc2626;
    color: #fff;
    font-size: .62rem;
    font-weight: 900;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 4px;
    border: 2px solid #fff;
    line-height: 1;
}
.alert-list-item {
    margin: .3rem 0;
    padding: .42rem .52rem;
    border-radius: 8px;
    border: 1px solid #fed7aa;
    background: #ffffff;
    color: #0f172a;
    font-size: .78rem;
    line-height: 1.28;
}
.alert-list-item-red { border-left: 4px solid #dc2626; }
.alert-list-item-yellow { border-left: 4px solid #f59e0b; }
.alert-list-item-info { border-left: 4px solid #2563eb; }
.alert-group-header { font-weight: 900; color: #334155; margin: .55rem 0 .2rem; font-size: .82rem; }
@media (max-width: 1200px) {
    .dispatch-card { grid-template-columns: 68px 84px minmax(120px,1.1fr) minmax(110px,1fr) 70px 84px 112px 100px; }
    .dispatch-cell { font-size: .73rem; padding: .36rem; }
}
</style>
"""


def render_dispatch_board_page() -> None:
    st.markdown(_BOARD_CSS, unsafe_allow_html=True)
    rows = load_dispatch_board_rows()
    try:
        raw_rate_docs = [normalize_rate_confirmation_doc(doc) for doc in load_rate_confirmation_documents(days=14)]
        rate_docs = dedupe_rate_confirmation_documents(raw_rate_docs)
    except Exception as exc:
        st.warning(f"Could not load rate confirmations: {exc}")
        rate_docs = []
    docs_by_truck = group_rate_confirmations_by_truck(rate_docs)
    try:
        gps_by_truck = _load_current_truck_gps()
    except Exception as exc:
        st.warning(f"Could not load GPS data: {exc}")
        gps_by_truck = {}
    alerts = rate_confirmation_alerts(rate_docs)

    nav_left, nav_right = st.columns([1, 1])
    with nav_left:
        st.markdown(
        """
        <div class="dispatch-hero">
          <h1>Dispatch Board</h1>
          <p>Read-only web mirror of the dispatcher sheet, grouped the same way dispatchers already think about it.</p>
        </div>
        """,
        unsafe_allow_html=True,
        )
    with nav_right:
        st.markdown("<div style='height:0.85rem'></div>", unsafe_allow_html=True)
        st.link_button("Open GPS Fleet Map", "?route=gps-map", use_container_width=True)

    if not rows:
        st.warning("No dispatch board rows are in Supabase yet. Run Utilities → ⬆️ Publish Dispatch Board to Supabase in the Sheet.")
        st.link_button("Open Google Sheet", DISPATCH_BOARD_SHEET_URL)
        return

    rows = [_normalize_row(row) for row in rows]
    _attach_rate_confirmations(rows, docs_by_truck)
    _attach_current_gps(rows, gps_by_truck)
    latest_publish = max((str(row.get("source_updated_at") or "") for row in rows), default="")
    dispatchers = sorted({row["dispatcher"] for row in rows if row["dispatcher"]})
    statuses = sorted({row["status"] for row in rows if row["status"]})

    _render_metrics(rows, dispatchers, latest_publish, rate_docs, alerts)
    filtered = _render_filters(rows, dispatchers, statuses)

    col_sheet, col_csv, col_contacts, col_alerts = st.columns([1, 1, 1, 1])
    with col_sheet:
        st.link_button("Open editable Google Sheet", DISPATCH_BOARD_SHEET_URL, use_container_width=True)
    with col_csv:
        st.download_button(
            "Download filtered CSV",
            _rows_to_csv(filtered),
            file_name="dispatch_board_filtered.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with col_contacts:
        if st.button("📇 Company Contacts / Info", use_container_width=True):
            _contacts_dialog()
    with col_alerts:
        _render_alert_bell(alerts)

    if not filtered:
        st.info("No rows match the current filters.")
        return

    _render_board(filtered)
    _render_raw_inspector(filtered)


_HIDDEN_STATUSES = {"INACTIVE", "VACATION", "OUT", "TERMINATED"}


def _render_metrics(rows: list[dict[str, Any]], dispatchers: list[str], latest_publish: str, rate_docs: list[dict[str, Any]], alerts: list[dict[str, Any]]) -> None:
    active = sum(1 for row in rows if row["status"].upper() == "ACTIVE")
    no_gps = sum(1 for row in rows if _is_no_gps(row.get("last_gps", "")))
    gps_active = sum(1 for row in rows if row.get("gps_active_24h"))
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Board Rows", len(rows))
    col2.metric("Active", active)
    col3.metric("Dispatchers", len(dispatchers))
    col4.metric("GPS Active 24h", gps_active, delta=f"{no_gps} no gps" if no_gps else None)
    col5.metric("Rate Cons", len(rate_docs))
    col6.metric("RC Alerts", len(alerts), delta=None if not alerts else "review")
    st.caption(f"Last board mirror: {_short_dt(latest_publish)}")


def _render_filters(rows: list[dict[str, Any]], dispatchers: list[str], statuses: list[str]) -> list[dict[str, Any]]:
    # Default-hide inactive/vacation statuses
    default_statuses = [s for s in statuses if s.upper() not in _HIDDEN_STATUSES]
    with st.sidebar:
        st.subheader("Dispatch Board Filters")
        query = st.text_input("Search truck, trailer, driver, city, note", "").strip().lower()
        selected_dispatchers = st.multiselect("Dispatcher", dispatchers, default=dispatchers)
        selected_statuses = st.multiselect("Status", statuses, default=default_statuses)
        hide_no_unit = st.toggle("Hide rows without truck/trailer", value=True)
        st.toggle("Show raw JSON inspector", value=False, key="dispatch_show_raw")

    filtered = []
    for row in rows:
        if selected_dispatchers and row["dispatcher"] not in selected_dispatchers:
            continue
        if selected_statuses and row["status"] not in selected_statuses:
            continue
        if hide_no_unit and not row["truck_id"] and not row["trailer_id"]:
            continue
        if query and query not in _row_search_blob(row):
            continue
        filtered.append(row)
    return filtered


def _render_board(rows: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["dispatcher"] or "Unassigned"].append(row)

    for dispatcher in sorted(grouped):
        section_rows = grouped[dispatcher]
        truck_count = sum(1 for r in section_rows if r.get("truck_id"))
        st.markdown(
            f'<div class="dispatch-section">--- {_h(dispatcher)} --- <span class="dispatch-muted">{truck_count} truck{"s" if truck_count != 1 else ""}</span></div>',
            unsafe_allow_html=True,
        )
        _render_header_row()
        for row in section_rows:
            st.markdown(_row_card_html(row), unsafe_allow_html=True)
            _render_truck_detail_expander(row)


def _render_header_row() -> None:
    labels = ["Truck", "Trailer", "Driver", "Location", "Date", "Status", "Rate Conf", "GPS"]
    cells = "".join(f'<div class="dispatch-cell">{label}</div>' for label in labels)
    st.markdown(f'<div class="dispatch-card dispatch-header-row">{cells}</div>', unsafe_allow_html=True)


def _row_card_html(row: dict[str, Any]) -> str:
    return "".join([
        '<div class="dispatch-card">',
        f'<div class="dispatch-cell dispatch-unit">{_h(row["truck_id"])}</div>',
        f'<div class="dispatch-cell dispatch-trailer">{_h(row["trailer_id"])}</div>',
        f'<div class="dispatch-cell"><b>{_h(row["driver_name"])}</b></div>',
        f'<div class="dispatch-cell">{_h(row["location"])}</div>',
        f'<div class="dispatch-cell"><b>{_h(row["date_text"])}</b></div>',
        f'<div class="dispatch-cell"><span class="status-pill {_status_class(row["status"])}">{_h(row["status"] or "—")}</span></div>',
        f'<div class="dispatch-cell">{_rate_conf_cell_html(row)}</div>',
        f'<div class="dispatch-cell">{_gps_cell_html(row)}</div>',
        '</div>',
    ])


def _render_truck_detail_expander(row: dict[str, Any]) -> None:
    """Collapsible detail panel below each truck row with GPS + rate-con info."""
    truck_id = str(row.get("truck_id") or "").strip()
    if not truck_id:
        return
    with st.expander(f"Truck {truck_id} details", expanded=False):
        col_gps, col_rc = st.columns(2)
        gps = row.get("current_gps") if isinstance(row.get("current_gps"), dict) else {}
        with col_gps:
            st.markdown("**GPS**")
            active = bool(gps.get("active_24h"))
            st.markdown(f"Status: **{'Active' if active else 'Stale/Missing'}**")
            st.markdown(f"Last ping: {gps.get('last_ping_label') or 'N/A'}")
            st.markdown(f"Provider: {gps.get('provider') or row.get('eld_provider') or '—'}")
            st.markdown(f"Location: {gps.get('address') or row.get('location') or '—'}")
            lat, lon = gps.get("lat"), gps.get("lon")
            coords = _format_coords(lat, lon)
            if coords:
                st.markdown(f"Coords: {coords}")
                st.markdown(f"[Open in Google Maps](https://www.google.com/maps?q={lat},{lon})")
            speed = _format_gps_number(gps.get("speed"), suffix=" mph/kph")
            heading = _format_gps_number(gps.get("heading_deg"), suffix="°")
            if speed:
                st.markdown(f"Speed: {speed}")
            if heading:
                st.markdown(f"Heading: {heading}")
        docs = row.get("rate_confirmations") if isinstance(row.get("rate_confirmations"), list) else []
        with col_rc:
            st.markdown("**Rate Confirmations**")
            if not docs:
                st.caption("No rate confirmations matched to this truck.")
            else:
                for doc in docs[:8]:
                    title = _rate_doc_title(doc)
                    received = _short_dt(doc.get("received_at"))
                    ref = str(doc.get("load_reference") or "")
                    domain = str(doc.get("sender_domain") or "")
                    duplicate_count = int(doc.get("duplicate_count") or 1)
                    copy_note = f" · {duplicate_count} copies collapsed" if duplicate_count > 1 else ""
                    st.markdown(f"- **{title}** ({received}{copy_note})")
                    st.caption(f"Ref: {ref} | {domain}" if ref else domain)
        notes = str(row.get("notes") or "")
        planning = str(row.get("planning_note") or "")
        if notes or planning:
            st.markdown("---")
            if notes:
                st.markdown(f"**Notes:** {notes}")
            if planning:
                st.markdown(f"**Planning/Equipment:** {planning}")


def _load_current_truck_gps() -> dict[str, dict[str, Any]]:
    assets = load_current_assets()
    out: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if str(getattr(asset, "asset_type", "")).lower() != "truck":
            continue
        truck_id = str(getattr(asset, "asset_id", "") or "").strip()
        if not truck_id:
            continue
        last_ping = getattr(asset, "last_ping", None)
        out[truck_id] = {
            "asset_id": truck_id,
            "lat": getattr(asset, "lat", None),
            "lon": getattr(asset, "lon", None),
            "speed": getattr(asset, "speed", None),
            "heading_deg": getattr(asset, "heading_deg", None),
            "last_ping": last_ping.isoformat() if last_ping else "",
            "address": str(getattr(asset, "address", "") or "").strip(),
            "zip": str(getattr(asset, "zip", "") or "").strip(),
            "provider": str(getattr(asset, "provider", "") or "").strip(),
            "division": str(getattr(asset, "division", "") or "").strip(),
        }
    return out


def _attach_current_gps(rows: list[dict[str, Any]], gps_by_truck: dict[str, dict[str, Any]]) -> None:
    for row in rows:
        gps = dict(gps_by_truck.get(str(row.get("truck_id") or "").strip()) or {})
        if not gps:
            gps = {
                "last_ping": row.get("last_gps") or "",
                "provider": row.get("eld_provider") or "",
                "address": row.get("location") or "",
            }
        last_ping = str(gps.get("last_ping") or row.get("last_gps") or "")
        parsed = _parse_dt(last_ping)
        active_24h = bool(parsed and datetime.now(UTC) - _ensure_utc(parsed) <= timedelta(days=1))
        gps["active_24h"] = active_24h
        gps["last_ping_label"] = _short_dt(last_ping)
        row["current_gps"] = gps
        row["gps_active_24h"] = active_24h


def _gps_cell_html(row: dict[str, Any]) -> str:
    gps = row.get("current_gps") if isinstance(row.get("current_gps"), dict) else {}
    has_ping = bool(str(gps.get("last_ping") or row.get("last_gps") or "").strip()) and not _is_no_gps(str(gps.get("last_ping") or row.get("last_gps") or ""))
    active = bool(gps.get("active_24h"))
    dot_class = "gps-dot-active" if active else ("gps-dot-stale" if has_ping else "gps-dot-missing")
    status = "Active" if active else ("Stale" if has_ping else "No GPS")
    last_ping = _h(str(gps.get("last_ping_label") or "N/A"))
    provider = _h(str(gps.get("provider") or row.get("eld_provider") or ""))
    address = _h(str(gps.get("address") or row.get("location") or ""))
    speed = _format_gps_number(gps.get("speed"), suffix=" mph/kph")
    heading = _format_gps_number(gps.get("heading_deg"), suffix="°")
    lat = gps.get("lat")
    lon = gps.get("lon")
    coords = _format_coords(lat, lon)
    map_link = ""
    if coords:
        map_link = f'<br><a href="https://www.google.com/maps?q={_h(str(lat))},{_h(str(lon))}" target="_blank">Open map</a>'
    title = _h(f"GPS {status}. Last ping: {last_ping}. Provider: {provider or '—'}")
    return "".join([
        '<div class="gps-stack">',
        f'<span class="gps-status-line" title="{title}"><span class="gps-dot {dot_class}"></span>{_h(status)}</span>',
        f'<details><summary class="gps-summary">GPS details</summary><div class="gps-details-panel">',
        f'<b>Last:</b> {last_ping}<br>',
        f'<b>Provider:</b> {provider or "—"}<br>',
        f'<b>Location:</b> {address or "—"}<br>',
        f'<b>Coords:</b> {_h(coords or "—")}<br>',
        f'<b>Speed:</b> {_h(speed or "—")}<br>',
        f'<b>Heading:</b> {_h(heading or "—")}',
        map_link,
        '</div></details>',
        '</div>',
    ])


def _attach_rate_confirmations(rows: list[dict[str, Any]], docs_by_truck: dict[str, list[dict[str, Any]]]) -> None:
    for row in rows:
        docs = docs_by_truck.get(str(row.get("truck_id") or "").strip(), [])
        row["rate_confirmations"] = docs
        row["rate_conf_count"] = len(docs)
        row["rate_conf_alert_count"] = sum(1 for doc in docs if _doc_alert_level(doc) in {"red", "yellow"})
        row["latest_rate_confirmation"] = docs[0] if docs else None


def _rate_conf_cell_html(row: dict[str, Any]) -> str:
    docs = row.get("rate_confirmations") if isinstance(row.get("rate_confirmations"), list) else []
    if not docs:
        return '<span class="dispatch-muted">—</span>'
    latest = docs[0]
    alert_count = int(row.get("rate_conf_alert_count") or 0)
    pill_class = "rate-pill-alert" if alert_count else ("rate-pill-warn" if str(latest.get("match_status") or "") in {"near_match", "cancelled"} else "")
    title = _h(_rate_doc_title(latest))
    count = len(docs)
    label = f"{count} doc" if count == 1 else f"{count} docs"
    if alert_count:
        label += f" · {alert_count} alert"
    return "".join([
        '<div class="rate-conf-stack">',
        f'<span class="rate-pill {pill_class}">📄 {_h(label)}</span>',
        f'<span class="rate-line" title="{title}">{title}</span>',
        f'<span class="dispatch-muted">{_h(_short_dt(latest.get("received_at")))}</span>',
        '</div>',
    ])


def _dash(value: Any) -> str:
    """Render missing sheet values as a dash instead of an empty hole."""
    text = str(value or "").strip()
    return text if text else "—"


@st.dialog("📇 Company Contacts / Info", width="large")
def _contacts_dialog() -> None:
    edit_mode = st.toggle("✏️ Edit directory", key="contacts_edit_mode", help="Add, edit, or remove dispatchers and company info.")
    if edit_mode:
        _render_contacts_editor()
        return

    companies = load_company_info()
    st.markdown("#### 🏢 Companies")
    company_cols = st.columns(max(1, min(3, len(companies))))
    for idx, company in enumerate(companies):
        with company_cols[idx % len(company_cols)]:
            st.markdown(
                "\n".join([
                    f"**{_dash(company.get('division'))}**",
                    f"- {_dash(company.get('mc_number'))}",
                    f"- {_dash(company.get('dot_number'))}",
                    f"- {_dash(company.get('fin_number'))}",
                    f"- 📧 {_dash(company.get('dispatch_email'))}",
                    f"- ☎️ {_dash(company.get('company_phone'))}",
                    f"- 🛠️ Setup: {_dash(company.get('setup_phone'))} ({_dash(company.get('setup_contact'))})",
                    f"- 📍 {_dash(company.get('address'))}",
                ])
            )
    st.caption("Xpress Trans has no per-dispatcher emails — everyone shares the main dispatch inbox.")
    st.divider()

    st.markdown("#### 👥 Dispatchers")
    grouped = group_contacts_by_dispatcher(load_dispatcher_contacts())
    if not grouped:
        st.info("No dispatcher contacts yet. Use ✏️ Edit directory to add some.")
        return
    for name, entries in grouped.items():
        with st.expander(f"👤 Contacts for {name}", expanded=False):
            for i, entry in enumerate(entries):
                if i:
                    st.markdown("---")
                division = str(entry.get("division") or "").strip()
                if division.lower().startswith("personal cell"):
                    st.markdown(f"**Personal Cell (internal only — don't give to brokers):** {_dash(entry.get('phone'))}")
                    continue
                lines = [
                    f"**Division:** {_dash(division)}",
                    f"**Email Address:** {_dash(entry.get('email'))}",
                    f"**Phone:** {_dash(entry.get('phone'))}",
                ]
                if str(entry.get("extension") or "").strip():
                    lines.append(f"**Extension:** {entry['extension']}")
                st.markdown("  \n".join(lines))


def _render_contacts_editor() -> None:
    """Pencil mode: edit/add/remove dispatcher contact rows and company info."""
    st.caption("Add or delete rows, then hit Save. Blank cells display as dashes. Saving requires the contact directory tables (migration 0025).")

    st.markdown("**Dispatcher contacts**")
    contact_rows = [
        {
            "dispatcher_name": str(entry.get("dispatcher_name") or ""),
            "division": str(entry.get("division") or ""),
            "email": str(entry.get("email") or ""),
            "phone": str(entry.get("phone") or ""),
            "extension": str(entry.get("extension") or ""),
            "sort_order": int(entry.get("sort_order") or 0),
        }
        for entry in load_dispatcher_contacts()
    ]
    edited_contacts = st.data_editor(
        contact_rows,
        num_rows="dynamic",
        use_container_width=True,
        key="contacts_editor_dispatchers",
        column_config={
            "dispatcher_name": st.column_config.TextColumn("Dispatcher", required=True),
            "division": st.column_config.TextColumn("Division"),
            "email": st.column_config.TextColumn("Email"),
            "phone": st.column_config.TextColumn("Phone"),
            "extension": st.column_config.TextColumn("Ext"),
            "sort_order": st.column_config.NumberColumn("Order", min_value=0, step=1),
        },
    )

    st.markdown("**Company / division info**")
    company_rows = [
        {
            "division": str(company.get("division") or ""),
            "mc_number": str(company.get("mc_number") or ""),
            "dot_number": str(company.get("dot_number") or ""),
            "fin_number": str(company.get("fin_number") or ""),
            "dispatch_email": str(company.get("dispatch_email") or ""),
            "company_phone": str(company.get("company_phone") or ""),
            "setup_phone": str(company.get("setup_phone") or ""),
            "setup_contact": str(company.get("setup_contact") or ""),
            "address": str(company.get("address") or ""),
            "sort_order": int(company.get("sort_order") or 0),
        }
        for company in load_company_info()
    ]
    edited_companies = st.data_editor(
        company_rows,
        num_rows="dynamic",
        use_container_width=True,
        key="contacts_editor_companies",
    )

    if st.button("💾 Save directory", type="primary", use_container_width=True):
        try:
            saved_contacts = save_dispatcher_contacts(list(edited_contacts))
            saved_companies = save_company_info(list(edited_companies))
        except Exception as exc:
            st.error(f"Could not save the directory: {exc}")
        else:
            st.success(f"Saved {saved_contacts} contact rows and {saved_companies} companies.")
            st.session_state["contacts_edit_mode"] = False
            st.rerun()


def _render_alert_bell(alerts: list[dict[str, Any]]) -> None:
    """Bell-badge notification popover with issue-type filters and email previews."""
    count = len(alerts)
    with st.popover(f"🔔 {count} Alert{'s' if count != 1 else ''}" if count else "🔔 Alerts", use_container_width=True):
        if not alerts:
            st.caption("No rate-confirmation alerts right now.")
            return
        category_counts: dict[str, int] = defaultdict(int)
        for doc in alerts:
            category_counts[_alert_category(doc)[0]] += 1

        st.markdown("**What needs review?**")
        c1, c2 = st.columns(2)
        show_need_truck = c1.toggle(
            f"No truck match ({category_counts.get('need_truck', 0)})",
            value=True,
            key="alert_show_need_truck",
        )
        show_ambiguous = c2.toggle(
            f"Multiple truck candidates ({category_counts.get('ambiguous', 0)})",
            value=True,
            key="alert_show_ambiguous",
        )
        c3, c4 = st.columns(2)
        show_cancel = c3.toggle(
            f"Cancellations ({category_counts.get('cancel', 0)})",
            value=True,
            key="alert_show_cancel",
        )
        show_low_conf = c4.toggle(
            f"Low confidence ({category_counts.get('low_confidence', 0)})",
            value=False,
            key="alert_show_low_confidence",
        )
        show_other = st.toggle(
            f"Other ({category_counts.get('other', 0)})",
            value=False,
            key="alert_show_other",
        )
        hide_old = st.toggle("Hide alerts older than 5 days", value=True, key="alert_hide_old")
        st.markdown("---")

        enabled_categories = {
            "need_truck": show_need_truck,
            "ambiguous": show_ambiguous,
            "cancel": show_cancel,
            "low_confidence": show_low_conf,
            "other": show_other,
        }
        now = datetime.now(UTC)
        visible: list[dict[str, Any]] = []
        for doc in alerts:
            category, _label = _alert_category(doc)
            if not enabled_categories.get(category, False):
                continue
            if hide_old:
                received = _parse_dt(str(doc.get("received_at") or ""))
                if received and (now - _ensure_utc(received)).days > 5:
                    continue
            visible.append(doc)

        st.caption(f"Showing {len(visible)} of {count} alerts")
        if not visible:
            st.info("All alerts filtered out. Adjust toggles above.")
            return

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for doc in visible:
            category, label = _alert_category(doc)
            grouped[label].append(doc)
        for label in sorted(grouped):
            st.markdown(f'<div class="alert-group-header">{_h(label)}</div>', unsafe_allow_html=True)
            for doc in grouped[label][:20]:
                _render_alert_preview(doc)


def _alert_category(doc: dict[str, Any]) -> tuple[str, str]:
    """Return (category_key, display_label) for a rate-confirmation alert."""
    status = str(doc.get("match_status") or "").strip().lower()
    codes = {str(code) for code in (doc.get("alert_codes") or [])}
    if status == "cancelled" or "cancel_notice" in codes:
        return "cancel", "Cancellations / cancel notices"
    if status == "unmatched" or "no_board_truck_match" in codes:
        return "need_truck", "No truck match"
    if status == "ambiguous" or "multiple_truck_candidates_one_attachment" in codes:
        return "ambiguous", "Multiple truck candidates"
    if status == "near_match" or codes.intersection({
        "one_digit_off_truck_match",
        "two_digits_off_truck_match_review",
        "body_noise_single_pick",
        "dispatcher_or_gps_tiebreak",
    }):
        return "low_confidence", "Low-confidence / auto-resolved"
    return "other", "Other alerts"


def _render_alert_preview(doc: dict[str, Any]) -> None:
    """Render one alert with the email evidence needed to troubleshoot it."""
    category, _label = _alert_category(doc)
    truck = str(doc.get("matched_truck_id") or "").strip() or "no truck"
    sender = str(doc.get("sender_name") or doc.get("sender_email") or "unknown sender").strip()
    received = _short_dt(doc.get("received_at"))
    title = _rate_doc_title(doc)
    status = str(doc.get("match_status") or "").strip()
    expander_label = f"{truck} · {status or category} · {sender} · {received} · {title}"
    with st.expander(expander_label[:220], expanded=False):
        subject = str(doc.get("subject") or "").strip() or "(blank subject)"
        attachment = str(doc.get("attachment_filename") or "").strip() or "(no attachment filename)"
        email = str(doc.get("sender_email") or "").strip()
        notes = str(doc.get("alert_notes") or "").strip()
        raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
        body_preview = str(raw.get("body_preview") or "").strip()
        st.markdown(f"**Issue:** {_alert_category(doc)[1]}")
        if notes:
            st.warning(notes)
        st.markdown(f"**From:** {sender} `<{email}>`")
        st.markdown(f"**Subject:** {subject}")
        st.markdown(f"**Attachment:** `{attachment}`")
        duplicate_count = int(doc.get("duplicate_count") or 1)
        if duplicate_count > 1:
            st.info(f"{duplicate_count} duplicate email copies collapsed into this one alert.")
        st.markdown(f"**Matched truck:** `{truck}`  ")
        st.caption(
            " | ".join(part for part in [
                f"source={doc.get('match_source') or '—'}",
                f"token={doc.get('match_token') or '—'}",
                f"confidence={doc.get('match_confidence') or '—'}",
                f"dispatcher={doc.get('board_dispatcher') or '—'}",
            ] if part)
        )
        candidates = _candidate_preview(doc)
        if candidates:
            st.markdown("**Candidate trucks:**")
            st.caption(candidates)
        if body_preview:
            st.markdown("**Email body preview:**")
            st.text_area(
                "",
                body_preview[:3000],
                height=180,
                label_visibility="collapsed",
                disabled=True,
                key=f"alert_body_{doc.get('document_key')}",
            )


def _candidate_preview(doc: dict[str, Any]) -> str:
    candidates = doc.get("candidate_matches")
    if not isinstance(candidates, list):
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for item in candidates[:12]:
        if not isinstance(item, dict):
            continue
        truck = str(item.get("matched_truck") or "").strip()
        if not truck or truck in seen:
            continue
        seen.add(truck)
        dispatcher = str(item.get("board_dispatcher") or "").strip() or "?"
        token = str(item.get("token") or "").strip()
        source = str(item.get("source") or "").strip()
        match_type = str(item.get("match_type") or "").strip()
        parts.append(f"{truck} ({dispatcher}, token {token}, {source}, {match_type})")
    return "; ".join(parts)


def _rate_doc_title(doc: dict[str, Any]) -> str:
    return str(
        doc.get("broker_name")
        or doc.get("load_reference")
        or doc.get("attachment_filename")
        or doc.get("subject")
        or "Rate confirmation"
    ).strip()


def _doc_alert_level(doc: dict[str, Any]) -> str:
    return str(doc.get("alert_level") or "").strip().lower()


def _render_raw_inspector(rows: list[dict[str, Any]]) -> None:
    if not st.session_state.get("dispatch_show_raw"):
        return
    with st.expander("Raw mirrored rows", expanded=False):
        for row in rows[:100]:
            st.markdown(f"**Row {row.get('sheet_row')} — Truck {row.get('truck_id')} / Trailer {row.get('trailer_id')}**")
            st.json(row.get("raw") or {})


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    truck_id = str(row.get("truck_id") or "").strip()
    trailer_id = str(row.get("trailer_id") or "").strip()
    status = str(row.get("status") or raw.get("STATUS") or "").strip()
    dispatcher = str(row.get("dispatcher") or raw.get("DISPATCHER") or raw.get("_dispatcher_section") or "Unassigned").strip()
    return {
        **row,
        "truck_id": truck_id,
        "trailer_id": trailer_id,
        "driver_name": str(row.get("driver_name") or raw.get("DRIVER") or "").strip(),
        "dispatcher": dispatcher,
        "status": status,
        "cell": str(raw.get("CELL") or "").strip(),
        "location": str(row.get("origin") or raw.get("LOCATION") or "").strip(),
        "date_text": _date_label(row.get("pickup_at") or raw.get("DATE") or ""),
        "notes": str(raw.get("NOTES") or "").strip(),
        "planning_note": str(raw.get("Planning Note (Internal)") or "").strip(),
        "last_gps": str(raw.get("Last GPS Date") or "").strip(),
        "eld_provider": str(raw.get("ELD Provider") or "").strip(),
        "raw": raw,
    }


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    output = StringIO()
    fields = [
        "sheet_row", "dispatcher", "truck_id", "trailer_id", "driver_name", "cell",
        "location", "date_text", "notes", "planning_note", "status", "rate_conf_count",
        "rate_conf_alert_count", "last_gps", "eld_provider",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def _row_search_blob(row: dict[str, Any]) -> str:
    parts = [
        row.get("truck_id", ""), row.get("trailer_id", ""), row.get("driver_name", ""),
        row.get("dispatcher", ""), row.get("status", ""), row.get("cell", ""),
        row.get("location", ""), row.get("notes", ""), row.get("planning_note", ""),
        row.get("last_gps", ""), row.get("eld_provider", ""), json.dumps(row.get("raw") or {}, sort_keys=True),
        json.dumps(row.get("rate_confirmations") or [], sort_keys=True),
    ]
    return " ".join(str(part).lower() for part in parts)


def _status_class(status: str) -> str:
    upper = str(status or "").upper()
    if upper == "ACTIVE":
        return "status-active"
    if upper in {"INACTIVE", "OUT", "OFF"}:
        return "status-inactive"
    if upper in {"HOLD", "PENDING", "SHOP", "VACATION"}:
        return "status-warning"
    return "status-other"


def _is_no_gps(value: str) -> bool:
    return str(value or "").strip().upper() in {"", "N/A", "NA", "NAN", "NONE", "NULL"}


def _date_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = _parse_dt(text)
    if parsed:
        try:
            return f"{parsed.month}/{parsed.day:02d}"
        except Exception:
            return text
    return text


def _short_dt(value: object) -> str:
    parsed = _parse_dt(str(value or ""))
    if parsed:
        return parsed.strftime("%m/%d %H:%M")
    return str(value or "")[:16] or "—"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_gps_number(value: object, *, suffix: str = "") -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) < 0.005:
        number = 0.0
    return f"{number:.1f}{suffix}"


def _format_coords(lat: object, lon: object) -> str:
    try:
        if lat is None or lon is None or lat == "" or lon == "":
            return ""
        return f"{float(lat):.5f}, {float(lon):.5f}"
    except (TypeError, ValueError):
        return ""


def _parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.upper() == "N/A":
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%m/%d/%y", "%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _h(value: object) -> str:
    return escape(str(value or ""))
