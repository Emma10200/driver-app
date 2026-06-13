"""Mobile-first shop Inventory List view (Button 1 of the shop app).

Read-only, smartphone-optimised browser for the General Truck Service parts
catalog mirrored into ``public.shop_inventory`` by the QBO delta sync. Designed
for a non-technical shop manager: large tap targets, minimal typing, tolerant
fuzzy search, and an English/Bulgarian label toggle placeholder.

Routing: reached from ``app.py`` via ``?shop=1`` (or ``?route=inventory``).
Authentication is intentionally NOT implemented here yet (owner decision pending
- likely device-remember / phone allowlist rather than Google SSO).
"""

from __future__ import annotations

import logging
from typing import Any

import streamlit as st

from qbo.shop_inventory_sync import (
    build_services,
    resolve_shop_realm_id,
    sync_shop_inventory,
)
from services.qbo_supabase import SupabaseRestClient

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 50
_SEARCH_CACHE_TTL = 60  # seconds
_REALM_CACHE_TTL = 600  # seconds

# Minimal UI string table. Full Bulgarian translation is a follow-up; this gets
# the label toggle wired so the shop manager sees familiar words on key labels.
_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Shop Inventory",
        "search_label": "Search parts (name, SKU, or description)",
        "search_placeholder": "e.g. brake pad, 12345, filter…",
        "in_stock": "In stock",
        "no_qty": "Not tracked",
        "sku": "SKU",
        "price": "Price",
        "cost": "Cost",
        "no_results": "No parts found. Try a shorter or different word.",
        "type_to_search": "Start typing to search the parts catalog.",
        "showing": "Showing",
        "results": "parts",
        "updated": "Inventory last updated",
        "never": "not yet synced",
        "lang_toggle": "Език / Language",
        "refresh": "Refresh inventory",
        "refreshing": "Checking QuickBooks for changes…",
        "refresh_done": "Inventory updated",
        "refresh_none": "Already up to date",
        "refresh_failed": "Refresh failed",
        "not_connected": "The shop QuickBooks company is not connected yet. "
        "Ask accounting to connect it, then the inventory will appear here.",
        "load_error": "Could not load inventory right now. Please try again shortly.",
    },
    "bg": {
        "title": "Складова наличност",
        "search_label": "Търсене на части (име, SKU или описание)",
        "search_placeholder": "напр. накладки, 12345, филтър…",
        "in_stock": "Наличност",
        "no_qty": "Не се следи",
        "sku": "SKU",
        "price": "Цена",
        "cost": "Себестойност",
        "no_results": "Няма намерени части. Опитайте по-кратка или друга дума.",
        "type_to_search": "Започнете да пишете, за да търсите в каталога.",
        "showing": "Показани",
        "results": "части",
        "updated": "Последно обновяване",
        "never": "още не е синхронизирано",
        "lang_toggle": "Език / Language",
        "refresh": "Обнови наличността",
        "refreshing": "Проверка за промени в QuickBooks…",
        "refresh_done": "Наличността е обновена",
        "refresh_none": "Вече е актуална",
        "refresh_failed": "Обновяването не успя",
        "not_connected": "Фирмата в QuickBooks все още не е свързана. "
        "Помолете счетоводството да я свърже и наличността ще се появи тук.",
        "load_error": "Наличността не може да се зареди в момента. Опитайте отново.",
    },
}

