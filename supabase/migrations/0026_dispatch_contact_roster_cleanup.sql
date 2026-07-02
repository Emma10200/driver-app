-- Keep the editable dispatch contact directory aligned with the office roster.
-- Removes former Art/Arc rows and adds the Xpress Operations/Zack contacts
-- that were referenced in the source phone sheet but missing from 0025.

delete from public.dispatch_contact_entries
where lower(trim(dispatcher_name)) in ('art', 'arc');

update public.dispatch_company_info
set setup_contact = 'Dayana Sheytanova / Zack'
where division = 'Xpress Trans Inc'
    and setup_contact = 'Dayana Sheytanova / Zach';

insert into public.dispatch_contact_entries
    (dispatcher_name, division, email, phone, extension, sort_order)
select seed.dispatcher_name, seed.division, seed.email, seed.phone, seed.extension, seed.sort_order
from (values
    ('Operations', 'Xpress Trans Inc', 'dispatch@xpresstransinc.com', '224-341-6014', '', 1),
    ('Zack',       'Xpress Trans Inc', 'dispatch@xpresstransinc.com', '224-522-1354', '', 2)
) as seed(dispatcher_name, division, email, phone, extension, sort_order)
where not exists (
    select 1
    from public.dispatch_contact_entries existing
    where lower(trim(existing.dispatcher_name)) = lower(trim(seed.dispatcher_name))
      and existing.division = seed.division
);