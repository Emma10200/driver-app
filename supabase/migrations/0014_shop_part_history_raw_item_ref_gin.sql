-- Shop part history: raw QBO ItemRef fallback indexes for purchase/adjustment rows.
--
-- The fast part-history path primarily filters flattened line_items by item_id.
-- Some existing cached purchase/adjustment rows may predate that flattened shape
-- (or have incomplete flattened lines) while the authoritative raw QBO payload
-- still contains ItemRef.value. These raw jsonb_path_ops GIN indexes keep the
-- server-side raw containment fallback fast, without returning to full-history
-- Python scans.
--
-- Run AFTER 0010_shop_purchase_history_cache.sql and 0011_shop_inventory_adjustment_cache.sql.
-- Idempotent.

create index if not exists idx_shop_purchase_history_raw_gin
    on public.shop_purchase_history_cache using gin (raw jsonb_path_ops);

create index if not exists idx_shop_inventory_adjustment_raw_gin
    on public.shop_inventory_adjustment_cache using gin (raw jsonb_path_ops);
