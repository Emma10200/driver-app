# Prestige Transportation Inc. - Driver Application Portal

Streamlit application for Phase 1 driver pre-qualification.

## Compliance Features
- **No criminal history questions** — California Fair Chance Act (AB 1008)
- **FCRA disclosure as standalone page** — 15 U.S.C. § 1681b
- **Independent contractor language only** — no W-2/employee terminology
- **DOT-specific disqualification questions** — 49 CFR 391.15
- **Separate PSP and Clearinghouse disclosures**

## Setup

```bash
pip install -r requirements.txt
```

## Storage modes

The app supports three submission storage modes:

- `auto` — use Supabase if configured, otherwise fall back to local `submissions/`
- `local` — always save to the local `submissions/` folder
- `supabase` — always save to Supabase Storage
- `both` — save to both local storage and Supabase

Configuration can be supplied with either:

- local `.env` values for development, or
- Streamlit Community Cloud secrets for deployment

See `.env` and `.streamlit/secrets.toml.example` for the expected keys.

## Run

```bash
streamlit run app.py
```

## Free public deployment path

The fastest $0 starter setup is:

1. **Host the app on Streamlit Community Cloud**
	- public URL
	- HTTPS included
	- free starter hosting
2. **Store submissions in Supabase Storage**
	- persistent cloud storage instead of local files
	- free tier for small/early usage

### Deploy on Streamlit Community Cloud

1. Push this project to a GitHub repository.
2. Create a Supabase project.
3. In Supabase Storage, create a bucket named `driver-applications`.
4. Copy `.streamlit/secrets.toml.example` into your app secrets in Streamlit Community Cloud and fill in your real values.
5. In Streamlit Community Cloud, create a new app and choose:
	- repository: your GitHub repo
	- branch: your deployment branch
	- main file path: `app.py`

### Recommended Streamlit secrets

```toml
[app]
SUBMISSION_STORAGE_BACKEND = "auto"

[supabase]
SUPABASE_URL = "https://your-project-ref.supabase.co"
SUPABASE_SERVICE_KEY = "your-supabase-service-role-key"
SUPABASE_BUCKET = "driver-applications"
SUPABASE_TABLE = ""
```

### Notes on cost

- **Streamlit Community Cloud:** free starter tier
- **Supabase:** free starter tier
- **Custom domain / high volume / transactional email:** may cost extra later

So yes — you can get this live for real-world testing without paying upfront, as long as your usage stays within the free-tier limits.

## Project Structure

| File | Purpose |
|------|---------|
| `app.py` | Main Streamlit application (10-page form) |
| `pdf_generator.py` | PDF generation for application + standalone disclosures |
| `config.py` | Company info, constants, dropdown options |
| `submission_storage.py` | Local/Supabase submission saving helpers via HTTPS |
| `requirements.txt` | Python dependencies |

## Application Flow

1. Personal Information
2. Company Questions & Driving Experience
3. Licenses & Endorsements
4. Employment History (10 Years — 49 CFR 391.21)
5. Education & Trucking School
6. FMCSR Disqualifications & Records
7. Certifications & Digital Signature
8. FCRA Disclosure (Standalone)
9. PSP Disclosure (Standalone)
10. Clearinghouse Release (Standalone)

After submission, the applicant can download:
- Full application PDF
- Standalone FCRA Disclosure PDF
- Standalone PSP Disclosure PDF
- Standalone Clearinghouse Release PDF

The app also stores a company copy of each submission either locally or in Supabase, depending on configuration.
