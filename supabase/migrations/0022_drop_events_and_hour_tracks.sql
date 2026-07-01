-- Migration 0022: Hourly GPS track cache + dropped trailer events.
-- Run in Supabase SQL Editor before using incremental/drop-trailer scripts.

-- Compact per-asset/per-hour GPS rollup. This is the bridge away from scanning
-- raw assets_history for every pairing/drop computation.
create table if not exists public.asset_hour_tracks (
    id bigint generated always as identity primary key,
    hour_start timestamptz not null,
    service_date date not null,
    asset_type text not null check (asset_type in ('truck', 'trailer')),
    asset_id text not null,
    division text not null default '',
    provider text not null default '',
    source text not null default '',
    ping_count integer not null default 0,
    first_ping timestamptz,
    last_ping timestamptz,
    centroid_lat double precision,
    centroid_lon double precision,
    min_lat double precision,
    max_lat double precision,
    min_lon double precision,
    max_lon double precision,
    miles_traveled double precision not null default 0,
    moving boolean not null default false,
    avg_speed double precision,
    max_speed double precision,
    heading_deg double precision,
    yard_name text not null default '',
    address text not null default '',
    sample_points jsonb not null default '[]'::jsonb,
    source_row_count integer not null default 0,
    last_source_recorded_at timestamptz,
    job_id text not null default '',
    computed_at timestamptz not null default timezone('utc', now()),
    unique(hour_start, asset_type, asset_id)
);

comment on table public.asset_hour_tracks is
    'Per-asset/per-hour GPS rollup used to avoid repeatedly scanning raw assets_history for matching and drop-event jobs.';

create index if not exists idx_asset_hour_tracks_hour on public.asset_hour_tracks(hour_start desc);
create index if not exists idx_asset_hour_tracks_asset on public.asset_hour_tracks(asset_type, asset_id, hour_start desc);
create index if not exists idx_asset_hour_tracks_service_date on public.asset_hour_tracks(service_date desc, asset_type);
create index if not exists idx_asset_hour_tracks_moving_trailer on public.asset_hour_tracks(asset_id, hour_start desc)
    where asset_type = 'trailer' and moving = true;
create index if not exists idx_asset_hour_tracks_yard on public.asset_hour_tracks(yard_name, hour_start desc)
    where yard_name <> '';

-- Derived drop/custody events. Kept separate from billable usage so operational
-- drops do not accidentally become invoice hours.
create table if not exists public.trailer_drop_events (
    event_id text primary key,
    trailer_id text not null,
    status text not null default 'active_drop'
        check (status in ('active_drop', 'picked_up', 'returned_to_yard', 'yard_drop', 'unknown_dropper')),
    drop_started_at timestamptz not null,
    drop_ended_at timestamptz,
    idle_hours double precision not null default 0,
    lat double precision,
    lon double precision,
    address text not null default '',
    yard_name text not null default '',
    is_excluded_yard boolean not null default false,
    dropped_by_truck_id text not null default '',
    dropped_by_confidence double precision not null default 0,
    picked_up_by_truck_id text not null default '',
    pickup_confidence double precision not null default 0,
    last_pair_hour timestamptz,
    first_stationary_ping timestamptz,
    last_stationary_ping timestamptz,
    stationary_radius_miles double precision not null default 0,
    ping_count integer not null default 0,
    evidence jsonb not null default '{}'::jsonb,
    source_job_id text not null default '',
    computed_at timestamptz not null default timezone('utc', now())
);

comment on table public.trailer_drop_events is
    'Operational trailer custody/drop events. A trailer is considered dropped after 12h idle outside excluded yards; kept separate from billable usage.';

create index if not exists idx_trailer_drop_events_status on public.trailer_drop_events(status, drop_started_at desc);
create index if not exists idx_trailer_drop_events_trailer on public.trailer_drop_events(trailer_id, drop_started_at desc);
create index if not exists idx_trailer_drop_events_started on public.trailer_drop_events(drop_started_at desc);
create index if not exists idx_trailer_drop_events_dropper on public.trailer_drop_events(dropped_by_truck_id, drop_started_at desc)
    where dropped_by_truck_id <> '';

-- Incremental job state. `gps_pairing_job_runs` records runs; this table stores
-- resumable watermarks by job type.
create table if not exists public.gps_compute_state (
    job_type text primary key,
    last_success_at timestamptz,
    last_range_start timestamptz,
    last_range_end timestamptz,
    last_job_id text not null default '',
    metadata jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default timezone('utc', now())
);

alter table public.asset_hour_tracks enable row level security;
alter table public.trailer_drop_events enable row level security;
alter table public.gps_compute_state enable row level security;

drop policy if exists "service role can manage asset_hour_tracks" on public.asset_hour_tracks;
create policy "service role can manage asset_hour_tracks" on public.asset_hour_tracks
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage trailer_drop_events" on public.trailer_drop_events;
create policy "service role can manage trailer_drop_events" on public.trailer_drop_events
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage gps_compute_state" on public.gps_compute_state;
create policy "service role can manage gps_compute_state" on public.gps_compute_state
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

notify pgrst, 'reload schema';
