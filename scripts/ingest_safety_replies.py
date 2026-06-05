"""Scheduled runner: pull driver safety documents from email replies.

Designed to be invoked by a GitHub Actions cron (or any scheduler / cron host)
as a *separate process* from the Streamlit app. It reads the same Supabase-backed
state the app uses, so matched replies land under the right person and show up on
the ``?safety=1`` dashboard.

Required environment (set as GitHub Actions secrets):

    SUPABASE_URL, SUPABASE_SERVICE_KEY   shared storage/state
    SUPABASE_BUCKET                      optional (defaults to driver-applications)
    SAFETY_INBOX_MAILBOXES               JSON array of mailbox configs, e.g.
        [{"username": "statements@example.com", "password": "<gmail-app-pw>"}]

Usage:
    python -m scripts.ingest_safety_replies
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow ``python scripts/ingest_safety_replies.py`` as well as ``-m``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.safety_inbox import ingest_all, load_inbox_mailboxes  # noqa: E402

_SUBMISSIONS_DIR = _REPO_ROOT / "submissions"


def main() -> int:
    mailboxes = load_inbox_mailboxes()
    if not mailboxes:
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": "No mailboxes configured. Set the SAFETY_INBOX_MAILBOXES secret.",
                }
            )
        )
        return 1

    summary = ingest_all(_SUBMISSIONS_DIR, mailboxes=mailboxes)
    print(json.dumps(summary, indent=2, default=str))
    # Connection/search failures are recorded but should not crash the cron; a
    # non-zero exit is reserved for "nothing could run at all".
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
