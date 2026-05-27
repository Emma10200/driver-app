-- QBO Importer central history + encrypted token storage.
-- Run manually in Supabase SQL Editor for the existing driver-app project.
-- Requires Supabase Vault. In the dashboard, enable Database > Extensions > Vault first.

create table if not exists public.qbo_realms (
    realm_id text primary key,
    company_name text not null default '',
    environment text not null default 'production' check (environment in ('production', 'sandbox')),
    default_bank_account_name text not null default '',
    default_money_code_cc_account_name text not null default 'Fuel Card - EFS',
    connected_by_email text not null default '',
    connected_at timestamptz,
    updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.qbo_token_refs (
    realm_id text primary key references public.qbo_realms(realm_id) on delete cascade,
    access_secret_id uuid,
    refresh_secret_id uuid,
    access_expires_at timestamptz,
    refresh_expires_at timestamptz,
    connected_by_email text not null default '',
    updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.qbo_audit_log (
    id bigserial primary key,
    created_at timestamptz not null default timezone('utc', now()),
    imported_by_email text not null default '',
    txn_type text not null,
    realm_id text not null,
    division text not null default '',
    doc_number text not null default '',
    txn_date date,
    entity_name text not null default '',
    amount numeric(14, 2),
    status text not null check (status in ('success', 'duplicate', 'failed', 'warning', 'info')),
    qbo_id text not null default '',
    message text not null default '',
    source_file_name text not null default '',
    source_file_hash text not null default '',
    idempotency_key text not null default '',
    app_version text not null default '',
    raw_response jsonb
);

create index if not exists idx_qbo_audit_log_realm_date on public.qbo_audit_log(realm_id, txn_date desc);
create index if not exists idx_qbo_audit_log_doc on public.qbo_audit_log(doc_number);
create index if not exists idx_qbo_audit_log_source_hash on public.qbo_audit_log(source_file_hash);
create index if not exists idx_qbo_audit_log_imported_by on public.qbo_audit_log(imported_by_email);

create table if not exists public.qbo_idempotency (
    idempotency_key text primary key,
    realm_id text not null,
    txn_type text not null,
    doc_number text not null default '',
    txn_date date,
    entity_ref_id text not null default '',
    amount numeric(14, 2),
    source_file_hash text not null default '',
    audit_log_id bigint references public.qbo_audit_log(id) on delete set null,
    created_at timestamptz not null default timezone('utc', now()),
    created_by_email text not null default ''
);

create index if not exists idx_qbo_idempotency_realm_doc on public.qbo_idempotency(realm_id, doc_number);

create table if not exists public.qbo_app_settings (
    setting_key text primary key,
    setting_value jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default timezone('utc', now()),
    updated_by_email text not null default ''
);

-- Keep updated_at fresh for ordinary table updates.
create or replace function public.qbo_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$;

drop trigger if exists qbo_realms_touch_updated_at on public.qbo_realms;
create trigger qbo_realms_touch_updated_at
before update on public.qbo_realms
for each row execute function public.qbo_touch_updated_at();

drop trigger if exists qbo_token_refs_touch_updated_at on public.qbo_token_refs;
create trigger qbo_token_refs_touch_updated_at
before update on public.qbo_token_refs
for each row execute function public.qbo_touch_updated_at();

drop trigger if exists qbo_app_settings_touch_updated_at on public.qbo_app_settings;
create trigger qbo_app_settings_touch_updated_at
before update on public.qbo_app_settings
for each row execute function public.qbo_touch_updated_at();

-- Store or update encrypted QBO token bundle in Supabase Vault.
-- The Streamlit app calls this through PostgREST RPC with the service-role key.
create or replace function public.qbo_upsert_token_bundle(
    p_realm_id text,
    p_company_name text,
    p_environment text,
    p_access_token text,
    p_refresh_token text,
    p_access_expires_at timestamptz,
    p_refresh_expires_at timestamptz,
    p_connected_by_email text
)
returns void
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    existing public.qbo_token_refs%rowtype;
    access_id uuid;
    refresh_id uuid;
    safe_environment text := case when lower(coalesce(p_environment, 'production')) = 'sandbox' then 'sandbox' else 'production' end;
begin
    if coalesce(trim(p_realm_id), '') = '' then
        raise exception 'realm_id is required';
    end if;

    insert into public.qbo_realms (
        realm_id,
        company_name,
        environment,
        connected_by_email,
        connected_at
    ) values (
        p_realm_id,
        coalesce(p_company_name, p_realm_id),
        safe_environment,
        coalesce(p_connected_by_email, ''),
        timezone('utc', now())
    )
    on conflict (realm_id) do update set
        company_name = coalesce(nullif(excluded.company_name, ''), public.qbo_realms.company_name),
        environment = excluded.environment,
        connected_by_email = excluded.connected_by_email,
        connected_at = timezone('utc', now());

    select * into existing from public.qbo_token_refs where realm_id = p_realm_id;

    if existing.access_secret_id is null then
        access_id := vault.create_secret(
            coalesce(p_access_token, ''),
            'qbo_access_' || p_realm_id,
            'QBO access token for realm ' || p_realm_id
        );
    else
        access_id := existing.access_secret_id;
        perform vault.update_secret(
            access_id,
            coalesce(p_access_token, ''),
            'qbo_access_' || p_realm_id,
            'QBO access token for realm ' || p_realm_id
        );
    end if;

    if existing.refresh_secret_id is null then
        refresh_id := vault.create_secret(
            coalesce(p_refresh_token, ''),
            'qbo_refresh_' || p_realm_id,
            'QBO refresh token for realm ' || p_realm_id
        );
    else
        refresh_id := existing.refresh_secret_id;
        perform vault.update_secret(
            refresh_id,
            coalesce(p_refresh_token, ''),
            'qbo_refresh_' || p_realm_id,
            'QBO refresh token for realm ' || p_realm_id
        );
    end if;

    insert into public.qbo_token_refs (
        realm_id,
        access_secret_id,
        refresh_secret_id,
        access_expires_at,
        refresh_expires_at,
        connected_by_email
    ) values (
        p_realm_id,
        access_id,
        refresh_id,
        p_access_expires_at,
        p_refresh_expires_at,
        coalesce(p_connected_by_email, '')
    )
    on conflict (realm_id) do update set
        access_secret_id = excluded.access_secret_id,
        refresh_secret_id = excluded.refresh_secret_id,
        access_expires_at = excluded.access_expires_at,
        refresh_expires_at = excluded.refresh_expires_at,
        connected_by_email = excluded.connected_by_email;
end;
$$;

create or replace function public.qbo_get_token_bundle(p_realm_id text)
returns table (
    realm_id text,
    company_name text,
    environment text,
    access_token text,
    refresh_token text,
    access_expires_at timestamptz,
    refresh_expires_at timestamptz,
    connected_by_email text,
    updated_at timestamptz
)
language sql
security definer
set search_path = public, vault
as $$
    select
        r.realm_id,
        r.company_name,
        r.environment,
        coalesce(access_secret.decrypted_secret, '') as access_token,
        coalesce(refresh_secret.decrypted_secret, '') as refresh_token,
        t.access_expires_at,
        t.refresh_expires_at,
        t.connected_by_email,
        t.updated_at
    from public.qbo_realms r
    join public.qbo_token_refs t on t.realm_id = r.realm_id
    left join vault.decrypted_secrets access_secret on access_secret.id = t.access_secret_id
    left join vault.decrypted_secrets refresh_secret on refresh_secret.id = t.refresh_secret_id
    where r.realm_id = p_realm_id;
$$;

create or replace function public.qbo_disconnect_realm(p_realm_id text)
returns void
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    existing public.qbo_token_refs%rowtype;
begin
    select * into existing from public.qbo_token_refs where realm_id = p_realm_id;
    if existing.access_secret_id is not null then
        delete from vault.secrets where id = existing.access_secret_id;
    end if;
    if existing.refresh_secret_id is not null then
        delete from vault.secrets where id = existing.refresh_secret_id;
    end if;
    delete from public.qbo_token_refs where realm_id = p_realm_id;
end;
$$;

-- Optional RLS guardrails. The app uses the service-role key, so these do not
-- block the Streamlit backend. They prevent accidental anon/authenticated reads.
alter table public.qbo_realms enable row level security;
alter table public.qbo_token_refs enable row level security;
alter table public.qbo_audit_log enable row level security;
alter table public.qbo_idempotency enable row level security;
alter table public.qbo_app_settings enable row level security;

drop policy if exists "service role can manage qbo_realms" on public.qbo_realms;
create policy "service role can manage qbo_realms" on public.qbo_realms
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage qbo_token_refs" on public.qbo_token_refs;
create policy "service role can manage qbo_token_refs" on public.qbo_token_refs
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage qbo_audit_log" on public.qbo_audit_log;
create policy "service role can manage qbo_audit_log" on public.qbo_audit_log
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage qbo_idempotency" on public.qbo_idempotency;
create policy "service role can manage qbo_idempotency" on public.qbo_idempotency
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

drop policy if exists "service role can manage qbo_app_settings" on public.qbo_app_settings;
create policy "service role can manage qbo_app_settings" on public.qbo_app_settings
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
