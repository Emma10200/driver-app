"""
GPS Map & Trailer-Truck Matching page for Streamlit.
Shows all assets on a map, auto-matches trailers to nearby trucks,
and draws connection lines with confidence scores.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from html import escape
from io import StringIO
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import streamlit as st
import streamlit.components.v1 as components

from services.gps_data import (
    build_yard_mode_timeline,
    deactivate_manual_pair_assignment,
    load_asset_hour_coverage,
    load_assignments,
    load_current_assets_with_last_known,
    load_evidence_truck_map,
    load_hourly_evidence_rows,
    load_hourly_evidence_timeline,
    load_latest_pairing_job,
    load_manual_pair_assignments,
    load_trailer_drop_events,
    load_trailer_activity_summary,
    load_trailer_gps_trail,
    load_trailer_unmatched_windows,
    load_truck_gps_trail,
    load_usage_daily_summary,
    load_yard_proximity_pings,
    save_manual_pair_assignment,
)
from services.gps_matching import (
    Asset,
    MatchResult,
    TimelineSegment,
    YARD_GEOFENCES,
    in_yard,
    YARD_BOXES,
)


# ---------------------------------------------------------------------------
# Cached data loaders — avoid redundant Supabase round-trips within a session.
# TTL keeps data fresh without re-fetching on every widget interaction.
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300, show_spinner=False)
def _cached_hourly_timeline(unit_id: str, unit_type: str, start_iso: str, end_iso: str):
    """Cached wrapper for hourly evidence timeline (5-min TTL)."""
    from datetime import datetime, timezone
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return load_hourly_evidence_timeline(unit_id, unit_type, start, end)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_yard_pings(yard_name: str, start_iso: str, end_iso: str):
    """Cached wrapper for yard proximity pings (5-min TTL)."""
    from datetime import datetime, timezone
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return load_yard_proximity_pings(yard_name, start, end)


@st.cache_data(ttl=60, show_spinner=False)
def _cached_current_assets():
    """Cached wrapper for current asset positions (1-min TTL)."""
    return load_current_assets_with_last_known(stale_after_days=30)


def render_gps_map_page() -> None:
    st.header("GPS Fleet Map")

    # --- Load FAST data first (current positions only) ---
    with st.spinner("Loading positions..."):
        assets = _cached_current_assets()
        assignments = load_assignments()

    if not assets:
        st.warning("No GPS data found in Supabase. Ensure the dispatch board publisher is running.")
        return

    trucks = [a for a in assets if a.asset_type == "truck"]
    trailers = [a for a in assets if a.asset_type == "trailer"]

    divisions = sorted({a.division for a in assets if a.division})
    providers = sorted({a.provider for a in assets if a.provider})
    (
        active_divs,
        show_no_div,
        active_providers,
        show_trucks,
        show_trailers,
        show_historical,
        unit_search,
    ) = _render_map_controls(assets, divisions, providers)

    # --- Filter assets by toggles ---
    def _visible(a: Asset) -> bool:
        if a.asset_type == "truck" and not show_trucks:
            return False
        if a.asset_type == "trailer" and not show_trailers:
            return False
        if _is_historical_last_known(a) and not show_historical:
            return False
        if a.division:
            if a.division not in active_divs:
                return False
        elif not show_no_div:
            return False
        if a.provider and a.provider not in active_providers:
            return False
        return True

    visible_trucks = [t for t in trucks if _visible(t)]
    visible_trailers = [t for t in trailers if _visible(t)]
    visible_assets = [a for a in assets if _visible(a)]

    _render_sidebar_fleet_metrics(visible_trucks, visible_trailers, visible_assets, active_providers)

    focused_asset = _render_map_lookup_panel(visible_assets, unit_search)

    # --- Map (renders FIRST with just current positions) ---
    _render_map(visible_trucks, visible_trailers, divisions, assignments, focus_asset=focused_asset)

    # --- Tabs: dispatcher-friendly first, audit behind ---
    tab_fleet, tab_history, tab_timeline, tab_drops, tab_usage = st.tabs([
        "🚛 Fleet Overview", "🗺️ Unit History", "📍 Unit Timeline", "📦 Dropped Trailers", "📊 Trailer Usage & Billing",
    ])

    with tab_fleet:
        _render_fleet_overview_tab(visible_assets, assignments, unit_search)

    with tab_history:
        _render_unit_history_tab(visible_assets)

    with tab_timeline:
        _render_timeline_tab(visible_assets)

    with tab_drops:
        _render_dropped_trailers_tab()

    with tab_usage:
        _render_usage_dashboard(visible_assets, assignments, unit_search)


def _render_map(
    trucks: list[Asset],
    trailers: list[Asset],
    divisions: list[str],
    assignments: dict[str, str],
    *,
    focus_asset: Asset | None = None,
) -> None:
    """Render the pydeck map with current positions only (fast)."""
    try:
        import pydeck as pdk
    except ImportError:
        st.error("Install pydeck: `pip install pydeck`")
        return

    layers = []
    division_colors = _division_color_map(divisions)
    focus_key = f"{focus_asset.asset_type}:{focus_asset.asset_id}" if focus_asset and _has_coords(focus_asset) else ""

    truck_data = [
        {
            "lat": t.lat, "lon": t.lon, "id": t.asset_id,
            "division": _division_display(t.division), "provider": _provider_display(t.provider),
            "address": t.address, "coords": _coords(t),
            "last_ping": _last_ping_text(t), "ping_age": _ping_age_text(t),
            "location_status": _location_status(t), "status_note": _location_note(t),
            "maps_url": _maps_url(t),
            "color": [34, 197, 94, 245] if f"truck:{t.asset_id}" == focus_key else _asset_marker_color(t, _truck_color(t.division, division_colors)),
            "radius": 1150 if f"truck:{t.asset_id}" == focus_key else 700,
            "asset_type": "Truck",
        }
        for t in trucks if _has_coords(t)
    ]
    if truck_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=truck_data,
            get_position=["lon", "lat"], get_radius="radius",
            radius_min_pixels=14, radius_max_pixels=42,
            get_fill_color="color", stroked=True,
            get_line_color=[255, 255, 255, 220], get_line_width=2,
            pickable=True, auto_highlight=True,
        ))
        layers.append(pdk.Layer(
            "TextLayer", data=truck_data,
            get_position=["lon", "lat"], get_text="id",
            get_size=13, get_color=[255, 255, 255, 235],
            get_angle=0, get_text_anchor="middle", get_alignment_baseline="center",
            pickable=False,
        ))

    trailer_data = [
        {
            "lat": t.lat, "lon": t.lon, "id": t.asset_id,
            "division": _division_display(t.division), "provider": _provider_display(t.provider),
            "address": t.address, "coords": _coords(t),
            "last_ping": _last_ping_text(t), "ping_age": _ping_age_text(t),
            "location_status": _location_status(t), "status_note": _location_note(t),
            "maps_url": _maps_url(t),
            "in_yard": in_yard(t.lat, t.lon) or "",
            "color": [34, 197, 94, 245] if f"trailer:{t.asset_id}" == focus_key else _asset_marker_color(t, [255, 140, 0, 230]),
            "radius": 1100 if f"trailer:{t.asset_id}" == focus_key else 650,
            "asset_type": "Trailer",
        }
        for t in trailers if _has_coords(t)
    ]
    if trailer_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=trailer_data,
            get_position=["lon", "lat"], get_radius="radius",
            radius_min_pixels=13, radius_max_pixels=40,
            get_fill_color="color", stroked=True,
            get_line_color=[255, 255, 255, 220], get_line_width=2,
            pickable=True, auto_highlight=True,
        ))
        layers.append(pdk.Layer(
            "TextLayer", data=trailer_data,
            get_position=["lon", "lat"], get_text="id",
            get_size=12, get_color=[20, 24, 31, 245],
            get_angle=0, get_text_anchor="middle", get_alignment_baseline="center",
            pickable=False,
        ))

    all_coords = truck_data + trailer_data
    if focus_asset and _has_coords(focus_asset):
        center_lat = float(focus_asset.lat)
        center_lon = float(focus_asset.lon)
        zoom = 13
    elif all_coords:
        center_lat = sum(d["lat"] for d in all_coords) / len(all_coords)
        center_lon = sum(d["lon"] for d in all_coords) / len(all_coords)
        zoom = 5
    else:
        center_lat, center_lon = 39.8, -89.6
        zoom = 5

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0),
        tooltip={
            "html": (
                "<div style='font-size:13px;line-height:1.35;'>"
                "<b style='font-size:15px'>{asset_type}: {id}</b><br/>"
                "<b>Status:</b> {location_status}<br/>"
                "<b>Note:</b> {status_note}<br/>"
                "<b>Ping:</b> {last_ping}<br/>"
                "<b>Age:</b> {ping_age}<br/>"
                "<b>Coords:</b> {coords}<br/>"
                "<a style='color:#93c5fd' href='{maps_url}' target='_blank'>🗺️ Open in Google Maps</a><br/>"
                "<b>Division:</b> {division}<br/>"
                "<b>GPS:</b> {provider}<br/>"
                "<b>Address:</b> {address}"
                "</div>"
            ),
            "style": {"backgroundColor": "#111827", "color": "#f9fafb", "border": "1px solid #374151"},
        },
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    )
    st.pydeck_chart(deck)
    _render_map_legend(division_colors)


@st.fragment
def _render_fleet_overview_tab(assets: list[Asset], assignments: dict[str, str], unit_search: str) -> None:
    """Clean dispatcher-friendly fleet table with search, sort, and action buttons."""
    # Build reverse lookup: trailer → truck from dispatch board
    board_by_trailer: dict[str, str] = {
        trailer_id: truck_id for truck_id, trailer_id in assignments.items() if trailer_id
    }

    # Apply the same unit search that affects the map
    search_norm = (unit_search or "").strip().lower()

    # Build simple fleet rows
    fleet_rows: list[dict[str, Any]] = []
    for asset in sorted(assets, key=lambda a: (a.asset_type, _unit_sort_key(a.asset_id))):
        board_partner = ""
        if asset.asset_type == "trailer":
            board_partner = board_by_trailer.get(asset.asset_id, "")
        elif asset.asset_type == "truck":
            board_partner = assignments.get(asset.asset_id, "")

        coords = _coords(asset)
        row: dict[str, Any] = {
            "Type": asset.asset_type.title(),
            "Unit": asset.asset_id,
            "Dispatch Partner": board_partner,
            "Division": _division_display(asset.division),
            "Provider": _provider_display(asset.provider),
            "Location": _location_status(asset),
            "Last Ping": _last_ping_text(asset),
            "Age": _ping_age_text(asset),
            "Coords": coords,
            "Yard": in_yard(float(asset.lat), float(asset.lon)) if _has_coords(asset) else "",
            "Speed": asset.speed if asset.speed else "",
            "Address": asset.address or "",
        }

        if search_norm:
            haystack = " ".join(str(v).lower() for v in row.values() if v)
            if search_norm not in haystack:
                continue
        fleet_rows.append(row)

    # Summary metrics
    n_trucks = sum(1 for r in fleet_rows if r["Type"] == "Truck")
    n_trailers = sum(1 for r in fleet_rows if r["Type"] == "Trailer")
    n_in_yard = sum(1 for r in fleet_rows if r["Yard"])
    n_moving = sum(1 for r in fleet_rows if r["Speed"] and str(r["Speed"]) not in ("0", "0.0", ""))
    with st.sidebar:
        c1, c2 = st.columns(2)
        c1.metric("Trucks", n_trucks)
        c2.metric("Trailers", n_trailers)
        c3, c4 = st.columns(2)
        c3.metric("In Yard", n_in_yard)
        c4.metric("Moving", n_moving)

    if not fleet_rows:
        st.info("No units match the current filters." + (" Try clearing the search." if search_norm else ""))
        return

    # Sort options
    sort_col = st.selectbox(
        "Sort by",
        ["Unit", "Type", "Division", "Provider", "Age", "Dispatch Partner", "Yard"],
        key="fleet_overview_sort",
    )
    sort_map = {
        "Unit": lambda r: _unit_sort_key(str(r["Unit"])),
        "Type": lambda r: (r["Type"], _unit_sort_key(str(r["Unit"]))),
        "Division": lambda r: (str(r["Division"] or "~"), _unit_sort_key(str(r["Unit"]))),
        "Provider": lambda r: (str(r["Provider"] or "~"), _unit_sort_key(str(r["Unit"]))),
        "Age": lambda r: (str(r["Age"] or "~"), _unit_sort_key(str(r["Unit"]))),
        "Dispatch Partner": lambda r: (0 if r["Dispatch Partner"] else 1, _unit_sort_key(str(r["Unit"]))),
        "Yard": lambda r: (0 if r["Yard"] else 1, _unit_sort_key(str(r["Unit"]))),
    }
    fleet_rows.sort(key=sort_map.get(sort_col, sort_map["Unit"]))

    # Render clean dataframe with link column for coords
    display_rows = []
    for r in fleet_rows:
        display_rows.append({
            "Type": r["Type"],
            "Unit": r["Unit"],
            "Dispatch Partner": r["Dispatch Partner"],
            "Division": r["Division"],
            "Location": r["Location"],
            "Last Ping": r["Last Ping"],
            "Age": r["Age"],
            "Yard": r["Yard"],
            "Speed": r["Speed"],
            "Coords": r["Coords"],
            "Provider": r["Provider"],
        })

    st.dataframe(
        display_rows,
        use_container_width=True,
        hide_index=True,
        height=min(600, 40 + 35 * len(display_rows)),
        column_config={
            "Type": st.column_config.TextColumn(width="small"),
            "Unit": st.column_config.TextColumn(width="small"),
            "Dispatch Partner": st.column_config.TextColumn(width="small"),
            "Speed": st.column_config.TextColumn(width="small"),
        },
    )

    # Downloadable CSV
    csv_data = _rows_to_csv(display_rows)
    st.download_button(
        "⬇️ Download fleet CSV",
        data=csv_data,
        file_name="fleet_overview.csv",
        mime="text/csv",
        key="fleet_overview_download",
    )


@st.fragment
def _render_dropped_trailers_tab() -> None:
    """Operational dropped-trailer custody view.

    Reads the compact ``trailer_drop_events`` table so the UI does not scan raw
    GPS. Drops are separate from billable hours: this is a dispatcher/audit
    section for trailers left idle away from excluded yards.
    """
    st.subheader("Dropped Trailers")
    st.caption(
        "Operational custody events: trailers idle for **12+ hours** outside excluded yards. "
        "This is intentionally separate from billable trailer usage."
    )

    today = date.today()
    default_start = today - timedelta(days=14)
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        start_date = st.date_input("Drops from", value=default_start, key="drop_events_start")
    with c2:
        end_date = st.date_input("Drops to", value=today, key="drop_events_end")
    with c3:
        active_only = st.checkbox("Active drops only", value=False, key="drop_events_active_only")

    start_dt, end_dt = _local_date_range_to_utc(start_date, end_date)
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    with st.spinner("Loading dropped trailer events..."):
        events = load_trailer_drop_events(start_dt, end_dt, active_only=active_only)

    if not events:
        st.info(
            "No drop events found for this range. If this is the first setup, apply migration `0022_drop_events_and_hour_tracks.sql`, "
            "then run the hourly-track and drop-event scripts."
        )
        return

    active_count = sum(1 for row in events if str(row.get("status") or "") == "active_drop")
    picked_count = sum(1 for row in events if str(row.get("status") or "") == "picked_up")
    unknown_count = sum(1 for row in events if str(row.get("status") or "") == "unknown_dropper")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Drop Events", len(events))
    m2.metric("Active", active_count)
    m3.metric("Picked Up", picked_count)
    m4.metric("Unknown Dropper", unknown_count)

    display_rows: list[dict[str, Any]] = []
    for row in events:
        lat = row.get("lat")
        lon = row.get("lon")
        maps_url = _maps_url_for_coords(lat, lon)
        status = _drop_status_label(row.get("status"))
        display_rows.append({
            "Trailer": str(row.get("trailer_id") or ""),
            "Status": status,
            "Idle Hours": float(row.get("idle_hours") or 0),
            "Drop Started": _format_dt_text(row.get("drop_started_at")),
            "Drop Ended": _format_dt_text(row.get("drop_ended_at")),
            "Dropped By": str(row.get("dropped_by_truck_id") or ""),
            "Drop Conf": float(row.get("dropped_by_confidence") or 0) * 100.0,
            "Picked Up By": str(row.get("picked_up_by_truck_id") or ""),
            "Pickup Conf": float(row.get("pickup_confidence") or 0) * 100.0,
            "Yard": str(row.get("yard_name") or ""),
            "Address": str(row.get("address") or ""),
            "Maps": maps_url,
            "Stationary Pings": int(row.get("ping_count") or 0),
            "Last Pair Hour": _format_dt_text(row.get("last_pair_hour")),
        })

    st.dataframe(
        display_rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Idle Hours": st.column_config.NumberColumn(format="%.1f"),
            "Drop Conf": st.column_config.NumberColumn(format="%.1f%%"),
            "Pickup Conf": st.column_config.NumberColumn(format="%.1f%%"),
            "Maps": st.column_config.LinkColumn(display_text="Open map"),
        },
    )

    csv_data = _rows_to_csv(display_rows)
    st.download_button(
        "⬇️ Download drop events CSV",
        data=csv_data,
        file_name=f"trailer_drop_events_{start_date}_{end_date}.csv",
        mime="text/csv",
        key="drop_events_download",
    )


@st.fragment
def _render_usage_dashboard(assets: list[Asset], assignments: dict[str, str], unit_search: str) -> None:
    """Main dense-evidence dashboard for trailer usage and truck usage."""
    st.subheader("Trailer Usage Dashboard")

    with st.expander("ℹ️ How matching & billing works — parameters explained", expanded=False):
        st.markdown("""
