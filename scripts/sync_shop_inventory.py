"""Scheduled runner: sync General Truck Service shop inventory from QBO.

Designed to be invoked by a GitHub Actions cron (or any scheduler) as a
*separate process* from the Streamlit app. Streamlit Community Cloud has no
scheduler, so the periodic QBO -> Supabase delta sync lives here.

It reads/writes the same Supabase-backed state the app uses, so the mobile
"Inventory List" view always reads fresh data.

Required environment (set as GitHub Actions secrets):

    SUPABASE_URL, SUPABASE_SERVICE_KEY   shared storage/state
    QBO_CLIENT_ID, QBO_CLIENT_SECRET     QBO OAuth app credentials
    QBO_ENVIRONMENT                      optional ("production" default)
    SHOP_REALM_ID                        optional; otherwise resolved by name
    SHOP_COMPANY_NAME                    optional (default "General Truck Service")

Optional flags:
    SHOP_FULL_SYNC=1     force a full re-pull (ignore the delta cursor)
    SHOP_FORCE=1         run even outside business hours (07:00-17:00 Central)

Usage:
    python -m scripts.sync_shop_inventory
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

# Allow ``python scripts/sync_shop_inventory.py`` as well as ``-m``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qbo.shop_inventory_sync import (  # noqa: E402
    is_within_business_hours,
    sync_shop_inventory,
)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    force = _truthy(os.getenv("SHOP_FORCE"))
    if not force and not is_within_business_hours():
        print(
            json.dumps(
                {
                    "status": "skipped",
                    "message": "Outside business hours (07:00-17:00 Central, Mon-Sat). "
                    "Set SHOP_FORCE=1 to override.",
                }
            )
        )
        return 0

    result = sync_shop_inventory(force_full=_truthy(os.getenv("SHOP_FULL_SYNC")))
    print(
        json.dumps(
            {
                "status": result.status,
                "mode": result.mode,
                "realm_id": result.realm_id,
                "company_name": result.company_name,
                "items_fetched": result.items_fetched,
                "items_upserted": result.items_upserted,
                "message": result.message,
                "errors": result.errors,
            },
            indent=2,
            default=str,
        )
    )
    # Reserve non-zero exit for a hard failure so the cron surfaces it.
    return 0 if result.status in {"success", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
