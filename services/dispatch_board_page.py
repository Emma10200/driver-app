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
from services.rate_confirmation_data import (
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
    grid-template-columns: 74px 92px minmax(165px,1.25fr) 112px minmax(130px,1fr) 76px minmax(145px,1.05fr) minmax(150px,1fr) 92px 126px 108px;
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
    .dispatch-card { grid-template-columns: 68px 84px minmax(150px,1.2fr) 98px minmax(120px,1fr) 70px minmax(130px,1fr) minmax(130px,1fr) 84px 112px 98px; }
    .dispatch-cell { font-size: .73rem; padding: .36rem; }
}
</style>
"""


def render_dispatch_board_page() -> None:
    st.markdown(_BOARD_CSS, unsafe_allow_html=True)
    rows = load_dispatch_board_rows()
    try:
        rate_docs = [normalize_rate_confirmation_doc(doc) for doc in load_rate_confirmation_documents(days=14)]
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

    st.link_button("Open editable Google Sheet", DISPATCH_BOARD_SHEET_URL)
    st.download_button(
        "Download filtered CSV",
        _rows_to_csv(filtered),
        file_name="dispatch_board_filtered.csv",
        mime="text/csv",
        use_container_width=False,
    )

    if not filtered:
        st.info("No rows match the current filters.")
        return

    _render_alert_bell(alerts)
    _render_board(filtered)
    _render_raw_inspector(filtered)


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
    with st.sidebar:
        st.subheader("Dispatch Board Filters")
        query = st.text_input("Search truck, trailer, driver, city, note", "").strip().lower()
        selected_dispatchers = st.multiselect("Dispatcher", dispatchers, default=dispatchers)
        selected_statuses = st.multiselect("Status", statuses, default=statuses)
        hide_no_unit = st.toggle("Hide rows without truck/trailer", value=True)
        st.toggle("Show raw JSON inspector", value=False, key="dispatch_show_raw")
        st.caption("Raw inspector is useful while tuning the web UI against the real sheet shape.")

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
        st.markdown(
            f'<div class="dispatch-section">--- {_h(dispatcher)} --- <span class="dispatch-muted">{len(section_rows)} rows</span></div>',
            unsafe_allow_html=True,
        )
        _render_header_row()
        for row in section_rows:
            st.markdown(_row_card_html(row), unsafe_allow_html=True)


def _render_header_row() -> None:
    labels = ["Truck", "Trailer", "Driver", "Cell", "Location", "Date", "Notes", "Planning", "Status", "Rate Conf", "GPS"]
    cells = "".join(f'<div class="dispatch-cell">{label}</div>' for label in labels)
    st.markdown(f'<div class="dispatch-card dispatch-header-row">{cells}</div>', unsafe_allow_html=True)


def _row_card_html(row: dict[str, Any]) -> str:
    return "".join([
        '<div class="dispatch-card">',
        f'<div class="dispatch-cell dispatch-unit">{_h(row["truck_id"])}</div>',
        f'<div class="dispatch-cell dispatch-trailer">{_h(row["trailer_id"])}</div>',
        f'<div class="dispatch-cell"><div><b>{_h(row["driver_name"])}</b><br><span class="dispatch-muted">row {int(row.get("sheet_row") or 0)}</span></div></div>',
        f'<div class="dispatch-cell">{_h(row["cell"])}</div>',
        f'<div class="dispatch-cell">{_h(row["location"])}</div>',
        f'<div class="dispatch-cell"><b>{_h(row["date_text"])}</b></div>',
        f'<div class="dispatch-cell dispatch-note">{_h(row["notes"])}</div>',
        f'<div class="dispatch-cell">{_h(row["planning_note"])}</div>',
        f'<div class="dispatch-cell"><span class="status-pill {_status_class(row["status"])}">{_h(row["status"] or "—")}</span></div>',
        f'<div class="dispatch-cell">{_rate_conf_cell_html(row)}</div>',
        f'<div class="dispatch-cell">{_gps_cell_html(row)}</div>',
        '</div>',
    ])


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


def _render_alert_bell(alerts: list[dict[str, Any]]) -> None:
    """Compact bell-badge notification: click to expand the alert list."""
    count = len(alerts)
    badge_html = (
        f'<div class="alert-bell-badge">'
        f'🔔 Alerts'
        f'<span class="alert-bell-count">{count}</span>'
        f'</div>'
    ) if count else '<span class="dispatch-muted">🔔 No alerts</span>'
    with st.popover(f"🔔 {count} Alert{'s' if count != 1 else ''}" if count else "🔔 Alerts", use_container_width=False):
        if not alerts:
            st.caption("No rate-confirmation alerts right now.")
            return
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for doc in alerts:
            dispatcher = str(doc.get("board_dispatcher") or "").strip() or "Unmatched / Needs Review"
            grouped[dispatcher].append(doc)
        for dispatcher in sorted(grouped):
            st.markdown(f'<div class="alert-group-header">{_h(dispatcher)}</div>', unsafe_allow_html=True)
            for doc in grouped[dispatcher][:15]:
                level = _doc_alert_level(doc) or "info"
                truck = str(doc.get("matched_truck_id") or "").strip() or "no truck"
                notes = str(doc.get("alert_notes") or "").strip() or _rate_doc_title(doc)
                meta = " · ".join(part for part in [
                    f"Truck {truck}",
                    str(doc.get("match_status") or ""),
                    _short_dt(doc.get("received_at")),
                ] if part)
                st.markdown(
                    f'<div class="alert-list-item alert-list-item-{_h(level)}">'
                    f'<b>{_h(meta)}</b><br>'
                    f'{_h(notes)}<br>'
                    f'<span class="dispatch-muted">{_h(_rate_doc_title(doc))}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )


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
