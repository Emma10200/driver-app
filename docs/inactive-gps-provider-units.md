# Inactive/deactivated GPS unit archive

Goal: keep GPS units discoverable even when they are no longer active on the dispatch board or cache sheets.

## Why this exists

The dispatch board is an operational roster, not a complete historical GPS roster. A truck or trailer can be deactivated/removed from the board while its GPS history still matters for:

- unmatched moving trailer alerts,
- trailer billing review,
- truck↔trailer evidence rebuilds,
- explaining why a trailer appears to have no active-board match.

## Tables

### `assets_history`

This remains the source of truth for historical GPS pings. The matcher already reads this table directly and does not require a unit to be present on the dispatch board.

If an inactive truck/trailer has pings in `assets_history`, `compute_pair_hourly_evidence.py` can match it.

### `gps_provider_units`

Added by `supabase/migrations/0023_gps_provider_units.sql`.

This is the provider-unit archive. It stores active, inactive, and history-inferred units from GPS providers:

- provider/account,
- provider-native unit/device ID,
- fleet-facing unit ID when known,
- status / active flag when the provider exposes it,
- first/last seen timestamps,
- last history timestamp,
- latest known position from history when available,
- raw provider roster row for debugging.

This table is intentionally independent from `assets_current` and `dispatch_board_rows`.

## Sync command

After running migration `0023`, run:

```powershell
python scripts/sync_gps_provider_units.py --provider all --history-lookback-days 180
```

Optional Anytrek direct transaction discovery:

```powershell
python scripts/sync_gps_provider_units.py --provider anytrek --anytrek-discovery-days 180 --history-lookback-days 180
```

For a safe preview:

```powershell
python scripts/sync_gps_provider_units.py --dry-run
```

## Provider notes

- **GPSTab**: roster endpoint can populate truck units independent of the board. Historical pings come from `backfill_anytrek_history.py --provider gpstab`.
- **888 ELD / Track Mile**: roster comes from HOSQL `vehicles`; status history quality depends on provider endpoint behavior.
- **EROAD**: roster comes from `/vehicles`; current-state snapshots can still write to `assets_history`.
- **Anytrek**: no separate roster endpoint is used here. Units can be inferred from `assets_history`, and optionally discovered from transactions over a lookback window.
- **Motive**: currently handled in Apps Script OAuth/current-location flow; Python roster sync is not implemented yet. Motive units can still be inferred from `assets_history` if published/backfilled there.

## Billing/mismatch behavior

The final unmatched-trailer check should be:

1. use computed GPS evidence from `asset_pair_hourly_evidence`,
2. if a trailer looks unmatched, check whether evidence already paired it with any truck, including trucks no longer on the board,
3. use `gps_provider_units` to label that truck/trailer as provider-known inactive/history-inferred instead of treating it as unknown,
4. only show the trailer as truly unmatched if there is no GPS evidence and no manual/board-confirmed assignment.

Current UI already performs step 2 in `_render_unmatched_trailers_alert()` by using GPS evidence as truth even if the truck is deactivated from the dispatch board. Next UI improvement is to surface `gps_provider_units.status` / `is_active` next to those evidence-only trucks.

## Six-month strategy

Use `180` days as the default archive lookback. That keeps inactive units relevant to recent billing without pulling every ancient provider record forever.

For actual pings, run provider history backfills into `assets_history` for the same window when needed:

```powershell
python scripts/backfill_anytrek_history.py --days 180 --provider all --blind
```

Then refresh derived GPS tables:

1. `build_asset_hour_tracks.py`
2. `compute_pair_hourly_evidence.py`
3. `compute_trailer_drop_events.py`
