"""
GPS Map & Trailer-Truck Matching page for Streamlit.
Shows all assets on a map, auto-matches trailers to nearby trucks,
and draws connection lines with confidence scores.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from io import StringIO
from urllib.parse import quote_plus

import streamlit as st
import streamlit.components.v1 as components

from services.gps_data import (
    load_all_unit_ids,
    load_assignments,
    load_asset_pairing_timeline,
    load_asset_history,
    load_asset_history_range,
    load_current_assets,
    load_match_reviews,
    load_recent_match_reviews,
    load_unit_timeline_history,
    save_match_reviews,
)
from services.gps_matching import (
    Asset,
    MatchResult,
    TimelineSegment,
    compute_historical_usage,
    compute_matches,
    compute_unit_timeline,
    in_yard,
    YARD_BOXES,
)


def render_gps_map_page() -> None:
    st.header("GPS Fleet Map & Trailer Matching")

    # --- Load FAST data first (current positions only) ---
    with st.spinner("Loading positions..."):
        assets = load_current_assets()
        assignments = load_assignments()

    if not assets:
        st.warning("No GPS data found in Supabase. Ensure the dispatch board publisher is running.")
        return

    trucks = [a for a in assets if a.asset_type == "truck"]
    trailers = [a for a in assets if a.asset_type == "trailer"]

    # --- Sidebar: toggles for companies + providers ---
    st.sidebar.subheader("Visibility")
    divisions = sorted({a.division for a in assets if a.division})
    providers = sorted({a.provider for a in assets if a.provider})

    # Company toggles
    st.sidebar.caption("**Companies**")
    active_divs: set[str] = set()
    for div in divisions:
        if st.sidebar.checkbox(div, value=True, key=f"div_{div}"):
            active_divs.add(div)
    # Include assets with empty division if any company is checked
    show_no_div = st.sidebar.checkbox("(No division)", value=True, key="div_none")

    # Provider toggles
    st.sidebar.caption("**GPS Providers**")
    active_providers: set[str] = set()
    for prov in providers:
        short = prov.split("(")[0].strip() if "(" in prov else prov
        if st.sidebar.checkbox(short, value=True, key=f"prov_{prov}"):
            active_providers.add(prov)

    st.sidebar.subheader("Matching")
    max_dist = st.sidebar.slider("Match radius (miles)", 0.1, 5.0, 0.5, 0.1)
    max_stale = st.sidebar.slider("Max stale (minutes)", 15, 240, 60, 15)
    history_hours = st.sidebar.slider("History lookback (hours)", 12, 168, 48, 12)
    min_history_hits = st.sidebar.slider("Min route hits", 1, 6, 2, 1)
    unit_search = st.sidebar.text_input("Search units", placeholder="Unit, provider, address, VIN...")

    # --- Filter assets by toggles ---
    def _visible(a: Asset) -> bool:
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

    # --- Metrics (instant) ---
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Trucks", len(visible_trucks))
    col2.metric("Trailers", len(visible_trailers))
    col3.metric("Divisions", len(active_divs))
    col4.metric("Providers", len(active_providers))

    # --- Map (renders FIRST with just current positions) ---
    _render_map(visible_trucks, visible_trailers, divisions, assignments)

    # --- Tabs for heavy content ---
    tab_fleet, tab_timeline, tab_matches, tab_history, tab_review = st.tabs([
        "Fleet Overview", "Unit Timeline", "Auto-Matches", "Historical Usage", "Match Review",
    ])

    with tab_fleet:
        with st.expander("All Found Units", expanded=True):
            unit_rows = _build_unit_rows(visible_assets, [], assignments, None, unit_search)
            if unit_rows:
                _render_copy_grid(unit_rows)
            else:
                st.info("No units match the current filters.")

    with tab_timeline:
        _render_timeline_tab(visible_assets, max_dist)

    with tab_matches:
        _render_matches_tab(
            visible_trucks, visible_trailers, assignments,
            history_hours, min_history_hits, max_dist, max_stale, active_divs,
        )

    with tab_history:
        _render_history_tab(max_dist, active_divs)

    with tab_review:
        _render_review_tab(
            visible_trucks, visible_trailers, assignments,
            history_hours, min_history_hits, max_dist, max_stale, active_divs,
        )


def _render_map(
    trucks: list[Asset],
    trailers: list[Asset],
    divisions: list[str],
    assignments: dict[str, str],
) -> None:
    """Render the pydeck map with current positions only (fast)."""
    try:
        import pydeck as pdk
    except ImportError:
        st.error("Install pydeck: `pip install pydeck`")
        return

    layers = []
    division_colors = _division_color_map(divisions)

    truck_data = [
        {
            "lat": t.lat, "lon": t.lon, "id": t.asset_id,
            "division": t.division, "provider": t.provider,
            "address": t.address, "coords": _coords(t),
            "color": _truck_color(t.division, division_colors),
            "asset_type": "Truck",
        }
        for t in trucks if _has_coords(t)
    ]
    if truck_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=truck_data,
            get_position=["lon", "lat"], get_radius=450,
            radius_min_pixels=8, radius_max_pixels=26,
            get_fill_color="color", stroked=True,
            get_line_color=[255, 255, 255, 220], get_line_width=2,
            pickable=True, auto_highlight=True,
        ))

    trailer_data = [
        {
            "lat": t.lat, "lon": t.lon, "id": t.asset_id,
            "division": t.division, "provider": t.provider,
            "address": t.address, "coords": _coords(t),
            "in_yard": in_yard(t.lat, t.lon) or "",
            "color": [255, 140, 0, 230], "asset_type": "Trailer",
        }
        for t in trailers if _has_coords(t)
    ]
    if trailer_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=trailer_data,
            get_position=["lon", "lat"], get_radius=400,
            radius_min_pixels=7, radius_max_pixels=24,
            get_fill_color="color", stroked=True,
            get_line_color=[255, 255, 255, 220], get_line_width=2,
            pickable=True, auto_highlight=True,
        ))

    all_coords = truck_data + trailer_data
    if all_coords:
        center_lat = sum(d["lat"] for d in all_coords) / len(all_coords)
        center_lon = sum(d["lon"] for d in all_coords) / len(all_coords)
    else:
        center_lat, center_lon = 39.8, -89.6

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=5, pitch=0),
        tooltip={
            "html": "<b>{asset_type}: {id}</b><br/>{coords}<br/>{address}<br/>{division}<br/>{provider}",
            "style": {"backgroundColor": "#1f2937", "color": "#e5e7eb"},
        },
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    )
    st.pydeck_chart(deck)
    _render_map_legend(division_colors)


def _render_timeline_tab(assets: list[Asset], max_dist: float) -> None:
    """Unit timeline: select a unit and see who it was paired with over time."""
    st.subheader("Unit Assignment Timeline")
    st.caption(
        "Select a truck or trailer to see its historical pairings. "
        "Yard visits act as assignment boundaries — entering the yard ends one pairing."
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

    use_raw_fallback = st.checkbox(
        "If no precomputed pairings are found, compute from raw GPS pings (slow)",
        value=False,
        key="tl_raw_fallback",
        help="Normally leave this off. Raw computation can load hundreds of thousands of GPS points.",
    )

    if st.button("Load Timeline", type="primary", key="tl_compute"):
        start_dt = datetime.combine(tl_start, time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(tl_end, time.max, tzinfo=timezone.utc)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt

        with st.spinner("Loading precomputed pairings..."):
            segments = load_asset_pairing_timeline(unit_id, unit_type, start_dt, end_dt)

        if not segments and use_raw_fallback:
            days = max(1, (end_dt - start_dt).days)
            if days > 3:
                st.warning(
                    "Raw timeline fallback is intentionally limited to 3 days in the UI. "
                    "For longer ranges, run `python scripts/compute_pairings.py --days 7` locally first."
                )
                return
            with st.spinner(f"No precomputed rows found. Loading raw GPS history for {unit_id} ({days} days)..."):
                history = load_unit_timeline_history(unit_id, start_dt, end_dt)
            if not history:
                st.warning("No raw history found for this unit in the selected range.")
                return
            st.info(f"Loaded {len(history):,} history points. Computing raw timeline...")
            segments = compute_unit_timeline(
                unit_id, unit_type, history, max_distance_miles=max_dist,
            )

        if not segments:
            st.info(
                "No precomputed pairing segments found for this unit/range yet. "
                "Run `python scripts/compute_pairings.py --days 7` locally to populate asset_pairings."
            )
            return

        # Store in session for display
        st.session_state["timeline_segments"] = segments
        st.session_state["timeline_selected_unit"] = f"{unit_type}:{unit_id}"

    # Display timeline if available
    if "timeline_segments" in st.session_state:
        segments = st.session_state["timeline_segments"]
        _render_timeline_visual(segments)


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

        title = f"{seg.partner_id}: {seg.start.strftime('%m/%d %H:%M')}–{seg.end.strftime('%H:%M')} ({seg.duration_minutes:.0f}min)"
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
            "Start": seg.start.strftime("%m/%d %H:%M"),
            "End": seg.end.strftime("%m/%d %H:%M"),
            "Duration": f"{seg.duration_minutes:.0f} min" if seg.duration_minutes < 120 else f"{seg.duration_minutes / 60:.1f} h",
            "Avg Dist (mi)": f"{seg.avg_distance_miles:.3f}" if seg.avg_distance_miles > 0 else "—",
            "Confidence": f"{seg.confidence:.0%}" if seg.confidence > 0 else "—",
            "Buckets": seg.bucket_count,
        })

    st.dataframe(table_rows, use_container_width=True, hide_index=True)


def _render_matches_tab(
    trucks: list[Asset],
    trailers: list[Asset],
    assignments: dict[str, str],
    history_hours: int,
    min_history_hits: int,
    max_dist: float,
    max_stale: float,
    active_divs: set[str],
) -> None:
    """Auto-match tab — only loads history when user clicks."""
    st.subheader("Auto-Match Results")
    st.caption("Computes trailer↔truck matches using current positions + GPS history evidence.")

    if st.button("Compute Matches", type="primary", key="compute_matches_btn"):
        with st.spinner(f"Loading {history_hours}h of GPS history..."):
            history = load_asset_history(hours=history_hours)
        st.session_state["match_history"] = history
        st.session_state["match_computed"] = True

    if not st.session_state.get("match_computed"):
        st.info("Click **Compute Matches** to load history and run the matching engine.")
        return

    history = st.session_state.get("match_history", [])
    matches = compute_matches(
        trucks, trailers, assignments, history,
        max_distance_miles=max_dist,
        max_stale_minutes=max_stale,
        min_history_hits=min_history_hits,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Matches", len(matches))
    c2.metric("Board Agrees", sum(1 for m in matches if m.on_board))
    c3.metric("With Segments", sum(1 for m in matches if m.segment_count > 0))
    c4.metric("History Rows", len(history))

    if matches:
        # Show match table inside expander
        with st.expander("Match Details", expanded=True):
            match_rows = _build_match_review_rows(matches)
            _render_match_copy_grid(match_rows)

        # Quick unit rows showing match results
        with st.expander("Full Fleet Table (with matches)"):
            unit_rows = _build_unit_rows(
                [a for a in (trucks + trailers)], matches, assignments, None, "",
            )
            if unit_rows:
                _render_copy_grid(unit_rows)
    else:
        st.info("No matches found with current settings.")


def _render_history_tab(max_dist: float, active_divs: set[str]) -> None:
    """Historical usage tab — only computes on button click."""
    st.subheader("Historical Trailer Usage")
    st.caption(
        "Many-to-many co-location analysis. Shows which trucks used which trailers over a date range. "
        "Only loads and computes when you click the button."
    )

    today = date.today()
    col1, col2, col3 = st.columns(3)
    with col1:
        usage_start = st.date_input("From", value=today - timedelta(days=7), key="hist_start")
    with col2:
        usage_end = st.date_input("To", value=today, key="hist_end")
    with col3:
        usage_min_hits = st.slider("Min hours co-located", 1, 10, 2, 1, key="hist_min_hits")

    if st.button("Compute Historical Usage", type="primary", key="hist_compute_btn"):
        usage_start_dt = datetime.combine(usage_start, time.min, tzinfo=timezone.utc)
        usage_end_dt = datetime.combine(usage_end, time.max, tzinfo=timezone.utc)
        if usage_end_dt < usage_start_dt:
            usage_start_dt, usage_end_dt = usage_end_dt, usage_start_dt

        with st.spinner(f"Loading {(usage_end_dt - usage_start_dt).days} days of history..."):
            usage_history = load_asset_history_range(usage_start_dt, usage_end_dt)

        if not usage_history:
            st.warning("No history data in selected range.")
            return

        st.info(f"Loaded {len(usage_history)} points. Computing co-locations...")
        usage_rows = _build_historical_usage_rows(
            usage_history, selected_div=None, max_distance_miles=max_dist, min_hits=usage_min_hits,
        )
        st.session_state["hist_usage_rows"] = usage_rows

    if "hist_usage_rows" in st.session_state:
        usage_rows = st.session_state["hist_usage_rows"]
        if usage_rows:
            st.dataframe(
                usage_rows, use_container_width=True, hide_index=True,
                column_config={
                    "Hits": st.column_config.NumberColumn("Hours", format="%d"),
                    "Min Distance (mi)": st.column_config.NumberColumn(format="%.3f"),
                    "Avg Distance (mi)": st.column_config.NumberColumn(format="%.3f"),
                },
            )
        else:
            st.info("No co-locations found for the selected range.")


def _render_review_tab(
    trucks: list[Asset],
    trailers: list[Asset],
    assignments: dict[str, str],
    history_hours: int,
    min_history_hits: int,
    max_dist: float,
    max_stale: float,
    active_divs: set[str],
) -> None:
    """Match review (confirm/reject) tab — reuses match data if available."""
    st.subheader("Match Review & Confirmation")

    # Reuse matches from the Auto-Matches tab if already computed
    history = st.session_state.get("match_history", [])
    if not history:
        st.info("Go to **Auto-Matches** tab and click Compute Matches first.")
        return

    matches = compute_matches(
        trucks, trailers, assignments, history,
        max_distance_miles=max_dist,
        max_stale_minutes=max_stale,
        min_history_hits=min_history_hits,
    )

    if not matches:
        st.info("No matches to review.")
        return

    _render_match_review(matches, selected_div=None)


def _has_coords(asset: Asset) -> bool:
    if asset.lat is None or asset.lon is None:
        return False
    try:
        lat = float(asset.lat)
        lon = float(asset.lon)
    except (TypeError, ValueError):
        return False
    return not (lat == 0 and lon == 0)


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


def _render_map_legend(division_colors: dict[str, list[int]]) -> None:
    legend_items = ["<span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:rgb(255,140,0);border:1px solid #fff;margin-right:4px;'></span>Trailers"]
    for division, color in division_colors.items():
        legend_items.append(
            f"<span style='display:inline-block;width:12px;height:12px;border-radius:50%;background:rgba({color[0]},{color[1]},{color[2]},{color[3] / 255:.2f});border:1px solid #fff;margin-right:4px;'></span>Truck: {escape(division)}"
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
    return asset.last_ping.strftime("%Y-%m-%d %H:%M UTC")


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
            "Matched Unit": matched_unit,
            "Match Status": match_status,
            "Board Assigned": board_unit,
            "Distance (mi)": distance,
            "Confidence": confidence,
            "Provider": asset.provider,
            "Division": asset.division,
            "VIN": _vin(asset),
            "Last Ping": _last_ping_text(asset),
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
        unit = escape(str(row.get("Unit") or ""))
        unit_type = escape(str(row.get("Type") or ""))
        matched = escape(str(row.get("Matched Unit") or ""))
        status = escape(str(row.get("Match Status") or ""))
        provider = escape(str(row.get("Provider") or ""))
        division = escape(str(row.get("Division") or ""))
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
            f"<td class='coords'>{coords_escaped}</td>"
            f"<td>{matched}</td>"
            f"<td>{status}</td>"
            f"<td>{division}</td>"
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
            <th>Type</th><th>Unit</th><th>Copy</th><th>Coords</th><th>Matched</th>
            <th>Status</th><th>Division</th><th>VIN</th><th>Yard</th><th>History</th>
            <th>Provider</th><th>Identifying Info</th><th>Address</th>
          </tr>
        </thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </div>
    """
    components.html(html, height=470, scrolling=False)


