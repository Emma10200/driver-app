-- Shop invoice history cache (General Truck Service).
--
-- Purpose: make Invoice History + invoice detail views load as quickly as the
-- inventory list by reading Supabase instead of calling QBO on every page view.
-- Producer: manual in-app refresh / future cron -> qbo/shop_invoice_history_sync.py
-- Consumer: services/shop_inventory_page.py Invoice History and detail views.
--
-- Run AFTER 0005_shop_invoice_queue.sql in the Supabase SQL editor. Idempotent.
-- Server-side Streamlit uses the service-role key, so RLS targets service_role.

create table if not exists public.shop_invoice_history_cache (
    realm_id text not null,
    qbo_invoice_id text not null,
    doc_number text not null default '',
    txn_date date,
    customer_name text not null default '',
    total numeric(14, 2) not null default 0,
    balance numeric(14, 2) not null default 0,
    unit text not null default '',                  -- QBO custom field: Unit
    vin text not null default '',                   -- QBO custom field: VIN
    miles text not null default '',                 -- QBO custom field: Miles
    line_items jsonb not null default '[]'::jsonb,  -- flattened editable-QBO-style lines
    active boolean not null default true,
    qbo_last_updated_at timestamptz,                -- Invoice.MetaData.LastUpdatedTime
    qbo_created_at timestamptz,
    last_synced timestamptz not null default timezone('utc', now()),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    raw jsonb,                                      -- full QBO Invoice payload
    primary key (realm_id, qbo_invoice_id)
);

create index if not exists idx_shop_invoice_history_realm_date
    on public.shop_invoice_history_cache(realm_id, txn_date desc, doc_number desc);
create index if not exists idx_shop_invoice_history_updated
    on public.shop_invoice_history_cache(realm_id, qbo_last_updated_at desc);
create index if not exists idx_shop_invoice_history_doc
    on public.shop_invoice_history_cache(doc_number);
create index if not exists idx_shop_invoice_history_unit
    on public.shop_invoice_history_cache(unit);
create index if not exists idx_shop_invoice_history_vin
    on public.shop_invoice_history_cache(vin);

drop trigger if exists shop_invoice_history_cache_touch_updated_at on public.shop_invoice_history_cache;
create trigger shop_invoice_history_cache_touch_updated_at
before update on public.shop_invoice_history_cache
for each row execute function public.gps_touch_updated_at();

create table if not exists public.shop_invoice_history_sync_state (
    realm_id text primary key,
    last_qbo_updated_at timestamptz,
    last_run_at timestamptz,
    last_run_status text not null default '' check (
        last_run_status in ('', 'success', 'partial', 'failed', 'skipped')
    ),
    last_run_message text not null default '',
    invoices_upserted integer not null default 0,
    full_sync_completed_at timestamptz,
    updated_at timestamptz not null default timezone('utc', now())
);

drop trigger if exists shop_invoice_history_sync_state_touch on public.shop_invoice_history_sync_state;
create trigger shop_invoice_history_sync_state_touch
before update on public.shop_invoice_history_sync_state
for each row execute function public.gps_touch_updated_at();

alter table public.shop_invoice_history_cache enable row level security;
drop policy if exists "service role can manage shop_invoice_history_cache" on public.shop_invoice_history_cache;
create policy "service role can manage shop_invoice_history_cache" on public.shop_invoice_history_cache
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.shop_invoice_history_sync_state enable row level security;
drop policy if exists "service role can manage shop_invoice_history_sync_state" on public.shop_invoice_history_sync_state;
create policy "service role can manage shop_invoice_history_sync_state" on public.shop_invoice_history_sync_state
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');