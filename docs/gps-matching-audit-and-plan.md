# GPS Matching — Audit, Cleanup & Algorithm Plan

> Perpetual handoff doc. Written 2026-07-01 during a high-context audit so the next
> (lower-context) session can implement the algorithm changes without re-deriving
> everything. Read this first.

---

## 1. What this system does (one paragraph)

Apps Script (dispatch-board) is the only thing that calls ELD/GPS provider APIs.
It pushes normalized GPS into Supabase. Everything else — matching trailers to
trucks, billing evidence, dropped-trailer detection, the Streamlit UI — is Python
that **reads precomputed evidence tables**. No matching happens live in the UI
anymore; it is all batch-computed by scripts and stored in Supabase.

---

## 2. Data flow (authoritative)

```
ELD providers (GPSTab, Anytrek, 888/TrackMile, EROAD, Motive)
      │  (Apps Script: dispatch-board/GpsFetcher.js + SupabasePublisher.js)
      ▼
Supabase raw GPS
  ├─ assets_current      (live roster + latest position; UI map)
  └─ assets_history      (dense time-series; the source of truth for matching)
      │
      │  scripts/build_asset_hour_tracks.py   (raw pings → 1 row per asset/hour)
      ▼
  asset_hour_tracks      (compact per-asset/per-hour rollup)
      │
      │  scripts/compute_pair_hourly_evidence.py   (THE matcher)
      ▼
  asset_pair_hourly_evidence   (per truck×trailer×hour: paired/near/same_yard)
      ├─ asset_pair_daily_summary     (rolled up per day)
      ├─ asset_pair_weekly_review     (rolled up per week)
      └─ trailer_activity_summary     (per trailer/day moving vs paired hours)
      │
      │  scripts/compute_trailer_drop_events.py
      ▼
  trailer_drop_events    (operational custody drops, 12h idle, separate from billing)

  gps_compute_state      (incremental watermarks per job_type)
  manual_pair_assignments (paper-log driver overrides; suppress unmatched alerts)
```

Streamlit (`services/gps_map_page.py`) reads only: `assets_current` (map),
`asset_pair_hourly_evidence`, daily/weekly summaries, `trailer_activity_summary`,
`asset_hour_tracks`, `trailer_drop_events`, `manual_pair_assignments`, and raw
`assets_history` trails for the route viewer.

---

## 3. File inventory — KEEP / LEGACY / DELETED

### KEEP (active, current architecture)

| File | Role |
|---|---|
| `services/gps_map_page.py` | Streamlit GPS UI (map, fleet, unit history, timeline, dropped trailers, usage/billing, unmatched alerts + route overlay). |
| `services/gps_data.py` | Supabase read/write access for all evidence tables. |
| `services/gps_matching.py` | Now trimmed to shared primitives only: `Asset`, `TimelineSegment`, `haversine_miles`, `in_yard`, yard geofence constants. |
| `scripts/compute_pair_hourly_evidence.py` | **THE matcher.** Dense timestamp matching → `asset_pair_hourly_evidence` + summaries + activity. |
| `scripts/build_asset_hour_tracks.py` | Raw `assets_history` → compact `asset_hour_tracks`. Foundation for incremental jobs. |
| `scripts/compute_trailer_drop_events.py` | Operational dropped-trailer custody events. |
| `scripts/finalize_hourly_evidence_job.py` | Recovers a job from already-written hourly rows if final summary write failed. |
| `scripts/backfill_anytrek_history.py` | Provider backfill + shotgun fallback into `assets_history`. |
| `scripts/report_gps_last_known_locations.py` | Standalone ops report (last-known positions). Harmless. |
| `scripts/pair_hourly_history.py` | Standalone forensic CSV tool for ONE truck↔trailer pair. Self-contained (no `services` imports). Kept as a debugging aid. |

### DELETED in this pass (dead or dangerous)

| File | Why removed |
|---|---|
| `scripts/cleanup_legacy_gps.py` | **Dangerous landmine.** It deletes `assets_history` rows where `source in ('truck_publish','trailer_publish','gps_update')`. But `truck_publish` is now an accepted matching source (888 ELD live GPS lands there). Running it would delete active 888 matching data. |
| `scripts/compute_pairings.py` | Legacy. Writes the `asset_pairings` table which the UI no longer reads (Timeline tab explicitly says "no legacy asset_pairings fallback"). Depended on the removed live-matching functions. |