def _render_match_review(matches: list[MatchResult], *, selected_div: str | None) -> None:
    match_rows = _build_match_review_rows(matches)
    st.caption(
        "Use the copy buttons to paste truck coordinates into Anytrek. Then mark each auto-match as Confirmed, Rejected, or Pending."
    )
    _render_match_copy_grid(match_rows)

    saved_reviews = load_match_reviews([str(row["Match ID"]) for row in match_rows])
    editor_rows = []
    for row in match_rows:
        existing = saved_reviews.get(str(row["Match ID"]), {})
        editor_rows.append({
            "Decision": existing.get("decision", "Pending"),
            "Reviewer Note": existing.get("reviewer_note", ""),
            "Match ID": row["Match ID"],
            "Trailer": row["Trailer"],
            "Truck": row["Truck"],
            "Truck Coords": row["Truck Coords"],
            "Trailer Coords": row["Trailer Coords"],
            "Distance (mi)": row["Distance (mi)"],
            "Confidence": row["Confidence"],
            "History Hits": row["History Hits"],
            "On Board": row["On Board"],
            "Trailer Yard": row["Trailer Yard"],
            "Truck Yard": row["Truck Yard"],
            "Reasons": row["Reasons"],
        })

    with st.form("gps_match_review_submit_form"):
        edited_rows = st.data_editor(
            editor_rows,
            use_container_width=True,
            hide_index=True,
            disabled=[
                "Match ID",
                "Trailer",
                "Truck",
                "Truck Coords",
                "Trailer Coords",
                "Distance (mi)",
                "Confidence",
                "History Hits",
                "On Board",
                "Trailer Yard",
                "Truck Yard",
                "Reasons",
            ],
            column_config={
                "Decision": st.column_config.SelectboxColumn(
                    "Decision",
                    options=["Pending", "Confirmed", "Rejected"],
                    required=True,
                    help="Mark whether your Anytrek coordinate check agrees with the auto-match.",
                ),
                "Reviewer Note": st.column_config.TextColumn(
                    "Reviewer Note",
                    help="Optional note, especially useful for rejected matches.",
                ),
                "Distance (mi)": st.column_config.NumberColumn("Distance (mi)", format="%.3f"),
                "History Hits": st.column_config.NumberColumn("History Hits", format="%d"),
            },
            key="gps_match_review_editor",
        )
        submitted = st.form_submit_button("Save confirmed/rejected reviews to database", use_container_width=True)

    edited_list = _table_to_records(edited_rows)
    review_state = {
        str(row["Match ID"]): {
            "Decision": row.get("Decision", "Pending"),
            "Reviewer Note": row.get("Reviewer Note", ""),
        }
        for row in edited_list
    }

    artifact_rows = _build_review_artifact_rows(match_rows, review_state, selected_div=selected_div)
    save_rows = [row for row in artifact_rows if row["decision"] in {"Confirmed", "Rejected"}]
    if submitted:
        if not save_rows:
            st.warning("Mark at least one row Confirmed or Rejected before saving.")
        else:
            try:
                saved_count = save_match_reviews(save_rows)
                st.success(f"Saved {saved_count} review decision(s) to Supabase.")
            except Exception as exc:
                st.error(
                    "Could not save reviews. Run `supabase/migrations/0016_gps_match_reviews.sql` in Supabase first, "
                    f"then try again. Error: {exc}"
                )

    reviewed_count = sum(1 for row in artifact_rows if row["decision"] != "Pending")
    rejected_count = sum(1 for row in artifact_rows if row["decision"] == "Rejected")
    confirmed_count = sum(1 for row in artifact_rows if row["decision"] == "Confirmed")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Reviewed", reviewed_count)
    c2.metric("Confirmed", confirmed_count)
    c3.metric("Rejected", rejected_count)
    c4.metric("Pending", len(artifact_rows) - reviewed_count)

    reviewed_artifact_rows = [row for row in artifact_rows if row["decision"] != "Pending"]
    if reviewed_artifact_rows:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        csv_data = _rows_to_csv(reviewed_artifact_rows)
        json_data = json.dumps(reviewed_artifact_rows, indent=2, default=str)
        d1, d2 = st.columns(2)
        d1.download_button(
            "Download reviewed matches CSV",
            data=csv_data,
            file_name=f"gps_match_review_{timestamp}.csv",
            mime="text/csv",
            use_container_width=True,
        )
        d2.download_button(
            "Download reviewed matches JSON",
            data=json_data,
            file_name=f"gps_match_review_{timestamp}.json",
            mime="application/json",
            use_container_width=True,
        )
    else:
        st.info("Confirm or reject at least one auto-match to generate a downloadable review artifact.")

    saved_export_rows = load_recent_match_reviews(limit=1000)
    if saved_export_rows:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        st.download_button(
            "Download saved review database export CSV",
            data=_rows_to_csv(saved_export_rows),
            file_name=f"gps_saved_match_reviews_{timestamp}.csv",
            mime="text/csv",
            use_container_width=True,
        )


