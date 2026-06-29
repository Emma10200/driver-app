"""
GPS Map & Trailer-Truck Matching page for Streamlit.
Shows all assets on a map, auto-matches trailers to nearby trucks,
and draws connection lines with confidence scores.
"""
from __future__ import annotations

import streamlit as st

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
            {"lat": t.lat, "lon": t.lon, "id": t.asset_id, "division": t.division, "provider": t.provider}
            for t in trucks
            if t.lat and t.lon and (selected_div is None or t.division == selected_div)
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
                "in_yard": in_yard(t.lat, t.lon) or "",
            }
            for t in trailers
            if t.lat and t.lon and (selected_div is None or t.division == selected_div)
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
            if m.trailer.lat and m.truck.lat
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
    all_lats = [a.lat for a in assets if a.lat and (selected_div is None or a.division == selected_div)]
    all_lons = [a.lon for a in assets if a.lon and (selected_div is None or a.division == selected_div)]

    if all_lats and all_lons:
        center_lat = sum(all_lats) / len(all_lats)
        center_lon = sum(all_lons) / len(all_lons)
    else:
        center_lat, center_lon = 39.8, -89.6  # US center fallback

    view_state = pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=5, pitch=0)

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        tooltip={"text": "{id}\n{division}\n{provider}"},
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    )

    st.pydeck_chart(deck)

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