**Matching Engine Parameters**

| Parameter | Value | Description |
|-----------|-------|-------------|
| Match radius | 0.5 miles | Truck and trailer must be within 0.5 mi in the same hour to be considered a candidate pair |
| Exclusive matching | 1:1 per hour | Each truck can only match ONE trailer per hour, and each trailer can only match ONE truck (greedy assignment by confidence) |
| Movement-evidence gate | ≥ 0.5 mi traveled OR ≥ 1 movement-compatible ping | Stationary-only proximity (parked near each other) does NOT produce a "paired" status — demoted to "near" |
| Confidence threshold | Weighted score | Combines distance (closer = better), ping count, co-movement pattern, and history overlap |
| Yard suppression | CA & TX yards | Hours where both units are inside a known yard polygon are tagged "same_yard" and excluded from billable hours |
| Source data | Dense GPS backfills + 888 live history | Uses accepted backfill sources (gpstab, anytrek, track888, eroad), blank legacy imports, and `truck_publish` for 888 ELD because that provider currently arrives as live 10-minute history |

**Billing Logic**

| Rule | Description |
|------|-------------|
| Paired Hours | Total hours a trailer was exclusively matched to a truck (passed all gates above) |
| Billable Candidate | Stricter — only counts non-yard paired hours with sufficient confidence AND a repeated pattern across multiple service dates |
| Near-only hours | NOT billable — these are proximity detections that failed the movement-evidence gate or exclusivity check |
| Manual assignments | Paper-log drivers confirmed via dispatch board cross-reference — counted as paired, suppresses unmatched alerts |

**Unmatched Trailer Alerts**

| Threshold | Value |
|-----------|-------|
| Minimum unmatched moving hours | ≥ 3 hours |
| Minimum miles moved | ≥ 10 miles |
| Excluded | Trailers in yard zones, manually-assigned trailers |

