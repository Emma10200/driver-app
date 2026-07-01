-- Dispatch board row mirror for read-only web UI.
--
-- Google Sheets remains the editable source of truth. Apps Script publishes
-- snapshots here so Streamlit can render a fast dispatcher-friendly web UI
-- and join board rows to GPS/current/dense evidence tables.

create table if not exists public.dispatch_board_rows (
    row_key text primary key,
    snapshot_id text not null,
    sheet_id text not null default '',
    sheet_name text not null default 'DISPATCH',
    sheet_row integer not null,
    truck_id text not null default '',
    trailer_id text not null default '',
    driver_name text not null default '',
    dispatcher text not null default '',
    division text not null default '',
    status text not null default '',
    origin text not null default '',
    destination text not null default '',
    pickup_at timestamptz,
    delivery_at timestamptz,
    updated_timestamp timestamptz,
    source_updated_at timestamptz not null default timezone('utc', now()),
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_dispatch_board_rows_snapshot on public.dispatch_board_rows(snapshot_id);
create index if not exists idx_dispatch_board_rows_updated on public.dispatch_board_rows(source_updated_at desc);
create index if not exists idx_dispatch_board_rows_truck on public.dispatch_board_rows(truck_id);
create index if not exists idx_dispatch_board_rows_trailer on public.dispatch_board_rows(trailer_id);
create index if not exists idx_dispatch_board_rows_dispatcher on public.dispatch_board_rows(dispatcher);
create index if not exists idx_dispatch_board_rows_status on public.dispatch_board_rows(status);

create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$;

drop trigger if exists dispatch_board_rows_touch_updated_at on public.dispatch_board_rows;
create trigger dispatch_board_rows_touch_updated_at
before update on public.dispatch_board_rows
for each row execute function public.set_updated_at();

alter table public.dispatch_board_rows enable row level security;

drop policy if exists "service role can manage dispatch_board_rows" on public.dispatch_board_rows;
create policy "service role can manage dispatch_board_rows" on public.dispatch_board_rows
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

notify pgrst, 'reload schema';
