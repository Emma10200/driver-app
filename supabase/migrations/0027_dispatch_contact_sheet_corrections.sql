-- Corrections from the pasted Prestige / Prestige Transportation email roster.
-- Do not store mailbox passwords from the source sheet in the database.

delete from public.dispatch_contact_entries
where lower(trim(dispatcher_name)) in ('art', 'arc');

delete from public.dispatch_contact_entries
where lower(trim(dispatcher_name)) in ('operations', 'zack')
  and lower(trim(email)) in ('dispatch@xpresstransinc.com', 'operations@prestige.inc', 'operations@prestigecalifornia.com');

update public.dispatch_company_info
set setup_contact = 'Dayana Sheytanova / Zach'
where division = 'Xpress Trans Inc'
  and setup_contact in ('Dayana Sheytanova / Zack', 'Dayana Sheytanova / Zach');

update public.dispatch_contact_entries set phone = '909-206-2911'
where dispatcher_name = 'Anna' and division = 'Prestige Transportation Inc';

update public.dispatch_contact_entries set phone = '708-701-1109'
where dispatcher_name = 'Brittany' and division = 'Prestig Inc';

update public.dispatch_contact_entries set phone = '909-206-4747'
where dispatcher_name = 'Brittany' and division = 'Prestige Transportation Inc';

update public.dispatch_contact_entries set phone = '909-900-6411'
where dispatcher_name = 'Carlos IL' and division = 'Prestig Inc';

update public.dispatch_contact_entries set phone = '909-206-5247'
where dispatcher_name = 'Carlos IL' and division = 'Prestige Transportation Inc';

update public.dispatch_contact_entries set phone = '909-206-4536'
where dispatcher_name = 'Carlos CA' and division = 'Prestige Transportation Inc';

update public.dispatch_contact_entries set phone = '909-206-2005'
where dispatcher_name = 'Felix' and division = 'Prestige Transportation Inc';

update public.dispatch_contact_entries set phone = '909-206-4365'
where dispatcher_name = 'Lily' and division = 'Prestige Transportation Inc';

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
      and lower(trim(existing.email)) = lower(trim(seed.email))
);