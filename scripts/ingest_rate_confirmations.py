"""Scheduled/manual runner for read-only rate-confirmation email ingest.

Required secrets/env:
    RATE_CONF_EMAIL
    RATE_CONF_APP_PASSWORD
    SUPABASE_URL
    SUPABASE_SERVICE_KEY

Optional:
    RATE_CONF_IMAP_HOST      default imap.gmail.com
    RATE_CONF_IMAP_PORT      default 993
    RATE_CONF_MAILBOX_FOLDER default INBOX

Examples:
    python scripts/ingest_rate_confirmations.py --days 14 --dry-run
    python scripts/ingest_rate_confirmations.py --days 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.rate_confirmation_ingest import ingest_recent_rate_confirmations  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest recent rate-confirmation emails read-only.")
    parser.add_argument("--days", type=int, default=14, help="How many recent days to scan.")
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on newest messages fetched.")
    parser.add_argument("--dry-run", action="store_true", help="Analyze and summarize without writing Supabase rows.")
    args = parser.parse_args()

    summary = ingest_recent_rate_confirmations(days=args.days, limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
