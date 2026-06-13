-- Shop inventory (General Truck Service repair shop) mirrored from QBO.
-- Producer: GitHub Actions cron -> qbo/shop_inventory_sync.py (delta sync via
--   Item.MetaData.LastUpdatedTime). Consumer: mobile Streamlit "Inventory List".
--
-- Run AFTER 0003_gps_assets.sql in the Supabase SQL editor. Safe to re-run
-- (idempotent). Server-side Streamlit + the cron both use the service-role key,
-- so RLS policies target service_role, consistent with the rest of the app.

-- pg_trgm powers tolerant fuzzy search (ILIKE + similarity) on SKU / name / desc.
create extension if not exists pg_trgm;

-- Re-declare the shared updated_at trigger fn so this migration is self-sufficient
-- even if 0003 has not been applied. Identical body to 0003_gps_assets.sql.
create or replace function public.gps_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$ language plpgsql;

-- ---------------------------------------------------------------------------
-- shop_inventory: one row per QBO Item (Inventory + Non-Inventory + Service).
-- Keyed by (realm_id, qbo_item_id) so the table is multi-company-safe even
-- though only the General Truck Service realm uses it today.
-- qty_on_hand is NULLABLE on purpose: only Type='Inventory' items track quantity.
-- ---------------------------------------------------------------------------
create table if not exists public.shop_inventory (
    realm_id text not null,
    qbo_item_id text not null,
    name text not null default '',
    fully_qualified_name text not null default '',  -- includes parent for sub-items
    sku text not null default '',
    sales_description text not null default '',       -- Item.Description (sales forms)
    purchase_description text not null default '',    -- Item.PurchaseDesc (purchase forms)
    item_type text not null default '',               -- Inventory | NonInventory | Service
    qty_on_hand numeric(14, 2),                       -- NULL for non-inventory items
    reorder_point numeric(14, 2),                     -- QBO ReorderPoint when present
    sales_price numeric(14, 2),                       -- Item.UnitPrice (sales price / rate)
    purchase_cost numeric(14, 2),                     -- Item.PurchaseCost (cost / purchase price)
    income_account_name text not null default '',     -- IncomeAccountRef
    expense_account_name text not null default '',    -- ExpenseAccountRef
    asset_account_name text not null default '',      -- AssetAccountRef (inventory asset)
    taxable boolean,
    active boolean not null default true,
    qbo_last_updated_at timestamptz,                 -- Item.MetaData.LastUpdatedTime (delta cursor)
    qbo_created_at timestamptz,                      -- Item.MetaData.CreateTime
    last_synced timestamptz not null default timezone('utc', now()),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now()),
    raw jsonb,                                        -- full QBO Item for forward-compat
    primary key (realm_id, qbo_item_id)
);

-- Lookups + filtered list (active items for the realm).
create index if not exists idx_shop_inventory_realm_active
    on public.shop_inventory(realm_id, active);
create index if not exists idx_shop_inventory_updated
    on public.shop_inventory(realm_id, qbo_last_updated_at desc);

-- Trigram indexes for fuzzy / partial search by SKU, name, and descriptions.
create index if not exists idx_shop_inventory_sku_trgm
    on public.shop_inventory using gin (sku gin_trgm_ops);
create index if not exists idx_shop_inventory_name_trgm
    on public.shop_inventory using gin (name gin_trgm_ops);
create index if not exists idx_shop_inventory_sales_desc_trgm
    on public.shop_inventory using gin (sales_description gin_trgm_ops);
create index if not exists idx_shop_inventory_purchase_desc_trgm
    on public.shop_inventory using gin (purchase_description gin_trgm_ops);

drop trigger if exists shop_inventory_touch_updated_at on public.shop_inventory;
create trigger shop_inventory_touch_updated_at
before update on public.shop_inventory
for each row execute function public.gps_touch_updated_at();

-- ---------------------------------------------------------------------------
-- shop_inventory_sync_state: one row per realm. Stores the delta-sync cursor
-- (high-water mark of Item.MetaData.LastUpdatedTime seen) + last run telemetry
-- so the cron can resume incrementally instead of re-pulling the full catalog.
-- ---------------------------------------------------------------------------
create table if not exists public.shop_inventory_sync_state (
    realm_id text primary key,
    last_qbo_updated_at timestamptz,                 -- highest Item LastUpdatedTime ingested
    last_run_at timestamptz,
    last_run_status text not null default '' check (
        last_run_status in ('', 'success', 'partial', 'failed', 'skipped')
    ),
    last_run_message text not null default '',
    items_upserted integer not null default 0,       -- count from the most recent run
    full_sync_completed_at timestamptz,              -- last time a full (non-delta) pull finished
    updated_at timestamptz not null default timezone('utc', now())
);

drop trigger if exists shop_inventory_sync_state_touch on public.shop_inventory_sync_state;
create trigger shop_inventory_sync_state_touch
before update on public.shop_inventory_sync_state
for each row execute function public.gps_touch_updated_at();

-- ---------------------------------------------------------------------------
-- shop_inventory_search: tolerant fuzzy search for the mobile UI. Matches the
-- term against SKU, name, and description using both substring (ILIKE) and
-- trigram similarity, then ranks best matches first. Returns active items only
-- by default. SECURITY DEFINER so it runs under the table owner; callers reach
-- it via PostgREST /rpc with the service-role key.
-- ---------------------------------------------------------------------------
create or replace function public.shop_inventory_search(
    p_realm_id text,
    p_term text,
    p_limit integer default 50,
    p_active_only boolean default true
)
returns setof public.shop_inventory
language sql
stable
security definer
set search_path = public
as $$
    select *
    from public.shop_inventory si
    where si.realm_id = p_realm_id
      and (not p_active_only or si.active = true)
      and (
            coalesce(trim(p_term), '') = ''
         or si.sku ilike '%' || p_term || '%'
         or si.name ilike '%' || p_term || '%'
         or si.sales_description ilike '%' || p_term || '%'
         or si.purchase_description ilike '%' || p_term || '%'
         or si.fully_qualified_name ilike '%' || p_term || '%'
         or (si.sku <> '' and si.sku % p_term)
         or (si.name <> '' and si.name % p_term)
      )
    order by
        -- 1) An exact SKU match always wins, even if it is out of stock.
        case when si.sku <> '' and lower(si.sku) = lower(trim(p_term)) then 0 else 1 end,
        -- 2) In stock (or quantity-not-tracked) above anything out of stock,
        --    regardless of how well the term matched.
        case when si.qty_on_hand is null or si.qty_on_hand > 0 then 0 else 1 end,
        -- 3) Then best fuzzy relevance within each stock tier.
        case when coalesce(trim(p_term), '') = '' then 0
             else greatest(
                similarity(si.sku, p_term),
                similarity(si.name, p_term),
                similarity(si.sales_description, p_term)
             )
        end desc,
        si.name asc
    limit greatest(1, coalesce(p_limit, 50));
$$;

-- ---------------------------------------------------------------------------
-- Row level security: service role manages everything (server-side Streamlit
-- + GitHub Actions cron both use the service-role key).
-- ---------------------------------------------------------------------------
alter table public.shop_inventory enable row level security;
drop policy if exists "service role can manage shop_inventory" on public.shop_inventory;
create policy "service role can manage shop_inventory" on public.shop_inventory
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

alter table public.shop_inventory_sync_state enable row level security;
drop policy if exists "service role can manage shop_inventory_sync_state" on public.shop_inventory_sync_state;
create policy "service role can manage shop_inventory_sync_state" on public.shop_inventory_sync_state
for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');
