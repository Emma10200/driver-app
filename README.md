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

- `app.py` — app entry point
- `app_sections/` — page-by-page form sections
- `ui/common.py` — shared styles/components
- `services/` — drafts, notifications, logging
- `pdf_generator.py` — generated PDF output
- `submission_storage.py` — local/Supabase storage logic
- `config.py` — company profile + options
- `docs/PROJECT_MAP.md` — navigation guide for the repo layout
- `docs/ADMIN_AUTH.md` — admin password/Google SSO setup

## Data handling summary

- Drafts and submissions can be stored locally, in Supabase, or both
- Supporting docs: PDF/JPG/PNG
- Notification emails are summary-only (no attachment payloads)
- Test mode keeps fake data separate from production records

## Internal ownership

If someone else needs to maintain this later, start with:

1. `README.md` (this file)
2. `DEPLOYMENT_HANDOFF.md`
3. `config.py` for company-specific wording and settings

---

If you are reading this as a teammate: this project was built to be practical, maintainable, and affordable for our hiring process.