### Dead code removed from `services/gps_matching.py`

All of these had **zero callers** except each other and the deleted `compute_pairings.py`:
- `compute_matches()` — old live 1:1 matcher (UI no longer live-matches).
- `compute_historical_usage()` — old many-to-many usage scan.
- `compute_unit_timeline()` — old timeline builder (UI uses `load_hourly_evidence_timeline`).
- `MatchResult`, `HistoricalUsageResult` dataclasses.
- Private helpers: `_build_time_index`, `_pair_co_location_buckets`, `_detect_co_travel_segments`,
  `_bucket_to_datetime`, `_history_agreement`, `_heading_agreement`, `_speed_agreement`,
  `_freshness_score`, `_valid_coords`, and the `_TimeIndex`/`_AssetSummary` type aliases.

### Dead code removed from `services/gps_data.py`

Zero callers anywhere (services, scripts, tests, app.py):
- `load_asset_history()`, `load_asset_history_range()` — superseded; matching reads `asset_hour_tracks`.
- `load_unit_timeline_history()`, `load_all_unit_ids()` — unused loaders.
- `load_asset_pairing_timeline()` — reads legacy `asset_pairings`.
- `load_match_reviews()`, `save_match_reviews()`, `load_recent_match_reviews()` — the old
  auto-match Confirm/Reject review UI is gone; these read/write legacy `gps_match_reviews`.

### `services/gps_map_page.py` simplification

- `_build_unit_rows()` was always called with `matches=[]`, so its entire auto-match branch
  (MatchResult, confidence, history hits, trip segments columns) was dead. Simplified to drop
  the `matches` param and the dead branch; removed the `MatchResult` import.

### LEGACY Supabase objects (NOT dropped — dropping tables is destructive)

Leave the migration files in place; just know these tables are no longer used:
- `0016_gps_match_reviews.sql` → `gps_match_reviews` (unused)
- `0017_asset_pairings.sql` → `asset_pairings` (unused)

If you want to reclaim space later, drop them manually in the SQL editor after confirming
no other consumer. Do **not** auto-drop.

### CORE Supabase migrations (keep)

`0003_gps_assets` (assets_current/assets_history/dispatch_assignments),
`0018_asset_pair_hourly_evidence`, `0020_hourly_evidence_miles_traveled`,
`0021_trailer_activity_and_manual_assignments`, `0022_drop_events_and_hour_tracks`.
(`0019_dispatch_board_rows` belongs to the separate dispatch-board mirror feature.)

---

## 4. The matcher today — how a truck×trailer×hour becomes `paired`

File: `scripts/compute_pair_hourly_evidence.py`, function
`compute_hourly_evidence(...)` (the per-hour candidate loop).

Current default tunables (argparse):
| Flag | Default | Meaning |
|---|---|---|
| `--max-distance` | `0.5` mi | Distance to classify a timestamp match as "close" (paired candidate). |
| `--near-distance` | `1.0` mi | Distance to retain as "near" (review, not paired). |
| `--max-ping-gap` | `5` min | Max time gap when interpolating the denser track to the sparser unit's ping. |
| `--min-matches` | `2` | Min close timestamp matches in the hour to allow "paired". |
| `--min-match-ratio` | `0.5` | Fallback ratio of close/among-matched to allow "paired" when sparse. |
| `--billable-min-confidence` | `0.55` | Min hourly confidence for billable candidate. |

Per hour, per trailer, per truck it:
1. Skips if both are inside a yard suppression zone (parked).
2. Interpolates the denser track to the sparser unit's timestamps → `matches`.
3. Drops matches flagged `movement_mismatch` (one moving, one stationary).
4. `close_matches` = matches within `max_distance` (0.5 mi).
5. Computes `paired_by_distance = close_matches AND (close_count >= min_matches OR evidence_ratio >= min_match_ratio)`.
6. **Movement-evidence gate:** `movement_evidence = miles_traveled_together >= 0.5 OR movement_compatible_count >= 1`.
   If `paired_by_distance` but NOT `movement_evidence` → demoted to `near`.
