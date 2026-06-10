-- QBO exact import-batch signatures.
-- Run after 0001_qbo_importer.sql. This table lets the app reject an exact
-- repeat of a money-code/amount batch without relying on QuickBooks-side
-- per-money-code duplicate checks.

create table if not exists public.qbo_import_batch_signatures (
    id bigserial primary key,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    imported_by_email text not null default '',
    template_type text not null,
    realm_id text not null,
    fingerprint text not null unique,
    signature_version integer not null default 1,
    status text not null default 'pending' check (status in ('pending', 'complete', 'partial', 'failed')),
    entries jsonb not null default '[]'::jsonb,
    entry_count integer not null default 0,
    total_amount numeric(14, 2),
    source_file_name text not null default '',
    source_file_hash text not null default '',
    posted_count integer not null default 0,
    failed_count integer not null default 0,
    duplicate_count integer not null default 0,
    message text not null default ''
);

create index if not exists idx_qbo_import_batch_signatures_realm_template
    on public.qbo_import_batch_signatures(realm_id, template_type, created_at desc);

create index if not exists idx_qbo_import_batch_signatures_source_hash
    on public.qbo_import_batch_signatures(source_file_hash);

drop trigger if exists qbo_import_batch_signatures_touch_updated_at on public.qbo_import_batch_signatures;
create trigger qbo_import_batch_signatures_touch_updated_at
before update on public.qbo_import_batch_signatures
for each row execute function public.qbo_touch_updated_at();

alter table public.qbo_import_batch_signatures enable row level security;

drop policy if exists "service role can manage qbo_import_batch_signatures" on public.qbo_import_batch_signatures;
create policy "service role can manage qbo_import_batch_signatures" on public.qbo_import_batch_signatures
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
