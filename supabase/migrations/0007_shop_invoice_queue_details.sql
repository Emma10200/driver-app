-- Shop invoice queue detail fields.
-- Adds VIN / Miles / edit tracking needed for shop-side invoice templates.
-- Run AFTER 0005_shop_invoice_queue.sql. Idempotent.

alter table public.shop_invoice_queue
    add column if not exists vin text not null default '',
    add column if not exists miles text not null default '',
    add column if not exists edit_count integer not null default 0,
    add column if not exists shop_locked_at timestamptz,
    add column if not exists last_shop_edit_at timestamptz;

create index if not exists idx_shop_invoice_queue_vin
    on public.shop_invoice_queue(vin);

-- Allow the shop to explicitly mark that a draft was edited after initial submit.
-- Accounting review/import UI (future ?qbo=1 work) should treat updated_at/edit_count
-- as the signal to re-review a queued draft before creating the QBO invoice.
