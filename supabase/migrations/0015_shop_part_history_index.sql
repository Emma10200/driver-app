-- Shop part history derived index (General Truck Service).
--
-- One row per item/transaction line, rebuilt from the authoritative cached QBO
-- invoice, purchase/bill, and inventory-adjustment tables. This makes the Part
-- history screen a single indexed lookup by (realm_id, item_id), instead of
-- filtering JSON payloads at runtime.
--
-- Source of truth remains the QBO history cache tables. This table is disposable
-- and fully rebuildable during Sync All.
--
-- Run AFTER 0006_shop_invoice_history_cache.sql, 0010_shop_purchase_history_cache.sql,
-- and 0011_shop_inventory_adjustment_cache.sql. Idempotent.

create or replace function public.gps_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$ language plpgsql;

create table if not exists public.shop_part_history_index (
    realm_id text not null,
    event_id text not null,
    item_id text not null,
    item_name text not null default '',
    kind text not null check (kind in ('sold', 'bought', 'adjusted')),
    qbo_txn_type text not null default '',
    qbo_txn_id text not null default '',
    doc_number text not null default '',
    txn_date date,
    counterparty_name text not null default '',
    unit text not null default '',
    vin text not null default '',
    miles text not null default '',
    qty numeric,
    rate numeric,
    amount numeric,
    memo text not null default '',
    source_updated_at timestamptz,
    raw_line jsonb,
    last_rebuilt_at timestamptz not null default timezone('utc', now()),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    primary key (realm_id, event_id)
);

create index if not exists idx_shop_part_history_item_date
    on public.shop_part_history_index(realm_id, item_id, txn_date desc, event_id desc);
create index if not exists idx_shop_part_history_txn
    on public.shop_part_history_index(realm_id, qbo_txn_type, qbo_txn_id);
create index if not exists idx_shop_part_history_kind
    on public.shop_part_history_index(realm_id, kind, txn_date desc);

drop trigger if exists shop_part_history_index_touch_updated_at on public.shop_part_history_index;
create trigger shop_part_history_index_touch_updated_at
before update on public.shop_part_history_index
for each row execute function public.gps_touch_updated_at();

create table if not exists public.shop_part_history_index_state (
    realm_id text primary key,
    last_run_at timestamptz,
    last_run_status text not null default '' check (
        last_run_status in ('', 'success', 'partial', 'failed', 'skipped')
    ),
    last_run_message text not null default '',
    events_upserted integer not null default 0,
    full_rebuild_completed_at timestamptz,
    updated_at timestamptz not null default timezone('utc', now())
);

drop trigger if exists shop_part_history_index_state_touch on public.shop_part_history_index_state;
create trigger shop_part_history_index_state_touch
before update on public.shop_part_history_index_state
for each row execute function public.gps_touch_updated_at();

alter table public.shop_part_history_index enable row level security;
drop policy if exists "service role can manage shop_part_history_index" on public.shop_part_history_index;
create policy "service role can manage shop_part_history_index" on public.shop_part_history_index
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.shop_part_history_index_state enable row level security;
drop policy if exists "service role can manage shop_part_history_index_state" on public.shop_part_history_index_state;
create policy "service role can manage shop_part_history_index_state" on public.shop_part_history_index_state
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
