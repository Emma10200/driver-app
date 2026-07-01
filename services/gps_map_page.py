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
from typing import Any
from urllib.parse import quote_plus

import streamlit as st
import streamlit.components.v1 as components

from services.gps_data import (
    load_assignments,
    load_asset_history,
    load_asset_history_range,
    load_current_assets_with_last_known,
    load_hourly_evidence_timeline,
    load_latest_pairing_job,
    load_match_reviews,
    load_recent_match_reviews,
    load_usage_daily_summary,
    save_match_reviews,
)
from services.gps_matching import (
    Asset,
    MatchResult,
    TimelineSegment,
    compute_historical_usage,
    compute_matches,
    in_yard,
    YARD_BOXES,
)


def render_gps_map_page() -> None:
    st.header("GPS Fleet Map & Trailer Matching")

    # --- Load FAST data first (current positions only) ---
    with st.spinner("Loading positions..."):
        assets = load_current_assets_with_last_known(stale_after_days=30)
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

    with st.sidebar.expander("Advanced diagnostic tuning", expanded=False):
        st.caption("Used only by the diagnostic tabs below — not by the dense usage dashboard.")
        max_dist = st.slider("Match radius (miles)", 0.1, 5.0, 0.5, 0.1)
        max_stale = st.slider("Max stale (minutes)", 15, 240, 60, 15)
        history_hours = st.slider("History lookback (hours)", 12, 168, 48, 12)
        min_history_hits = st.slider("Min route hits", 1, 6, 2, 1)

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
        if unit_search and not _asset_matches_search(a, unit_search):
            return False
        return True

    visible_trucks = [t for t in trucks if _visible(t)]
    visible_trailers = [t for t in trailers if _visible(t)]
    visible_assets = [a for a in assets if _visible(a)]

    # --- Metrics (instant) ---
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Trucks", len(visible_trucks))
    col2.metric("Trailers", len(visible_trailers))
    col3.metric("Last-Known", sum(1 for a in visible_assets if _is_historical_last_known(a)))
    col4.metric("No Coordinates", sum(1 for a in visible_assets if not _has_coords(a)))
    col5.metric("Providers", len(active_providers))

    _render_map_lookup_panel(visible_assets, unit_search)

    # --- Map (renders FIRST with just current positions) ---
    _render_map(visible_trucks, visible_trailers, divisions, assignments)

    # --- Tabs for heavy content ---
    tab_usage, tab_timeline, tab_matches, tab_history, tab_review = st.tabs([
        "Trailer Usage Dashboard", "Unit Timeline", "Current Proximity Diagnostics", "Raw History Diagnostics", "Match Review",
    ])

    with tab_usage:
        _render_usage_dashboard(visible_assets, assignments, unit_search)

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
            "division": _division_display(t.division), "provider": _provider_display(t.provider),
            "address": t.address, "coords": _coords(t),
            "last_ping": _last_ping_text(t), "ping_age": _ping_age_text(t),
            "location_status": _location_status(t), "status_note": _location_note(t),
            "maps_url": _maps_url(t),
            "color": _asset_marker_color(t, _truck_color(t.division, division_colors)),
            "asset_type": "Truck",
        }
        for t in trucks if _has_coords(t)
    ]
    if truck_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=truck_data,
            get_position=["lon", "lat"], get_radius=700,
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
            "color": _asset_marker_color(t, [255, 140, 0, 230]), "asset_type": "Trailer",
        }
        for t in trailers if _has_coords(t)
    ]
    if trailer_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=trailer_data,
            get_position=["lon", "lat"], get_radius=650,
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
    if all_coords:
        center_lat = sum(d["lat"] for d in all_coords) / len(all_coords)
        center_lon = sum(d["lon"] for d in all_coords) / len(all_coords)
    else:
        center_lat, center_lon = 39.8, -89.6

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=5, pitch=0),
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


