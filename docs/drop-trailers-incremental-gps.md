# Dropped Trailers + Incremental GPS Jobs

This adds three compact derived layers so GPS operations do not need to keep recomputing the full raw `assets_history` window.

## Supabase migration

Run this in Supabase SQL Editor first:

- `supabase/migrations/0022_drop_events_and_hour_tracks.sql`

It creates:

- `asset_hour_tracks` — one row per asset/hour, used as a compact GPS cache.
- `trailer_drop_events` — operational dropped-trailer custody events.
- `gps_compute_state` — incremental job watermarks.

## Drop trailer rule

Default threshold is now **12 hours**.

A trailer drop is detected when a trailer is stationary in one location for at least 12 hours outside excluded yards.

Default excluded yard:

- `California Yard`

The drop logic is intentionally separate from billable hours.

## Recommended run order after the current rebuild finishes

### First-time setup / backfill

1. Build hourly GPS tracks for the desired history window.
2. Compute drop events from those hourly tracks.
3. Use incremental mode going forward.

Example local commands:

```powershell
py -3 scripts\build_asset_hour_tracks.py --days 60 --chunk-days 1
py -3 scripts\compute_trailer_drop_events.py --days 60
```

### Normal incremental maintenance

```powershell
py -3 scripts\build_asset_hour_tracks.py --incremental --overlap-hours 72
py -3 scripts\compute_pair_hourly_evidence.py --incremental --overlap-hours 72 --chunk-days 1
py -3 scripts\compute_trailer_drop_events.py --incremental --overlap-hours 96
```

## Notes

- `compute_pair_hourly_evidence.py --incremental` still uses the existing raw-history matcher, but only over a recent overlap window.
- `asset_hour_tracks` is the foundation for a future faster matcher that reads compact hourly rows instead of raw pings.
- `compute_trailer_drop_events.py` already reads from `asset_hour_tracks`, so drop detection should be much lighter than full pairing recomputes.
- Keep full 60-day rebuilds for provider fixes/backfill corrections only.
