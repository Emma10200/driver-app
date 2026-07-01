# Dispatch Board Web UI handoff

Goal: keep dispatchers editing the Google Sheet as usual, but render a faster dispatcher-friendly web UI in `prestige-driver-app` using Supabase as the read model.

## Recommended approach

Use Apps Script as the bridge, not direct Google Sheets reads from Streamlit.

1. Add a Supabase table that mirrors the visible dispatch-board rows.
2. Add a standalone Apps Script publisher file in the existing `dispatch-board` clasp project.
3. Publish snapshots on a time-driven trigger every 5-10 minutes, plus optionally from `onEdit` for faster updates.
4. Render the board in Streamlit from Supabase and join it to:
   - `assets_current` for current truck/trailer GPS
   - `dispatch_assignments` for current truck→trailer pairing
   - dense evidence tables for anomaly/usage context

This keeps the Sheet as the editable source of truth and avoids putting Google credentials into the web app.

## What is needed from the user

Minimum:
- The Google Sheet URL or spreadsheet ID.
- Confirmation of the dispatch sheet tab name, currently expected from `CONFIG.DISPATCH.sheetName`.
- Confirmation that the existing Apps Script project is bound to that Sheet or has access to it.

Helpful but not strictly required because scripts can infer headers:
- Which board columns should be shown first in the web UI.
- Which statuses are considered active/open/completed/cancelled.
- Whether rows should be grouped by dispatcher, division, status, pickup date, or truck.

## Suggested Supabase table

`dispatch_board_rows` should store the normalized row plus raw row JSON:

- `row_key text primary key` — stable key, ideally truck id or sheet row id plus board date
- `sheet_row integer`
- `truck_id text`
- `trailer_id text`
- `driver_name text`
- `dispatcher text`
- `division text`
- `status text`
- `origin text`
- `destination text`
- `pickup_at timestamptz`
- `delivery_at timestamptz`
- `updated_timestamp timestamptz`
- `source_updated_at timestamptz`
- `raw jsonb not null default '{}'::jsonb`

A separate history/audit table can come later if needed.

## Trigger strategy

Best first version:
- Time-driven trigger every 5 minutes or 10 minutes.
- Manual menu item: `Publish Dispatch Board to Supabase`.

Optional second version:
- Installable `onEdit` trigger that publishes only the edited row or debounces a full snapshot.

Avoid direct live reads from Google Sheets in Streamlit. It works, but it is slower, credential-heavy, and more fragile than the existing Apps Script → Supabase push pattern.

## UI phases

1. Read-only board mirror with filters/search and GPS freshness badges.
2. Add map/route context: truck/trailer last ping, yards, anomaly flag, copy/map links.
3. Add action links back to the Sheet row for editing.
4. Later: controlled edits from web UI by writing either to Supabase queue or calling an Apps Script web app endpoint.

## Estimated difficulty

Read-only mirror: low/medium.
- One migration
- One Apps Script publisher
- One Streamlit page/tab

Interactive web editing: medium/high.
- Need auth/permissions
- Need conflict handling with Sheet edits
- Need a safe write-back path
