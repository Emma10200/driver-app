-- Shop attachment index cache (General Truck Service).
--
-- Caches WHICH QBO transactions have scanned documents (and each file's stable
-- metadata) so the Invoice/Purchase history lists and part-history rows can show
-- document badges + open buttons instantly, without scanning the QBO Attachable
-- entity on every page load.
--
-- IMPORTANT: only lightweight metadata is cached here (file name, content type,
-- size, note). The document BYTES are never copied into Supabase -- they always
-- stream on demand straight from QuickBooks' short-lived pre-signed URLs. So
-- this table stays tiny: one row per transaction-with-documents, each holding a
-- small JSON array of file metadata (KBs, not the multi-GB PDFs).
--
-- Run AFTER 0006_shop_invoice_history_cache.sql and 0010_shop_purchase_history_cache.sql.
-- Idempotent. Server-side Streamlit uses the service-role key.

create or replace function public.gps_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$ language plpgsql;

-- One row per (realm, entity) that has at least one linked file attachment.
-- entity_type is stored lowercased ('invoice' / 'purchase' / 'bill') to match
-- the app's case-insensitive lookup key.
create table if not exists public.shop_attachment_index_cache (
    realm_id text not null,
    entity_type text not null,
    entity_id text not null,
    attachments jsonb not null default '[]'::jsonb,
    attachment_count integer not null default 0,
    last_synced timestamptz not null default timezone('utc', now()),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    primary key (realm_id, entity_type, entity_id)
);

create index if not exists idx_shop_attachment_index_realm
    on public.shop_attachment_index_cache(realm_id);
create index if not exists idx_shop_attachment_index_synced
    on public.shop_attachment_index_cache(realm_id, last_synced desc);

drop trigger if exists shop_attachment_index_cache_touch_updated_at on public.shop_attachment_index_cache;
create trigger shop_attachment_index_cache_touch_updated_at
before update on public.shop_attachment_index_cache
for each row execute function public.gps_touch_updated_at();

create table if not exists public.shop_attachment_index_sync_state (
    realm_id text primary key,
    last_run_at timestamptz,
    last_run_status text not null default '' check (
        last_run_status in ('', 'success', 'partial', 'failed', 'skipped')
    ),
    last_run_message text not null default '',
    links_upserted integer not null default 0,
    full_sync_completed_at timestamptz,
    updated_at timestamptz not null default timezone('utc', now())
);

drop trigger if exists shop_attachment_index_sync_state_touch on public.shop_attachment_index_sync_state;
create trigger shop_attachment_index_sync_state_touch
before update on public.shop_attachment_index_sync_state
for each row execute function public.gps_touch_updated_at();

alter table public.shop_attachment_index_cache enable row level security;
drop policy if exists "service role can manage shop_attachment_index_cache" on public.shop_attachment_index_cache;
create policy "service role can manage shop_attachment_index_cache" on public.shop_attachment_index_cache
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.shop_attachment_index_sync_state enable row level security;
drop policy if exists "service role can manage shop_attachment_index_sync_state" on public.shop_attachment_index_sync_state;
create policy "service role can manage shop_attachment_index_sync_state" on public.shop_attachment_index_sync_state
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
