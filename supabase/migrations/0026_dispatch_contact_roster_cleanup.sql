-- Keep the editable dispatch contact directory aligned with the office roster.
-- Removes former Art/Arc rows and adds Zach's operations contacts
-- that were referenced in the source phone sheet but missing from 0025.

delete from public.dispatch_contact_entries
where lower(trim(dispatcher_name)) in ('art', 'arc');

update public.dispatch_company_info
set setup_contact = 'Dayana Sheytanova / Zach'
where division = 'Xpress Trans Inc'
    and setup_contact in ('Dayana Sheytanova / Zack', 'Dayana Sheytanova / Zach');

insert into public.dispatch_contact_entries
    (dispatcher_name, division, email, phone, extension, sort_order)
select seed.dispatcher_name, seed.division, seed.email, seed.phone, seed.extension, seed.sort_order
from (values
    ('Zach', 'Prestig Inc',                 'operations@prestige.inc',           '708-701-1109', '', 1),
    ('Zach', 'Prestige Transportation Inc', 'operations@prestigecalifornia.com', '224-522-1354', '', 2),
    ('Zach', 'Xpress Trans Inc',            'dispatch@xpresstransinc.com',       '224-522-1354', '', 3)
) as seed(dispatcher_name, division, email, phone, extension, sort_order)
where not exists (
    select 1
    from public.dispatch_contact_entries existing
    where lower(trim(existing.dispatcher_name)) = lower(trim(seed.dispatcher_name))
      and existing.division = seed.division
);