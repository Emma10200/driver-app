from __future__ import annotations

from services.dispatch_contacts import (
    SEED_COMPANIES,
    SEED_DISPATCHER_CONTACTS,
    XPRESS_SHARED_EMAIL,
    build_email_to_dispatcher_map,
    group_contacts_by_dispatcher,
)


def test_felix_seed_matches_expected_directory_format() -> None:
    grouped = group_contacts_by_dispatcher(SEED_DISPATCHER_CONTACTS)
    felix = grouped["Felix"]

    prestig = felix[0]
    assert prestig["division"] == "Prestig Inc"
    assert prestig["email"] == "dispatch7@prestige.inc"
    assert prestig["phone"] == "773-726-2998"
    assert prestig["extension"] == ""

    pt = felix[1]
    assert pt["division"] == "Prestige Transportation Inc"
    assert pt["email"] == "dispatch7@prestigecalifornia.com"
    assert pt["phone"] == "909-206-2005"
    assert pt["extension"] == "217"

    xpress = felix[2]
    assert xpress["division"] == "Xpress Trans Inc"
    assert xpress["email"] == XPRESS_SHARED_EMAIL
    assert xpress["phone"] == "708-356-4412"
    assert xpress["extension"] == "202"


def test_every_dispatcher_shares_the_xpress_inbox() -> None:
    """No dispatcher has an individual Xpress email — all use the shared one."""
    for entry in SEED_DISPATCHER_CONTACTS:
        if entry["division"] == "Xpress Trans Inc":
            assert entry["email"] == XPRESS_SHARED_EMAIL


def test_shared_xpress_email_excluded_from_dispatcher_map() -> None:
    mapping = build_email_to_dispatcher_map(SEED_DISPATCHER_CONTACTS)

    assert XPRESS_SHARED_EMAIL not in mapping
    assert mapping["dispatch7@prestige.inc"] == "Felix"
    assert mapping["dispatch1@prestigecalifornia.com"] == "Carlos IL"
    assert mapping["matt@prestige.inc"] == "Matt"
    assert mapping["operations@prestige.inc"] == "Zach"
    assert mapping["operations@prestigecalifornia.com"] == "Zach"


def test_contact_seed_includes_zach_operations_contacts_but_not_art_or_arc() -> None:
    grouped = group_contacts_by_dispatcher(SEED_DISPATCHER_CONTACTS)

    assert "Zach" in grouped
    assert grouped["Zach"] == [
        {
            "dispatcher_name": "Zach",
            "division": "Prestig Inc",
            "email": "operations@prestige.inc",
            "phone": "708-701-1109",
            "extension": "",
            "sort_order": 1,
        },
        {
            "dispatcher_name": "Zach",
            "division": "Prestige Transportation Inc",
            "email": "operations@prestigecalifornia.com",
            "phone": "224-522-1354",
            "extension": "",
            "sort_order": 2,
        },
        {
            "dispatcher_name": "Zach",
            "division": "Xpress Trans Inc",
            "email": XPRESS_SHARED_EMAIL,
            "phone": "224-522-1354",
            "extension": "",
            "sort_order": 3,
        },
    ]

    normalized_names = {name.strip().lower() for name in grouped}
    assert "art" not in normalized_names
    assert "arc" not in normalized_names
    assert "zack" not in normalized_names
    assert "sanjuana" not in normalized_names
    assert "san juana" not in normalized_names


def test_pasted_sheet_phone_updates_are_reflected_in_seed() -> None:
    grouped = group_contacts_by_dispatcher(SEED_DISPATCHER_CONTACTS)

    assert grouped["Carlos IL"][0]["phone"] == "909-900-6411"
    assert grouped["Carlos IL"][1]["phone"] == "909-206-5247"
    assert grouped["Brittany"][0]["phone"] == "708-701-1109"
    assert grouped["Brittany"][1]["phone"] == "909-206-4747"
    assert grouped["Anna"][1]["phone"] == "909-206-2911"
    assert grouped["Lily"][1]["phone"] == "909-206-4365"


def test_company_seeds_have_mc_dot_fin() -> None:
    assert {c["division"] for c in SEED_COMPANIES} == {
        "Prestig Inc",
        "Prestige Transportation Inc",
        "Xpress Trans Inc",
    }
    for company in SEED_COMPANIES:
        assert company["mc_number"].startswith("MC ")
        assert company["dot_number"].startswith("DOT ")
        assert company["fin_number"].startswith("FIN ")

    xpress = next(c for c in SEED_COMPANIES if c["division"] == "Xpress Trans Inc")
    assert xpress["setup_contact"] == "Dayana Sheytanova / Zach"
