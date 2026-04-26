# Project map

This repo is intentionally small, but the root has several Streamlit entry/config files. Use this map as the navigation guide.

## Runtime entry points

- `app.py` — main Streamlit entry point used by deployment.
- `main.py` — alternate/local entry point if needed.
- `runtime_context.py` — reads URL query params such as company, test mode, and admin dashboard route.
- `state.py` — Streamlit session-state initialization.
- `config.py` — company profiles, wording, and app options.

## UI and application pages

- `app_sections/` — individual driver application pages/sections.
- `ui/` — shared UI shell, styling, validation display, and common Streamlit helpers.

## Services

- `services/admin_dashboard.py` — hidden admin dashboard at `?dashboard=1`.
- `services/submission_service.py` — submission orchestration.
- `services/draft_service.py` — draft save/resume support.
- `services/document_service.py` — supporting document handling.
- `services/notification_service.py` — internal email notifications.
- `services/sheets_export.py` — Google Sheets export.
- `services/csv_export.py` — CSV export helpers.
- `services/error_log_service.py` — runtime error logging.
- `services/test_mode_service.py` — test-mode isolation.

## Storage and output

- `submission_storage.py` — local/Supabase storage implementation.
- `pdf_generator.py` — generated PDF output.
- `submissions/` — local runtime output; ignored by Git.

## Tests and docs

- `tests/` — regression/unit tests.
- `docs/ADMIN_AUTH.md` — admin auth and Google SSO setup.
- `DEPLOYMENT_HANDOFF.md` — deployment checklist.
- `README.md` — high-level project overview.

## Files intentionally kept at repo root

Some files stay in the root because Streamlit/GitHub/cloud tooling expects or commonly discovers them there:

- `app.py`
- `requirements.txt`
- `pyproject.toml`
- `.env.example`
- `.streamlit/config.toml`

A deeper package reorganization is possible later, but it should be done as a separate refactor with full regression testing because many imports are currently root-relative.
