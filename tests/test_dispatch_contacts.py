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
    assert pt["phone"] == "708-356-4427"
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
