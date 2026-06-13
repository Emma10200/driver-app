-- Shop invoice queue (General Truck Service): drafts the shop manager submits for
-- accounting review. The shop app NEVER posts to QuickBooks directly; "Finish
-- invoice" writes a pending row here, and accounting reviews / imports it later.
--
-- Run AFTER 0004_shop_inventory.sql in the Supabase SQL editor. Idempotent.
-- Server-side Streamlit uses the service-role key, so RLS targets service_role.

create table if not exists public.shop_invoice_queue (
    id uuid primary key default gen_random_uuid(),
    realm_id text not null,
    proposed_doc_number text not null default '',     -- auto-suggested next invoice #
    status text not null default 'pending' check (
        status in ('pending', 'approved', 'rejected', 'imported')
    ),
    customer_name text not null default '',
    truck_unit text not null default '',              -- optional truck/unit reference
    notes text not null default '',
    line_items jsonb not null default '[]'::jsonb,     -- [{qbo_item_id, sku, name, qty, unit_price, line_total}]
    total numeric(14, 2) not null default 0,
    submitted_by text not null default '',            -- shop username (auth gate)
    reviewed_by text not null default '',             -- accounting email/name
    reviewed_at timestamptz,
    qbo_invoice_id text not null default '',          -- set when accounting imports it
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_shop_invoice_queue_realm_status
    on public.shop_invoice_queue(realm_id, status, created_at desc);
create index if not exists idx_shop_invoice_queue_created
    on public.shop_invoice_queue(created_at desc);

-- Reuse the shared updated_at trigger fn declared in earlier migrations.
drop trigger if exists shop_invoice_queue_touch_updated_at on public.shop_invoice_queue;
create trigger shop_invoice_queue_touch_updated_at
before update on public.shop_invoice_queue
for each row execute function public.gps_touch_updated_at();

alter table public.shop_invoice_queue enable row level security;
drop policy if exists "service role can manage shop_invoice_queue" on public.shop_invoice_queue;
create policy "service role can manage shop_invoice_queue" on public.shop_invoice_queue
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
