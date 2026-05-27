# Driver Application Portal

This is the online driver application app I built for our company in my free time.

It replaces a paid third-party hiring portal with a tool we can control ourselves.

## What this app does

- Lets CDL owner-operator / driver applicants complete the application online
- Saves progress as a draft so applicants can come back later
- Generates PDFs for company records and applicant copies
- Handles required disclosures (FCRA, PSP, Clearinghouse)
- Sends internal notification emails when a new application is submitted

## Why this exists

- Reduce monthly software cost
- Keep our process in one place
- Make updates quickly when our company process changes

## Compliance notes (high level)

This app is structured to support common FMCSA hiring workflows and required disclosures, including:

- Driver application data collection aligned with 49 CFR § 391.21
- FCRA disclosure/authorization handling
- PSP disclosure/authorization handling
- Clearinghouse consent step before query process

> Final legal/compliance decisions should still be reviewed by company safety/HR/legal leadership.

## Quick start (local)

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Configure environment values:

- Copy `.env.example` to `.env` and fill in local values, or
- Use Streamlit secrets in `.streamlit/secrets.toml` for cloud-style config
- The admin dashboard intentionally has no hardcoded fallback password; use
  `ADMIN_PASSWORD`, Google SSO, or both via `ADMIN_AUTH_MODE`.
- For Google SSO on the admin dashboard, see `docs/ADMIN_AUTH.md`.

3. Run the app:

```bash
streamlit run app.py
```

## Deployment (short version)

Recommended setup:

- Host app on **Streamlit Community Cloud**
- Store submissions in **Supabase**
- Keep real secrets in Streamlit Secrets, not in GitHub.
- Protect the hidden admin dashboard with Google SSO allowlisted Gmail accounts.

For full deployment steps and troubleshooting, see:

- `DEPLOYMENT_HANDOFF.md`

## Core files

- `app.py` — app entry point and route dispatcher (driver app / `?dashboard=1` admin / `?qbo=1` QBO importer)
- `app_sections/` — page-by-page form sections
- `ui/common.py` — shared styles/components
- `services/` — drafts, notifications, logging, **QBO importer subsystem** (`qbo_dashboard.py`, `qbo_auth.py`, `qbo_audit.py`, `qbo_sheets_log.py`, `qbo_supabase.py`)
- `qbo/` — QBO API client, parsers, and import service used by the QBO subsystem
- `supabase/migrations/` — Supabase schema for both submissions and QBO (`qbo_realms`, `qbo_token_refs`, `qbo_audit_log`, `qbo_idempotency`, `qbo_app_settings`)
- `pdf_generator.py` — generated PDF output
- `submission_storage.py` — local/Supabase storage logic + shared `get_runtime_secret` helper
- `config.py` — company profile + options
- `docs/PROJECT_MAP.md` — navigation guide for the repo layout
- `docs/ADMIN_AUTH.md` — admin password/Google SSO setup

## Repository structure (where things live)

This single Streamlit app now hosts **three** routes from the same deploy:

| Route | URL | Entry function | Purpose |
| --- | --- | --- | --- |
| Driver application | `/` (default) | `app.py` main flow | Public CDL driver application form |
| Admin dashboard | `/?dashboard=1` | `admin_dashboard.render_admin_dashboard` | Internal review of submitted applications |
| **QBO Importer** | `/?qbo=1` | `services.qbo_dashboard.render_qbo_dashboard` | Imports invoices / driver statements / money codes into QuickBooks Online |

Live deploy: <https://driver-application.streamlit.app/>

### Related/sibling repositories

- **This repo (`prestige-driver-app`, GitHub: `Emma10200/driver-app`)** — the live deployed app and **the active home of the QBO Importer**. All new QBO work happens here.
- **`QBO_App`** (sibling folder, separate repo) — the original Tkinter / Google Apps Script prototype that the QBO Importer was ported from. It is **no longer the deployed importer**, but it is kept as a reference because a few features have not been ported yet:
  - `parking_pk` — Prestig Inc–only flow that appends `PK` to invoice DocNumbers containing the `Parking` line item. Still lives in `QBO_App/src_tkinter/qbo/parking_pk.py`.
  - The original Apps Script `Controller.gs` defines the legacy `ImportLog` Google Sheet schema (14 columns) that the Streamlit QBO Importer now also writes to — so historic and new imports share one bookmarkable history sheet.

### QBO Importer history sources

Every QBO import is recorded in two places:

1. **Supabase** (`qbo_audit_log`, `qbo_idempotency`) — authoritative, per-transaction. Drives the **Live (Supabase)** tab of the in-app history.
2. **Google Sheet `ImportLog` tab** (sheet id configured via `QBO_IMPORT_LOG_SHEET_ID`) — one summary row per import, mirroring the schema written by the legacy Apps Script tool so the ~300 rows of pre-Streamlit history stay in one place. Drives the **Legacy import log (Google Sheets)** tab.

Sheet writes are best-effort: if the Google service account or Sheets API is unavailable, imports still succeed and the failure is logged.

## Data handling summary

- Drafts and submissions can be stored locally, in Supabase, or both
- Supporting docs: PDF/JPG/PNG
- Notification emails are summary-only (no attachment payloads)
- Test mode keeps fake data separate from production records

## Copying an application to another company

Use `scripts/copy_submission_company.py` to regenerate a submitted application packet for a different company profile. It loads an existing `submission.json`, switches the company metadata, regenerates the branded PDFs, and saves the copy under `companies/<target-company>/<mode>/submissions`.

Examples:

```bash
python scripts/copy_submission_company.py submissions/companies/xpress/live/submissions/<submission-key> --to prestig
python scripts/copy_submission_company.py --remote-prefix companies/xpress/live/submissions/<submission-key> --to pg --backend supabase
```

The default backend is `local` for safety. Use `--backend supabase` or `--backend both` only when Supabase secrets are configured and you intentionally want to write the generated copy there.

## Internal ownership

If someone else needs to maintain this later, start with:

1. `README.md` (this file)
2. `DEPLOYMENT_HANDOFF.md`
3. `config.py` for company-specific wording and settings

---

If you are reading this as a teammate: this project was built to be practical, maintainable, and affordable for our hiring process.