def _build_match_review_rows(matches: list[MatchResult]) -> list[dict[str, object]]:
    rows = []
    for m in matches:
        rows.append({
            "Match ID": f"{m.trailer.asset_id}__{m.truck.asset_id}",
            "Trailer": m.trailer.asset_id,
            "Truck": m.truck.asset_id,
            "Truck Coords": _coords(m.truck),
            "Trailer Coords": _coords(m.trailer),
            "Distance (mi)": m.distance_miles,
            "Confidence": f"{m.confidence:.0%}",
            "History Hits": m.history_hits,
            "On Board": "Yes" if m.on_board else "No",
            "Trailer Yard": m.trailer_yard,
            "Truck Yard": m.truck_yard,
            "Reasons": ", ".join(m.reasons),
            "Truck Provider": m.truck.provider,
            "Trailer Provider": m.trailer.provider,
            "Truck Address": m.truck.address,
            "Trailer Address": m.trailer.address,
            "Truck Division": m.truck.division,
            "Trailer Division": m.trailer.division,
        })
    return rows


def _render_match_copy_grid(rows: list[dict[str, object]]) -> None:
    table_rows = []
    for row in rows:
        truck_coords = str(row.get("Truck Coords") or "")
        trailer_coords = str(row.get("Trailer Coords") or "")
        truck_coords_js = truck_coords.replace("\\", "\\\\").replace("'", "\\'")
        trailer_coords_js = trailer_coords.replace("\\", "\\\\").replace("'", "\\'")
        table_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('Trailer') or ''))}</td>"
            f"<td>{escape(str(row.get('Truck') or ''))}</td>"
            f"<td><button onclick=\"copyCoords('{truck_coords_js}', this)\">Copy truck</button></td>"
            f"<td class='coords'>{escape(truck_coords)}</td>"
            f"<td><button onclick=\"copyCoords('{trailer_coords_js}', this)\">Copy trailer</button></td>"
            f"<td class='coords'>{escape(trailer_coords)}</td>"
            f"<td>{escape(str(row.get('Distance (mi)') or ''))}</td>"
            f"<td>{escape(str(row.get('Confidence') or ''))}</td>"
            f"<td>{escape(str(row.get('History Hits') or ''))}</td>"
            f"<td>{escape(str(row.get('On Board') or ''))}</td>"
            f"<td title='{escape(str(row.get('Reasons') or ''))}'>{escape(str(row.get('Reasons') or ''))}</td>"
            "</tr>"
        )
    html = f"""
    <style>
      body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; }}
      .wrap {{ max-height: 320px; overflow: auto; border: 1px solid #2f3b4a; border-radius: 8px; }}
      table {{ border-collapse: collapse; width: 100%; font-size: 13px; color: #f3f6fb; background: #0e1117; }}
      th, td {{ border-bottom: 1px solid #283241; padding: 7px 8px; text-align: left; white-space: nowrap; }}
      th {{ position: sticky; top: 0; background: #1f2937; z-index: 1; }}
      tr:hover {{ background: #172033; }}
      button {{ cursor: pointer; border: 0; border-radius: 6px; padding: 5px 9px; background: #2563eb; color: white; font-weight: 600; }}
      .coords {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
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
            <th>Trailer</th><th>Truck</th><th>Copy Truck</th><th>Truck Coords</th>
            <th>Copy Trailer</th><th>Trailer Coords</th><th>Dist</th><th>Conf</th><th>Hist</th><th>Board</th><th>Reasons</th>
          </tr>
        </thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </div>
    """
    components.html(html, height=350, scrolling=False)


