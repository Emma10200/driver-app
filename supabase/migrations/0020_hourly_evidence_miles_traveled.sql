-- Add miles_traveled column to evidence tables for distance traveled together.
-- Run in Supabase SQL Editor.

alter table public.asset_pair_hourly_evidence
    add column if not exists miles_traveled double precision not null default 0;

alter table public.asset_pair_daily_summary
    add column if not exists miles_traveled double precision not null default 0;

alter table public.asset_pair_weekly_review
    add column if not exists miles_traveled double precision not null default 0;

comment on column public.asset_pair_hourly_evidence.miles_traveled is
    'Sum of haversine distance between consecutive matched positions during the hour. Represents miles the truck/trailer traveled together.';
comment on column public.asset_pair_daily_summary.miles_traveled is
    'Total miles traveled together for the day (sum of hourly values).';
comment on column public.asset_pair_weekly_review.miles_traveled is
    'Total miles traveled together for the week (sum of daily values).';