_MOBILE_CSS = """
<style>
  /* Tighten the page for phones and make everything big and tappable. */
  .block-container { padding-top: 1rem; padding-bottom: 4rem; max-width: 720px; }
  .shop-title { font-size: 1.9rem; font-weight: 800; margin: 0.2rem 0 0.6rem; }
  /* Large search box. */
  div[data-testid="stTextInput"] input {
      font-size: 1.25rem !important;
      padding: 0.85rem 0.9rem !important;
      height: 3.25rem !important;
  }
  /* Part cards: big legible rows with clear separation. */
  .part-card {
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 14px;
      padding: 0.9rem 1rem;
      margin: 0.55rem 0;
      background: rgba(255,255,255,0.03);
  }
  .part-name { font-size: 1.3rem; font-weight: 700; line-height: 1.25; }
  .part-meta { font-size: 1.02rem; color: #cfcfcf; margin-top: 0.25rem; }
  .part-badges { margin-top: 0.55rem; display: flex; flex-wrap: wrap; gap: 0.4rem; }
  .badge {
      display: inline-block; font-size: 1.0rem; font-weight: 600;
      padding: 0.3rem 0.7rem; border-radius: 999px;
  }
  .badge-stock { background: rgba(52,199,89,0.18); color: #8ef0a6; }
  .badge-stock-zero { background: rgba(255,69,58,0.18); color: #ff938c; }
  .badge-untracked { background: rgba(142,142,147,0.22); color: #d0d0d2; }
  .badge-price { background: rgba(10,132,255,0.18); color: #8fc3ff; }
  .badge-cost { background: rgba(175,82,222,0.18); color: #d3a4f0; }
  .badge-sku { background: rgba(255,214,10,0.16); color: #ffe27a; }
</style>
"""


def _t(lang: str, key: str) -> str:
    table = _STRINGS.get(lang) or _STRINGS["en"]
    return table.get(key) or _STRINGS["en"].get(key, key)


@st.cache_data(ttl=_REALM_CACHE_TTL, show_spinner=False)
def _cached_shop_realm_id() -> str:
    _, token_repo, _ = build_services()
    return resolve_shop_realm_id(token_repo)


@st.cache_data(ttl=_SEARCH_CACHE_TTL, show_spinner=False)
def _search_inventory(realm_id: str, term: str, limit: int) -> list[dict[str, Any]]:
    supabase = SupabaseRestClient()
    payload = supabase.rpc(
        "shop_inventory_search",
        {
            "p_realm_id": realm_id,
            "p_term": term,
            "p_limit": limit,
            "p_active_only": True,
        },
    )
    return payload if isinstance(payload, list) else []


@st.cache_data(ttl=_SEARCH_CACHE_TTL, show_spinner=False)
def _last_synced(realm_id: str) -> str:
    supabase = SupabaseRestClient()
    rows = supabase.select(
        "shop_inventory_sync_state",
        select="last_run_at,last_run_status",
        filters={"realm_id": f"eq.{realm_id}"},
        limit=1,
    )
    if not rows:
        return ""
    return str(rows[0].get("last_run_at") or "")


