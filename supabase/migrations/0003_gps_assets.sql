-- GPS asset positions for the dispatch map / trailer-truck auto-matching.
-- Producer: dispatch-board Apps Script (pushes normalized JSON after each fetch cycle).
-- Consumer: prestige-driver-app Streamlit GPS page (read-only + on-read matching).
--
-- Run after the existing QBO migrations. Safe to re-run (idempotent).
-- Apps Script writes with the service-role key, so RLS policies below target service_role.

-- Shared trigger function to maintain updated_at on row changes.
create or replace function public.gps_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$ language plpgsql;

-- ---------------------------------------------------------------------------
-- assets_current: latest known position per asset. Upsert on (asset_type, asset_id).
-- Mirrors GPS_DATA (trucks) + TRAILER_DATA (trailers) normalized into one shape.
-- ---------------------------------------------------------------------------
create table if not exists public.assets_current (
    asset_type text not null check (asset_type in ('truck', 'trailer')),
    asset_id text not null,
    division text not null default '',
    lat double precision,
    lon double precision,
    address text not null default '',
    zip text not null default '',
    speed double precision,
    speed_unit text not null default '',          -- 'kph' (trucks) / provider unit for trailers
    heading_deg double precision,                 -- numeric bearing when available
    heading_cardinal text not null default '',    -- 'NE' etc. when only cardinal is known
    temp text not null default '',                -- reefer temp (trailers); blank for trucks
    provider text not null default '',
    last_ping timestamptz,                         -- parsed ping time (nullable if unparseable)
    last_ping_raw text not null default '',        -- original formatted string from the sheet
    source_updated_at timestamptz,                 -- when Apps Script observed this position
    ingested_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    raw jsonb,                                      -- full original row for debugging / future fields
    primary key (asset_type, asset_id)
);

create index if not exists idx_assets_current_division on public.assets_current(division);
create index if not exists idx_assets_current_type on public.assets_current(asset_type);
create index if not exists idx_assets_current_last_ping on public.assets_current(last_ping desc);

drop trigger if exists assets_current_touch_updated_at on public.assets_current;
create trigger assets_current_touch_updated_at
before update on public.assets_current
for each row execute function public.gps_touch_updated_at();

-- ---------------------------------------------------------------------------
-- assets_history: append-only, change-only trail (mirror of GPS_HISTORY).
-- Used for movement trails / playback and co-movement analysis later.
-- ---------------------------------------------------------------------------
create table if not exists public.assets_history (
    id bigserial primary key,
    asset_type text not null check (asset_type in ('truck', 'trailer')),
    asset_id text not null,
    division text not null default '',
    lat double precision,
    lon double precision,
    address text not null default '',
    zip text not null default '',
    speed double precision,
    heading_deg double precision,
    provider text not null default '',
    recorded_at timestamptz,                       -- ping time of this position
    source text not null default '',               -- GpsSource
    ingested_at timestamptz not null default timezone('utc', now()),
    raw jsonb
);

create index if not exists idx_assets_history_asset on public.assets_history(asset_type, asset_id, recorded_at desc);
create index if not exists idx_assets_history_recorded_at on public.assets_history(recorded_at desc);

-- ---------------------------------------------------------------------------
-- dispatch_assignments (optional): the board's ACTUAL truck<->trailer pairings,
-- so auto-match suggestions can be compared against reality. Upsert on truck_id.
-- ---------------------------------------------------------------------------
create table if not exists public.dispatch_assignments (
    truck_id text primary key,
    trailer_id text not null default '',
    division text not null default '',
    source_updated_at timestamptz,
    ingested_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_dispatch_assignments_trailer on public.dispatch_assignments(trailer_id);

drop trigger if exists dispatch_assignments_touch_updated_at on public.dispatch_assignments;
create trigger dispatch_assignments_touch_updated_at
before update on public.dispatch_assignments
for each row execute function public.gps_touch_updated_at();

-- ---------------------------------------------------------------------------
-- Row level security: service role manages everything (Apps Script + server-side
-- Streamlit both use the service-role key, consistent with the rest of the app).
-- ---------------------------------------------------------------------------
alter table public.assets_current enable row level security;
drop policy if exists "service role can manage assets_current" on public.assets_current;
create policy "service role can manage assets_current" on public.assets_current
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.assets_history enable row level security;
drop policy if exists "service role can manage assets_history" on public.assets_history;
create policy "service role can manage assets_history" on public.assets_history
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.dispatch_assignments enable row level security;
drop policy if exists "service role can manage dispatch_assignments" on public.dispatch_assignments;
create policy "service role can manage dispatch_assignments" on public.dispatch_assignments
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