def _build_review_artifact_rows(
    match_rows: list[dict[str, object]],
    review_state: dict[str, dict[str, str]],
    *,
    selected_div: str | None,
) -> list[dict[str, object]]:
    reviewed_at = datetime.now(timezone.utc).isoformat()
    out = []
    for row in match_rows:
        state = review_state.get(str(row["Match ID"]), {})
        decision = state.get("Decision", "Pending")
        reviewer_note = state.get("Reviewer Note", "")
        out.append({
            "match_id": row["Match ID"],
            "decision": decision,
            "reviewer_note": reviewer_note,
            "reviewed_at": reviewed_at,
            "division_filter": selected_div or "All",
            "trailer_id": row["Trailer"],
            "truck_id": row["Truck"],
            "truck_coords": row["Truck Coords"],
            "trailer_coords": row["Trailer Coords"],
            "distance_miles": row["Distance (mi)"],
            "confidence": row["Confidence"],
            "history_hits": row["History Hits"],
            "on_board": row["On Board"],
            "trailer_yard": row["Trailer Yard"],
            "truck_yard": row["Truck Yard"],
            "reasons": row["Reasons"],
            "truck_provider": row["Truck Provider"],
            "trailer_provider": row["Trailer Provider"],
            "truck_address": row["Truck Address"],
            "trailer_address": row["Trailer Address"],
            "truck_division": row["Truck Division"],
            "trailer_division": row["Trailer Division"],
            "raw": row,
        })
    return out