These parameters are applied automatically by the hourly evidence rebuild script. Data refreshes on each scheduled rebuild.
""")


    latest_job = load_latest_pairing_job()
    if latest_job:
        status = str(latest_job.get("status") or "unknown")
        finished = _format_dt_text(latest_job.get("finished_at"))
        st.info(
            f"Dense evidence job: **{status}** · hourly rows: **{int(latest_job.get('hourly_rows') or 0):,}** · "
            f"daily rows: **{int(latest_job.get('daily_rows') or 0):,}** · finished: **{finished or '—'}**"
        )

    today = date.today()
    default_start = today.replace(day=1)
    c1, c2, c3, c4 = st.columns([1, 1, 1.2, 1.2])
    with c1:
        usage_start = st.date_input("Usage from", value=default_start, key="usage_dash_start")
    with c2:
        usage_end = st.date_input("Usage to", value=today, key="usage_dash_end")
    with c3:
        view_mode = st.radio("Group by", ["Truck", "Trailer"], horizontal=True, key="usage_dash_mode")
    with c4:
        sort_by = st.selectbox(
            "Sort",
            ["Paired hours", "Billable hours", "Unique partners", "Avg confidence", "Unit"],
            key="usage_dash_sort",
        )

    min_hours = st.slider("Minimum paired hours to show", 0, 200, 0, 1, key="usage_dash_min_hours")
    show_near_only = st.checkbox(
        "Show near-only review rows",
        value=False,
        help="Off = assignment/dashboard views only show actual paired evidence. Near rows remain in hourly evidence details.",
        key="usage_dash_show_near_only",
    )
    start_dt, end_dt = _local_date_range_to_utc(usage_start, usage_end)
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    with st.spinner("Loading dense usage summaries..."):
        daily_rows = load_usage_daily_summary(start_dt, end_dt)

    if not daily_rows:
        st.warning("No dense usage summary rows found for this date range yet.")
        with st.expander("All Found Units / Coordinates", expanded=False):
            unit_rows = _build_unit_rows(assets, [], assignments, None, unit_search)
            _render_copy_grid(unit_rows) if unit_rows else st.info("No units match the current filters.")
        return

    primary_type = "truck" if view_mode == "Truck" else "trailer"
    partner_type = "trailer" if primary_type == "truck" else "truck"
    overview_rows, pair_rows_by_unit = _aggregate_usage_rows(daily_rows, primary_type=primary_type)
    if not show_near_only:
        overview_rows = [r for r in overview_rows if r["Paired Hours"] > 0]
        pair_rows_by_unit = {
            unit: [row for row in rows if row["Paired Hours"] > 0]
            for unit, rows in pair_rows_by_unit.items()
        }
    overview_rows = [r for r in overview_rows if r["Paired Hours"] >= min_hours]
    overview_rows = _sort_usage_overview(overview_rows, sort_by)

    paired_hours = sum(float(r.get("paired_hours") or 0) for r in daily_rows)
    billable_hours = sum(float(r.get("billable_candidate_hours") or 0) for r in daily_rows)
    unique_trucks = len({str(r.get("truck_id") or "") for r in daily_rows if r.get("truck_id")})
    unique_trailers = len({str(r.get("trailer_id") or "") for r in daily_rows if r.get("trailer_id")})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Paired Hours", f"{paired_hours:,.0f}")
    m2.metric("Billable Candidate", f"{billable_hours:,.0f}")
    m3.metric("Trucks", unique_trucks)
    m4.metric("Trailers", unique_trailers)

    st.markdown(f"#### {view_mode} usage ranking")
    if overview_rows:
        st.dataframe(
            overview_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Paired Hours": st.column_config.NumberColumn(format="%.1f"),
                "Billable Hours": st.column_config.NumberColumn(format="%.1f"),
                "Near Hours": st.column_config.NumberColumn(format="%.1f"),
                "Avg Confidence": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )
    else:
        st.info("No units meet the current filters.")
        return

    unit_options = [str(r["Unit"]) for r in overview_rows]
    selected_unit = st.selectbox(f"Inspect {view_mode.lower()}", unit_options, key="usage_dash_selected_unit")
    detail_rows = pair_rows_by_unit.get(selected_unit, [])
    st.markdown(f"#### {view_mode} {selected_unit} → {partner_type.title()} breakdown")
    if detail_rows:
        st.dataframe(
            detail_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Paired Hours": st.column_config.NumberColumn(format="%.1f"),
                "Billable Hours": st.column_config.NumberColumn(format="%.1f"),
                "Near Hours": st.column_config.NumberColumn(format="%.1f"),
                "Avg Confidence": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )

    hourly_rows = load_hourly_evidence_rows(selected_unit, primary_type, start_dt, end_dt)

    with st.expander(f"Timeline runs for {view_mode} {selected_unit}", expanded=True):
        segments = load_hourly_evidence_timeline(selected_unit, primary_type, start_dt, end_dt)
        if segments:
            _render_timeline_visual(_consolidate_timeline(segments))
            _render_match_path_points(hourly_rows, primary_type=primary_type)
        else:
            st.info("No paired timeline runs found for this unit/range. Near/review rows are still available below.")

    with st.expander(f"Hourly coordinate evidence for {view_mode} {selected_unit}", expanded=False):
        detail_rows = _build_hourly_evidence_detail_rows(hourly_rows, primary_type=primary_type)
        if detail_rows:
            st.caption(
                "This is the row-level evidence behind the dashboard. Use review flags, local times, coords, yards, addresses, "
                "distance, and ping gap to verify whether a pairing was road evidence, an anomaly, or just yard/near-yard noise."
            )
            st.dataframe(
                detail_rows,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Confidence": st.column_config.NumberColumn(format="%.1f%%"),
                    "Distance (mi)": st.column_config.NumberColumn(format="%.3f"),
                    "Ping Gap (min)": st.column_config.NumberColumn(format="%.2f"),
                },
            )
            csv_data = _rows_to_csv(detail_rows)
            st.download_button(
                "Download hourly evidence CSV",
                data=csv_data,
                file_name=f"hourly_evidence_{primary_type}_{selected_unit}_{usage_start}_{usage_end}.csv",
                mime="text/csv",
                key=f"usage_hourly_evidence_download_{primary_type}_{selected_unit}",
            )
        else:
            st.info("No raw hourly evidence rows found for this selected unit/range.")

    with st.expander("All Found Units / Coordinates", expanded=False):
        unit_rows = _build_unit_rows(assets, [], assignments, None, unit_search)
        if unit_rows:
            _render_copy_grid(unit_rows)
        else:
            st.info("No units match the current filters.")

    # --- Unmatched Moving Trailers Alert ---
    _render_unmatched_trailers_alert(start_dt, end_dt)

    # --- Manual Pair Assignments ---
    _render_manual_assignments_section()


def _render_unmatched_trailers_alert(start_dt: datetime, end_dt: datetime) -> None:
    """Collapsible warning section for trailers moving without matches."""
    activity_rows = load_trailer_activity_summary(start_dt, end_dt)
    if not activity_rows:
        return

    # Load manual assignments to exclude manually-paired trailers
    manual_assignments = load_manual_pair_assignments(active_only=True)
    manually_assigned_trailers = {str(a.get("trailer_id") or "") for a in manual_assignments}

    # Load dispatch board assignments for cross-reference (trailer → truck)
    dispatch_map = load_assignments()  # truck_id -> trailer_id
    trailer_to_truck: dict[str, str] = {}
    for truck, trailer in dispatch_map.items():
        if trailer:
            trailer_to_truck[str(trailer)] = str(truck)

    # Load GPS-evidence pairings — this is the SOURCE OF TRUTH for who actually
    # pulled each trailer, regardless of whether the truck is still on the board.
    evidence_truck_map = load_evidence_truck_map(start_dt, end_dt)

    # Aggregate per trailer across the date range
    trailer_agg: dict[str, dict[str, Any]] = {}
    for row in activity_rows:
        tid = str(row.get("trailer_id") or "")
        if not tid:
            continue
        if tid in manually_assigned_trailers:
            continue
        acc = trailer_agg.setdefault(tid, {
            "trailer_id": tid,
            "active_hours": 0,
            "moving_hours": 0,
            "miles_moved": 0.0,
            "paired_hours": 0,
            "unmatched_moving_hours": 0,
            "in_yard_hours": 0,
        })
        acc["active_hours"] += int(row.get("active_hours") or 0)
        acc["moving_hours"] += int(row.get("moving_hours") or 0)
        acc["miles_moved"] += float(row.get("miles_moved") or 0)
        acc["paired_hours"] += int(row.get("paired_hours") or 0)
        acc["unmatched_moving_hours"] += int(row.get("unmatched_moving_hours") or 0)
        acc["in_yard_hours"] += int(row.get("in_yard_hours") or 0)

    # Exclude trailers that have strong GPS-evidence pairing — even if the
    # truck is no longer on the dispatch board. GPS evidence is primary truth.
    for tid, trucks in evidence_truck_map.items():
        if tid not in trailer_agg:
            continue
        total_evidence_hours = sum(trucks.values())
        agg = trailer_agg[tid]
        # If GPS evidence shows this trailer was paired for most of its moving
        # hours, it's NOT truly unmatched — the truck was just deactivated.
        if total_evidence_hours >= max(agg["moving_hours"] * 0.5, 3):
            # Attach the evidence truck to the trailer data for display
            best_truck = max(trucks, key=trucks.get)
            agg["evidence_truck"] = best_truck
            agg["evidence_hours"] = total_evidence_hours
            # Reduce unmatched hours by evidence-paired hours
            agg["unmatched_moving_hours"] = max(0, agg["unmatched_moving_hours"] - total_evidence_hours)

    # Filter: only show trailers with significant unmatched movement
    alerts = [
        v for v in trailer_agg.values()
        if v["unmatched_moving_hours"] >= 3 and v["miles_moved"] >= 10.0
    ]
    if not alerts:
        return

    alerts.sort(key=lambda r: r["unmatched_moving_hours"], reverse=True)

    alert_trailers = [str(a["trailer_id"]) for a in alerts]
    alert_dispatch_trucks = [
        trailer_to_truck.get(str(a["trailer_id"]), "")
        for a in alerts
        if trailer_to_truck.get(str(a["trailer_id"]), "")
    ]
    trailer_coverage = load_asset_hour_coverage("trailer", alert_trailers, start_dt, end_dt)
    truck_coverage = load_asset_hour_coverage("truck", alert_dispatch_trucks, start_dt, end_dt)

    active_route_tid = str(st.session_state.get("active_unmatched_route_trailer") or "")
    alert_ids = {str(a["trailer_id"]) for a in alerts}
    if active_route_tid and active_route_tid not in alert_ids:
        st.session_state.pop("active_unmatched_route_trailer", None)
        active_route_tid = ""

    with st.expander(f"⚠️ Unmatched Moving Trailers ({len(alerts)})", expanded=bool(active_route_tid)):
        st.caption(
            "Trailers with significant GPS movement but few or no matched truck hours. "
            "GPS evidence is the primary source of truth — trailers paired in GPS data "
            "are excluded even if the truck was deactivated from the dispatch board. "
            "The GPS coverage columns help identify whether the trailer side or board-assigned truck side is missing GPS data."
        )
        display_rows = []
        for a in alerts:
            match_pct = (a["paired_hours"] / max(1, a["moving_hours"])) * 100.0
            dispatch_truck = trailer_to_truck.get(a["trailer_id"], "—")
            evidence_truck = a.get("evidence_truck", "")
            truck_display = dispatch_truck
            if evidence_truck and evidence_truck != dispatch_truck:
                truck_display = f"{evidence_truck} (GPS)" if not dispatch_truck or dispatch_truck == "—" else f"{dispatch_truck} / {evidence_truck} (GPS)"
            trailer_cov = trailer_coverage.get(str(a["trailer_id"]), {})
            truck_cov = truck_coverage.get(str(dispatch_truck), {}) if dispatch_truck and dispatch_truck != "—" else {}
            display_rows.append({
                "Trailer": a["trailer_id"],
                "Truck": truck_display,
                "GPS Gap": _gps_gap_label(trailer_cov, truck_cov, dispatch_truck),
                "Trailer GPS": _coverage_summary(trailer_cov),
                "Truck GPS": _coverage_summary(truck_cov),
                "Moving Hours": a["moving_hours"],
                "Paired Hours": a["paired_hours"],
                "Unmatched Hours": a["unmatched_moving_hours"],
                "Miles Moved": round(a["miles_moved"], 1),
                "Match %": round(match_pct, 1),
                "Yard Hours": a["in_yard_hours"],
            })
        st.dataframe(
            display_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Miles Moved": st.column_config.NumberColumn(format="%.1f"),
                "Match %": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )

        # --- One-click confirm & expandable trail per trailer ---
        for idx, a in enumerate(alerts):
            tid = a["trailer_id"]
            dispatch_truck = trailer_to_truck.get(tid, "")
            header_parts = [f"**{tid}**"]
            if dispatch_truck:
                header_parts.append(f"→ Dispatch truck: **{dispatch_truck}**")
            header_parts.append(f"{a['unmatched_moving_hours']}h unmatched, {round(a['miles_moved'], 1)} mi")

            route_is_active = active_route_tid == str(tid)
            with st.expander(" · ".join(header_parts), expanded=route_is_active):
                # Quick-confirm button if dispatch board has a truck
                if dispatch_truck:
                    st.info(
                        f"Trailer **{tid}** is assigned to truck **{dispatch_truck}** on the dispatch board. "
                        "If this is a paper-log driver, confirm below to suppress future alerts."
                    )
                    if st.button(
                        f"✅ Confirm {tid} ↔ {dispatch_truck}",
                        key=f"confirm_unmatched_{idx}_{tid}",
                    ):
                        ok = save_manual_pair_assignment({
                            "truck_id": dispatch_truck,
                            "trailer_id": tid,
                            "start_date": start_dt.date().isoformat(),
                            "assigned_by": "dispatch_board_confirm",
                            "notes": f"One-click confirmed from unmatched alert. Dispatch board shows {dispatch_truck}↔{tid}.",
                            "active": True,
                        })
                        if ok:
                            st.success(f"Confirmed! {tid} ↔ {dispatch_truck} saved. It will be excluded from future alerts.")
                            st.rerun()
                        else:
                            st.error("Failed to save confirmation. Check logs.")
                else:
                    st.warning(
                        f"Trailer **{tid}** is **not on the dispatch board**. "
                        "No truck assigned — investigate whether GPS is missing or the trailer is unassigned."
                    )

                # Persistent GPS trail panel. Buttons are momentary in Streamlit;
                # storing the active trailer prevents the route/overlay widgets
                # from disappearing when typing a custom truck ID reruns the app.
                c_show, c_hide = st.columns([1, 1])
                with c_show:
                    if st.button(f"🗺️ Show travel route for {tid}", key=f"trail_btn_{idx}_{tid}"):
                        st.session_state["active_unmatched_route_trailer"] = str(tid)
                        st.rerun()
                with c_hide:
                    if route_is_active and st.button("Hide route", key=f"hide_trail_btn_{idx}_{tid}"):
                        st.session_state.pop("active_unmatched_route_trailer", None)
                        st.rerun()

                if route_is_active:
                    suggested_trucks = [t for t in [dispatch_truck, a.get("evidence_truck", "")] if t]
                    _render_unmatched_trailer_trail(tid, start_dt, end_dt, suggested_trucks=suggested_trucks)


def _render_unmatched_trailer_trail(
    trailer_id: str,
    start_dt: datetime,
    end_dt: datetime,
    *,
    suggested_trucks: list[str] | None = None,
) -> None:
    """Show GPS trail for an unmatched trailer on a pydeck map, with timestamps."""
    try:
        import pydeck as pdk
    except ImportError:
        st.warning("pydeck not available for map rendering.")
        return

    with st.spinner(f"Loading unmatched GPS trail for {trailer_id}..."):
        unmatched_windows = load_trailer_unmatched_windows(trailer_id, start_dt, end_dt)
        raw_trail = load_trailer_gps_trail(trailer_id, start_dt, end_dt)
        trail_segments = _trail_segments_for_windows(raw_trail, unmatched_windows)
        trail = [row for segment in trail_segments for row in segment]

    if not trail:
        st.info(f"No unmatched-window GPS pings found for trailer {trailer_id} in the selected date range.")
        return

    if unmatched_windows:
        st.caption(
            f"Showing **unmatched moving windows only**: {len(unmatched_windows)} segment(s), "
            f"{sum(int(w.get('hours') or 0) for w in unmatched_windows)} hour(s). "
            "Overlay trucks use these same exact windows."
        )
    else:
        st.warning(
            "Could not derive compact unmatched-hour windows, so this route falls back to the selected date range. "
            "Run the asset-hour-track/drop-event jobs if this persists."
        )

    # Parse timestamps for display
    timestamps = []
    for r in trail:
        ts = r.get("recorded_at")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
            except (ValueError, AttributeError):
                pass
    first_ts = min(timestamps) if timestamps else None
    last_ts = max(timestamps) if timestamps else None

    segment_points = [_dedupe_path_points([(r["lat"], r["lon"]) for r in segment]) for segment in trail_segments]
    segment_points = [segment for segment in segment_points if len(segment) >= 2]
    points = [point for segment in segment_points for point in segment]
    if len(points) < 2:
        st.info(f"Only {len(points)} unique GPS point(s) — not enough for a route.")
        return

    # Show timestamp range
    if first_ts and last_ts:
        fmt = "%m/%d %I:%M %p"
        st.caption(
            f"{len(points)} GPS points for trailer {trailer_id}  ·  "
            f"**{first_ts.strftime(fmt)} — {last_ts.strftime(fmt)} UTC**"
        )
    else:
        st.caption(f"{len(points)} GPS points for trailer {trailer_id}")

    # Build timestamped point data for tooltips
    path_data = [
        {
            "trailer": trailer_id,
            "path": [[lon, lat] for lat, lon in segment],
            "color": [249, 115, 22, 200],
            "label": f"Trailer {trailer_id} unmatched segment {i + 1} — {len(segment)} pts",
        }
        for i, segment in enumerate(segment_points)
    ]
    point_data = []
    for i, row in enumerate(trail):
        lat, lon = row.get("lat"), row.get("lon")
        if not lat or not lon:
            continue
        is_start = i == 0
        is_end = i == len(trail) - 1
        color = [34, 197, 94, 240] if is_start else ([239, 68, 68, 240] if is_end else [249, 115, 22, 180])
        ts_str = str(row.get("recorded_at", ""))[:16].replace("T", " ")
        tag = "START" if is_start else ("END" if is_end else f"#{i + 1}")
        point_data.append({
            "lat": float(lat), "lon": float(lon),
            "label": f"{trailer_id} {tag} — {ts_str}",
            "color": color,
        })

    center_lat = sum(lat for lat, _ in points) / len(points)
    center_lon = sum(lon for _, lon in points) / len(points)

    layers = [
        pdk.Layer("PathLayer", data=path_data, get_path="path", get_color="color",
                  width_min_pixels=3, pickable=True),
        pdk.Layer("ScatterplotLayer", data=point_data, get_position=["lon", "lat"],
                  get_radius=400, radius_min_pixels=4, radius_max_pixels=12,
                  get_fill_color="color", pickable=True),
    ]
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=7, pitch=0),
        tooltip={"text": "{label}"},
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    )
    st.pydeck_chart(deck)

    # Google Maps link + copy coords
    maps_url = _google_maps_route_url(points)
    if maps_url:
        st.markdown(f"[Open route in Google Maps]({maps_url})")

    # --- Overlay Truck GPS Lookup ---
    st.markdown("---")
    st.markdown("**Overlay unit GPS**")
    st.caption("Select the board/suggested truck or enter a custom truck. The overlay uses the trailer's unmatched windows only.")
    suggested_trucks = _unique_nonblank([t for t in (suggested_trucks or []) if t and t != "—"])
    overlay_enabled = st.toggle(
        "Overlay truck GPS for the same unmatched windows",
        value=False,
        key=f"overlay_enabled_{trailer_id}",
    )
    if overlay_enabled:
        options = ["Custom truck #"] + [f"Suggested: {truck}" for truck in suggested_trucks]
        default_idx = 1 if suggested_trucks else 0
        selected = st.selectbox("Overlay truck", options, index=default_idx, key=f"overlay_select_{trailer_id}")
        custom_truck = st.text_input(
            "Custom truck #",
            key=f"truck_guess_{trailer_id}",
            placeholder="e.g. 39",
            disabled=not selected.startswith("Custom"),
        ).strip()
        overlay_truck = custom_truck if selected.startswith("Custom") else selected.replace("Suggested: ", "").strip()
        if overlay_truck:
            _render_truck_guess_overlay(
                overlay_truck,
                trailer_id,
                start_dt,
                end_dt,
                points,
                first_ts,
                last_ts,
                unmatched_windows=unmatched_windows,
                trailer_segment_points=segment_points,
            )

    with st.expander(f"Copy coordinates ({len(points)} points)", expanded=False):
        st.code(_format_path_points(points), language="text")


def _render_truck_guess_overlay(
    truck_id: str,
    trailer_id: str,
    start_dt: datetime,
    end_dt: datetime,
    trailer_points: list[tuple[float, float]],
    trailer_first_ts: datetime | None,
    trailer_last_ts: datetime | None,
    *,
    unmatched_windows: list[dict[str, Any]] | None = None,
    trailer_segment_points: list[list[tuple[float, float]]] | None = None,
) -> None:
    """Overlay a guessed truck's GPS trail on the unmatched trailer trail."""
    try:
        import pydeck as pdk
    except ImportError:
        st.warning("pydeck not available.")
        return

    with st.spinner(f"Loading GPS trail for truck {truck_id}..."):
        raw_truck_trail = load_truck_gps_trail(truck_id, start_dt, end_dt)
        truck_segments = _trail_segments_for_windows(raw_truck_trail, unmatched_windows or [])
        truck_trail = [row for segment in truck_segments for row in segment]

    if not truck_trail:
        st.warning(f"No GPS pings found for truck **{truck_id}** in the trailer's unmatched windows.")
        return

    truck_segment_points = [_dedupe_path_points([(r["lat"], r["lon"]) for r in segment]) for segment in truck_segments]
    truck_segment_points = [segment for segment in truck_segment_points if len(segment) >= 2]
    truck_points = [point for segment in truck_segment_points for point in segment]
    truck_timestamps = []
    for r in truck_trail:
        ts = r.get("recorded_at")
        if ts:
            try:
                truck_timestamps.append(datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
            except (ValueError, AttributeError):
                pass
    truck_first = min(truck_timestamps) if truck_timestamps else None
    truck_last = max(truck_timestamps) if truck_timestamps else None

    if len(truck_points) < 2:
        st.warning(f"Truck {truck_id} has only {len(truck_points)} GPS point(s) — not enough for a route.")
        return

    fmt = "%m/%d %I:%M %p"
    st.markdown(
        f"**Truck {truck_id}**: {len(truck_points)} GPS points"
        + (f"  ·  {truck_first.strftime(fmt)} — {truck_last.strftime(fmt)} UTC" if truck_first and truck_last else "")
    )

    # Simple overlap check
    if trailer_first_ts and trailer_last_ts and truck_first and truck_last:
        overlap_start = max(trailer_first_ts, truck_first)
        overlap_end = min(trailer_last_ts, truck_last)
        if overlap_start < overlap_end:
            overlap_hrs = (overlap_end - overlap_start).total_seconds() / 3600
            st.success(f"Time overlap: **{overlap_hrs:.1f}h** ({overlap_start.strftime(fmt)} — {overlap_end.strftime(fmt)} UTC)")
        else:
            st.warning("No time overlap between truck and trailer GPS windows.")

    # Build combined map
    layers = [
        # Trailer path (orange) — segmented to avoid drawing through matched gaps.
        pdk.Layer("PathLayer", data=[{
            "path": [[lon, lat] for lat, lon in segment],
            "color": [249, 115, 22, 200],
        } for segment in (trailer_segment_points or [trailer_points])], get_path="path", get_color="color", width_min_pixels=3, pickable=False),
        # Truck path (blue) for the same unmatched windows.
        pdk.Layer("PathLayer", data=[{
            "path": [[lon, lat] for lat, lon in segment],
            "color": [59, 130, 246, 200],
        } for segment in truck_segment_points], get_path="path", get_color="color", width_min_pixels=3, pickable=False),
    ]

    # Truck waypoints with timestamps
    truck_point_data = []
    for i, row in enumerate(truck_trail):
        lat, lon = row.get("lat"), row.get("lon")
        if not lat or not lon:
            continue
        is_start = i == 0
        is_end = i == len(truck_trail) - 1
        color = [34, 197, 94, 240] if is_start else ([239, 68, 68, 240] if is_end else [59, 130, 246, 160])
        ts_str = str(row.get("recorded_at", ""))[:16].replace("T", " ")
        tag = "START" if is_start else ("END" if is_end else f"#{i + 1}")
        truck_point_data.append({
            "lat": float(lat), "lon": float(lon),
            "label": f"Truck {truck_id} {tag} — {ts_str}",
            "color": color,
        })

    layers.append(
        pdk.Layer("ScatterplotLayer", data=truck_point_data, get_position=["lon", "lat"],
                  get_radius=350, radius_min_pixels=4, radius_max_pixels=10,
                  get_fill_color="color", pickable=True),
    )

    all_points = list(trailer_points) + list(truck_points)
    center_lat = sum(lat for lat, _ in all_points) / len(all_points)
    center_lon = sum(lon for _, lon in all_points) / len(all_points)

    st.markdown(
        "<span style='color:#f97316;font-weight:800;'>■</span> Trailer "
        f"<span style='color:#3b82f6;font-weight:800;'>■</span> Truck {truck_id}",
        unsafe_allow_html=True,
    )
    st.pydeck_chart(pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=6, pitch=0),
        tooltip={"text": "{label}"},
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    ))