def _render_usage_dashboard(assets: list[Asset], assignments: dict[str, str], unit_search: str) -> None:
    """Main dense-evidence dashboard for trailer usage and truck usage."""
    st.subheader("Trailer Usage Dashboard")
    st.caption(
        "Dense timestamp-matched usage from `asset_pair_daily_summary` and `asset_pair_hourly_evidence`. "
        "This does not use legacy asset_pairings or live map snapshots."
    )

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
    start_dt = datetime.combine(usage_start, time.min, tzinfo=timezone.utc)
    end_dt = datetime.combine(usage_end, time.max, tzinfo=timezone.utc)
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

    with st.expander(f"Timeline runs for {view_mode} {selected_unit}", expanded=True):
        segments = load_hourly_evidence_timeline(selected_unit, primary_type, start_dt, end_dt)
        if segments:
            _render_timeline_visual(_consolidate_timeline(segments))
        else:
            st.info("No hourly evidence timeline rows found for this unit/range.")

    with st.expander("All Found Units / Coordinates", expanded=False):
        unit_rows = _build_unit_rows(assets, [], assignments, None, unit_search)
        if unit_rows:
            _render_copy_grid(unit_rows)
        else:
            st.info("No units match the current filters.")


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
            "Near Hours": round(acc["near_hours"], 1),
            "Yard Hours": round(acc["same_yard_hours"], 1),
            "Evidence Days": len(acc["evidence_days"]),
            "Avg Confidence": round(avg_conf, 1),
            "Min Distance": round(float(acc["min_distance"] or 0), 3) if acc["min_distance"] is not None else None,
            "First Seen": acc["first"].strftime("%m/%d %H:%M") if acc["first"] else "—",
            "Last Seen": acc["last"].strftime("%m/%d %H:%M") if acc["last"] else "—",
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


def _format_dt_text(value: object) -> str:
    parsed = _parse_ui_dt(value)
    return parsed.strftime("%m/%d %H:%M UTC") if parsed else ""


def _render_timeline_tab(assets: list[Asset], max_dist: float) -> None:
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

    if st.button("Load Timeline", type="primary", key="tl_compute"):
        start_dt = datetime.combine(tl_start, time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(tl_end, time.max, tzinfo=timezone.utc)
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt

        # Primary: hourly evidence table (detailed hour-by-hour view)
        with st.spinner("Loading hourly evidence..."):
            segments = load_hourly_evidence_timeline(unit_id, unit_type, start_dt, end_dt)

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
    st.subheader("Current Proximity Diagnostics")
    st.caption(
        "Diagnostic-only view using current positions plus accepted historical GPS points. "
        "For billing/usage decisions, use the dense Trailer Usage Dashboard and Unit Timeline."
    )

    if st.button("Compute Matches", type="primary", key="compute_matches_btn"):
        with st.spinner(f"Loading {history_hours}h of GPS history..."):
            history = load_asset_history(hours=history_hours)
        st.session_state["match_history"] = history
        st.session_state["match_computed"] = True

    if not st.session_state.get("match_computed"):
        st.info("Click **Compute Matches** only when you need a live proximity diagnostic. Dense usage evidence is already precomputed in the first two tabs.")
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
    st.subheader("Raw History Diagnostics")
    st.caption(
        "Diagnostic-only raw GPS co-location analysis. This is slower and not the official usage view. "
        "Use Trailer Usage Dashboard for precomputed dense timestamp-matched results."
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
        st.info("Go to **Current Proximity Diagnostics** and click Compute Matches first if you need manual review rows.")
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


def _render_map_lookup_panel(assets: list[Asset], unit_search: str) -> None:
    """Render a compact copy/Google Maps action panel for the currently visible map units."""
    lookup_assets = [a for a in assets if _has_coords(a)]
    if not lookup_assets:
        st.info("No visible units have coordinates with the current filters.")
        return

    lookup_assets = sorted(lookup_assets, key=lambda a: (a.asset_type, _unit_sort_key(a.asset_id)))
    by_key = {f"{a.asset_type}:{a.asset_id}": a for a in lookup_assets}
    labels = {
        key: f"{asset.asset_type.title()} {asset.asset_id} · {_location_status(asset)} · {_ping_age_text(asset)}"
        for key, asset in by_key.items()
    }

    default_index = 0
    if unit_search:
        search_norm = unit_search.strip().lower()
        for idx, asset in enumerate(lookup_assets):
            if asset.asset_id.lower() == search_norm:
                default_index = idx
                break

    selected_key = st.selectbox(
        "Coordinate actions",
        list(by_key.keys()),
        index=default_index,
        format_func=lambda key: labels.get(key, key),
        key="gps_map_coordinate_actions",
        help="Use this instead of hunting through the sidebar: select a visible unit, copy coordinates, or open Google Maps.",
    )
    selected = by_key[selected_key]
    _render_coordinate_action_card(selected)


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
      <div class="meta">Ping: <b>{last_ping}</b> ({ping_age}) · GPS: <b>{provider}</b> · Division: <b>{division}</b></div>
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
    return asset.last_ping.astimezone(timezone.utc).strftime("%m/%d/%Y %I:%M %p UTC")


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
