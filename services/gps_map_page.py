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

from services.gps_data import load_current_assets, load_asset_history, load_asset_history_range, load_assignments
from services.gps_matching import (
    Asset,
    MatchResult,
    compute_historical_usage,
    compute_matches,
    in_yard,
    YARD_BOXES,
)


def render_gps_map_page() -> None:
    st.header("GPS Fleet Map & Trailer Matching")

    # --- Load data ---
    with st.spinner("Loading asset positions..."):
        assets = load_current_assets()
        assignments = load_assignments()

    if not assets:
        st.warning("No GPS data found in Supabase. Ensure the dispatch board publisher is running.")
        return

    trucks = [a for a in assets if a.asset_type == "truck"]
    trailers = [a for a in assets if a.asset_type == "trailer"]

    # --- Sidebar filters ---
    st.sidebar.subheader("Filters")
    divisions = sorted({a.division for a in assets if a.division})
    div_filter = st.sidebar.selectbox("Division", ["All"] + divisions)
    selected_div = None if div_filter == "All" else div_filter

    max_dist = st.sidebar.slider("Match radius (miles)", 0.1, 5.0, 0.5, 0.1)
    max_stale = st.sidebar.slider("Max stale (minutes)", 15, 240, 60, 15)
    history_hours = st.sidebar.slider("History lookback (hours)", 12, 96, 48, 12)
    min_history_hits = st.sidebar.slider("Min route hits", 1, 6, 2, 1)
    st.sidebar.subheader("Historical Usage")
    today = date.today()
    usage_start = st.sidebar.date_input("Usage from", value=today - timedelta(days=7))
    usage_end = st.sidebar.date_input("Usage to", value=today - timedelta(days=1))
    usage_min_hits = st.sidebar.slider("Usage min hits", 1, 10, 2, 1)

    show_trucks = st.sidebar.checkbox("Show trucks", True)
    show_trailers = st.sidebar.checkbox("Show trailers", True)
    show_matches = st.sidebar.checkbox("Show match lines", True)
    unit_search = st.sidebar.text_input("Search units", placeholder="Unit, provider, address, VIN...")

    with st.spinner("Loading recent GPS history..."):
        history = load_asset_history(hours=history_hours, division=selected_div)

    usage_start_dt = datetime.combine(usage_start, time.min, tzinfo=timezone.utc)
    usage_end_dt = datetime.combine(usage_end, time.max, tzinfo=timezone.utc)
    if usage_end_dt < usage_start_dt:
        usage_start_dt, usage_end_dt = usage_end_dt, usage_start_dt

    with st.spinner("Loading historical usage range..."):
        usage_history = load_asset_history_range(usage_start_dt, usage_end_dt, division=selected_div)

    # --- Compute matches ---
    matches = compute_matches(
        trucks,
        trailers,
        assignments,
        history,
        max_distance_miles=max_dist,
        max_stale_minutes=max_stale,
        min_history_hits=min_history_hits,
        division_filter=selected_div,
    )

    # --- Metrics ---
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Trucks", len(trucks))
    col2.metric("Trailers", len(trailers))
    col3.metric("Auto-Matches", len(matches))
    col4.metric("Board Agrees", sum(1 for m in matches if m.on_board))
    col5.metric("History Rows", len(history))

    # --- Map (pydeck) ---
    try:
        import pydeck as pdk
    except ImportError:
        st.error("Install pydeck: `pip install pydeck`")
        return

    layers = []
    division_colors = _division_color_map(divisions)

    # Truck layer
    if show_trucks:
        truck_data = [
            {
                "lat": t.lat,
                "lon": t.lon,
                "id": t.asset_id,
                "division": t.division,
                "provider": t.provider,
                "address": t.address,
                "coords": _coords(t),
                "color": _truck_color(t.division, division_colors),
                "asset_type": "Truck",
            }
            for t in trucks
            if _has_coords(t) and (selected_div is None or t.division == selected_div)
        ]
        if truck_data:
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=truck_data,
                get_position=["lon", "lat"],
                get_radius=450,
                radius_min_pixels=8,
                radius_max_pixels=26,
                get_fill_color="color",
                stroked=True,
                get_line_color=[255, 255, 255, 220],
                get_line_width=2,
                pickable=True,
                auto_highlight=True,
            ))

    # Trailer layer
    if show_trailers:
        trailer_data = [
            {
                "lat": t.lat,
                "lon": t.lon,
                "id": t.asset_id,
                "division": t.division,
                "provider": t.provider,
                "address": t.address,
                "coords": _coords(t),
                "in_yard": in_yard(t.lat, t.lon) or "",
                "color": [255, 140, 0, 230],
                "asset_type": "Trailer",
            }
            for t in trailers
            if _has_coords(t) and (selected_div is None or t.division == selected_div)
        ]
        if trailer_data:
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=trailer_data,
                get_position=["lon", "lat"],
                get_radius=400,
                radius_min_pixels=7,
                radius_max_pixels=24,
                get_fill_color="color",
                stroked=True,
                get_line_color=[255, 255, 255, 220],
                get_line_width=2,
                pickable=True,
                auto_highlight=True,
            ))

    # Match lines
    if show_matches and matches:
        line_data = [
            {
                "from_lat": m.trailer.lat,
                "from_lon": m.trailer.lon,
                "to_lat": m.truck.lat,
                "to_lon": m.truck.lon,
                "confidence": m.confidence,
                "trailer": m.trailer.asset_id,
                "truck": m.truck.asset_id,
                "id": f"Trailer {m.trailer.asset_id} ↔ Truck {m.truck.asset_id}",
                "coords": f"{_coords(m.trailer)} → {_coords(m.truck)}",
                "address": f"{m.distance_miles:.3f} mi | {m.confidence:.0%} confidence",
                "division": m.truck.division or m.trailer.division,
                "provider": "Auto-match line",
                "asset_type": "Match",
            }
            for m in matches
            if _has_coords(m.trailer) and _has_coords(m.truck)
        ]
        if line_data:
            layers.append(pdk.Layer(
                "LineLayer",
                data=line_data,
                get_source_position=["from_lon", "from_lat"],
                get_target_position=["to_lon", "to_lat"],
                get_color=[50, 205, 50, 180],  # limegreen
                get_width=3,
                pickable=True,
            ))

    # Compute map center
    all_lats = [a.lat for a in assets if _has_coords(a) and (selected_div is None or a.division == selected_div)]
    all_lons = [a.lon for a in assets if _has_coords(a) and (selected_div is None or a.division == selected_div)]

    if all_lats and all_lons:
        center_lat = sum(all_lats) / len(all_lats)
        center_lon = sum(all_lons) / len(all_lons)
    else:
        center_lat, center_lon = 39.8, -89.6  # US center fallback

    view_state = pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=5, pitch=0)

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip={
            "html": "<b>{asset_type}: {id}</b><br/>{coords}<br/>{address}<br/>{division}<br/>{provider}",
            "style": {"backgroundColor": "#1f2937", "color": "#e5e7eb"},
        },
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    )

    st.pydeck_chart(deck)
    _render_map_legend(division_colors)

    # --- All units table + coordinate copy controls ---
    unit_rows = _build_unit_rows(assets, matches, assignments, selected_div, unit_search)
    st.subheader("All Found Units")
    st.caption(
        "Every truck and trailer currently in Supabase is listed here, even if no auto-match was found. "
        "Use the copy buttons to paste coordinates into GPSTab/888/EROAD/Anytrek dashboards."
    )

    if unit_rows:
        _render_copy_grid(unit_rows)
    else:
        st.info("No units match the current filters/search.")

    # --- Match table ---
    if matches:
        st.subheader("Auto-Match Results")
        _render_match_review(matches, selected_div=selected_div)
    else:
        st.info("No trailer-truck matches found with current filters.")

    # --- Historical many-to-many usage table ---
    with st.expander("Historical Trailer Usage", expanded=False):
        st.caption(
            "Many-to-many historical co-location outside yards. This is for week/day analysis, "
            "so a truck can appear with multiple trailers if it had multiple route hits."
        )
        usage_rows = _build_historical_usage_rows(
            usage_history,
            selected_div=selected_div,
            max_distance_miles=max_dist,
            min_hits=usage_min_hits,
        )
        if usage_rows:
            st.dataframe(
                usage_rows,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Hits": st.column_config.NumberColumn("Hits", format="%d"),
                    "Min Distance (mi)": st.column_config.NumberColumn("Min Distance (mi)", format="%.3f"),
                    "Avg Distance (mi)": st.column_config.NumberColumn("Avg Distance (mi)", format="%.3f"),
                },
            )
        else:
            st.info("No historical truck/trailer co-location found for the selected date range and filters.")


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

        if auto_match:
            matched_unit = auto_match.truck.asset_id if asset.asset_type == "trailer" else auto_match.trailer.asset_id
            distance = auto_match.distance_miles
            confidence = f"{auto_match.confidence:.0%}"
            history_hits = auto_match.history_hits
            history_score = f"{auto_match.history_score:.0%}" if auto_match.history_score else ""
            match_status = "Auto + Board" if auto_match.on_board else "Auto"
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

    review_state = st.session_state.setdefault("gps_match_review_state", {})
    editor_rows = []
    for row in match_rows:
        existing = review_state.get(row["Match ID"], {})
        editor_rows.append({
            "Decision": existing.get("Decision", "Pending"),
            "Reviewer Note": existing.get("Reviewer Note", ""),
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

    for row in edited_rows:
        review_state[row["Match ID"]] = {
            "Decision": row.get("Decision", "Pending"),
            "Reviewer Note": row.get("Reviewer Note", ""),
        }

    artifact_rows = _build_review_artifact_rows(match_rows, review_state, selected_div=selected_div)
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
        out.append({
            "reviewed_at_utc": reviewed_at,
            "division_filter": selected_div or "All",
            "decision": state.get("Decision", "Pending"),
            "reviewer_note": state.get("Reviewer Note", ""),
            "match_id": row["Match ID"],
            "trailer": row["Trailer"],
            "truck": row["Truck"],
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
        })
    return out


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
        rows.append({
            "Truck": item.truck_id,
            "Trailer": item.trailer_id,
            "Hits": item.hits,
            "Days": ", ".join(item.days),
            "First Seen": item.first_seen.strftime("%Y-%m-%d %H:%M UTC") if item.first_seen else "",
            "Last Seen": item.last_seen.strftime("%Y-%m-%d %H:%M UTC") if item.last_seen else "",
            "Min Distance (mi)": item.min_distance_miles,
            "Avg Distance (mi)": item.avg_distance_miles,
            "Confidence": f"{item.confidence:.0%}",
        })
    return rows