def _coverage_summary(coverage: dict[str, Any]) -> str:
    if not coverage:
        return "0h / 0 pings"
    hours = int(coverage.get("gps_hours") or 0)
    moving = int(coverage.get("moving_hours") or 0)
    pings = int(coverage.get("ping_count") or 0)
    miles = float(coverage.get("miles_traveled") or 0)
    return f"{hours}h / {pings} pings / {moving} moving / {miles:.1f} mi"


def _gps_gap_label(trailer_cov: dict[str, Any], truck_cov: dict[str, Any], dispatch_truck: str) -> str:
    trailer_hours = int((trailer_cov or {}).get("gps_hours") or 0)
    trailer_moving = int((trailer_cov or {}).get("moving_hours") or 0)
    truck_hours = int((truck_cov or {}).get("gps_hours") or 0)
    truck_moving = int((truck_cov or {}).get("moving_hours") or 0)
    has_dispatch_truck = bool(dispatch_truck and dispatch_truck != "—")

    if trailer_hours <= 0:
        return "Trailer GPS missing"
    if not has_dispatch_truck:
        return "No board truck"
    if truck_hours <= 0:
        return "Truck GPS missing"
    if trailer_moving > 0 and truck_moving <= 0:
        return "Truck GPS stationary/sparse"
    return "Both have GPS — inspect overlay"


def _trail_segments_for_windows(
    trail: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    if not trail:
        return []
    if not windows:
        return [trail]

    segments: list[list[dict[str, Any]]] = []
    for window in windows:
        start = _parse_ui_dt(window.get("start"))
        end = _parse_ui_dt(window.get("end"))
        if start is None or end is None:
            continue
        segment: list[dict[str, Any]] = []
        for row in trail:
            ts = _parse_ui_dt(row.get("recorded_at"))
            if ts is not None and start <= ts <= end:
                segment.append(row)
        if segment:
            segments.append(segment)
    return segments


def _unique_nonblank(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text == "—" or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _render_manual_assignments_section() -> None:
    """UI for viewing and creating manual truck/trailer pair assignments."""
    with st.expander("📋 Manual Pair Assignments (Paper-Log Drivers)", expanded=False):
        st.caption(
            "Manually assign trailers to trucks for paper-log drivers or GPS-blind units. "
            "This suppresses unmatched-trailer alerts and provides a history trail."
        )

        assignments = load_manual_pair_assignments(active_only=False)
        active = [a for a in assignments if a.get("active")]
        inactive = [a for a in assignments if not a.get("active")]

        if active:
            st.markdown("**Active Assignments**")
            active_display = []
            for a in active:
                active_display.append({
                    "ID": int(a.get("id") or 0),
                    "Truck": str(a.get("truck_id") or ""),
                    "Trailer": str(a.get("trailer_id") or ""),
                    "Start": str(a.get("start_date") or ""),
                    "End": str(a.get("end_date") or "—"),
                    "Assigned By": str(a.get("assigned_by") or ""),
                    "Notes": str(a.get("notes") or ""),
                })
            st.dataframe(active_display, use_container_width=True, hide_index=True)

            # Unassign button
            unassign_id = st.selectbox(
                "Select assignment to deactivate",
                options=[0] + [int(a.get("id") or 0) for a in active],
                format_func=lambda x: "— Select —" if x == 0 else f"ID {x}",
                key="manual_unassign_id",
            )
            if unassign_id and st.button("Deactivate Assignment", key="btn_deactivate_assignment"):
                if deactivate_manual_pair_assignment(int(unassign_id)):
                    st.success(f"Assignment {unassign_id} deactivated.")
                    st.rerun()
                else:
                    st.error("Failed to deactivate assignment.")

        # New assignment form
        st.markdown("**Create New Assignment**")
        col1, col2 = st.columns(2)
        with col1:
            new_truck = st.text_input("Truck ID", key="manual_new_truck")
        with col2:
            new_trailer = st.text_input("Trailer ID", key="manual_new_trailer")
        col3, col4 = st.columns(2)
        with col3:
            new_start = st.date_input("Start Date", value=date.today(), key="manual_new_start")
        with col4:
            new_end = st.date_input("End Date (optional)", value=None, key="manual_new_end")
        col5, col6 = st.columns(2)
        with col5:
            new_by = st.text_input("Assigned By", key="manual_new_by")
        with col6:
            new_notes = st.text_input("Notes", key="manual_new_notes")

        if st.button("Save Assignment", key="btn_save_manual_assignment"):
            if not new_truck or not new_trailer:
                st.error("Truck ID and Trailer ID are required.")
            else:
                row = {
                    "truck_id": new_truck.strip(),
                    "trailer_id": new_trailer.strip(),
                    "start_date": new_start.isoformat() if new_start else date.today().isoformat(),
                    "end_date": new_end.isoformat() if new_end else None,
                    "assigned_by": new_by.strip() or "unknown",
                    "notes": new_notes.strip(),
                    "active": True,
                }
                if save_manual_pair_assignment(row):
                    st.success(f"Assigned trailer {new_trailer} to truck {new_truck}.")
                    st.rerun()
                else:
                    st.error("Failed to save assignment.")

        if inactive:
            with st.expander("History (inactive assignments)", expanded=False):
                hist_display = []
                for a in inactive:
                    hist_display.append({
                        "Truck": str(a.get("truck_id") or ""),
                        "Trailer": str(a.get("trailer_id") or ""),
                        "Start": str(a.get("start_date") or ""),
                        "End": str(a.get("end_date") or "—"),
                        "Assigned By": str(a.get("assigned_by") or ""),
                        "Unassigned": str(a.get("unassigned_at") or ""),
                        "Notes": str(a.get("notes") or ""),
                    })
                st.dataframe(hist_display, use_container_width=True, hide_index=True)


def _aggregate_usage_rows(
    daily_rows: list[dict[str, Any]],
    *,
    primary_type: str,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    primary_col = "truck_id" if primary_type == "truck" else "trailer_id"
    partner_col = "trailer_id" if primary_type == "truck" else "truck_id"

    pair_acc: dict[tuple[str, str], dict[str, Any]] = {}
    for row in daily_rows:
        primary = str(row.get(primary_col) or "")
        partner = str(row.get(partner_col) or "")
        if not primary or not partner:
            continue
        key = (primary, partner)
        acc = pair_acc.setdefault(key, {
            "primary": primary,
            "partner": partner,
            "paired_hours": 0.0,
            "billable_hours": 0.0,
            "near_hours": 0.0,
            "same_yard_hours": 0.0,
            "miles_traveled": 0.0,
            "evidence_days": set(),
            "first": None,
            "last": None,
            "confidence_weighted": 0.0,
            "confidence_hours": 0.0,
            "min_distance": None,
        })
        paired = float(row.get("paired_hours") or 0)
        billable = float(row.get("billable_candidate_hours") or 0)
        near = float(row.get("near_hours") or 0)
        same_yard = float(row.get("same_yard_hours") or 0)
        evidence = float(row.get("evidence_hours") or 0)
        acc["paired_hours"] += paired
        acc["billable_hours"] += billable
        acc["near_hours"] += near
        acc["same_yard_hours"] += same_yard
        acc["miles_traveled"] += float(row.get("miles_traveled") or 0)
        if row.get("service_date"):
            acc["evidence_days"].add(str(row.get("service_date")))
        conf = float(row.get("avg_confidence") or 0)
        weight = max(paired, evidence, 1.0)
        acc["confidence_weighted"] += conf * weight
        acc["confidence_hours"] += weight
        min_dist = row.get("min_distance_miles")
        if min_dist is not None:
            min_dist_f = float(min_dist)
            acc["min_distance"] = min_dist_f if acc["min_distance"] is None else min(acc["min_distance"], min_dist_f)
        first = _parse_ui_dt(row.get("first_evidence_at"))
        last = _parse_ui_dt(row.get("last_evidence_at"))
        if first and (acc["first"] is None or first < acc["first"]):
            acc["first"] = first
        if last and (acc["last"] is None or last > acc["last"]):
            acc["last"] = last

    pair_rows_by_unit: dict[str, list[dict[str, Any]]] = {}
    overview_acc: dict[str, dict[str, Any]] = {}
    for acc in pair_acc.values():
        avg_conf = (acc["confidence_weighted"] / acc["confidence_hours"] * 100.0) if acc["confidence_hours"] else 0.0
        detail = {
            "Partner": acc["partner"],
            "Paired Hours": round(acc["paired_hours"], 1),
            "Billable Hours": round(acc["billable_hours"], 1),
            "Miles Traveled": round(acc["miles_traveled"], 1),
            "Near Hours": round(acc["near_hours"], 1),
            "Yard Hours": round(acc["same_yard_hours"], 1),
            "Evidence Days": len(acc["evidence_days"]),
            "Avg Confidence": round(avg_conf, 1),
            "Min Distance": round(float(acc["min_distance"] or 0), 3) if acc["min_distance"] is not None else None,
            "First Seen": _format_dt_text(acc["first"]) if acc["first"] else "—",
            "Last Seen": _format_dt_text(acc["last"]) if acc["last"] else "—",
        }
        pair_rows_by_unit.setdefault(acc["primary"], []).append(detail)

        ov = overview_acc.setdefault(acc["primary"], {
            "Unit": acc["primary"],
            "Paired Hours": 0.0,
            "Billable Hours": 0.0,
            "Near Hours": 0.0,
            "Unique Partners": set(),
            "Evidence Days": set(),
            "confidence_weighted": 0.0,
            "confidence_hours": 0.0,
            "Top Partner": "",
            "top_partner_hours": -1.0,
        })
        ov["Paired Hours"] += acc["paired_hours"]
        ov["Billable Hours"] += acc["billable_hours"]
        ov["Near Hours"] += acc["near_hours"]
        ov["Unique Partners"].add(acc["partner"])
        ov["Evidence Days"].update(acc["evidence_days"])
        ov["confidence_weighted"] += acc["confidence_weighted"]
        ov["confidence_hours"] += acc["confidence_hours"]
        if acc["paired_hours"] > ov["top_partner_hours"]:
            ov["top_partner_hours"] = acc["paired_hours"]
            ov["Top Partner"] = acc["partner"]

    overview_rows: list[dict[str, Any]] = []
    for ov in overview_acc.values():
        avg_conf = (ov["confidence_weighted"] / ov["confidence_hours"] * 100.0) if ov["confidence_hours"] else 0.0
        overview_rows.append({
            "Unit": ov["Unit"],
            "Paired Hours": round(ov["Paired Hours"], 1),
            "Billable Hours": round(ov["Billable Hours"], 1),
            "Near Hours": round(ov["Near Hours"], 1),
            "Unique Partners": len(ov["Unique Partners"]),
            "Evidence Days": len(ov["Evidence Days"]),
            "Avg Confidence": round(avg_conf, 1),
            "Top Partner": ov["Top Partner"],
        })

    for rows in pair_rows_by_unit.values():
        rows.sort(key=lambda row: (row["Paired Hours"], row["Avg Confidence"]), reverse=True)
    return overview_rows, pair_rows_by_unit


def _build_hourly_evidence_detail_rows(rows: list[dict[str, Any]], *, primary_type: str) -> list[dict[str, Any]]:
    partner_col = "trailer_id" if primary_type == "truck" else "truck_id"
    out: list[dict[str, Any]] = []
    for row in rows:
        truck_tz = _timezone_for_coords(row.get("truck_lat"), row.get("truck_lon"))
        trailer_tz = _timezone_for_coords(row.get("trailer_lat"), row.get("trailer_lon"))
        truck_coords = _format_coords(row.get("truck_lat"), row.get("truck_lon"))
        trailer_coords = _format_coords(row.get("trailer_lat"), row.get("trailer_lon"))
        out.append({
            "Hour (User Local)": _format_dt_text(row.get("hour_start")) or str(row.get("hour_start") or ""),
            "Hour (Truck Local)": _format_dt_text(row.get("hour_start"), tz=truck_tz),
            "Hour (Trailer Local)": _format_dt_text(row.get("hour_start"), tz=trailer_tz),
            "Partner": str(row.get(partner_col) or ""),
            "Review Flag": _evidence_review_flag(row),
            "Status": str(row.get("status") or ""),
            "Billable?": "Yes" if row.get("billable_candidate") else "No",
            "Confidence": round(float(row.get("confidence") or 0) * 100.0, 1),
            "Distance (mi)": float(row.get("best_distance_miles") or 0),
            "Miles Traveled": float(row.get("miles_traveled") or 0),
            "Ping Gap (min)": float(row.get("best_ping_gap_minutes") or 0),
            "Truck Coords": truck_coords,
            "Trailer Coords": trailer_coords,
            "Truck Ping Window": _format_time_window(row.get("truck_first_ping"), row.get("truck_last_ping"), tz=truck_tz),
            "Trailer Ping Window": _format_time_window(row.get("trailer_first_ping"), row.get("trailer_last_ping"), tz=trailer_tz),
            "Truck Yard": str(row.get("truck_yard") or ""),
            "Trailer Yard": str(row.get("trailer_yard") or ""),
            "Truck Address": str(row.get("truck_address") or ""),
            "Trailer Address": str(row.get("trailer_address") or ""),
            "Truck Pings": int(row.get("truck_pings") or 0),
            "Trailer Pings": int(row.get("trailer_pings") or 0),
        })
    return out



def _render_match_path_points(rows: list[dict[str, Any]], *, primary_type: str) -> None:
    """Render copyable/visual physical path points for paired timeline runs."""
    paired_rows = [r for r in rows if str(r.get("status") or "") == "paired"]
    if not paired_rows:
        return

    coord_prefix = "truck" if primary_type == "truck" else "trailer"
    partner_col = "trailer_id" if primary_type == "truck" else "truck_id"
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in paired_rows:
        partner = str(row.get(partner_col) or "")
        if not partner:
            continue
        groups.setdefault(partner, []).append(row)

    path_groups: list[dict[str, Any]] = []
    route_rows: list[dict[str, Any]] = []
    for partner, group in sorted(groups.items(), key=lambda item: _unit_sort_key(item[0])):
        group.sort(key=lambda r: str(r.get("hour_start") or ""))
        points = _dedupe_path_points([
            (row.get(f"{coord_prefix}_lat"), row.get(f"{coord_prefix}_lon"))
            for row in group
        ])
        if not points:
            continue
        partner_points = _dedupe_path_points([
            (row.get("trailer_lat" if coord_prefix == "truck" else "truck_lat"), row.get("trailer_lon" if coord_prefix == "truck" else "truck_lon"))
            for row in group
        ])
        start_label = _format_dt_text(group[0].get("hour_start")) or str(group[0].get("hour_start") or "")
        end_label = _format_dt_text(group[-1].get("hour_start")) or str(group[-1].get("hour_start") or "")
        miles = sum(float(row.get("miles_traveled") or 0) for row in group)
        maps_url = _google_maps_route_url(points)
        coords_text = _format_path_points(points)
        route_rows.append({
            "Partner": partner,
            "Hours": len(group),
            "Miles": round(miles, 1),
            "Start": start_label,
            "End": end_label,
            "Start Point": _format_point(points[0]),
            "End Point": _format_point(points[-1]),
            "Google Maps Route": maps_url,
            "Coordinate Points": coords_text,
        })
        path_groups.append({
            "partner": partner,
            "selected_points": points,
            "partner_points": partner_points,
            "coords_text": coords_text,
            "hours": len(group),
            "miles": round(miles, 1),
            "start_label": start_label,
            "end_label": end_label,
        })

    if not route_rows:
        return

    st.markdown("##### Physical path points")
    st.caption(
        "Approximate route from the hourly matched coordinates. "
        "Select a trip below to highlight it on the map, or show all."
    )

    # Per-trip selector
    trip_options = ["All trips"] + [g["partner"] for g in path_groups]
    selected_trip = st.selectbox(
        "Highlight trip",
        trip_options,
        index=0,
        key="path_trip_selector",
    )
    if selected_trip == "All trips":
        visible_groups = path_groups
    else:
        visible_groups = [g for g in path_groups if g["partner"] == selected_trip]

    _render_path_points_map(visible_groups)
    st.dataframe(
        route_rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Miles": st.column_config.NumberColumn(format="%.1f"),
            "Google Maps Route": st.column_config.LinkColumn(display_text="Open route"),
        },
    )
    for group in path_groups:
        with st.expander(f"Copy coordinates for partner {group['partner']}", expanded=False):
            st.code(group["coords_text"], language="text")
            if group["partner_points"]:
                st.caption("Partner GPS points for the same matched hours:")
                st.code(_format_path_points(group["partner_points"]), language="text")


def _render_path_points_map(path_groups: list[dict[str, Any]]) -> None:
    try:
        import pydeck as pdk
    except ImportError:
        return

    colors = [
        [59, 130, 246, 220], [16, 185, 129, 220], [139, 92, 246, 220],
        [236, 72, 153, 220], [249, 115, 22, 220], [6, 182, 212, 220],
    ]
    path_data = []
    point_data = []
    all_points: list[tuple[float, float]] = []
    for idx, group in enumerate(path_groups):
        points = group["selected_points"]
        if not points:
            continue
        color = colors[idx % len(colors)]
        all_points.extend(points)
        label = f"{group['partner']} — {group.get('hours', '?')}h, {group.get('miles', '?')} mi"
        if group.get("start_label"):
            label += f"\n{group['start_label']} → {group.get('end_label', '')}"
        path_data.append({
            "partner": group["partner"],
            "path": [[lon, lat] for lat, lon in points],
            "color": color,
            "label": label,
        })
        for point_idx, (lat, lon) in enumerate(points):
            point_data.append({
                "partner": group["partner"],
                "lat": lat,
                "lon": lon,
                "label": f"{group['partner']} #{point_idx + 1}",
                "color": [34, 197, 94, 240] if point_idx == 0 else ([239, 68, 68, 240] if point_idx == len(points) - 1 else color),
            })

    if not all_points:
        return
    center_lat = sum(lat for lat, _lon in all_points) / len(all_points)
    center_lon = sum(lon for _lat, lon in all_points) / len(all_points)
    layers = []
    if path_data:
        layers.append(pdk.Layer(
            "PathLayer",
            data=path_data,
            get_path="path",
            get_color="color",
            width_min_pixels=3,
            pickable=True,
        ))
    if point_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=point_data,
            get_position=["lon", "lat"],
            get_radius=500,
            radius_min_pixels=5,
            radius_max_pixels=14,
            get_fill_color="color",
            pickable=True,
        ))
    st.pydeck_chart(pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=6, pitch=0),
        tooltip={"text": "{label}"},
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    ))


