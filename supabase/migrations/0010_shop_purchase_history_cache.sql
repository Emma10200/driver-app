-- Shop purchase history cache (General Truck Service).
--
-- Mirrors QBO purchase-side item transactions into Supabase so part detail and
-- purchase history views do not query QuickBooks on every page view.
--
-- Covers:
-- - QBO Purchase (expenses/checks/credit-card purchases)
-- - QBO Bill (vendor bills)
--
-- Run AFTER 0006_shop_invoice_history_cache.sql and 0004_shop_inventory.sql.
-- Idempotent. Server-side Streamlit uses the service-role key.

create or replace function public.gps_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$ language plpgsql;

create table if not exists public.shop_purchase_history_cache (
    realm_id text not null,
    qbo_txn_type text not null check (qbo_txn_type in ('Purchase', 'Bill')),
    qbo_txn_id text not null,
    doc_number text not null default '',
    txn_date date,
    vendor_name text not null default '',
    payment_type text not null default '',
    total numeric(14, 2) not null default 0,
    line_items jsonb not null default '[]'::jsonb,
    qbo_last_updated_at timestamptz,
    qbo_created_at timestamptz,
    last_synced timestamptz not null default timezone('utc', now()),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    raw jsonb,
    primary key (realm_id, qbo_txn_type, qbo_txn_id)
);

create index if not exists idx_shop_purchase_history_realm_date
    on public.shop_purchase_history_cache(realm_id, txn_date desc, doc_number desc);
create index if not exists idx_shop_purchase_history_updated
    on public.shop_purchase_history_cache(realm_id, qbo_last_updated_at desc);
create index if not exists idx_shop_purchase_history_vendor
    on public.shop_purchase_history_cache(vendor_name);
create index if not exists idx_shop_purchase_history_lines_gin
    on public.shop_purchase_history_cache using gin (line_items jsonb_path_ops);

drop trigger if exists shop_purchase_history_cache_touch_updated_at on public.shop_purchase_history_cache;
create trigger shop_purchase_history_cache_touch_updated_at
before update on public.shop_purchase_history_cache
for each row execute function public.gps_touch_updated_at();

create table if not exists public.shop_purchase_history_sync_state (
    realm_id text primary key,
    last_qbo_updated_at timestamptz,
    last_run_at timestamptz,
    last_run_status text not null default '' check (
        last_run_status in ('', 'success', 'partial', 'failed', 'skipped')
    ),
    last_run_message text not null default '',
    purchases_upserted integer not null default 0,
    full_sync_completed_at timestamptz,
    updated_at timestamptz not null default timezone('utc', now())
);

drop trigger if exists shop_purchase_history_sync_state_touch on public.shop_purchase_history_sync_state;
create trigger shop_purchase_history_sync_state_touch
before update on public.shop_purchase_history_sync_state
for each row execute function public.gps_touch_updated_at();

alter table public.shop_purchase_history_cache enable row level security;
drop policy if exists "service role can manage shop_purchase_history_cache" on public.shop_purchase_history_cache;
create policy "service role can manage shop_purchase_history_cache" on public.shop_purchase_history_cache
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.shop_purchase_history_sync_state enable row level security;
drop policy if exists "service role can manage shop_purchase_history_sync_state" on public.shop_purchase_history_sync_state;
create policy "service role can manage shop_purchase_history_sync_state" on public.shop_purchase_history_sync_state
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
