-- GPS auto-match human review decisions.
-- Stores reviewer confirmations/rejections so matching quality can be tuned from real feedback.

create table if not exists public.gps_match_reviews (
    match_id text primary key,
    decision text not null check (decision in ('Confirmed', 'Rejected')),
    reviewer_note text not null default '',
    reviewed_at timestamptz not null default timezone('utc', now()),
    division_filter text not null default '',
    trailer_id text not null default '',
    truck_id text not null default '',
    truck_coords text not null default '',
    trailer_coords text not null default '',
    distance_miles double precision,
    confidence text not null default '',
    history_hits integer not null default 0,
    on_board text not null default '',
    trailer_yard text not null default '',
    truck_yard text not null default '',
    reasons text not null default '',
    truck_provider text not null default '',
    trailer_provider text not null default '',
    truck_address text not null default '',
    trailer_address text not null default '',
    truck_division text not null default '',
    trailer_division text not null default '',
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_gps_match_reviews_decision on public.gps_match_reviews(decision);
create index if not exists idx_gps_match_reviews_reviewed_at on public.gps_match_reviews(reviewed_at desc);
create index if not exists idx_gps_match_reviews_truck on public.gps_match_reviews(truck_id);
create index if not exists idx_gps_match_reviews_trailer on public.gps_match_reviews(trailer_id);

drop trigger if exists gps_match_reviews_touch_updated_at on public.gps_match_reviews;
create trigger gps_match_reviews_touch_updated_at
before update on public.gps_match_reviews
for each row execute function public.gps_touch_updated_at();

alter table public.gps_match_reviews enable row level security;
drop policy if exists "service role can manage gps_match_reviews" on public.gps_match_reviews;
create policy "service role can manage gps_match_reviews" on public.gps_match_reviews
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