def _dedupe_path_points(raw_points: list[tuple[object, object]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for lat_raw, lon_raw in raw_points:
        try:
            lat = float(lat_raw)
            lon = float(lon_raw)
        except (TypeError, ValueError):
            continue
        if abs(lat) < 0.000001 and abs(lon) < 0.000001:
            continue
        rounded = (round(lat, 6), round(lon, 6))
        if not points or rounded != points[-1]:
            points.append(rounded)
    return points


def _format_point(point: tuple[float, float]) -> str:
    return f"{point[0]:.6f},{point[1]:.6f}"


def _format_path_points(points: list[tuple[float, float]]) -> str:
    return "\n".join(_format_point(point) for point in points)


def _google_maps_route_url(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    if len(points) == 1:
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(_format_point(points[0]))}"
    origin = _format_point(points[0])
    destination = _format_point(points[-1])
    middle = _sample_route_waypoints(points[1:-1], max_points=8)
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin)}"
        f"&destination={quote_plus(destination)}"
        "&travelmode=driving"
    )
    if middle:
        waypoint_text = "|".join(_format_point(point) for point in middle)
        url += f"&waypoints={quote_plus(waypoint_text)}"
    return url


def _sample_route_waypoints(points: list[tuple[float, float]], *, max_points: int) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    if max_points <= 0:
        return []
    step = (len(points) - 1) / max(1, max_points - 1)
    sampled: list[tuple[float, float]] = []
    for i in range(max_points):
        sampled.append(points[round(i * step)])
    return _dedupe_path_points(sampled)

def _format_coords(lat: object, lon: object) -> str:
    try:
        return f"{float(lat):.6f}, {float(lon):.6f}"
    except (TypeError, ValueError):
        return ""


