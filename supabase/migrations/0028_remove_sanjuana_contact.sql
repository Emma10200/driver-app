-- Remove old Sanjuana/San Juana dispatcher contact rows from the editable roster.

delete from public.dispatch_contact_entries
where lower(replace(trim(dispatcher_name), ' ', '')) = 'sanjuana';