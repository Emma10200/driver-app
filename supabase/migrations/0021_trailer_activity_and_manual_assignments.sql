-- Migration 0021: Trailer activity summary + manual pair assignments.
-- Run in Supabase SQL Editor.

-- 1) Trailer activity summary: tracks movement per trailer per day
-- independent of whether it was matched to a truck.
create table if not exists public.trailer_activity_summary (
    id bigint generated always as identity primary key,
    service_date date not null,
    week_start date not null,
    trailer_id text not null,
    active_hours integer not null default 0,
    moving_hours integer not null default 0,
    miles_moved double precision not null default 0,
    paired_hours integer not null default 0,
    unmatched_moving_hours integer not null default 0,
    in_yard_hours integer not null default 0,
    provider text not null default '',
    division text not null default '',
    job_id text not null default '',
    computed_at timestamptz not null default now(),
    unique(service_date, trailer_id)
);

comment on table public.trailer_activity_summary is
    'Per-trailer daily activity: how many hours it moved, how many were matched, how many need attention.';

create index if not exists idx_trailer_activity_week on public.trailer_activity_summary(week_start, trailer_id);
create index if not exists idx_trailer_activity_unmatched on public.trailer_activity_summary(unmatched_moving_hours desc)
    where unmatched_moving_hours > 0;

-- 2) Manual pair assignments: human override for paper-log drivers.
create table if not exists public.manual_pair_assignments (
    id bigint generated always as identity primary key,
    truck_id text not null,
    trailer_id text not null,
    start_date date not null,
    end_date date,
    assigned_by text not null default 'unknown',
    assigned_at timestamptz not null default now(),
    unassigned_at timestamptz,
    notes text not null default '',
    active boolean not null default true,
    unique(truck_id, trailer_id, start_date)
);

comment on table public.manual_pair_assignments is
    'Human-assigned truck/trailer pairings for paper-log drivers or GPS-blind units. Suppresses unmatched-trailer alerts and provides history trail.';

create index if not exists idx_manual_pair_active on public.manual_pair_assignments(trailer_id, active)
    where active = true;
create index if not exists idx_manual_pair_truck on public.manual_pair_assignments(truck_id, active)
    where active = true;
