-- Migration 0023: GPS provider-unit archive.
-- Purpose: retain active + inactive/deactivated GPS units independently of the
-- dispatch board, so historical matching/billing can find units that no longer
-- appear in current board/cache sheets.

create table if not exists public.gps_provider_units (
    provider text not null,
    provider_account text not null default '',
    provider_unit_id text not null,
    asset_type text not null default 'unknown'
        check (asset_type in ('truck', 'trailer', 'unknown')),
    asset_id text not null default '',
    canonical_asset_id text not null default '',
    display_name text not null default '',
    status text not null default '',
    is_active boolean,
    first_seen_at timestamptz not null default timezone('utc', now()),
    last_seen_at timestamptz not null default timezone('utc', now()),
    last_history_at timestamptz,
    history_lookback_days integer,
    last_position_lat double precision,
    last_position_lon double precision,
    source text not null default '',
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    primary key (provider, provider_account, provider_unit_id)
);

comment on table public.gps_provider_units is
    'Provider roster/archive for GPS units, including inactive/deactivated units, independent of dispatch board membership.';
comment on column public.gps_provider_units.asset_id is
    'Fleet-facing unit ID when known, e.g. truck number or canonical trailer ID.';
comment on column public.gps_provider_units.provider_unit_id is
    'Provider-native unit/device/vehicle identifier; primary conflict key with provider/provider_account.';
comment on column public.gps_provider_units.is_active is
    'Provider-reported active flag when available; null means unknown/history-inferred.';

create index if not exists idx_gps_provider_units_asset
    on public.gps_provider_units(asset_type, asset_id);
create index if not exists idx_gps_provider_units_canonical
    on public.gps_provider_units(asset_type, canonical_asset_id)
    where canonical_asset_id <> '';
create index if not exists idx_gps_provider_units_active
    on public.gps_provider_units(is_active, provider, last_seen_at desc);
create index if not exists idx_gps_provider_units_last_history
    on public.gps_provider_units(last_history_at desc)
    where last_history_at is not null;
create index if not exists idx_gps_provider_units_display_name
    on public.gps_provider_units using gin (to_tsvector('simple', display_name));

drop trigger if exists gps_provider_units_touch_updated_at on public.gps_provider_units;
create trigger gps_provider_units_touch_updated_at
before update on public.gps_provider_units
for each row execute function public.gps_touch_updated_at();

-- Rebuild/update archive rows from already-stored GPS history. This is useful
-- for units that no longer appear in provider roster endpoints but did report
-- pings during the selected lookback window.
create or replace function public.refresh_gps_provider_units_from_history(lookback_days integer default 180)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    affected integer := 0;
begin
    with base as (
        select
            coalesce(nullif(provider, ''), nullif(source, ''), 'unknown') as provider_name,
            coalesce(
                nullif(raw->>'account', ''),
                nullif(raw->>'company', ''),
                ''
            ) as provider_account_name,
            asset_type,
            asset_id,
            coalesce(
                nullif(raw->>'VehicleId', ''),
                nullif(raw->>'vehicleId', ''),
                nullif(raw->>'deviceId', ''),
                nullif(raw->>'vehicle_ref', ''),
                nullif(raw->>'eroad_id', ''),
                nullif(raw->>'canonicalId', ''),
                asset_id
            ) as provider_native_id,
            coalesce(
                nullif(raw->>'vehicleName', ''),
                nullif(raw->>'anytrek_name', ''),
                asset_id
            ) as provider_display_name,
            lat,
            lon,
            recorded_at,
            source,
            raw,
            row_number() over (
                partition by
                    coalesce(nullif(provider, ''), nullif(source, ''), 'unknown'),
                    coalesce(nullif(raw->>'account', ''), nullif(raw->>'company', ''), ''),
                    coalesce(
                        nullif(raw->>'VehicleId', ''),
                        nullif(raw->>'vehicleId', ''),
                        nullif(raw->>'deviceId', ''),
                        nullif(raw->>'vehicle_ref', ''),
                        nullif(raw->>'eroad_id', ''),
                        nullif(raw->>'canonicalId', ''),
                        asset_id
                    )
                order by recorded_at desc nulls last, id desc
            ) as rn
        from public.assets_history
        where recorded_at >= timezone('utc', now()) - make_interval(days => greatest(1, lookback_days))
          and asset_type in ('truck', 'trailer')
          and asset_id <> ''
    ), grouped as (
        select
            provider_name,
            provider_account_name,
            provider_native_id,
            min(asset_type) as asset_type,
            min(asset_id) as asset_id,
            min(asset_id) as canonical_asset_id,
            min(provider_display_name) as provider_display_name,
            min(recorded_at) as first_seen_at,
            max(recorded_at) as last_history_at,
            count(*) as history_rows
        from base
        group by provider_name, provider_account_name, provider_native_id
    ), latest as (
        select * from base where rn = 1
    )
    insert into public.gps_provider_units (
        provider,
        provider_account,
        provider_unit_id,
        asset_type,
        asset_id,
        canonical_asset_id,
        display_name,
        status,
        is_active,
        first_seen_at,
        last_seen_at,
        last_history_at,
        history_lookback_days,
        last_position_lat,
        last_position_lon,
        source,
        raw
    )
    select
        g.provider_name,
        g.provider_account_name,
        g.provider_native_id,
        g.asset_type,
        g.asset_id,
        g.canonical_asset_id,
        coalesce(nullif(g.provider_display_name, ''), g.asset_id),
        'history_seen',
        null,
        g.first_seen_at,
        timezone('utc', now()),
        g.last_history_at,
        greatest(1, lookback_days),
        l.lat,
        l.lon,
        coalesce(nullif(l.source, ''), 'assets_history'),
        jsonb_build_object(
            'history_rows', g.history_rows,
            'latest_raw', coalesce(l.raw, '{}'::jsonb),
            'history_inferred', true
        )
    from grouped g
    join latest l
      on l.provider_name = g.provider_name
     and l.provider_account_name = g.provider_account_name
     and l.provider_native_id = g.provider_native_id
    on conflict (provider, provider_account, provider_unit_id) do update set
        asset_type = excluded.asset_type,
        asset_id = coalesce(nullif(excluded.asset_id, ''), gps_provider_units.asset_id),
        canonical_asset_id = coalesce(nullif(excluded.canonical_asset_id, ''), gps_provider_units.canonical_asset_id),
        display_name = coalesce(nullif(excluded.display_name, ''), gps_provider_units.display_name),
        first_seen_at = least(gps_provider_units.first_seen_at, excluded.first_seen_at),
        last_seen_at = excluded.last_seen_at,
        last_history_at = greatest(
            coalesce(gps_provider_units.last_history_at, '-infinity'::timestamptz),
            coalesce(excluded.last_history_at, '-infinity'::timestamptz)
        ),
        history_lookback_days = excluded.history_lookback_days,
        last_position_lat = excluded.last_position_lat,
        last_position_lon = excluded.last_position_lon,
        source = excluded.source,
        raw = gps_provider_units.raw || excluded.raw,
        updated_at = timezone('utc', now());

    get diagnostics affected = row_count;
    return affected;
end;
$$;

alter table public.gps_provider_units enable row level security;
drop policy if exists "service role can manage gps_provider_units" on public.gps_provider_units;
create policy "service role can manage gps_provider_units" on public.gps_provider_units
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

notify pgrst, 'reload schema';