def _sort_usage_overview(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    if sort_by == "Billable hours":
        return sorted(rows, key=lambda r: (r["Billable Hours"], r["Paired Hours"]), reverse=True)
    if sort_by == "Unique partners":
        return sorted(rows, key=lambda r: (r["Unique Partners"], r["Paired Hours"]), reverse=True)
    if sort_by == "Avg confidence":
        return sorted(rows, key=lambda r: (r["Avg Confidence"], r["Paired Hours"]), reverse=True)
    if sort_by == "Unit":
        return sorted(rows, key=lambda r: _unit_sort_key(str(r["Unit"])))
    return sorted(rows, key=lambda r: r["Paired Hours"], reverse=True)


def _parse_ui_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_dt_text(value: object, *, tz: timezone | ZoneInfo | None = None) -> str:
    parsed = _parse_ui_dt(value)
    if not parsed:
        return ""
    display_tz = tz or _user_timezone()
    return parsed.astimezone(display_tz).strftime("%m/%d %I:%M %p %Z")


def _format_time_window(start: object, end: object, *, tz: timezone | ZoneInfo | None = None) -> str:
    start_dt = _format_dt_text(start, tz=tz)
    end_dt = _format_dt_text(end, tz=tz)
    if not start_dt and not end_dt:
        return ""
    if start_dt == end_dt or not end_dt:
        return start_dt
    if not start_dt:
        return end_dt
    return f"{start_dt} → {end_dt}"


def _drop_status_label(value: object) -> str:
    status = str(value or "").strip()
    labels = {
        "active_drop": "Active Drop",
        "picked_up": "Picked Up",
        "returned_to_yard": "Returned to Yard",
        "yard_drop": "Yard Drop",
        "unknown_dropper": "Unknown Dropper",
    }
    return labels.get(status, status.replace("_", " ").title() if status else "")


def _maps_url_for_coords(lat: object, lon: object) -> str:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return ""
    if lat_f == 0 and lon_f == 0:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={lat_f:.6f},{lon_f:.6f}"


def _local_date_range_to_utc(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    local_tz = _user_timezone()
    start_local = datetime.combine(start_date, time.min, tzinfo=local_tz)
    end_local = datetime.combine(end_date, time.max, tzinfo=local_tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _user_timezone() -> tzinfo:
    tz = datetime.now().astimezone().tzinfo
    return tz or timezone.utc


def _timezone_for_coords(lat: object, lon: object) -> ZoneInfo | timezone:
    """Best-effort location timezone from coordinates without adding a heavy dependency.

    This intentionally favors clear dispatcher display over perfect border-level
    timezone lookup. The stored evidence remains UTC.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return _user_timezone()
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return _user_timezone()

    try:
        if 18 <= lat_f <= 23 and -161 <= lon_f <= -154:
            return ZoneInfo("Pacific/Honolulu")
        if 51 <= lat_f <= 72 and -170 <= lon_f <= -130:
            return ZoneInfo("America/Anchorage")
        if 31 <= lat_f <= 37.5 and -115 <= lon_f <= -109:
            return ZoneInfo("America/Phoenix")
        if lon_f <= -114:
            return ZoneInfo("America/Los_Angeles")
        if lon_f <= -101:
            return ZoneInfo("America/Denver")
        if lon_f <= -86:
            return ZoneInfo("America/Chicago")
        return ZoneInfo("America/New_York")
    except Exception:
        return _user_timezone()


def _evidence_review_flag(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "")
    billable = bool(row.get("billable_candidate"))
    truck_yard = str(row.get("truck_yard") or "")
    trailer_yard = str(row.get("trailer_yard") or "")
    truck_pings = int(row.get("truck_pings") or 0)
    trailer_pings = int(row.get("trailer_pings") or 0)
    confidence = float(row.get("confidence") or 0)
    if billable:
        return "Billable pattern"
    if status == "paired" and not truck_yard and not trailer_yard:
        if min(truck_pings, trailer_pings) >= 2 or confidence >= 0.65:
            return "Review anomaly: repeated non-yard paired pings"
        return "Review anomaly: non-yard paired evidence"
    if status == "same_yard":
        return "Yard only"
    if truck_yard or trailer_yard:
        return "Near/yard-edge review"
    if status == "near":
        return "Near review"
    return "Review"


# ---------------------------------------------------------------------------
# Unit History Tab — trip-by-trip view with route map and day navigation
# ---------------------------------------------------------------------------

@st.fragment
def _render_unit_history_tab(assets: list[Asset]) -> None:
    """Unit History: type a unit ID, pick a date, see trip segments + route map."""
    st.subheader("Unit History")
    st.caption(
        "Enter any truck or trailer number to see its GPS history trip-by-trip. "
        "Shows driving/idle segments, distances, locations, and a route map."
    )

    # Unit input
    all_ids = sorted(
        [(a.asset_type, a.asset_id) for a in assets],
        key=lambda x: (x[0], _unit_sort_key(x[1])),
    )
    unit_options = [""] + [f"{atype.title()}: {aid}" for atype, aid in all_ids]

    col_unit, col_type = st.columns([3, 1])
    with col_unit:
        selected = st.selectbox(
            "Select or search unit",
            unit_options,
            key="history_unit_select",
            placeholder="Type unit # ...",
        )
    with col_type:
        manual_type = st.selectbox("Type", ["truck", "trailer"], key="history_unit_type")

    if selected:
        parts = selected.split(": ", 1)
        unit_type = parts[0].lower()
        unit_id = parts[1]
    else:
        unit_id = ""
        unit_type = manual_type

    # Freeform unit input for units not in current list
    typed_id = st.text_input(
        "Or type unit # directly",
        key="history_unit_typed",
        placeholder="e.g. 575005, 1987, 4907-15",
    ).strip()
    if typed_id:
        unit_id = typed_id

    if not unit_id:
        st.info("Select or type a unit number above to begin.")
        return

    # Date navigation
    today = date.today()
    col_d1, col_d2, col_d3 = st.columns([1, 2, 1])
    with col_d1:
        if st.button("← Prev Day", key="hist_prev_day"):
            current = st.session_state.get("history_date", today)
            st.session_state["history_date"] = current - timedelta(days=1)
    with col_d2:
        hist_date = st.date_input(
            "Date",
            value=st.session_state.get("history_date", today),
            key="history_date_input",
        )
        st.session_state["history_date"] = hist_date
    with col_d3:
        if st.button("Next Day →", key="hist_next_day"):
            current = st.session_state.get("history_date", today)
            st.session_state["history_date"] = current + timedelta(days=1)

    hist_date = st.session_state.get("history_date", today)

    # Optional custom range
    use_range = st.toggle("Custom date range", value=False, key="hist_use_range")
    if use_range:
        rc1, rc2 = st.columns(2)
        with rc1:
            range_start = st.date_input("From", value=hist_date - timedelta(days=3), key="hist_range_start")
        with rc2:
            range_end = st.date_input("To", value=hist_date, key="hist_range_end")
    else:
        range_start = hist_date
        range_end = hist_date

    # Load data
    if st.button("Load History", type="primary", key="hist_load"):
        start_dt = datetime.combine(range_start, time.min).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(range_end, time(23, 59, 59)).replace(tzinfo=timezone.utc)

        with st.spinner(f"Loading GPS history for {unit_type} {unit_id}..."):
            if unit_type == "trailer":
                trail = load_trailer_gps_trail(unit_id, start_dt, end_dt)
            else:
                trail = load_truck_gps_trail(unit_id, start_dt, end_dt)

        if not trail:
            st.warning(f"No GPS pings found for **{unit_type} {unit_id}** on {range_start} — {range_end}.")
            return

        # Build trip segments from raw pings
        trips = _build_trip_segments(trail)
        st.session_state["history_trail"] = trail
        st.session_state["history_trips"] = trips
        st.session_state["history_meta"] = {
            "unit_id": unit_id,
            "unit_type": unit_type,
            "date_label": str(range_start) if range_start == range_end else f"{range_start} — {range_end}",
            "ping_count": len(trail),
        }

    # Display results
    if "history_trips" in st.session_state:
        meta = st.session_state["history_meta"]
        trail = st.session_state["history_trail"]
        trips = st.session_state["history_trips"]
        _render_unit_history_results(trail, trips, meta)


def _render_unit_history_results(
    trail: list[dict[str, Any]],
    trips: list[dict[str, Any]],
    meta: dict[str, Any],
) -> None:
    """Render history trail: metrics, map, trip table."""
    try:
        import pydeck as pdk
    except ImportError:
        st.warning("pydeck not available.")
        return

    unit_id = meta["unit_id"]
    unit_type = meta["unit_type"]

    # Metrics
    total_distance = sum(t["distance_mi"] for t in trips)
    driving_trips = [t for t in trips if t["status"] == "Driving"]
    idle_trips = [t for t in trips if t["status"] == "Idle"]
    driving_time = sum(t["duration_min"] for t in driving_trips)
    idle_time = sum(t["duration_min"] for t in idle_trips)
    providers = sorted({p.get("provider", "") for p in trail if p.get("provider")})

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pings", f"{meta['ping_count']:,}")
    c2.metric("Trips", len(trips))
    c3.metric("Distance", f"{total_distance:.1f} mi")
    c4.metric("Driving", f"{driving_time / 60:.1f}h")
    c5.metric("Idle", f"{idle_time / 60:.1f}h")

    if providers:
        st.caption(f"GPS sources: **{', '.join(providers)}** · {meta['date_label']}")

    # Route map
    points = [(float(r["lat"]), float(r["lon"])) for r in trail if r.get("lat") and r.get("lon")]
    if len(points) >= 2:
        deduped = _dedupe_path_points(points)
        path_data = [{
            "path": [[lon, lat] for lat, lon in deduped],
            "color": [59, 130, 246, 200] if unit_type == "truck" else [249, 115, 22, 200],
        }]

        # Start/end markers
        marker_data = [
            {"lat": deduped[0][0], "lon": deduped[0][1], "label": "START", "color": [34, 197, 94, 240]},
            {"lat": deduped[-1][0], "lon": deduped[-1][1], "label": "END", "color": [239, 68, 68, 240]},
        ]

        # Add trip-start waypoints with timestamps
        for trip in trips:
            if trip["start_lat"] and trip["start_lon"] and trip["status"] == "Driving":
                marker_data.append({
                    "lat": trip["start_lat"], "lon": trip["start_lon"],
                    "label": f"{trip['start_time']} — {trip['status']} {trip['distance_mi']:.1f}mi",
                    "color": [59, 130, 246, 180] if unit_type == "truck" else [249, 115, 22, 180],
                })

        center_lat = sum(lat for lat, _ in deduped) / len(deduped)
        center_lon = sum(lon for _, lon in deduped) / len(deduped)

        layers = [
            pdk.Layer("PathLayer", data=path_data, get_path="path", get_color="color",
                      width_min_pixels=3, pickable=False),
            pdk.Layer("ScatterplotLayer", data=marker_data, get_position=["lon", "lat"],
                      get_radius=500, radius_min_pixels=5, radius_max_pixels=14,
                      get_fill_color="color", pickable=True),
        ]

        st.pydeck_chart(pdk.Deck(
            layers=layers,
            initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=6, pitch=0),
            tooltip={"text": "{label}"},
            map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        ))

        maps_url = _google_maps_route_url(deduped[:25])  # Google Maps limit
        if maps_url:
            st.markdown(f"[Open route in Google Maps]({maps_url})")

    # Trip segments table
    st.subheader("Trip Segments")
    table_data = []
    for i, trip in enumerate(trips, 1):
        status_icon = "🚗" if trip["status"] == "Driving" else "⏸️"
        table_data.append({
            "#": i,
            "": status_icon,
            "Start Time": trip["start_time"],
            "End Time": trip["end_time"],
            "Duration": _format_duration(trip["duration_min"]),
            "Distance": f"{trip['distance_mi']:.1f}mi" if trip["distance_mi"] > 0 else "0.0mi",
            "Status": trip["status"],
            "Provider": trip["provider"],
            "Location": trip["location"],
        })
    st.dataframe(table_data, use_container_width=True, hide_index=True)


def _build_trip_segments(trail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build trip segments (driving/idle) from raw GPS pings.

    A 'trip' is a continuous period of driving or idling. Status changes when:
    - Speed goes from 0 to >0 (idle→driving)
    - Speed goes from >0 to 0 for >5 minutes (driving→idle)
    """
    from services.gps_matching import haversine_miles

    if not trail:
        return []

    segments: list[dict[str, Any]] = []
    current_status = None
    seg_start_idx = 0

    IDLE_THRESHOLD = 1.0  # speed below this = idle (accounts for GPS drift)

    for i, ping in enumerate(trail):
        speed = float(ping.get("speed") or 0)
        status = "Driving" if speed > IDLE_THRESHOLD else "Idle"

        if current_status is None:
            current_status = status
            seg_start_idx = i
            continue

        if status != current_status:
            # Close current segment
            segments.append(_finalize_segment(trail, seg_start_idx, i - 1, current_status))
            current_status = status
            seg_start_idx = i

    # Close final segment
    if trail:
        segments.append(_finalize_segment(trail, seg_start_idx, len(trail) - 1, current_status or "Idle"))

    # Merge very short idle segments (< 5 min) into surrounding driving
    merged = []
    for seg in segments:
        if (
            merged
            and seg["status"] == "Idle"
            and seg["duration_min"] < 5
            and merged[-1]["status"] == "Driving"
        ):
            # Absorb into previous driving segment
            merged[-1] = _merge_segments(merged[-1], seg)
        elif (
            merged
            and seg["status"] == "Driving"
            and merged[-1]["status"] == "Driving"
        ):
            # Merge consecutive driving after absorption
            merged[-1] = _merge_segments(merged[-1], seg)
        else:
            merged.append(seg)

    return merged


def _finalize_segment(
    trail: list[dict[str, Any]], start_idx: int, end_idx: int, status: str
) -> dict[str, Any]:
    """Build a single trip segment dict from a range of trail pings."""
    from services.gps_matching import haversine_miles

    start_ping = trail[start_idx]
    end_ping = trail[end_idx]

    start_ts = _parse_history_ts(start_ping.get("recorded_at"))
    end_ts = _parse_history_ts(end_ping.get("recorded_at"))
    duration_min = (end_ts - start_ts).total_seconds() / 60 if start_ts and end_ts else 0

    # Calculate total distance along the path
    total_dist = 0.0
    for i in range(start_idx, end_idx):
        p1 = trail[i]
        p2 = trail[i + 1]
        if p1.get("lat") and p1.get("lon") and p2.get("lat") and p2.get("lon"):
            total_dist += haversine_miles(
                float(p1["lat"]), float(p1["lon"]),
                float(p2["lat"]), float(p2["lon"]),
            )

    start_lat = float(start_ping["lat"]) if start_ping.get("lat") else None
    start_lon = float(start_ping["lon"]) if start_ping.get("lon") else None

    # Collect providers in this segment
    providers = sorted({str(trail[j].get("provider", "")) for j in range(start_idx, end_idx + 1) if trail[j].get("provider")})

    fmt = "%m/%d %I:%M %p"
    return {
        "status": status,
        "start_time": start_ts.strftime(fmt) if start_ts else "?",
        "end_time": end_ts.strftime(fmt) if end_ts else "?",
        "duration_min": round(duration_min, 1),
        "distance_mi": round(total_dist, 1),
        "start_lat": start_lat,
        "start_lon": start_lon,
        "location": str(start_ping.get("address") or end_ping.get("address") or ""),
        "provider": ", ".join(providers),
    }


def _merge_segments(seg_a: dict[str, Any], seg_b: dict[str, Any]) -> dict[str, Any]:
    """Merge two adjacent segments into one."""
    return {
        **seg_a,
        "end_time": seg_b["end_time"],
        "duration_min": round(seg_a["duration_min"] + seg_b["duration_min"], 1),
        "distance_mi": round(seg_a["distance_mi"] + seg_b["distance_mi"], 1),
    }


def _parse_history_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _format_duration(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}min"
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours}h{mins:02d}m"


@st.fragment
def _render_timeline_tab(assets: list[Asset]) -> None:
    """Unit timeline: select a unit and see who it was paired with over time."""
    st.subheader("Unit Assignment Timeline")
    st.caption(
        "Select a truck or trailer to see its historical pairings. "
        "This uses dense timestamp-matched hourly evidence only — no legacy asset_pairings fallback."
    )

    # Unit selector
    all_ids = sorted(
        [(a.asset_type, a.asset_id) for a in assets],
        key=lambda x: (x[0], _unit_sort_key(x[1])),
    )
    unit_options = [f"{atype.title()}: {aid}" for atype, aid in all_ids]

    if not unit_options:
        st.info("No units available.")
        return

    selected = st.selectbox("Select unit", unit_options, key="timeline_unit_select")
    if not selected:
        return

    parts = selected.split(": ", 1)
    unit_type = parts[0].lower()
    unit_id = parts[1]

    # Date range
    today = date.today()
    col1, col2 = st.columns(2)
    with col1:
        tl_start = st.date_input("From", value=today - timedelta(days=7), key="tl_start")
    with col2:
        tl_end = st.date_input("To", value=today, key="tl_end")

    # --- Yard Mode toggle ---
    yard_mode = st.toggle(
        "Yard Mode (aggressive matching)",
        value=False,
        key="tl_yard_mode",
        help=(
            "Hyper-specific yard analysis. Throws out all conservative logic and "
            "shows every data point at 5- or 10-minute intervals across all GPS "
            "providers. Catches trailer shuffling in the yard. Does NOT affect "
            "normal pairing logic."
        ),
    )

    if yard_mode:
        _render_yard_mode_controls(unit_id, unit_type, tl_start, tl_end)
        return

    if st.button("Load Timeline", type="primary", key="tl_compute"):
        start_dt, end_dt = _local_date_range_to_utc(tl_start, tl_end)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt

        # Primary: hourly evidence table (detailed hour-by-hour view)
        with st.spinner("Loading hourly evidence..."):
            segments = _cached_hourly_timeline(unit_id, unit_type, start_dt.isoformat(), end_dt.isoformat())

        if not segments:
            st.info(
                "No dense hourly evidence found for this unit/range yet. "
                "Try a date range inside the latest dense evidence job window."
            )
            return

        # Store in session for display
        consolidated = _consolidate_timeline(segments)
        st.session_state["timeline_segments"] = consolidated
        st.session_state["timeline_selected_unit"] = f"{unit_type}:{unit_id}"

    # Display timeline if available
    if "timeline_segments" in st.session_state:
        segments = st.session_state["timeline_segments"]
        _render_timeline_visual(segments)


# ---------------------------------------------------------------------------
# Yard Mode — aggressive fine-grained proximity timeline
# ---------------------------------------------------------------------------

_YARD_MODE_CSS = """
<style>
.yard-alert { background: rgba(239,68,68,.14); border: 1px solid rgba(239,68,68,.35); border-radius: 10px; padding: .65rem .85rem; margin: .45rem 0; }
.yard-alert b { color: #fca5a5; }
.yard-detail-row { font-size: .85rem; }
.yard-close { color: #fbbf24; font-weight: 800; }
.yard-far { color: #94a3b8; }
</style>
"""


def _render_yard_mode_controls(
    unit_id: str, unit_type: str, tl_start: date, tl_end: date
) -> None:
    """Render yard-mode-specific controls and results."""
    st.markdown(_YARD_MODE_CSS, unsafe_allow_html=True)

    st.info(
        "**Yard Mode** — every available ping, every provider, no conservative "
        "filtering. Use short date ranges (1–3 days) for best performance.",
        icon="🔬",
    )

    yard_names = list(YARD_GEOFENCES.keys())
    yc1, yc2 = st.columns(2)
    with yc1:
        yard_name = st.selectbox("Yard", yard_names, key="tl_yard_name")
    with yc2:
        bucket_minutes = st.selectbox(
            "Bucket interval",
            [5, 10, 15],
            index=0,
            key="tl_yard_bucket",
            help="Smaller = more data points. 5 min recommended for catching shuffles.",
        )

    if st.button("Run Yard Analysis", type="primary", key="tl_yard_run"):
        start_dt, end_dt = _local_date_range_to_utc(tl_start, tl_end)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt

        # Guard: warn on ranges > 3 days
        span_days = (end_dt - start_dt).total_seconds() / 86400
        if span_days > 5:
            st.warning("Yard mode works best with 1–3 day ranges. This range is "
                        f"{span_days:.0f} days — results may be slow or truncated.")

        with st.spinner("Loading ALL yard pings (every provider, no filters)..."):
            raw_pings = _cached_yard_pings(yard_name, start_dt.isoformat(), end_dt.isoformat())

        if not raw_pings:
            st.warning(
                f"No GPS pings found inside **{yard_name}** for this date range. "
                "The unit may not have been in the yard, or history data hasn't been backfilled yet."
            )
            return

        with st.spinner(f"Building {bucket_minutes}-min proximity buckets..."):
            segments, detail_rows = build_yard_mode_timeline(
                unit_id, unit_type, raw_pings, bucket_minutes=bucket_minutes,
            )

        if not segments:
            st.warning(
                f"**{unit_type.title()} {unit_id}** had no pings inside "
                f"**{yard_name}** during this period."
            )
            # Show what WAS there
            _render_yard_other_units(raw_pings, unit_id, unit_type)
            return

        st.session_state["yard_segments"] = segments
        st.session_state["yard_details"] = detail_rows
        st.session_state["yard_raw_count"] = len(raw_pings)
        st.session_state["yard_meta"] = {
            "unit": f"{unit_type}:{unit_id}",
            "yard": yard_name,
            "bucket": bucket_minutes,
        }

    # Display yard results if available
    if "yard_segments" in st.session_state:
        meta = st.session_state.get("yard_meta", {})
        segments = st.session_state["yard_segments"]
        detail_rows = st.session_state["yard_details"]
        raw_count = st.session_state.get("yard_raw_count", 0)

        _render_yard_metrics(segments, detail_rows, raw_count, meta)
        _render_timeline_visual(segments)  # reuse existing bar
        _render_yard_detail_table(detail_rows, meta)


def _render_yard_metrics(
    segments: list[TimelineSegment],
    detail_rows: list[dict[str, Any]],
    raw_count: int,
    meta: dict[str, Any],
) -> None:
    """Top-level metrics for yard mode results."""
    partner_segs = [s for s in segments if s.partner_type not in ("gap",)]
    unique_partners = len({s.partner_id for s in partner_segs})
    close_buckets = sum(1 for d in detail_rows if 0 < d["distance_ft"] <= 100)
    avg_dist = (
        sum(d["distance_ft"] for d in detail_rows if d["distance_ft"] > 0)
        / max(sum(1 for d in detail_rows if d["distance_ft"] > 0), 1)
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Raw Pings", f"{raw_count:,}")
    c2.metric("Buckets", len(detail_rows))
    c3.metric("Unique Neighbors", unique_partners)
    c4.metric("< 100 ft buckets", close_buckets)
    c5.metric("Avg Distance", f"{avg_dist:.0f} ft")


def _render_yard_detail_table(
    detail_rows: list[dict[str, Any]],
    meta: dict[str, Any],
) -> None:
    """Render the per-bucket detail table for yard mode."""
    if not detail_rows:
        return

    st.subheader("Yard Proximity Detail")
    st.caption(
        f"Every {meta.get('bucket', 5)}-minute bucket where "
        f"**{meta.get('unit', '?')}** was inside **{meta.get('yard', '?')}**. "
        "Distances in feet."
    )

    # Highlight close encounters
    table_data = []
    for d in detail_rows:
        dist_ft = d["distance_ft"]
        if dist_ft == 0:
            dist_display = "—"
            flag = ""
        elif dist_ft <= 50:
            dist_display = f"{dist_ft:.0f}"
            flag = "🔴"
        elif dist_ft <= 150:
            dist_display = f"{dist_ft:.0f}"
            flag = "🟡"
        elif dist_ft <= 500:
            dist_display = f"{dist_ft:.0f}"
            flag = "🟢"
        else:
            dist_display = f"{dist_ft:.0f}"
            flag = ""

        table_data.append({
            "": flag,
            "Time": d["time"],
            "Partner": d["partner_id"],
            "Type": d["partner_type"],
            "Dist (ft)": dist_display,
            "Unit Pings": d["unit_pings"],
            "Partner Pings": d["partner_pings"],
            "Providers": d["unit_providers"],
            "Avg Speed": f"{d['avg_speed']:.1f}" if d["avg_speed"] > 0 else "0",
        })

    st.dataframe(table_data, use_container_width=True, hide_index=True)

    # CSV download
    csv_out = StringIO()
    if table_data:
        writer = csv.DictWriter(csv_out, fieldnames=list(table_data[0].keys()), extrasaction="ignore")
        writer.writeheader()
        for row in table_data:
            writer.writerow(row)
    st.download_button(
        "Download yard detail CSV",
        csv_out.getvalue(),
        file_name="yard_mode_detail.csv",
        mime="text/csv",
    )


def _render_yard_other_units(
    raw_pings: list[dict[str, Any]],
    unit_id: str,
    unit_type: str,
) -> None:
    """When the selected unit has no yard pings, show what units ARE there."""
    from collections import Counter
    others: Counter[str] = Counter()
    for p in raw_pings:
        aid = str(p.get("asset_id", ""))
        atype = str(p.get("asset_type", ""))
        if aid and not (atype == unit_type and aid == unit_id):
            others[f"{atype.title()}: {aid}"] += 1

    if others:
        st.markdown("**Units that *were* in the yard during this period:**")
        top = others.most_common(20)
        for label, count in top:
            st.markdown(f"- {label} — {count} pings")
    else:
        st.info("No units had pings inside this yard during the selected range.")


def _consolidate_timeline(segments: list[TimelineSegment], *, max_absorb_hours: int = 2) -> list[TimelineSegment]:
    """Merge consecutive same-partner hours and absorb short interruptions.

    Logic:
    - Consecutive hours with the same partner merge into one segment.
    - If partner A runs for N hours, then a *different* partner B appears for
      ≤ max_absorb_hours, then A resumes — B is absorbed into A's run.
    - Yard visits are NEVER absorbed; they act as hard streak-breakers.
    - After absorption, consecutive same-partner runs are merged again.

    This keeps the timeline clean without hiding real assignment changes.
    """
    if len(segments) <= 1:
        return list(segments)

    # --- Pass 1: absorb short interruptions ---
    absorbed: list[TimelineSegment] = list(segments)
    changed = True
    while changed:
        changed = False
        new: list[TimelineSegment] = []
        i = 0
        while i < len(absorbed):
            seg = absorbed[i]
            # Look ahead: is this a short non-yard interruption between the same partner?
            if (
                seg.partner_type not in ("yard", "gap")
                and i >= 1
                and i + 1 < len(absorbed)
            ):
                prev = new[-1] if new else None
                nxt = absorbed[i + 1]
                interruption_hours = seg.duration_minutes / 60.0
                if (
                    prev is not None
                    and prev.partner_type not in ("yard", "gap")
                    and nxt.partner_type not in ("yard", "gap")
                    and prev.partner_id == nxt.partner_id
                    and prev.partner_id != seg.partner_id
                    and interruption_hours <= max_absorb_hours
                    and seg.partner_type != "yard"
                ):
                    # Absorb: replace this segment with the dominant partner
                    absorbed_seg = TimelineSegment(
                        unit_id=seg.unit_id,
                        unit_type=seg.unit_type,
                        partner_id=prev.partner_id,
                        partner_type=prev.partner_type,
                        start=seg.start,
                        end=seg.end,
                        duration_minutes=seg.duration_minutes,
                        avg_distance_miles=seg.avg_distance_miles,
                        bucket_count=seg.bucket_count,
                        confidence=seg.confidence * 0.5,  # lower confidence for absorbed hours
                    )
                    new.append(absorbed_seg)
                    changed = True
                    i += 1
                    continue
            new.append(seg)
            i += 1
        absorbed = new

    # --- Pass 2: merge consecutive same-partner segments ---
    merged: list[TimelineSegment] = [absorbed[0]]
    for seg in absorbed[1:]:
        prev = merged[-1]
        if (
            prev.partner_id == seg.partner_id
            and prev.partner_type == seg.partner_type
            and prev.partner_type not in ("gap",)
        ):
            # Merge into previous
            total_minutes = prev.duration_minutes + seg.duration_minutes
            total_buckets = prev.bucket_count + seg.bucket_count
            # Weighted average distance and confidence
            if total_minutes > 0:
                avg_dist = (
                    prev.avg_distance_miles * prev.duration_minutes
                    + seg.avg_distance_miles * seg.duration_minutes
                ) / total_minutes
                avg_conf = (
                    prev.confidence * prev.duration_minutes
                    + seg.confidence * seg.duration_minutes
                ) / total_minutes
            else:
                avg_dist = seg.avg_distance_miles
                avg_conf = seg.confidence
            merged[-1] = TimelineSegment(
                unit_id=prev.unit_id,
                unit_type=prev.unit_type,
                partner_id=prev.partner_id,
                partner_type=prev.partner_type,
                start=prev.start,
                end=seg.end,
                duration_minutes=round(total_minutes, 1),
                avg_distance_miles=round(avg_dist, 3),
                bucket_count=total_buckets,
                confidence=round(avg_conf, 3),
            )
        else:
            merged.append(seg)

    return merged


def _render_timeline_visual(segments: list[TimelineSegment]) -> None:
    """Render the timeline as a visual HTML bar + summary table."""
    if not segments:
        return

    # --- Summary stats ---
    partner_segments = [s for s in segments if s.partner_type not in ("yard", "gap")]
    yard_segments = [s for s in segments if s.partner_type == "yard"]
    gap_segments = [s for s in segments if s.partner_type == "gap"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Paired Segments", len(partner_segments))
    c2.metric("Yard Visits", len(yard_segments))
    c3.metric("Gaps", len(gap_segments))
    total_paired_hrs = sum(s.duration_minutes for s in partner_segments) / 60
    c4.metric("Total Paired", f"{total_paired_hrs:.1f}h")

    # --- Visual timeline bar (HTML) ---
    _COLORS = {
        "yard": "#f59e0b",      # amber
        "gap": "#6b7280",       # gray
    }
    _PARTNER_COLORS = [
        "#3b82f6", "#10b981", "#8b5cf6", "#ec4899",
        "#06b6d4", "#f97316", "#6366f1", "#14b8a6",
    ]
    # Assign colors to partners
    unique_partners = list(dict.fromkeys(
        s.partner_id for s in segments if s.partner_type not in ("yard", "gap")
    ))
    partner_color_map = {p: _PARTNER_COLORS[i % len(_PARTNER_COLORS)] for i, p in enumerate(unique_partners)}

    total_minutes = sum(s.duration_minutes for s in segments)
    if total_minutes <= 0:
        total_minutes = 1

    bar_items = []
    for seg in segments:
        pct = max(0.5, (seg.duration_minutes / total_minutes) * 100)
        if seg.partner_type == "yard":
            color = _COLORS["yard"]
            label = f"🅿️ {seg.partner_id}"
        elif seg.partner_type == "gap":
            color = _COLORS["gap"]
            label = "—"
        else:
            color = partner_color_map.get(seg.partner_id, "#3b82f6")
            label = seg.partner_id

        title = f"{seg.partner_id}: {_format_dt_text(seg.start)}–{_format_dt_text(seg.end)} ({seg.duration_minutes:.0f}min)"
        bar_items.append(
            f"<div style='flex:{pct};background:{color};padding:2px 4px;overflow:hidden;"
            f"white-space:nowrap;font-size:11px;color:#fff;border-right:1px solid #0e1117;'"
            f" title='{escape(title)}'>{escape(label)}</div>"
        )

    bar_html = (
        "<div style='display:flex;height:32px;border-radius:6px;overflow:hidden;"
        "border:1px solid #374151;margin:8px 0 16px 0;'>"
        + "".join(bar_items)
        + "</div>"
    )

    # Legend
    legend_items = []
    for partner, color in partner_color_map.items():
        legend_items.append(
            f"<span style='display:inline-block;width:12px;height:12px;border-radius:3px;"
            f"background:{color};margin-right:4px;'></span>{escape(partner)}"
        )
    legend_items.append(
        "<span style='display:inline-block;width:12px;height:12px;border-radius:3px;"
        "background:#f59e0b;margin-right:4px;'></span>Yard"
    )
    legend_items.append(
        "<span style='display:inline-block;width:12px;height:12px;border-radius:3px;"
        "background:#6b7280;margin-right:4px;'></span>Unmatched"
    )
    legend_html = "<div style='font-size:12px;margin-bottom:12px;'>" + " &nbsp;&nbsp; ".join(legend_items) + "</div>"

    st.markdown(bar_html + legend_html, unsafe_allow_html=True)

    # --- Detail table ---
    table_rows = []
    for seg in segments:
        if seg.partner_type == "yard":
            icon = "🅿️"
            partner_label = f"Yard: {seg.partner_id}"
        elif seg.partner_type == "gap":
            icon = "⬜"
            partner_label = "Unmatched / No signal"
        else:
            icon = "🚛" if seg.partner_type == "truck" else "📦"
            partner_label = f"{seg.partner_type.title()} {seg.partner_id}"

        table_rows.append({
            "": icon,
            "Partner": partner_label,
            "Start": _format_dt_text(seg.start),
            "End": _format_dt_text(seg.end),
            "Duration": f"{seg.duration_minutes:.0f} min" if seg.duration_minutes < 120 else f"{seg.duration_minutes / 60:.1f} h",
            "Avg Dist (mi)": f"{seg.avg_distance_miles:.3f}" if seg.avg_distance_miles > 0 else "—",
            "Confidence": f"{seg.confidence:.0%}" if seg.confidence > 0 else "—",
            "Buckets": seg.bucket_count,
        })

    st.dataframe(table_rows, use_container_width=True, hide_index=True)


def _render_map_controls(
    assets: list[Asset],
    divisions: list[str],
    providers: list[str],
) -> tuple[set[str], bool, set[str], bool, bool, bool, str]:
    """Render dispatcher-friendly map controls above the map."""
    st.markdown("#### Map lookup")
    c1, c2, c3 = st.columns([2.4, 1.2, 1.4])
    with c1:
        unit_search = st.text_input(
            "Find unit on map",
            placeholder="Try 129, 759012, Anytrek, Prestige, address...",
            key="gps_map_unit_search",
        )
    with c2:
        shown_types = st.multiselect(
            "Show",
            ["Trucks", "Trailers"],
            default=["Trucks", "Trailers"],
            key="gps_map_types",
        )
    with c3:
        show_historical = st.checkbox(
            "Include historical last-known",
            value=True,
            key="gps_map_show_historical",
            help="Shows active units whose current GPS is missing/stale by borrowing their latest historical ping.",
        )

    with st.expander("Map filters", expanded=False):
        f1, f2 = st.columns(2)
        no_div_token = "__NO_DIVISION__"
        division_options = list(divisions)
        if any(not a.division for a in assets):
            division_options = [no_div_token] + division_options
        with f1:
            selected_divisions = st.multiselect(
                "Companies / divisions",
                division_options,
                default=division_options,
                format_func=lambda value: "No division" if value == no_div_token else _division_display(value),
                key="gps_map_division_filter",
            )
        with f2:
            selected_providers = st.multiselect(
                "GPS providers",
                providers,
                default=providers,
                format_func=_provider_display,
                key="gps_map_provider_filter",
            )

    return (
        {d for d in selected_divisions if d != no_div_token},
        no_div_token in selected_divisions,
        set(selected_providers),
        "Trucks" in shown_types,
        "Trailers" in shown_types,
        show_historical,
        unit_search.strip(),
    )


def _render_sidebar_fleet_metrics(
    visible_trucks: list[Asset],
    visible_trailers: list[Asset],
    visible_assets: list[Asset],
    active_providers: set[str],
) -> None:
    """Render compact fleet counts in the sidebar to keep mobile map layout clean."""
    with st.sidebar.expander("Fleet snapshot", expanded=True):
        st.metric("Trucks", len(visible_trucks))
        st.metric("Trailers", len(visible_trailers))
        st.metric("Last-Known", sum(1 for a in visible_assets if _is_historical_last_known(a)))
        st.metric("No Coordinates", sum(1 for a in visible_assets if not _has_coords(a)))
        st.metric("Providers", len(active_providers))


def _render_map_lookup_panel(assets: list[Asset], unit_search: str) -> Asset | None:
    """Render one search-driven copy/Google Maps/map-focus card for visible map units."""
    lookup_assets = [a for a in assets if _has_coords(a)]
    if not lookup_assets:
        st.info("No visible units have coordinates with the current filters.")
        return None

    if not unit_search:
        st.caption("Search a unit above to show its copy/paste card and center the map.")
        return None

    lookup_assets = sorted(lookup_assets, key=lambda a: (a.asset_type, _unit_sort_key(a.asset_id)))
    search_norm = unit_search.strip().lower()
    exact_matches = [a for a in lookup_assets if a.asset_id.lower() == search_norm]
    selected = exact_matches[0] if exact_matches else lookup_assets[0]

    st.session_state["gps_map_focus_key"] = f"{selected.asset_type}:{selected.asset_id}"
    _render_coordinate_action_card(selected)
    c1, c2 = st.columns([1, 2])
    with c1:
        if st.button("📍 Show on map", key=f"gps_focus_{selected.asset_type}_{selected.asset_id}"):
            st.session_state["gps_map_focus_key"] = f"{selected.asset_type}:{selected.asset_id}"
    with c2:
        if len(lookup_assets) > 1:
            st.caption(f"Showing best match of {len(lookup_assets)} visible search results. Narrow the search for an exact unit.")
    return selected


def _render_coordinate_action_card(asset: Asset) -> None:
    coords = _coords(asset)
    maps_url = _maps_url(asset)
    coords_js = coords.replace("\\", "\\\\").replace("'", "\\'")
    unit_label = escape(f"{asset.asset_type.title()} {asset.asset_id}")
    status = escape(_location_status(asset))
    note = escape(_location_note(asset))
    provider = escape(_provider_display(asset.provider))
    division = escape(_division_display(asset.division))
    last_ping = escape(_last_ping_text(asset) or "No ping time")
    ping_age = escape(_ping_age_text(asset))
    coords_escaped = escape(coords)
    maps_href = escape(maps_url, quote=True)

    html = f"""
    <style>
      body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
      .card {{ border: 1px solid #334155; border-radius: 12px; padding: 12px 14px; background: #0f172a; color: #f8fafc; }}
      .top {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
      .unit {{ font-weight: 800; font-size: 16px; }}
      .status {{ color: #fbbf24; font-weight: 700; }}
    .meta {{ margin-top: 6px; color: #cbd5e1; font-size: 13px; }}
    .updated {{ margin-top: 8px; color: #e0f2fe; font-size: 13px; }}
      .coords {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: #dbeafe; }}
      .actions {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
      button, a.btn {{ border: 0; border-radius: 8px; padding: 7px 10px; color: white; font-weight: 800; text-decoration: none; cursor: pointer; }}
      button {{ background: #2563eb; }}
      a.btn {{ background: #16a34a; }}
    </style>
    <script>
      async function copyCoords(coords, btn) {{
        try {{
          await navigator.clipboard.writeText(coords);
          const old = btn.innerText;
          btn.innerText = 'Copied';
          setTimeout(() => btn.innerText = old, 1200);
        }} catch (err) {{
          btn.innerText = 'Select coords';
        }}
      }}
    </script>
    <div class="card">
      <div class="top">
        <div>
          <span class="unit">{unit_label}</span>
          <span class="status"> · {status}</span>
        </div>
        <div class="actions">
          <span class="coords">{coords_escaped}</span>
          <button onclick="copyCoords('{coords_js}', this)">📋 Copy</button>
          <a class="btn" href="{maps_href}" target="_blank" rel="noopener">🗺️ Google Maps</a>
        </div>
      </div>
            <div class="updated">Last updated: <b>{last_ping}</b> · <b>{ping_age}</b></div>
            <div class="meta">GPS: <b>{provider}</b> · Division: <b>{division}</b></div>
      <div class="meta">{note}</div>
    </div>
    """
    components.html(html, height=118, scrolling=False)


def _has_coords(asset: Asset) -> bool:
    if asset.lat is None or asset.lon is None:
        return False
    try:
        lat = float(asset.lat)
        lon = float(asset.lon)
    except (TypeError, ValueError):
        return False
    return not (lat == 0 and lon == 0)


def _asset_matches_search(asset: Asset, search: str) -> bool:
    search_norm = (search or "").strip().lower()
    if not search_norm:
        return True
    fields = [
        asset.asset_type,
        asset.asset_id,
        asset.division,
        asset.provider,
        asset.address,
        asset.zip,
        _coords(asset),
        _location_status(asset),
        _raw_value(asset, "sheetId", "deviceId", "vehicleId", "vin", "VIN", "apiAccountName"),
    ]
    return search_norm in " ".join(str(v).lower() for v in fields if v)


def _is_historical_last_known(asset: Asset) -> bool:
    raw = asset.raw or {}
    return bool(raw.get("historicalLastKnown")) or str(raw.get("locationStatus") or "") == "Historical last known"


def _location_status(asset: Asset) -> str:
    raw = asset.raw or {}
    status = str(raw.get("locationStatus") or "").strip()
    if status:
        return status
    return "Current GPS" if _has_coords(asset) else "No coordinates"


def _location_note(asset: Asset) -> str:
    raw = asset.raw or {}
    reason = str(raw.get("historicalLookupReason") or "").strip()
    if _is_historical_last_known(asset):
        source = str(raw.get("historySource") or "").strip()
        source_note = f" · source: {source}" if source else ""
        return (reason or "Using latest historical ping because current GPS is stale/missing") + source_note
    if reason:
        return reason
    return "Live/current row from assets_current"


def _asset_marker_color(asset: Asset, base_color: list[int]) -> list[int]:
    if _is_historical_last_known(asset):
        return [245, 158, 11, 220]  # amber = stale/historical last known
    return base_color


def _division_color_map(divisions: list[str]) -> dict[str, list[int]]:
    palette = [
        [30, 144, 255, 235],   # blue
        [34, 197, 94, 235],    # green
        [168, 85, 247, 235],   # purple
        [236, 72, 153, 235],   # pink
        [20, 184, 166, 235],   # teal
        [250, 204, 21, 235],   # yellow
        [99, 102, 241, 235],   # indigo
        [244, 63, 94, 235],    # rose
    ]
    return {division: palette[index % len(palette)] for index, division in enumerate(sorted(divisions))}


def _truck_color(division: str, division_colors: dict[str, list[int]]) -> list[int]:
    return division_colors.get(division or "", [96, 165, 250, 235])


def _division_display(division: str | None) -> str:
    text = str(division or "").strip()
    normalized = text.lower().replace(" ", "").replace("_", "").replace("-", "")
    if normalized in ("prestige", "prestig", "prestigetransportation"):
        return "Prestige Transportation"
    if normalized in ("xpress", "express"):
        return "Xpress"
    return text or "No division"


def _provider_display(provider: str | None) -> str:
    text = str(provider or "").strip()
    if not text:
        return "Unknown GPS"
    if "gpstab" in text.lower():
        return text.replace("GPSTab", "GPS Tab")
    if "888" in text or "eld" in text.lower():
        return "888 ELD"
    if "anytrek" in text.lower():
        return "Anytrek"
    return text


def _render_map_legend(division_colors: dict[str, list[int]]) -> None:
    legend_items = [
        "<span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:rgb(255,140,0);border:1px solid #fff;margin-right:4px;'></span>Trailers",
        "<span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:rgb(245,158,11);border:1px solid #fff;margin-right:4px;'></span>Historical last-known",
    ]
    for division, color in division_colors.items():
        legend_items.append(
            f"<span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:rgba({color[0]},{color[1]},{color[2]},{color[3] / 255:.2f});border:1px solid #fff;margin-right:4px;'></span>Truck: {escape(_division_display(division))}"
        )
    st.markdown(
        "<div style='font-size:0.85rem;margin-top:-0.5rem;margin-bottom:0.75rem;'>"
        + " &nbsp; ".join(legend_items)
        + "</div>",
        unsafe_allow_html=True,
    )


def _coords(asset: Asset) -> str:
    if not _has_coords(asset):
        return ""
    return f"{float(asset.lat):.6f}, {float(asset.lon):.6f}"


def _maps_url(asset: Asset) -> str:
    coords = _coords(asset)
    if not coords:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(coords)


def _raw_value(asset: Asset, *keys: str) -> str:
    for key in keys:
        value = asset.raw.get(key) if asset.raw else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _identifying_info(asset: Asset) -> str:
    parts = []
    if asset.provider:
        parts.append(asset.provider)
    if asset.division:
        parts.append(f"Division: {asset.division}")

    # Trailer publisher stores the original sheet ID before numeric normalization.
    sheet_id = _raw_value(asset, "sheetId")
    if sheet_id and sheet_id != asset.asset_id:
        parts.append(f"Sheet ID: {sheet_id}")

    account = _raw_value(asset, "apiAccountName", "account", "company")
    if account:
        parts.append(f"Account: {account}")

    vin = _raw_value(asset, "vin", "VIN")
    if vin:
        parts.append(f"VIN: {vin}")

    device_id = _raw_value(asset, "deviceId", "device_id", "eldDeviceId", "trackerId")
    if device_id:
        parts.append(f"Device ID: {device_id}")

    vehicle_id = _raw_value(asset, "vehicleId", "vehicle_id", "id", "apiId")
    if vehicle_id:
        parts.append(f"Provider ID: {vehicle_id}")

    return " | ".join(parts)


def _vin(asset: Asset) -> str:
    return _raw_value(asset, "vin", "VIN")


def _last_ping_text(asset: Asset) -> str:
    if asset.last_ping is None:
        return ""
    return _format_dt_text(asset.last_ping, tz=_timezone_for_coords(asset.lat, asset.lon))


def _ping_age_text(asset: Asset) -> str:
    if asset.last_ping is None:
        return "Unknown"
    now = datetime.now(timezone.utc)
    ping = asset.last_ping.astimezone(timezone.utc)
    delta_seconds = max(0, int((now - ping).total_seconds()))
    if delta_seconds < 90:
        return "Just now"
    minutes = delta_seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        rem = minutes % 60
        return f"{hours}h {rem}m ago" if rem else f"{hours}h ago"
    days = hours // 24
    return f"{days} days ago"


def _build_unit_rows(
    assets: list[Asset],
    matches: list[MatchResult],
    assignments: dict[str, str],
    selected_div: str | None,
    search: str,
) -> list[dict[str, object]]:
    match_by_trailer = {m.trailer.asset_id: m for m in matches}
    match_by_truck = {m.truck.asset_id: m for m in matches}
    board_by_trailer = {trailer_id: truck_id for truck_id, trailer_id in assignments.items() if trailer_id}
    search_norm = (search or "").strip().lower()

    rows: list[dict[str, object]] = []
    for asset in sorted(assets, key=lambda a: ((a.division or "~"), a.asset_type, _unit_sort_key(a.asset_id))):
        if selected_div and asset.division != selected_div:
            continue

        auto_match = match_by_trailer.get(asset.asset_id) if asset.asset_type == "trailer" else match_by_truck.get(asset.asset_id)
        board_unit = board_by_trailer.get(asset.asset_id, "") if asset.asset_type == "trailer" else assignments.get(asset.asset_id, "")
        matched_unit = ""
        match_status = "Unmatched"
        distance = None
        confidence = ""
        history_hits = 0
        history_score = ""
        segment_info = ""

        if auto_match:
            matched_unit = auto_match.truck.asset_id if asset.asset_type == "trailer" else auto_match.trailer.asset_id
            distance = auto_match.distance_miles
            confidence = f"{auto_match.confidence:.0%}"
            history_hits = auto_match.history_hits
            history_score = f"{auto_match.history_score:.0%}" if auto_match.history_score else ""
            match_status = "Auto + Board" if auto_match.on_board else "Auto"
            if auto_match.segment_count:
                segment_info = f"{auto_match.segment_count} trip{'s' if auto_match.segment_count > 1 else ''} ({auto_match.segment_hours:.1f}h)"
        elif board_unit:
            matched_unit = board_unit
            match_status = "Board only"

        row = {
            "Type": asset.asset_type.title(),
            "Unit": asset.asset_id,
            "Coords": _coords(asset),
            "Map": _maps_url(asset),
            "Lat": float(asset.lat) if _has_coords(asset) else None,
            "Lon": float(asset.lon) if _has_coords(asset) else None,
            "Location Status": _location_status(asset),
            "Location Note": _location_note(asset),
            "Matched Unit": matched_unit,
            "Match Status": match_status,
            "Board Assigned": board_unit,
            "Distance (mi)": distance,
            "Confidence": confidence,
            "Provider": _provider_display(asset.provider),
            "Division": _division_display(asset.division),
            "VIN": _vin(asset),
            "Last Ping": _last_ping_text(asset),
            "Ping Age": _ping_age_text(asset),
            "Speed": asset.speed,
            "Heading": asset.heading_deg,
            "Yard": in_yard(float(asset.lat), float(asset.lon)) if _has_coords(asset) else "",
            "History Hits": history_hits,
            "History Score": history_score,
            "Trip Segments": segment_info,
            "Address": asset.address,
            "ZIP": asset.zip,
            "Identifying Info": _identifying_info(asset),
        }

        if search_norm:
            haystack = " ".join(str(v).lower() for v in row.values() if v is not None)
            if search_norm not in haystack:
                continue
        rows.append(row)

    return rows


def _unit_sort_key(unit: str) -> tuple[int, str]:
    text = str(unit)
    digits = "".join(ch for ch in text if ch.isdigit())
    return (int(digits) if digits else 999999999, text)


def _render_copy_grid(rows: list[dict[str, object]]) -> None:
    visible_rows = rows[:250]
    table_rows = []
    for row in visible_rows:
        coords = str(row.get("Coords") or "")
        disabled = "" if coords else "disabled"
        button_label = "Copy" if coords else "No coords"
        map_url = escape(str(row.get("Map") or ""), quote=True)
        unit = escape(str(row.get("Unit") or ""))
        unit_type = escape(str(row.get("Type") or ""))
        location_status = escape(str(row.get("Location Status") or ""))
        location_note = escape(str(row.get("Location Note") or ""))
        matched = escape(str(row.get("Matched Unit") or ""))
        status = escape(str(row.get("Match Status") or ""))
        provider = escape(str(row.get("Provider") or ""))
        division = escape(str(row.get("Division") or ""))
        ping_age = escape(str(row.get("Ping Age") or ""))
        last_ping = escape(str(row.get("Last Ping") or ""))
        vin = escape(str(row.get("VIN") or ""))
        yard = escape(str(row.get("Yard") or ""))
        history_hits = escape(str(row.get("History Hits") or ""))
        info = escape(str(row.get("Identifying Info") or ""))
        address = escape(str(row.get("Address") or ""))
        coords_escaped = escape(coords)
        coords_js = coords.replace("\\", "\\\\").replace("'", "\\'")
        table_rows.append(
            "<tr>"
            f"<td><span class='pill {unit_type.lower()}'>{unit_type}</span></td>"
            f"<td class='unit'>{unit}</td>"
            f"<td><button {disabled} onclick=\"copyCoords('{coords_js}', this)\">{button_label}</button></td>"
            f"<td><a class='map-link' href='{map_url}' target='_blank' rel='noopener'>🗺️ Maps</a></td>"
            f"<td class='coords'>{coords_escaped}</td>"
            f"<td title='{location_note}'>{location_status}</td>"
            f"<td>{matched}</td>"
            f"<td>{status}</td>"
            f"<td>{division}</td>"
            f"<td>{last_ping}</td>"
            f"<td>{ping_age}</td>"
            f"<td>{vin}</td>"
            f"<td>{yard}</td>"
            f"<td>{history_hits}</td>"
            f"<td>{provider}</td>"
            f"<td title='{info}'>{info}</td>"
            f"<td title='{address}'>{address}</td>"
            "</tr>"
        )

    html = f"""
    <style>
      body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; }}
      .wrap {{ max-height: 440px; overflow: auto; border: 1px solid #2f3b4a; border-radius: 8px; }}
      table {{ border-collapse: collapse; width: 100%; font-size: 13px; color: #f3f6fb; background: #0e1117; }}
      th, td {{ border-bottom: 1px solid #283241; padding: 7px 8px; text-align: left; white-space: nowrap; }}
      th {{ position: sticky; top: 0; background: #1f2937; z-index: 1; }}
      tr:hover {{ background: #172033; }}
      button {{ cursor: pointer; border: 0; border-radius: 6px; padding: 5px 9px; background: #2563eb; color: white; font-weight: 600; }}
      button:disabled {{ cursor: not-allowed; opacity: .45; background: #64748b; }}
    .map-link {{ display: inline-block; border-radius: 6px; padding: 5px 9px; background: #16a34a; color: white; font-weight: 700; text-decoration: none; }}
      .unit {{ font-weight: 700; color: #ffffff; }}
      .coords {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
      .pill {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-weight: 700; font-size: 12px; }}
      .pill.truck {{ background: #1d4ed8; }}
      .pill.trailer {{ background: #c2410c; }}
    </style>
    <script>
      async function copyCoords(coords, btn) {{
        try {{
          await navigator.clipboard.writeText(coords);
          const old = btn.innerText;
          btn.innerText = 'Copied';
          setTimeout(() => btn.innerText = old, 1200);
        }} catch (err) {{
          btn.innerText = 'Select coords';
        }}
      }}
    </script>
    <div class="wrap">
      <table>
        <thead>
          <tr>
                        <th>Type</th><th>Unit</th><th>Copy</th><th>Map</th><th>Coords</th><th>Location</th><th>Matched</th>
            <th>Status</th><th>Division</th><th>Last Ping</th><th>Age</th><th>VIN</th><th>Yard</th><th>History</th>
            <th>Provider</th><th>Identifying Info</th><th>Address</th>
          </tr>
        </thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </div>
    """
    components.html(html, height=470, scrolling=False)


def _rows_to_csv(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
