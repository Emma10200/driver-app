    -- Rate confirmation email/document mirror.
    --
    -- First pass goal: read-only ingest from the confirmations Gmail inbox, match each
    -- email attachment/document to at most ONE dispatch-board truck, and surface
    -- low-confidence cases in the dispatch-board web UI.
    --
    -- Design rules:
    -- - Google/Gmail remains the email source of truth.
    -- - One row represents one attachment/document-worthy message unit.
    -- - One row can have only one selected truck. Extra candidates are kept in JSON
    --   and should create review alerts, not multiple assignments.
    -- - Reusing the same load/reference for another truck is allowed as data, but
    --   must be alertable for cancellation/reassignment review.

    create table if not exists public.rate_confirmation_documents (
        document_key text primary key,
        message_id text not null default '',
        thread_id text not null default '',
        attachment_index integer not null default 0,
        attachment_filename text not null default '',
        attachment_content_type text not null default '',
        attachment_size_bytes integer not null default 0,
        attachment_sha256 text not null default '',

        received_at timestamptz,
        sender_name text not null default '',
        sender_email text not null default '',
        sender_domain text not null default '',
        domain_division text not null default '',
        subject text not null default '',

        matched_truck_id text not null default '',
        match_status text not null default 'unmatched',
        match_type text not null default '',
        match_source text not null default '',
        match_token text not null default '',
        match_confidence numeric(5,4),
        candidate_matches jsonb not null default '[]'::jsonb,
        extracted_numbers jsonb not null default '[]'::jsonb,

        board_dispatcher text not null default '',
        board_driver_name text not null default '',
        board_division text not null default '',
        board_sheet_row integer,

        load_reference text not null default '',
        broker_name text not null default '',
        pickup_summary text not null default '',
        delivery_summary text not null default '',
        pickup_at timestamptz,
        delivery_at timestamptz,
        rate_amount numeric(12,2),
        stops jsonb not null default '[]'::jsonb,
        parsed_fields jsonb not null default '{}'::jsonb,
        parse_status text not null default 'not_started',

        pdf_storage_path text not null default '',
        original_available boolean not null default true,

        alert_level text not null default '',
        alert_codes text[] not null default '{}'::text[],
        alert_notes text not null default '',
        raw jsonb not null default '{}'::jsonb,

        source_updated_at timestamptz not null default timezone('utc', now()),
        created_at timestamptz not null default timezone('utc', now()),
        updated_at timestamptz not null default timezone('utc', now()),

        constraint rate_confirmation_documents_match_status_check check (
            match_status in ('matched', 'near_match', 'ambiguous', 'unmatched', 'cancelled', 'ignored')
        ),
        constraint rate_confirmation_documents_alert_level_check check (
            alert_level in ('', 'info', 'yellow', 'red')
        ),
        constraint rate_confirmation_documents_parse_status_check check (
            parse_status in ('not_started', 'text_extracted', 'needs_ocr', 'parsed', 'needs_review', 'verified', 'failed')
        )
    );

    create unique index if not exists idx_rate_conf_documents_msg_attachment
    on public.rate_confirmation_documents(message_id, attachment_index);

    create index if not exists idx_rate_conf_documents_received
    on public.rate_confirmation_documents(received_at desc);

    create index if not exists idx_rate_conf_documents_truck
    on public.rate_confirmation_documents(matched_truck_id);

    create index if not exists idx_rate_conf_documents_load_ref
    on public.rate_confirmation_documents(load_reference);

    create index if not exists idx_rate_conf_documents_sender_domain
    on public.rate_confirmation_documents(sender_domain);

    create index if not exists idx_rate_conf_documents_alert_level
    on public.rate_confirmation_documents(alert_level)
    where alert_level <> '';

    create index if not exists idx_rate_conf_documents_candidate_matches_gin
    on public.rate_confirmation_documents using gin(candidate_matches);

    create or replace function public.set_updated_at()
    returns trigger language plpgsql as $$
    begin
        new.updated_at = timezone('utc', now());
        return new;
    end;
    $$;

    drop trigger if exists rate_confirmation_documents_touch_updated_at on public.rate_confirmation_documents;
    create trigger rate_confirmation_documents_touch_updated_at
    before update on public.rate_confirmation_documents
    for each row execute function public.set_updated_at();

    alter table public.rate_confirmation_documents enable row level security;

    drop policy if exists "service role can manage rate_confirmation_documents" on public.rate_confirmation_documents;
    create policy "service role can manage rate_confirmation_documents" on public.rate_confirmation_documents
    for all using (auth.role() = 'service_role') with check (auth.role() = 'service_role');

    notify pgrst, 'reload schema';
