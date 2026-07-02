"""Company & dispatcher contact directory for the dispatch-board web UI.

Data lives in Supabase (`dispatch_company_info`, `dispatch_contact_entries`,
seeded by migration 0025). If the tables are missing or empty the built-in
seeds below are used, so the directory renders before the migration runs.
Values missing from the source phone sheet are blank and display as a dash.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

COMPANY_TABLE = "dispatch_company_info"
CONTACT_TABLE = "dispatch_contact_entries"

# Shared inbox: no dispatcher has an individual Xpress email.
XPRESS_SHARED_EMAIL = "dispatch@xpresstransinc.com"

SEED_COMPANIES: list[dict[str, Any]] = [
    {
        "division": "Prestig Inc",
        "mc_number": "MC 553373",
        "dot_number": "DOT 1454866",
        "fin_number": "FIN 20-4146962",
        "dispatch_email": "dispatch@prestige.inc",
        "company_phone": "773-303-4616",
        "setup_phone": "224-715-1371",
        "setup_contact": "Deyana Koleva",
        "address": "3810 North Ave, Stone Park, IL 60165",
        "sort_order": 1,
    },
    {
        "division": "Prestige Transportation Inc",
        "mc_number": "MC 814849",
        "dot_number": "DOT 2374229",
        "fin_number": "FIN 90-0930803",
        "dispatch_email": "dispatch@prestigecalifornia.com",
        "company_phone": "877-549-9529",
        "setup_phone": "224-545-2148",
        "setup_contact": "Lubomir Anguelov",
        "address": "8622 Hemlock Ave, Fontana, CA 92335",
        "sort_order": 2,
    },
    {
        "division": "Xpress Trans Inc",
        "mc_number": "MC 715183",
        "dot_number": "DOT 2038003",
        "fin_number": "FIN 27-2631230",
        "dispatch_email": XPRESS_SHARED_EMAIL,
        "company_phone": "224-341-6014",
        "setup_phone": "224-522-1354",
        "setup_contact": "Dayana Sheytanova / Zach",
        "address": "2905 W Lake St, Melrose Park, IL 60160",
        "sort_order": 3,
    },
]

def _entry(name: str, division: str, email: str, phone: str, extension: str, sort_order: int) -> dict[str, Any]:
    return {
        "dispatcher_name": name,
        "division": division,
        "email": email,
        "phone": phone,
        "extension": extension,
        "sort_order": sort_order,
    }

SEED_DISPATCHER_CONTACTS: list[dict[str, Any]] = [
    _entry("Anna", "Prestig Inc", "dispatch5@prestige.inc", "773-484-9982", "", 1),
    _entry("Anna", "Prestige Transportation Inc", "dispatch5@prestigecalifornia.com", "909-206-2911", "213", 2),
    _entry("Anna", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "708-356-4423", "203", 3),
    _entry("Anna", "Personal Cell (internal)", "", "773-396-7011", "", 4),
    _entry("Brittany", "Prestig Inc", "dispatch3@prestige.inc", "708-701-1109", "", 1),
    _entry("Brittany", "Prestige Transportation Inc", "dispatch3@prestigecalifornia.com", "909-206-4747", "", 2),
    _entry("Brittany", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "708-356-4421", "210", 3),
    _entry("Brittany", "Personal Cell (internal)", "", "773-440-5468", "", 4),
    _entry("Carlos IL", "Prestig Inc", "dispatch1@prestige.inc", "909-900-6411", "", 1),
    _entry("Carlos IL", "Prestige Transportation Inc", "dispatch1@prestigecalifornia.com", "909-206-5247", "215", 2),
    _entry("Carlos IL", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "708-356-4424", "214", 3),
    _entry("Carlos IL", "Personal Cell (internal)", "", "708-438-1338", "", 4),
    _entry("Carlos CA", "Prestig Inc", "dispatch4@prestige.inc", "909-552-5206", "", 1),
    _entry("Carlos CA", "Prestige Transportation Inc", "dispatch4@prestigecalifornia.com", "909-206-4536", "316", 2),
    _entry("Carlos CA", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "909-302-0186", "306", 3),
    _entry("Felix", "Prestig Inc", "dispatch7@prestige.inc", "773-726-2998", "", 1),
    _entry("Felix", "Prestige Transportation Inc", "dispatch7@prestigecalifornia.com", "909-206-2005", "217", 2),
    _entry("Felix", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "708-356-4412", "202", 3),
    _entry("Felix", "Personal Cell (internal)", "", "312-581-2803", "", 4),
    _entry("Lily", "Prestig Inc", "dispatch8@prestige.inc", "714-272-3260", "", 1),
    _entry("Lily", "Prestige Transportation Inc", "dispatch8@prestigecalifornia.com", "909-206-4365", "310", 2),
    _entry("Lily", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "909-302-0181", "301", 3),
    _entry("Lily", "Personal Cell (internal)", "", "714-272-3260", "", 4),
    _entry("Sanjuana", "Prestig Inc", "dispatch6@prestige.inc", "909-243-9167", "", 1),
    _entry("Sanjuana", "Prestige Transportation Inc", "dispatch6@prestigecalifornia.com", "909-206-2445", "315", 2),
    _entry("Sanjuana", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "909-302-0185", "305", 3),
    _entry("Matt", "Prestig Inc", "matt@prestige.inc", "", "", 1),
    _entry("Zach", "Prestig Inc", "operations@prestige.inc", "708-701-1109", "", 1),
    _entry("Zach", "Prestige Transportation Inc", "operations@prestigecalifornia.com", "224-522-1354", "", 2),
    _entry("Zach", "Xpress Trans Inc", XPRESS_SHARED_EMAIL, "224-522-1354", "", 3),
]

_COMPANY_FIELDS = [
    "division", "mc_number", "dot_number", "fin_number", "dispatch_email",
    "company_phone", "setup_phone", "setup_contact", "address", "sort_order",
]
_CONTACT_FIELDS = ["dispatcher_name", "division", "email", "phone", "extension", "sort_order"]


def _get_client():
    from services.qbo_supabase import SupabaseRestClient

    return SupabaseRestClient()


def load_company_info() -> list[dict[str, Any]]:
    """Company/division cards (MC, DOT, FIN, addresses). Falls back to seeds."""
    try:
        rows = _get_client().select_all(COMPANY_TABLE, order="sort_order.asc")
        if rows:
            return rows
    except Exception as exc:
        logger.warning("Company info table unavailable, using seeds: %s", exc)
    return [dict(row) for row in SEED_COMPANIES]


def load_dispatcher_contacts() -> list[dict[str, Any]]:
    """Flat per-dispatcher/per-division contact entries. Falls back to seeds."""
    try:
        rows = _get_client().select_all(
            CONTACT_TABLE, order="dispatcher_name.asc,sort_order.asc"
        )
        if rows:
            return rows
    except Exception as exc:
        logger.warning("Dispatcher contacts table unavailable, using seeds: %s", exc)
    return [dict(row) for row in SEED_DISPATCHER_CONTACTS]


def group_contacts_by_dispatcher(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        name = str(entry.get("dispatcher_name") or "").strip()
        if not name:
            continue
        grouped.setdefault(name, []).append(entry)
    for name in grouped:
        grouped[name].sort(key=lambda item: int(item.get("sort_order") or 0))
    return dict(sorted(grouped.items(), key=lambda pair: pair[0].lower()))


def save_dispatcher_contacts(entries: list[dict[str, Any]]) -> int:
    """Replace all dispatcher contact entries (the table is tiny)."""
    client = _get_client()
    rows = [
        {field: str(entry.get(field) or "").strip() if field != "sort_order" else int(entry.get(field) or 0)
         for field in _CONTACT_FIELDS}
        for entry in entries
        if str(entry.get("dispatcher_name") or "").strip()
    ]
    client.delete(CONTACT_TABLE, filters={"id": "gte.0"})
    if rows:
        client.insert(CONTACT_TABLE, rows)
    return len(rows)


def save_company_info(companies: list[dict[str, Any]]) -> int:
    """Replace all company/division info rows."""
    client = _get_client()
    rows = [
        {field: str(company.get(field) or "").strip() if field != "sort_order" else int(company.get(field) or 0)
         for field in _COMPANY_FIELDS}
        for company in companies
        if str(company.get("division") or "").strip()
    ]
    client.delete(COMPANY_TABLE, filters={"division": "neq."})
    if rows:
        client.upsert(COMPANY_TABLE, rows, on_conflict="division")
    return len(rows)


def email_to_dispatcher_map() -> dict[str, str]:
    """Sender email -> dispatcher name from the directory.

    Shared inboxes used by more than one dispatcher (e.g. the Xpress dispatch
    email) are excluded because they cannot identify a single sender. Intended
    to back the rate-confirmation matcher with editable data instead of a
    hardcoded table.
    """
    return build_email_to_dispatcher_map(load_dispatcher_contacts())


def build_email_to_dispatcher_map(entries: list[dict[str, Any]]) -> dict[str, str]:
    counts: dict[str, set[str]] = {}
    for entry in entries:
        email = str(entry.get("email") or "").lower().strip()
        name = str(entry.get("dispatcher_name") or "").strip()
        if email and name:
            counts.setdefault(email, set()).add(name)
    return {email: next(iter(names)) for email, names in counts.items() if len(names) == 1}
