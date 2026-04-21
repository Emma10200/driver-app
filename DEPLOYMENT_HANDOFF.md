# Driver Application Deployment Handoff

This project is ready for a low-cost public launch using:

- **GitHub** for source control
- **Streamlit Community Cloud** for hosting
- **Supabase** for cloud submission storage

It now also supports:

- resumable **server-side draft saves**
- supporting document uploads for **PDF/JPG/PNG**
- internal **SMTP notification emails without attachments**

## What this app does

- Collects Phase 1 driver pre-qualification data
- Generates downloadable PDF copies
- Saves company copies locally or to Supabase depending on configuration

## Before you publish

Do **not** upload these local-only files to GitHub:

- `.env`
- `.streamlit/secrets.toml`
- `submissions/`
- `.venv/`

Safe templates already included in the repo:

- `.env.example`
- `.streamlit/secrets.toml.example`

## Fastest public launch path

### 1. Create the GitHub repo

Create a new GitHub repository and upload this project.

### 2. Create Supabase

In Supabase:

1. Create a new project
2. Open **Storage**
3. Create a bucket named `driver-applications`
4. Copy these values:
   - Project URL
   - Service role key

Optional:

- If you want metadata rows in a table, create a table and set `SUPABASE_TABLE`
- If not, leave `SUPABASE_TABLE` blank
- Drafts and supporting documents are stored under their own folders in the same bucket

### 3. Deploy on Streamlit Community Cloud

In Streamlit Community Cloud:

1. Click **New app**
2. Select the GitHub repository
3. Set branch to your deployment branch
4. Set main file path to `app.py`
5. Open **Advanced settings** or **Secrets**
6. Paste secrets using this template:

```toml
[app]
SUBMISSION_STORAGE_BACKEND = "auto"

[supabase]
SUPABASE_URL = "https://your-project-ref.supabase.co"
SUPABASE_SERVICE_KEY = "your-supabase-service-role-key"
SUPABASE_BUCKET = "driver-applications"
SUPABASE_TABLE = ""

[smtp]
SMTP_HOST = "smtp.your-provider.com"
SMTP_PORT = "587"
SMTP_USERNAME = "smtp-username"
SMTP_PASSWORD = "smtp-password"
SMTP_FROM_EMAIL = "alerts@your-company.com"
SMTP_USE_TLS = "true"
SMTP_USE_SSL = "false"
INTERNAL_NOTIFICATION_TO = "dispatch@your-company.com,safety@your-company.com"
```

### 4. Launch and test

After deploy:

1. Open the public Streamlit URL
2. Submit one test application
3. Confirm:
   - the app completes successfully
   - PDFs download
   - submission files, drafts, and supporting documents appear in Supabase Storage
   - internal notification email arrives without attachments (if SMTP is configured)

## Recommended settings

- Keep `SUBMISSION_STORAGE_BACKEND = "auto"` for launch
- Use `"both"` only if you also want local file saves during local development
- Do not put real secrets in GitHub
- Keep SMTP recipients limited to internal company inboxes only

## What safety/owner should review

- Company email in `config.py`
- Company wording and disclosures in the app
- Supabase project ownership
- Who should have access to submission records

## If something goes wrong

### App launches but submissions are not saved

Check:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`
- bucket name is exactly `driver-applications`
- Streamlit secrets were pasted correctly

### Uploads are rejected

Check:

- files are PDF, JPG/JPEG, or PNG
- each file is under the configured per-file limit
- `.streamlit/config.toml` is present in the repo for the deployed app

### Notification email does not arrive

Check:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_FROM_EMAIL`
- `INTERNAL_NOTIFICATION_TO`
- SMTP username/password if your provider requires authentication

### App works locally but not in the cloud

Check:

- `requirements.txt`
- `pyproject.toml`
- Streamlit main file is `app.py`
- Python version support on host

## Handoff summary

If GitHub, Supabase, and Streamlit are connected correctly, this app can be shared with drivers using a public HTTPS link and does not need to keep running on a personal computer.