def _fmt_price(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return ""


def _fmt_qty(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return f"{int(number)}" if number == int(number) else f"{number:g}"


def _render_part_card(item: dict[str, Any], lang: str) -> None:
    name = str(item.get("fully_qualified_name") or item.get("name") or "").strip() or "—"
    sku = str(item.get("sku") or "").strip()
    description = str(
        item.get("sales_description") or item.get("purchase_description") or ""
    ).strip()
    qty = _fmt_qty(item.get("qty_on_hand"))
    price = _fmt_price(item.get("sales_price"))
    cost = _fmt_price(item.get("purchase_cost"))

    badges: list[str] = []
    if qty is None:
        badges.append(f"<span class='badge badge-untracked'>{_t(lang, 'no_qty')}</span>")
    elif float(item.get("qty_on_hand") or 0) <= 0:
        badges.append(
            f"<span class='badge badge-stock-zero'>{_t(lang, 'in_stock')}: {qty}</span>"
        )
    else:
        badges.append(
            f"<span class='badge badge-stock'>{_t(lang, 'in_stock')}: {qty}</span>"
        )
    if price:
        badges.append(f"<span class='badge badge-price'>{_t(lang, 'price')}: {price}</span>")
    if cost:
        badges.append(f"<span class='badge badge-cost'>{_t(lang, 'cost')}: {cost}</span>")
    if sku:
        badges.append(f"<span class='badge badge-sku'>{_t(lang, 'sku')}: {_escape(sku)}</span>")

    meta_html = f"<div class='part-meta'>{_escape(description)}</div>" if description else ""
    st.markdown(
        f"""
        <div class='part-card'>
            <div class='part-name'>{_escape(name)}</div>
            {meta_html}
            <div class='part-badges'>{''.join(badges)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _run_refresh(realm_id: str, lang: str) -> None:
    """Run the same delta sync the scheduler would, then refresh the view.

    Pulls only QBO items changed since the last sync (full pull on first run),
    upserts them into Supabase, clears the cached reads so the list shows fresh
    data, and flashes a short status. Failures are surfaced without crashing.
    """
    with st.spinner(_t(lang, "refreshing")):
        result = sync_shop_inventory(realm_id)

    # Invalidate cached reads so the list + freshness stamp reflect the sync.
    _search_inventory.clear()
    _last_synced.clear()

    if result.status == "success":
        if result.items_upserted:
            st.session_state["shop_refresh_flash"] = (
                "success",
                f"{_t(lang, 'refresh_done')} (+{result.items_upserted})",
            )
        else:
            st.session_state["shop_refresh_flash"] = ("info", _t(lang, "refresh_none"))
    else:
        logger.error("Manual shop inventory refresh failed: %s", result.message)
        st.session_state["shop_refresh_flash"] = (
            "error",
            f"{_t(lang, 'refresh_failed')}: {result.message}",
        )
    st.rerun()


def _show_refresh_flash() -> None:
    flash = st.session_state.pop("shop_refresh_flash", None)
    if not flash:
        return
    level, message = flash
    {"success": st.success, "info": st.info, "error": st.error}.get(level, st.info)(message)


def render_shop_inventory_page() -> None:
    """Render the mobile shop inventory list. Public-by-link for now."""
    st.markdown(_MOBILE_CSS, unsafe_allow_html=True)

    lang = st.session_state.get("shop_lang", "en")
    header_col, lang_col = st.columns([3, 1])
    with header_col:
        st.markdown(f"<div class='shop-title'>🔧 {_t(lang, 'title')}</div>", unsafe_allow_html=True)
    with lang_col:
        bulgarian = st.toggle("БГ", value=(lang == "bg"), help=_t(lang, "lang_toggle"))
        new_lang = "bg" if bulgarian else "en"
        if new_lang != lang:
            st.session_state["shop_lang"] = new_lang
            st.rerun()

    try:
        realm_id = _cached_shop_realm_id()
    except Exception as exc:  # noqa: BLE001 - shop company not connected yet
        logger.warning("Shop inventory realm unavailable: %s", exc)
        st.info(_t(lang, "not_connected"))
        return

    term = st.text_input(
        _t(lang, "search_label"),
        value="",
        placeholder=_t(lang, "search_placeholder"),
        label_visibility="collapsed",
    ).strip()

    _show_refresh_flash()
    if st.button(f"\U0001f504 {_t(lang, 'refresh')}", use_container_width=True):
        _run_refresh(realm_id, lang)

    try:
        items = _search_inventory(realm_id, term, _SEARCH_LIMIT)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Shop inventory search failed: %s", exc)
        st.error(_t(lang, "load_error"))
        return

    last_run = _last_synced(realm_id)
    freshness = last_run.replace("T", " ")[:16] if last_run else _t(lang, "never")
    st.caption(f"{_t(lang, 'updated')}: {freshness}")

    if not term and not items:
        st.info(_t(lang, "type_to_search"))
        return
    if not items:
        st.warning(_t(lang, "no_results"))
        return

    st.caption(f"{_t(lang, 'showing')} {len(items)} {_t(lang, 'results')}")
    for item in items:
        _render_part_card(item, lang)
