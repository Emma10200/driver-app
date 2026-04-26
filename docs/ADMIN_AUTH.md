# Admin dashboard authentication

The public driver application does not require login. Only the hidden admin dashboard route (`?dashboard=1`) is protected by this setup.

## Current rollout plan

1. Deploy the no-hardcoded-password patch.
2. Use `ADMIN_AUTH_MODE = "both"` while Google SSO is being tested.
3. Add your Gmail address to `ADMIN_ALLOWED_EMAILS`.
4. Create a Google OAuth Web application client and paste the values into Streamlit Secrets.
5. After Google login works, change `ADMIN_AUTH_MODE = "google"`.

If admin auth is misconfigured, the admin dashboard can be unavailable, but the normal driver application pages should continue to run.

## Auth modes

Set this in Streamlit Secrets under `[app]` or in local `.env`:

- `ADMIN_AUTH_MODE = "both"` — Google SSO or password fallback. Recommended during rollout.
- `ADMIN_AUTH_MODE = "google"` — Google SSO only. Recommended after testing.
- `ADMIN_AUTH_MODE = "password"` — password only.
- `ADMIN_AUTH_MODE = "disabled"` — dashboard disabled.

There is no hardcoded password fallback. If `ADMIN_PASSWORD` is blank, password login is unavailable.

## Required app secrets

```toml
[app]
ADMIN_AUTH_MODE = "both"
ADMIN_ALLOWED_EMAILS = "owner@gmail.com,safety@gmail.com"
ADMIN_PASSWORD = "optional-long-random-password-for-rollout-only"
```

For Google-only mode, `ADMIN_PASSWORD` can be omitted or blank.

## Google OAuth secrets

These come from a Google Cloud OAuth **Web application** client. They are separate from the Google Sheets service account.

```toml
[auth]
redirect_uri = "https://your-app.streamlit.app/oauth2callback"
cookie_secret = "generate-a-long-random-string"

[auth.google]
client_id = "your-google-oauth-client-id.apps.googleusercontent.com"
client_secret = "your-google-oauth-client-secret"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
```

For local testing, use this redirect URI instead:

```toml
redirect_uri = "http://localhost:8501/oauth2callback"
```

If you need both local and deployed testing, add both redirect URIs in the Google Cloud OAuth client, then keep the currently running app's matching `redirect_uri` in Streamlit Secrets or local `.streamlit/secrets.toml`.

## Google Cloud setup checklist

1. Open Google Cloud Console for the existing project.
2. Go to **APIs & Services** > **OAuth consent screen**.
3. Configure the consent screen. For a tiny internal/admin tool, testing mode is fine while you are setting this up.
4. Add your admin Gmail account as a test user if the consent screen is in testing mode.
5. Go to **APIs & Services** > **Credentials**.
6. Create **OAuth client ID**.
7. Application type: **Web application**.
8. Add authorized redirect URI:
   - `https://your-app.streamlit.app/oauth2callback`
   - Optional local URI: `http://localhost:8501/oauth2callback`
9. Copy the generated client ID and client secret into Streamlit Secrets.
10. Put your allowed admin Gmail addresses in `ADMIN_ALLOWED_EMAILS`.

## Security notes

- Treat the old hardcoded password as permanently compromised because it existed in public Git history.
- Do not commit `.env` or `.streamlit/secrets.toml`.
- Use `ADMIN_AUTH_MODE = "google"` once Google sign-in is verified.
- Keep `ADMIN_ALLOWED_EMAILS` explicit; do not rely on just "any Google account can sign in."
- OAuth sign-in volume for this admin dashboard should be tiny; this should not be meaningful from a quota/cost perspective for normal Google OAuth usage.
