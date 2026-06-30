-- Hourly truck↔trailer evidence and weekly billing review support.
--
-- Purpose:
--   assets_history is the raw GPS audit trail.
--   asset_pairings is strict continuous segment detection.
--   These tables provide the practical weekly review layer: hour-by-hour
--   evidence, daily rollups, weekly rollups/review status, and job tracking.
--
-- Run this in Supabase SQL Editor before running:
--   python scripts/compute_pair_hourly_evidence.py --days 60

create table if not exists public.gps_pairing_job_runs (
    job_id text primary key,
    job_type text not null default 'hourly_evidence',
    status text not null default 'running' check (status in ('running', 'complete', 'failed')),
    started_at timestamptz not null default timezone('utc', now()),
    finished_at timestamptz,
    range_start timestamptz,
    range_end timestamptz,
    max_distance_miles double precision,
    near_distance_miles double precision,
    max_ping_gap_minutes double precision,
    history_rows integer not null default 0,
    usable_points integer not null default 0,
    hourly_rows integer not null default 0,
    daily_rows integer not null default 0,
    weekly_rows integer not null default 0,
    message text not null default '',
    error text not null default ''
);

create table if not exists public.asset_pair_hourly_evidence (
    id bigserial primary key,
    hour_start timestamptz not null,
    service_date date not null,
    week_start date not null,
    truck_id text not null,
    trailer_id text not null,
    status text not null check (status in ('paired', 'same_yard', 'near')),
    paired_evidence boolean not null default false,
    billable_candidate boolean not null default false,
    confidence double precision not null default 0,
    best_distance_miles double precision,
    best_ping_gap_minutes double precision,
    truck_pings integer not null default 0,
    trailer_pings integer not null default 0,
    truck_first_ping timestamptz,
    truck_last_ping timestamptz,
    trailer_first_ping timestamptz,
    trailer_last_ping timestamptz,
    truck_lat double precision,
    truck_lon double precision,
    trailer_lat double precision,
    trailer_lon double precision,
    truck_yard text not null default '',
    trailer_yard text not null default '',
    truck_provider text not null default '',
    trailer_provider text not null default '',
    truck_division text not null default '',
    trailer_division text not null default '',
    truck_address text not null default '',
    trailer_address text not null default '',
    source text not null default 'auto',
    job_id text references public.gps_pairing_job_runs(job_id) on delete set null,
    computed_at timestamptz not null default timezone('utc', now()),
    unique (hour_start, truck_id, trailer_id, source)
);

create index if not exists idx_asset_pair_hourly_date on public.asset_pair_hourly_evidence(service_date desc);
create index if not exists idx_asset_pair_hourly_week on public.asset_pair_hourly_evidence(week_start desc);
create index if not exists idx_asset_pair_hourly_truck on public.asset_pair_hourly_evidence(truck_id, service_date desc);
create index if not exists idx_asset_pair_hourly_trailer on public.asset_pair_hourly_evidence(trailer_id, service_date desc);
create index if not exists idx_asset_pair_hourly_pair on public.asset_pair_hourly_evidence(truck_id, trailer_id, hour_start desc);

create table if not exists public.asset_pair_daily_summary (
    id bigserial primary key,
    service_date date not null,
    week_start date not null,
    truck_id text not null,
    trailer_id text not null,
    paired_hours integer not null default 0,
    same_yard_hours integer not null default 0,
    near_hours integer not null default 0,
    billable_candidate_hours integer not null default 0,
    evidence_hours integer not null default 0,
    avg_distance_miles double precision,
    min_distance_miles double precision,
    avg_confidence double precision,
    first_evidence_at timestamptz,
    last_evidence_at timestamptz,
    truck_pings integer not null default 0,
    trailer_pings integer not null default 0,
    source text not null default 'auto',
    job_id text references public.gps_pairing_job_runs(job_id) on delete set null,
    computed_at timestamptz not null default timezone('utc', now()),
    unique (service_date, truck_id, trailer_id, source)
);

create index if not exists idx_asset_pair_daily_week on public.asset_pair_daily_summary(week_start desc);
create index if not exists idx_asset_pair_daily_trailer on public.asset_pair_daily_summary(trailer_id, service_date desc);
create index if not exists idx_asset_pair_daily_truck on public.asset_pair_daily_summary(truck_id, service_date desc);

create table if not exists public.asset_pair_weekly_review (
    id bigserial primary key,
    week_start date not null,
    truck_id text not null,
    trailer_id text not null,
    paired_hours integer not null default 0,
    same_yard_hours integer not null default 0,
    near_hours integer not null default 0,
    billable_candidate_hours integer not null default 0,
    evidence_days integer not null default 0,
    avg_distance_miles double precision,
    min_distance_miles double precision,
    avg_confidence double precision,
    first_evidence_at timestamptz,
    last_evidence_at timestamptz,
    review_status text not null default 'pending' check (review_status in ('pending', 'approved', 'rejected', 'needs_review')),
    billable_hours_override integer,
    review_notes text not null default '',
    reviewed_by text not null default '',
    reviewed_at timestamptz,
    source text not null default 'auto',
    job_id text references public.gps_pairing_job_runs(job_id) on delete set null,
    computed_at timestamptz not null default timezone('utc', now()),
    unique (week_start, truck_id, trailer_id, source)
);

create index if not exists idx_asset_pair_weekly_week on public.asset_pair_weekly_review(week_start desc);
create index if not exists idx_asset_pair_weekly_trailer on public.asset_pair_weekly_review(trailer_id, week_start desc);
create index if not exists idx_asset_pair_weekly_truck on public.asset_pair_weekly_review(truck_id, week_start desc);
create index if not exists idx_asset_pair_weekly_status on public.asset_pair_weekly_review(review_status, week_start desc);

alter table public.gps_pairing_job_runs enable row level security;
alter table public.asset_pair_hourly_evidence enable row level security;
alter table public.asset_pair_daily_summary enable row level security;
alter table public.asset_pair_weekly_review enable row level security;

drop policy if exists "service role can manage gps_pairing_job_runs" on public.gps_pairing_job_runs;
create policy "service role can manage gps_pairing_job_runs" on public.gps_pairing_job_runs
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage asset_pair_hourly_evidence" on public.asset_pair_hourly_evidence;
create policy "service role can manage asset_pair_hourly_evidence" on public.asset_pair_hourly_evidence
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage asset_pair_daily_summary" on public.asset_pair_daily_summary;
create policy "service role can manage asset_pair_daily_summary" on public.asset_pair_daily_summary
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage asset_pair_weekly_review" on public.asset_pair_weekly_review;
create policy "service role can manage asset_pair_weekly_review" on public.asset_pair_weekly_review
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

notify pgrst, 'reload schema';
