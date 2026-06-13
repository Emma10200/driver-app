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
from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)

_SEARCH_LIMIT = 50
_SEARCH_CACHE_TTL = 60  # seconds
_REALM_CACHE_TTL = 600  # seconds
_DEFAULT_SHOP_APP_URL = "https://driver-application.streamlit.app/?shop=1"

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
        "open_app": "Open in app",
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
        "open_app": "Отвори в приложението",
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
  /* Neutralise Streamlit's top toolbar so our title is never clipped, and give
     the page a clean, generous top margin for an older user on a phone. */
  header[data-testid="stHeader"] { background: transparent; height: 0; }
  div[data-testid="stToolbar"] { display: none; }
  .block-container {
      padding-top: 2.75rem;
      padding-bottom: 4rem;
      max-width: 680px;
  }

  /* Title: calm, high-contrast, plenty of breathing room. */
  .shop-title {
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.01em;
      margin: 0.25rem 0 0.15rem;
      color: #1f2933;
  }

  /* Large, clean search box. */
  div[data-testid="stTextInput"] input {
      font-size: 1.2rem !important;
      padding: 0.8rem 0.95rem !important;
      height: 3.2rem !important;
      border-radius: 12px !important;
      border: 1px solid #d4dae0 !important;
      background: #ffffff !important;
      color: #1f2933 !important;
  }
  div[data-testid="stTextInput"] input::placeholder { color: #9aa5b1 !important; }

  /* Buttons: flat, professional, full-width and easy to tap. */
  div[data-testid="stButton"] > button,
  div[data-testid="stLinkButton"] > a {
      border-radius: 12px !important;
      font-size: 1.08rem !important;
      font-weight: 600 !important;
      padding: 0.7rem 1rem !important;
      min-height: 3rem !important;
  }

  /* Part cards: clean white "paper" rows with a soft border + subtle shadow. */
  .part-card {
      border: 1px solid #e4e8ec;
      border-radius: 14px;
      padding: 0.95rem 1.05rem;
      margin: 0.6rem 0;
      background: #ffffff;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
  }
  .part-name {
      font-size: 1.22rem;
      font-weight: 650;
      line-height: 1.3;
      color: #1f2933;
  }
  .part-meta { font-size: 1.0rem; color: #616e7c; margin-top: 0.3rem; line-height: 1.35; }
  .part-badges { margin-top: 0.65rem; display: flex; flex-wrap: wrap; gap: 0.4rem; }
  .badge {
      display: inline-block;
      font-size: 0.95rem;
      font-weight: 600;
      padding: 0.32rem 0.7rem;
      border-radius: 8px;
      border: 1px solid transparent;
  }
  /* Muted, professional palette - soft tinted fills with dark, legible text. */
  .badge-stock { background: #e8f5ed; color: #1b7a43; border-color: #cdead8; }
  .badge-stock-zero { background: #fdecea; color: #b42318; border-color: #f7d4cf; }
  .badge-untracked { background: #f2f4f7; color: #4b5563; border-color: #e4e7ec; }
  .badge-price { background: #eaf1fb; color: #1a55b0; border-color: #d3e2f6; }
  .badge-cost { background: #f0ecf9; color: #5b3da6; border-color: #e0d8f2; }
  .badge-sku { background: #f5f1e6; color: #7a5c12; border-color: #ece3cd; }

  /* Freshness caption + section captions: quiet and unobtrusive. */
  div[data-testid="stCaptionContainer"] { color: #8793a1 !important; }
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


def _shop_app_url() -> str:
    """Deep link to this page inside the Streamlit mobile app.

    Reads SHOP_APP_URL (secret/env) so the deployed host can be overridden;
    falls back to the known public URL with the ?shop=1 route.
    """
    return (get_runtime_secret("SHOP_APP_URL", _DEFAULT_SHOP_APP_URL) or _DEFAULT_SHOP_APP_URL).strip()


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

    # Live search: the keyed text input reruns as the value changes, so the list
    # filters without pressing a separate Search button.
    term = st.text_input(
        _t(lang, "search_label"),
        key="shop_search_term",
        placeholder=_t(lang, "search_placeholder"),
        label_visibility="collapsed",
    ).strip()

    _show_refresh_flash()
    refresh_col, open_col = st.columns(2)
    with refresh_col:
        if st.button(f"\U0001f504 {_t(lang, 'refresh')}", use_container_width=True):
            _run_refresh(realm_id, lang)
    with open_col:
        st.link_button(
            f"\U0001f4f1 {_t(lang, 'open_app')}",
            _shop_app_url(),
            use_container_width=True,
        )

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
