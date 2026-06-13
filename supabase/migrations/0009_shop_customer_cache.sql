-- Shop customer cache (General Truck Service).
-- Mirrors QBO Customers into Supabase so the shop New Invoice customer search is
-- instant and does not call QBO on every keystroke/page view.
--
-- Run AFTER 0006_shop_invoice_history_cache.sql. Idempotent.

create extension if not exists pg_trgm;

create or replace function public.gps_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$ language plpgsql;

create table if not exists public.shop_customer_cache (
    realm_id text not null,
    qbo_customer_id text not null,
    display_name text not null default '',
    fully_qualified_name text not null default '',
    company_name text not null default '',
    active boolean not null default true,
    qbo_last_updated_at timestamptz,
    qbo_created_at timestamptz,
    last_synced timestamptz not null default timezone('utc', now()),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    raw jsonb,
    primary key (realm_id, qbo_customer_id)
);

create index if not exists idx_shop_customer_cache_realm_active
    on public.shop_customer_cache(realm_id, active);
create index if not exists idx_shop_customer_cache_updated
    on public.shop_customer_cache(realm_id, qbo_last_updated_at desc);
create index if not exists idx_shop_customer_display_trgm
    on public.shop_customer_cache using gin (display_name gin_trgm_ops);
create index if not exists idx_shop_customer_fqn_trgm
    on public.shop_customer_cache using gin (fully_qualified_name gin_trgm_ops);
create index if not exists idx_shop_customer_company_trgm
    on public.shop_customer_cache using gin (company_name gin_trgm_ops);

drop trigger if exists shop_customer_cache_touch_updated_at on public.shop_customer_cache;
create trigger shop_customer_cache_touch_updated_at
before update on public.shop_customer_cache
for each row execute function public.gps_touch_updated_at();

create table if not exists public.shop_customer_sync_state (
    realm_id text primary key,
    last_qbo_updated_at timestamptz,
    last_run_at timestamptz,
    last_run_status text not null default '' check (
        last_run_status in ('', 'success', 'partial', 'failed', 'skipped')
    ),
    last_run_message text not null default '',
    customers_upserted integer not null default 0,
    full_sync_completed_at timestamptz,
    updated_at timestamptz not null default timezone('utc', now())
);

drop trigger if exists shop_customer_sync_state_touch on public.shop_customer_sync_state;
create trigger shop_customer_sync_state_touch
before update on public.shop_customer_sync_state
for each row execute function public.gps_touch_updated_at();

create or replace function public.shop_customer_search(
    p_realm_id text,
    p_term text,
    p_limit integer default 25,
    p_active_only boolean default true
)
returns setof public.shop_customer_cache
language sql
stable
security definer
set search_path = public
as $$
    select *
    from public.shop_customer_cache c
    where c.realm_id = p_realm_id
      and (not p_active_only or c.active = true)
      and (
            coalesce(trim(p_term), '') = ''
         or c.display_name ilike '%' || p_term || '%'
         or c.fully_qualified_name ilike '%' || p_term || '%'
         or c.company_name ilike '%' || p_term || '%'
         or (c.display_name <> '' and c.display_name % p_term)
         or (c.company_name <> '' and c.company_name % p_term)
      )
    order by
        case when coalesce(trim(p_term), '') = '' then 0
             else greatest(
                 similarity(c.display_name, p_term),
                 similarity(c.fully_qualified_name, p_term),
                 similarity(c.company_name, p_term)
             )
        end desc,
        c.display_name asc
    limit greatest(1, coalesce(p_limit, 25));
$$;

alter table public.shop_customer_cache enable row level security;
drop policy if exists "service role can manage shop_customer_cache" on public.shop_customer_cache;
create policy "service role can manage shop_customer_cache" on public.shop_customer_cache
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.shop_customer_sync_state enable row level security;
drop policy if exists "service role can manage shop_customer_sync_state" on public.shop_customer_sync_state;
create policy "service role can manage shop_customer_sync_state" on public.shop_customer_sync_state
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
