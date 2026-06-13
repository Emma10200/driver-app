-- Shop invoice queue draft support.
-- Adds draft status + new-customer metadata for shop-created invoice templates.
-- Run AFTER 0007_shop_invoice_queue_details.sql. Idempotent.

alter table public.shop_invoice_queue
    add column if not exists customer_is_new boolean not null default false;

-- Replace the original status check so the shop can Save Draft before Finish.
alter table public.shop_invoice_queue
    drop constraint if exists shop_invoice_queue_status_check;

alter table public.shop_invoice_queue
    add constraint shop_invoice_queue_status_check check (
        status in ('draft', 'pending', 'approved', 'rejected', 'imported')
    );

create index if not exists idx_shop_invoice_queue_status_updated
    on public.shop_invoice_queue(status, updated_at desc);
