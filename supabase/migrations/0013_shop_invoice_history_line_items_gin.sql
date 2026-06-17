-- Shop invoice history: GIN index on line_items for fast per-part lookups.
--
-- Part history "Sold" events match an invoice line's flattened item_id. Without
-- an index, finding the invoices that contain a given part means scanning every
-- cached invoice row in Python. This jsonb_path_ops GIN index lets PostgREST's
-- containment filter (line_items @> '[{"item_id":"123"}]') find just the
-- matching invoices server-side, so opening a part's history is near-instant.
--
-- The shop purchase cache (0010) and inventory adjustment cache (0011) already
-- have the equivalent GIN index; this brings invoices in line.
--
-- Run AFTER 0006_shop_invoice_history_cache.sql. Idempotent.

create index if not exists idx_shop_invoice_history_lines_gin
    on public.shop_invoice_history_cache using gin (line_items jsonb_path_ops);
