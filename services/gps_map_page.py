"""
GPS Map & Trailer-Truck Matching page for Streamlit.
Shows all assets on a map, auto-matches trailers to nearby trucks,
and draws connection lines with confidence scores.
"""
from __future__ import annotations

from html import escape
from urllib.parse import quote_plus

import streamlit as st
import streamlit.components.v1 as components

from services.gps_data import load_current_assets, load_assignments
from services.gps_matching import (
    Asset,
    MatchResult,
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

    show_trucks = st.sidebar.checkbox("Show trucks", True)
    show_trailers = st.sidebar.checkbox("Show trailers", True)
    show_matches = st.sidebar.checkbox("Show match lines", True)
    unit_search = st.sidebar.text_input("Search units", placeholder="Unit, provider, address, VIN...")

    # --- Compute matches ---
    matches = compute_matches(
        trucks,
        trailers,
        assignments,
        max_distance_miles=max_dist,
        max_stale_minutes=max_stale,
        division_filter=selected_div,
    )

    # --- Metrics ---
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Trucks", len(trucks))
    col2.metric("Trailers", len(trailers))
    col3.metric("Auto-Matches", len(matches))
    col4.metric("Board Agrees", sum(1 for m in matches if m.on_board))

    # --- Map (pydeck) ---
    try:
        import pydeck as pdk
    except ImportError:
        st.error("Install pydeck: `pip install pydeck`")
        return

    layers = []

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
            }
            for t in trucks
            if _has_coords(t) and (selected_div is None or t.division == selected_div)
        ]
        if truck_data:
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=truck_data,
                get_position=["lon", "lat"],
                get_radius=120,
                get_fill_color=[30, 144, 255, 200],  # dodgerblue
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
            }
            for t in trailers
            if _has_coords(t) and (selected_div is None or t.division == selected_div)
        ]
        if trailer_data:
            layers.append(pdk.Layer(
                "ScatterplotLayer",
                data=trailer_data,
                get_position=["lon", "lat"],
                get_radius=100,
                get_fill_color=[255, 140, 0, 200],  # orange
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
        tooltip={"text": "{id}\n{coords}\n{address}\n{division}\n{provider}"},
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    )

    st.pydeck_chart(deck)

    # --- All units table + coordinate copy controls ---
    unit_rows = _build_unit_rows(assets, matches, assignments, selected_div, unit_search)
    st.subheader("All Found Units")
    st.caption(
        "Every truck and trailer currently in Supabase is listed here, even if no auto-match was found. "
        "Use the copy buttons to paste coordinates into GPSTab/888/EROAD/Anytrek dashboards."
    )

    if unit_rows:
        _render_copy_grid(unit_rows)
        st.dataframe(
            unit_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Map": st.column_config.LinkColumn("Map", display_text="Open"),
                "Lat": st.column_config.NumberColumn("Lat", format="%.6f"),
                "Lon": st.column_config.NumberColumn("Lon", format="%.6f"),
                "Distance (mi)": st.column_config.NumberColumn("Distance (mi)", format="%.3f"),
            },
        )
    else:
        st.info("No units match the current filters/search.")

    # --- Match table ---
    if matches:
        st.subheader("Auto-Match Results")
        table_data = [
            {
                "Trailer": m.trailer.asset_id,
                "Truck": m.truck.asset_id,
                "Distance (mi)": m.distance_miles,
                "Confidence": f"{m.confidence:.0%}",
                "On Board": "✓" if m.on_board else "",
                "Notes": ", ".join(m.reasons),
            }
            for m in matches
        ]
        st.dataframe(table_data, use_container_width=True)
    else:
        st.info("No trailer-truck matches found with current filters.")


def _has_coords(asset: Asset) -> bool:
    if asset.lat is None or asset.lon is None:
        return False
    try:
        lat = float(asset.lat)
        lon = float(asset.lon)
    except (TypeError, ValueError):
        return False
    return not (lat == 0 and lon == 0)


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
    for asset in sorted(assets, key=lambda a: (a.asset_type, _unit_sort_key(a.asset_id))):
        if selected_div and asset.division != selected_div:
            continue

        auto_match = match_by_trailer.get(asset.asset_id) if asset.asset_type == "trailer" else match_by_truck.get(asset.asset_id)
        board_unit = board_by_trailer.get(asset.asset_id, "") if asset.asset_type == "trailer" else assignments.get(asset.asset_id, "")
        matched_unit = ""
        match_status = "Unmatched"
        distance = None
        confidence = ""

        if auto_match:
            matched_unit = auto_match.truck.asset_id if asset.asset_type == "trailer" else auto_match.trailer.asset_id
            distance = auto_match.distance_miles
            confidence = f"{auto_match.confidence:.0%}"
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
            "Last Ping": _last_ping_text(asset),
            "Speed": asset.speed,
            "Heading": asset.heading_deg,
            "Yard": in_yard(float(asset.lat), float(asset.lon)) if _has_coords(asset) else "",
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
            <th>Status</th><th>Provider</th><th>Identifying Info</th><th>Address</th>
          </tr>
        </thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </div>
    """
    components.html(html, height=470, scrolling=False)