def _table_to_records(data: object) -> list[dict[str, object]]:
    if hasattr(data, "to_dict"):
        try:
            records = data.to_dict("records")
            if isinstance(records, list):
                return records
        except TypeError:
            pass
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)]
    return []


def _rows_to_csv(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _build_historical_usage_rows(
    history: list[Asset],
    *,
    selected_div: str | None,
    max_distance_miles: float,
    min_hits: int,
) -> list[dict[str, object]]:
    usage = compute_historical_usage(
        history,
        max_distance_miles=max_distance_miles,
        min_hits=min_hits,
        division_filter=selected_div,
    )
    rows: list[dict[str, object]] = []
    for item in usage:
        seg_info = ""
        if item.segment_count:
            seg_info = f"{item.segment_count} trip{'s' if item.segment_count > 1 else ''} ({item.segment_hours:.1f}h)"
        rows.append({
            "Truck": item.truck_id,
            "Trailer": item.trailer_id,
            "Hits": item.hits,
            "Days": ", ".join(item.days),
            "Trip Segments": seg_info,
            "First Seen": item.first_seen.strftime("%Y-%m-%d %H:%M UTC") if item.first_seen else "",
            "Last Seen": item.last_seen.strftime("%Y-%m-%d %H:%M UTC") if item.last_seen else "",
            "Min Distance (mi)": item.min_distance_miles,
            "Avg Distance (mi)": item.avg_distance_miles,
            "Confidence": f"{item.confidence:.0%}",
        })
    return rows