7. Yard gates: if same yard → `same_yard`; if either side merely touches/near a yard → `near`
   (unless it passes the strict near-yard gate: California needs ~30 close matches, 0.80 evidence ratio, movement compat).
8. Else if `paired_by_distance` → `paired`, else `near`.
9. Exclusive 1:1 per hour: greedy by score; the loser of a truck/trailer conflict is demoted to `near`.

Confidence weighting (`_dense_confidence`): 0.30 distance, 0.20 gap, 0.20 consistency,
0.15 density, 0.15 movement. Non-paired hours are capped at ≤0.49.

---

## 5. Mismatch investigation findings (2026-07-01)

Inspected the 7 board-assigned trailers that show up in "Unmatched Moving Trailers"
for June 2026. **Only truck `802` truly lacks GPS (paper logs).** The rest have GPS
on BOTH sides — the matcher just keeps most hours at `near` instead of `paired`.

Per-pair (unmatched-moving-hours; board pair evidence rows):

| Trailer → Truck | Unmatched hrs | Board evidence | Notes |
|---|---:|---|---|
| `700481 → 333` | 75 | 81 near / 10 paired | 66 near rows are ≤0.5 mi; near avg dist 0.20 mi. |
| `4907-15 → 39` | 69 | 121 near / 11 paired | 81 near rows ≤0.5 mi; near avg dist 0.34 mi. |
| `876255 → 235` | 56 | 60 near / 11 paired | 43 near ≤0.5 mi; near avg dist 0.30 mi. |
| `4400-14 → 129` | 53 | 62 near / 4 paired | 39 near ≤0.5 mi; near avg dist 0.37 mi. |
| `575010 → 277` | 45 | 53 near / 32 paired | 38 near ≤0.5 mi. |
| `766729 → 975` | 29 | 33 near / 0 paired | 25 near ≤0.5 mi. |
| `700482 → 802` | 36 | (none) | **Truck GPS truly missing — paper logs. Expected.** |

**Root cause:** the movement-evidence gate (step 6). A huge share of `near` rows are
within 0.5 mi of the board truck but are demoted because that specific hour looked
stationary/sparse (`miles_traveled_together < 0.5` and no movement-compatible ping),
even though the trailer is obviously being pulled by that truck across the day.
Distance is NOT the problem for these pairs; the per-hour movement requirement is.

Diagnostic characteristics of the demoted `near` rows:
- Predominantly `miles_traveled < 0.5` in that hour (`stationary_miles<.5` dominates).
- Very low `best_distance_miles` (often < 0.35 mi).
- Sparse hours (1–3 pings per side) are common → single-hour movement proof is fragile.
- Almost none touch a yard (`yard_touch ~ 0`), so yard suppression is NOT the cause here.

---

## 6. Algorithm change plan (implement next session)

Goal: promote genuine on-road pairs without going loose near yards or in dense traffic.
Two complementary ideas, both grounded in the findings above and the user's intent.

### 6A. Distance tolerance that scales with distance-from-yard (context-aware radius)

Rationale (user): the farther from a yard/terminal, the fewer plausible trucks exist,
so proximity tolerance can widen. Near yards stay strict (many parked units).

Proposed `paired`-eligible radius as a function of min(truck,trailer) distance to the
nearest yard center:

| Distance from nearest yard | Paired radius | Strictness |
|---|---:|---|
| in-yard / ≤ 5 mi | 0.5 mi | strict (current) + dense/movement evidence required |
| 5–15 mi | 1.0 mi | moderate |
| 15–50 mi | 2.0 mi | moderate, prefer repeated hours |
| 50–150 mi | 5.0 mi | looser, require repeated/co-directional |
| > 150 mi (isolated) | up to 10 mi | loosest, still require repeated + co-directional |

Do NOT jump straight to 20+ mi as "paired". Keep 20+ mi as `near` unless a run rule (6B) confirms.

Implementation sketch:
- Add `def paired_radius_for(lat, lon) -> float` using `haversine_miles` to nearest
  `YARD_GEOFENCES` center; return the tiered radius above.
- In the candidate loop, replace the single `max_distance_miles` compare with
  `effective_radius = min(paired_radius_for(truck), paired_radius_for(trailer))`
  (use the more conservative/closer-to-yard side).
- Keep `near_distance` a bit above the effective radius for review retention.

