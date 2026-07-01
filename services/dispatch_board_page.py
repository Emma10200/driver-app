"""Dispatcher-friendly read-only web view of the Google Sheet dispatch board."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from html import escape
from io import StringIO
from typing import Any

import streamlit as st

from services.dispatch_board_data import load_dispatch_board_rows


DISPATCH_BOARD_SHEET_URL = "https://docs.google.com/spreadsheets/d/1Bk2p4B8oztxNew1Ei0D7s8UK2gQOsWXftmeRBZnsCII/edit?gid=0#gid=0"


_BOARD_CSS = """
<style>
.dispatch-hero {
  border: 1px solid rgba(148,163,184,.24);
  border-radius: 18px;
  padding: 1.15rem 1.25rem;
  background: linear-gradient(135deg, rgba(15,23,42,.96), rgba(30,41,59,.90));
  box-shadow: 0 14px 36px rgba(2,6,23,.22);
  margin-bottom: 1rem;
}
.dispatch-hero h1 { margin: 0 0 .25rem; font-size: 1.85rem; }
.dispatch-hero p { margin: 0; color: #cbd5e1; }
.dispatch-section {
  margin: 1.35rem 0 .55rem;
  padding: .45rem .75rem;
  border-radius: 999px;
  background: #111827;
  border: 1px solid rgba(148,163,184,.32);
  font-weight: 800;
  letter-spacing: .02em;
}
.dispatch-card {
  display: grid;
  grid-template-columns: 82px 108px minmax(190px,1.3fr) 124px minmax(145px,1fr) 88px minmax(150px,1fr) minmax(170px,1fr) 102px 112px;
  gap: 0;
  align-items: stretch;
  border: 1px solid rgba(148,163,184,.24);
  border-radius: 12px;
  margin: .32rem 0;
  overflow: hidden;
  background: rgba(255,255,255,.025);
}
.dispatch-cell {
  padding: .48rem .55rem;
  border-right: 1px solid rgba(148,163,184,.16);
  min-height: 42px;
  display: flex;
  align-items: center;
  font-size: .88rem;
  line-height: 1.22;
}
.dispatch-cell:last-child { border-right: 0; }
.dispatch-header-row {
  position: sticky;
  top: 0;
  z-index: 2;
  background: #0f172a;
  color: #e2e8f0;
  font-weight: 800;
  text-transform: uppercase;
  font-size: .72rem;
  letter-spacing: .045em;
}
.dispatch-unit { font-weight: 800; font-size: 1rem; color: #dbeafe; }
.dispatch-trailer { font-weight: 800; color: #bbf7d0; }
.dispatch-note { font-weight: 700; color: #fde68a; }
.dispatch-muted { color: #94a3b8; }
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
.status-active { background: rgba(34,197,94,.18); color: #bbf7d0; border-color: rgba(34,197,94,.35); }
.status-inactive { background: rgba(148,163,184,.16); color: #cbd5e1; border-color: rgba(148,163,184,.28); }
.status-warning { background: rgba(245,158,11,.18); color: #fde68a; border-color: rgba(245,158,11,.35); }
.status-other { background: rgba(59,130,246,.16); color: #bfdbfe; border-color: rgba(59,130,246,.30); }
.gps-na { color: #fca5a5; font-weight: 800; }
@media (max-width: 1200px) {
  .dispatch-card { grid-template-columns: 72px 96px minmax(180px,1.3fr) 110px minmax(135px,1fr) 78px minmax(140px,1fr) minmax(155px,1fr) 92px 100px; }
  .dispatch-cell { font-size: .8rem; padding: .42rem; }
}
</style>
"""


def render_dispatch_board_page() -> None:
    st.markdown(_BOARD_CSS, unsafe_allow_html=True)
    rows = load_dispatch_board_rows()

    st.markdown(
        """
        <div class="dispatch-hero">
          <h1>Dispatch Board</h1>
          <p>Read-only web mirror of the dispatcher sheet, grouped the same way dispatchers already think about it.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not rows:
        st.warning("No dispatch board rows are in Supabase yet. Run Utilities → ⬆️ Publish Dispatch Board to Supabase in the Sheet.")
        st.link_button("Open Google Sheet", DISPATCH_BOARD_SHEET_URL)
        return

    rows = [_normalize_row(row) for row in rows]
    latest_publish = max((str(row.get("source_updated_at") or "") for row in rows), default="")
    dispatchers = sorted({row["dispatcher"] for row in rows if row["dispatcher"]})
    statuses = sorted({row["status"] for row in rows if row["status"]})

    _render_metrics(rows, dispatchers, latest_publish)
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

    _render_board(filtered)
    _render_raw_inspector(filtered)


def _render_metrics(rows: list[dict[str, Any]], dispatchers: list[str], latest_publish: str) -> None:
    active = sum(1 for row in rows if row["status"].upper() == "ACTIVE")
    no_gps = sum(1 for row in rows if _is_no_gps(row.get("last_gps", "")))
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Board Rows", len(rows))
    col2.metric("Active", active)
    col3.metric("Dispatchers", len(dispatchers))
    col4.metric("No GPS / N/A", no_gps)
    col5.metric("Last Mirror", _short_dt(latest_publish))


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
    labels = ["Truck", "Trailer", "Driver", "Cell", "Location", "Date", "Notes", "Planning", "Status", "GPS"]
    cells = "".join(f'<div class="dispatch-cell">{label}</div>' for label in labels)
    st.markdown(f'<div class="dispatch-card dispatch-header-row">{cells}</div>', unsafe_allow_html=True)


def _row_card_html(row: dict[str, Any]) -> str:
    gps = _h(row["last_gps"] or "N/A")
    gps_class = "gps-na" if _is_no_gps(row["last_gps"]) else ""
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
        f'<div class="dispatch-cell {gps_class}">{gps}<br><span class="dispatch-muted">{_h(row["eld_provider"])}</span></div>',
        '</div>',
    ])


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
    fields = ["sheet_row", "dispatcher", "truck_id", "trailer_id", "driver_name", "cell", "location", "date_text", "notes", "planning_note", "status", "last_gps", "eld_provider"]
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