### 6B. Repeated-run promotion (the strongest fix for these 7 pairs)

Rationale: a single hour that looks stationary shouldn't block pairing when the truck
and trailer are near each other across many consecutive hours. Timestamp co-location
across a run is near-certain evidence even if any one hour is sparse.

Rule (post-process after per-hour scoring, before/within exclusivity):
- Group candidate hours by `(truck_id, trailer_id)` ordered by `hour_start`.
- Find runs of consecutive hours (allow 1-hour gaps) where they are within the
  effective radius (6A) and NOT same-yard.
- If a run has `>= N` hours (start with `N = 3`) OR spans a real move
  (cumulative `miles_traveled_together >= ~5 mi across the run`), promote the run's
  `near` hours to `paired` (or a new `paired_run` status if you want to keep audit
  separation), and set a modest confidence (e.g. 0.55–0.65) so they can be billable.
- Still enforce 1:1 exclusivity per hour after promotion (a truck can't be promoted
  with two trailers in the same hour; highest score wins).

### 6C. Timestamp-alignment principle (sanity guard)

User's instinct: at the same timestamp both units should be within ~1–2 miles of each
other regardless of ping cadence. Use this as a guard, not a looser gate:
- When promoting via 6A/6B, require that the *nearest-in-time* matched pings actually
  align in time (`best_ping_gap_minutes` small, already computed). This prevents
  "same hour but 40 minutes apart in different places" false promotions.

### Keep the guardrails

- Yard suppression + strict near-yard gate stay as-is (California parking-lot noise).
- `same_yard` stays review-only.
- Exclusivity stays.
- Movement-mismatch drop (one moving, one clearly stationary at the same timestamp) stays.

---

## 7. Implementation checklist (next session)

1. In `scripts/compute_pair_hourly_evidence.py`:
   - Add `paired_radius_for(lat, lon)` (tiered, §6A) near the geometry helpers.
   - In the per-hour candidate loop, compute `effective_radius` and use it for
     `close_matches` / `paired_by_distance` instead of the flat `max_distance_miles`.
   - Add a post-scoring `promote_repeated_runs(records, min_run_hours=3, min_run_miles=5.0)`
     pass (§6B) before the exclusivity pass; guard with `best_ping_gap` (§6C).
   - Add CLI flags: `--yard-strict-radius 0.5 --far-radius-max 10 --run-min-hours 3
     --run-min-miles 5` so it's tunable without code edits.
2. Dry-run on a known pair first:
   `py -3 scripts/compute_pair_hourly_evidence.py --start 2026-06-01 --end 2026-07-01 --dry-run`
   and re-run the §5 diagnostic to confirm the 6 GPS-having pairs gain `paired` hours
   while California yard rows stay ~0 billable.
3. Full incremental rebuild once satisfied:
   `py -3 scripts/build_asset_hour_tracks.py --incremental --overlap-hours 72`
   `py -3 scripts/compute_pair_hourly_evidence.py --incremental --overlap-hours 72 --chunk-days 1`
   `py -3 scripts/compute_trailer_drop_events.py --incremental --overlap-hours 96`
4. Re-check the Streamlit "Unmatched Moving Trailers" list shrinks to essentially just
   `802` (paper logs) and any genuinely GPS-less units.

### Validation commands
```
py -3 -m py_compile services\gps_map_page.py services\gps_data.py services\gps_matching.py scripts\compute_pair_hourly_evidence.py
py -3 -c "import services.gps_map_page, services.gps_data, services.gps_matching"
```

---

## 8. Reference: current tunables & thresholds (so they're not lost)

- Yard geofences: California `34.09686,-117.47642`; Illinois `41.896873,-87.86982`; radius `0.25` mi.
- `MOVING_SPEED_THRESHOLD = 5.0` (mph-ish, provider unit).
- Unmatched-moving-trailer alert fires when `unmatched_moving_hours >= 3` AND `miles_moved >= 10`.
- Evidence "primary truth": a trailer paired in GPS evidence is excluded from unmatched
  alerts even if its board truck was deactivated.
- Sources accepted by the matcher (`MATCHING_SOURCES`): `gpstab_backfill`, `anytrek_backfill`,
  `track888_backfill`, `eroad_backfill`, `truck_publish` (888 live), and blank-source legacy imports.
