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
from qbo.shop_invoices import fetch_recent_invoices, next_invoice_number
from services.qbo_supabase import SupabaseRestClient
from services.shop_invoice_queue import list_recent_drafts, submit_invoice_draft
from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)

_PAGE_SIZE = 250  # how many cards on first load and per "Show more"
_MAX_RESULTS = 2500  # hard ceiling so a runaway list can't lock up the phone
_SEARCH_CACHE_TTL = 60  # seconds
_REALM_CACHE_TTL = 600  # seconds
_INVOICE_CACHE_TTL = 120  # seconds
_DEFAULT_SHOP_APP_URL = "https://driver-application.streamlit.app/?shop=1"
_SHOP_BUILD_LABEL = "Shop app build 2026-06-13.2"

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
        "of": "of",
        "show_more": "Show more parts",
        "add_hint": "Add to invoice (coming soon)",
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
        # Home / navigation.
        "home_title": "Shop Menu",
        "home_subtitle": "Tap a button to begin",
        "menu": "Menu",
        "nav_home": "Home",
        "back_to_menu": "Back to menu",
        "card_inventory": "Inventory",
        "card_inventory_desc": "Look up parts, SKUs and stock",
        "card_new_invoice": "New Invoice",
        "card_new_invoice_desc": "Start an invoice for a job",
        "card_history": "Invoice History",
        "card_history_desc": "See past invoices",
        "card_scan": "Scan Document",
        "card_scan_desc": "Snap a part to find it",
        "coming_soon": "Coming soon",
        # Invoice history.
        "history_title": "Invoice History",
        "history_loading": "Loading invoices…",
        "history_empty": "No invoices yet.",
        "history_error": "Could not load invoices right now. Please try again shortly.",
        "invoice_no": "Invoice",
        "invoice_total": "Total",
        "invoice_balance": "Balance",
        "invoice_paid": "Paid",
        "next_invoice_hint": "Next invoice number will be",
        # New invoice / cart.
        "add": "Add",
        "add_to_invoice": "Add to invoice",
        "create_new_invoice": "Start new invoice",
        "add_to_current": "Add to current invoice",
        "added_toast": "Added to invoice",
        "cart_title": "Current Invoice",
        "cart_empty": "No parts added yet. Search and tap + to add parts.",
        "cart_search": "Search parts to add",
        "qty": "Qty",
        "remove": "Remove",
        "customer": "Customer / Company",
        "truck_unit": "Truck / Unit #",
        "notes": "Notes",
        "invoice_total_label": "Invoice total",
        "finish_invoice": "Finish invoice",
        "finish_help": "Sends this invoice to accounting for review. It is NOT final yet.",
        "finish_ok": "Invoice sent to accounting for review.",
        "finish_err": "Could not submit the invoice. Please try again.",
        "clear_invoice": "Clear",
        "recent_drafts": "Recently submitted",
        # Auth.
        "login_title": "Shop Login",
        "login_user": "Username",
        "login_pass": "Password",
        "login_btn": "Sign in",
        "login_err": "Wrong username or password.",
        "logout": "Sign out",
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
        "of": "от",
        "show_more": "Покажи още части",
        "add_hint": "Добави към фактура (скоро)",
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
        # Home / navigation.
        "home_title": "Меню",
        "home_subtitle": "Натиснете бутон, за да започнете",
        "menu": "Меню",
        "nav_home": "Начало",
        "back_to_menu": "Назад към менюто",
        "card_inventory": "Наличност",
        "card_inventory_desc": "Търсене на части, SKU и наличност",
        "card_new_invoice": "Нова фактура",
        "card_new_invoice_desc": "Започни фактура за работа",
        "card_history": "История на фактурите",
        "card_history_desc": "Виж минали фактури",
        "card_scan": "Сканирай документ",
        "card_scan_desc": "Снимай част, за да я намериш",
        "coming_soon": "Очаквайте скоро",
        # Invoice history.
        "history_title": "История на фактурите",
        "history_loading": "Зареждане на фактури…",
        "history_empty": "Все още няма фактури.",
        "history_error": "Фактурите не могат да се заредят сега. Опитайте отново.",
        "invoice_no": "Фактура",
        "invoice_total": "Общо",
        "invoice_balance": "Остатък",
        "invoice_paid": "Платена",
        "next_invoice_hint": "Следващ номер на фактура ще бъде",
        # New invoice / cart.
        "add": "Добави",
        "add_to_invoice": "Добави към фактура",
        "create_new_invoice": "Започни нова фактура",
        "add_to_current": "Добави към текущата фактура",
        "added_toast": "Добавено към фактурата",
        "cart_title": "Текуща фактура",
        "cart_empty": "Още няма добавени части. Търсете и натиснете +, за да добавите.",
        "cart_search": "Търсете части за добавяне",
        "qty": "Кол.",
        "remove": "Премахни",
        "customer": "Клиент / Фирма",
        "truck_unit": "Камион / Номер",
        "notes": "Бележки",
        "invoice_total_label": "Обща сума",
        "finish_invoice": "Завърши фактурата",
        "finish_help": "Изпраща фактурата към счетоводството за преглед. ОЩЕ НЕ е окончателна.",
        "finish_ok": "Фактурата е изпратена към счетоводството.",
        "finish_err": "Фактурата не може да бъде изпратена. Опитайте отново.",
        "clear_invoice": "Изчисти",
        "recent_drafts": "Наскоро изпратени",
        # Auth.
        "login_title": "Вход за сервиз",
        "login_user": "Потребител",
        "login_pass": "Парола",
        "login_btn": "Влез",
        "login_err": "Грешен потребител или парола.",
        "logout": "Изход",
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
  /* Tier 1: part number + SKU share a prominent header row. */
  .part-header {
      display: flex;
      flex-wrap: nowrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 0.6rem;
  }
  .part-head-main {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.5rem 0.6rem;
      min-width: 0;
  }
  .part-sku {
      font-size: 1.05rem;
      font-weight: 800;
      letter-spacing: 0.01em;
      color: #1a55b0;
      background: #eaf1fb;
      border: 1px solid #d3e2f6;
      border-radius: 8px;
      padding: 0.18rem 0.55rem;
      white-space: nowrap;
  }
  /* Placeholder "add to invoice" affordance (not wired yet). */
  .part-add {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 2.1rem;
      height: 2.1rem;
      border-radius: 999px;
      font-size: 1.5rem;
      font-weight: 600;
      line-height: 1;
      color: #2f7a48;
      background: #e8f5ed;
      border: 1px solid #cdead8;
      cursor: pointer;
      user-select: none;
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

  /* Make the collapsed sidebar control obvious for a non-technical user:
     a clear gray rounded square with a visible menu arrow. */
  div[data-testid="stSidebarCollapsedControl"] button,
  button[data-testid="stSidebarCollapseButton"],
  [data-testid="collapsedControl"] {
      background: #eceff3 !important;
      border: 1px solid #cfd6de !important;
      border-radius: 10px !important;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.08) !important;
  }

  /* Home menu header. */
  .home-title { font-size: 1.9rem; font-weight: 800; color: #1f2933; margin: 0.25rem 0 0.1rem; }
  .home-sub { font-size: 1.05rem; color: #616e7c; margin: 0 0 0.6rem; }

  /* Home menu cards: large tap targets with an icon, title and helper line. */
  .home-card {
      display: flex;
      align-items: center;
      gap: 0.9rem;
      border: 1px solid #e4e8ec;
      border-radius: 16px;
      padding: 1.1rem 1.15rem;
      margin: 0.55rem 0;
      background: #ffffff;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
  }
  .home-card-icon {
      flex: 0 0 auto;
      width: 3.1rem; height: 3.1rem;
      display: flex; align-items: center; justify-content: center;
      font-size: 1.7rem;
      border-radius: 12px;
      background: #eef2f7;
  }
  .home-card-body { min-width: 0; }
  .home-card-title { font-size: 1.3rem; font-weight: 750; color: #1f2933; line-height: 1.2; }
  .home-card-desc { font-size: 1.0rem; color: #6b7682; margin-top: 0.15rem; }
  .home-card-soon {
      margin-left: auto; flex: 0 0 auto;
      font-size: 0.8rem; font-weight: 700; letter-spacing: 0.02em;
      color: #8a6d1a; background: #f7efd6; border: 1px solid #ecdcae;
      padding: 0.2rem 0.5rem; border-radius: 999px; text-transform: uppercase;
  }

  /* Invoice history rows. */
  .inv-card {
      border: 1px solid #e4e8ec; border-radius: 14px;
      padding: 0.85rem 1.05rem; margin: 0.5rem 0;
      background: #ffffff; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
  }
  .inv-top { display: flex; justify-content: space-between; align-items: baseline; gap: 0.6rem; }
  .inv-no { font-size: 1.2rem; font-weight: 750; color: #1f2933; }
  .inv-date { font-size: 0.98rem; color: #6b7682; white-space: nowrap; }
  .inv-customer { font-size: 1.02rem; color: #3a4652; margin-top: 0.25rem; }
  .inv-amounts { margin-top: 0.5rem; display: flex; flex-wrap: wrap; gap: 0.4rem; }
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


def _card_html(item: dict[str, Any], lang: str) -> str:
    """Build one part card as a single flat HTML string.

    Returns HTML (rather than rendering) so the caller can join many cards into
    a single ``st.markdown`` call - far faster than one markdown call per card
    when showing hundreds of rows.
    """
    name = str(item.get("fully_qualified_name") or item.get("name") or "").strip() or "—"
    sku = str(item.get("sku") or "").strip()
    description = str(
        item.get("sales_description") or item.get("purchase_description") or ""
    ).strip()
    qty = _fmt_qty(item.get("qty_on_hand"))
    price = _fmt_price(item.get("sales_price"))
    cost = _fmt_price(item.get("purchase_cost"))

    # Tier 1 (most prominent): part number (QBO item name) + SKU shelf reference,
    # with a placeholder "+" affordance for the future "add to invoice" flow.
    sku_html = (
        f"<span class='part-sku'>{_t(lang, 'sku')} {_escape(sku)}</span>" if sku else ""
    )
    add_html = (
        f"<span class='part-add' title='{_escape(_t(lang, 'add_hint'))}'>+</span>"
    )
    header_html = (
        f"<div class='part-header'>"
        f"<span class='part-head-main'>"
        f"<span class='part-name'>{_escape(name)}</span>{sku_html}"
        f"</span>{add_html}"
        f"</div>"
    )

    # Tier 2: description.
    meta_html = f"<div class='part-meta'>{_escape(description)}</div>" if description else ""

    # Tier 3 (least prominent): stock + prices as small badges.
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

    # NOTE: keep this HTML flat (no leading indentation). Streamlit's Markdown
    # renderer treats 4+ leading spaces as a code block and would print the raw
    # tags instead of rendering them.
    badges_html = f"<div class='part-badges'>{''.join(badges)}</div>"
    return f"<div class='part-card'>{header_html}{meta_html}{badges_html}</div>"


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


# Navigation views. The four shop "buttons" plus the home menu.
_VIEW_HOME = "home"
_VIEW_INVENTORY = "inventory"
_VIEW_NEW_INVOICE = "new_invoice"
_VIEW_HISTORY = "history"
_VIEW_SCAN = "scan"
_VALID_VIEWS = {_VIEW_HOME, _VIEW_INVENTORY, _VIEW_NEW_INVOICE, _VIEW_HISTORY, _VIEW_SCAN}


def _go(view: str) -> None:
    st.session_state["shop_view"] = view
    st.rerun()


def _render_lang_toggle(lang: str) -> None:
    """Small BG/EN toggle, shared across views."""
    bulgarian = st.toggle("БГ", value=(lang == "bg"), help=_t(lang, "lang_toggle"), key="shop_lang_toggle")
    new_lang = "bg" if bulgarian else "en"
    if new_lang != lang:
        st.session_state["shop_lang"] = new_lang
        st.rerun()


def _render_sidebar_nav(lang: str, current: str) -> None:
    """Sidebar navigation between the four buttons + home."""
    with st.sidebar:
        st.markdown(f"### 🔧 {_t(lang, 'menu')}")
        nav = [
            (_VIEW_HOME, "🏠", _t(lang, "nav_home")),
            (_VIEW_INVENTORY, "📦", _t(lang, "card_inventory")),
            (_VIEW_NEW_INVOICE, "🧾", _t(lang, "card_new_invoice")),
            (_VIEW_HISTORY, "📜", _t(lang, "card_history")),
            (_VIEW_SCAN, "📷", _t(lang, "card_scan")),
        ]
        for view, icon, label in nav:
            disabled = view == current
            if st.button(
                f"{icon}  {label}",
                use_container_width=True,
                key=f"nav_{view}",
                disabled=disabled,
            ):
                _go(view)
        st.caption(_SHOP_BUILD_LABEL)


def render_shop_inventory_page() -> None:
    """Mobile shop app entry point: home menu + four navigable views.

    ``?shop=1`` lands on the home menu (four big cards). The sidebar mirrors the
    same navigation so the user can switch views from anywhere. Public-by-link.
    """
    st.markdown(_MOBILE_CSS, unsafe_allow_html=True)

    lang = st.session_state.get("shop_lang", "en")

    # Visible build stamp at the very top (sidebar is collapsed by default) so we
    # can confirm at a glance whether Streamlit Cloud is serving the new code.
    st.caption(_SHOP_BUILD_LABEL)

    # Existing browser sessions may have been left inside the old Inventory-only
    # screen before the four-card router existed. Initialise once to Home so the
    # upgraded menu becomes visible after redeploy, then let navigation persist.
    if not st.session_state.get("shop_view_initialized"):
        st.session_state["shop_view"] = _VIEW_HOME
        st.session_state["shop_view_initialized"] = True

    view = st.session_state.get("shop_view", _VIEW_HOME)
    if view not in _VALID_VIEWS:
        view = _VIEW_HOME

    _render_sidebar_nav(lang, view)

    # Resolve the realm once; every data view needs it. The home menu still
    # renders even if the realm is missing (so the user isn't stuck).
    try:
        realm_id = _cached_shop_realm_id()
    except Exception as exc:  # noqa: BLE001 - shop company not connected yet
        logger.warning("Shop realm unavailable: %s", exc)
        realm_id = ""

    if view == _VIEW_HOME:
        _render_home_view(lang, realm_id)
    elif view == _VIEW_INVENTORY:
        _render_inventory_view(lang, realm_id)
    elif view == _VIEW_HISTORY:
        _render_history_view(lang, realm_id)
    elif view == _VIEW_NEW_INVOICE:
        _render_new_invoice_view(lang, realm_id)
    elif view == _VIEW_SCAN:
        _render_scan_view(lang, realm_id)


def _render_home_view(lang: str, realm_id: str) -> None:
    """The four-button shop menu shown at ?shop=1."""
    header_col, lang_col = st.columns([3, 1])
    with header_col:
        st.markdown(f"<div class='home-title'>🔧 {_t(lang, 'home_title')}</div>", unsafe_allow_html=True)
    with lang_col:
        _render_lang_toggle(lang)
    st.markdown(f"<div class='home-sub'>{_t(lang, 'home_subtitle')}</div>", unsafe_allow_html=True)

    cards = [
        (_VIEW_INVENTORY, "📦", "card_inventory", "card_inventory_desc", False),
        (_VIEW_NEW_INVOICE, "🧾", "card_new_invoice", "card_new_invoice_desc", True),
        (_VIEW_HISTORY, "📜", "card_history", "card_history_desc", False),
        (_VIEW_SCAN, "📷", "card_scan", "card_scan_desc", True),
    ]
    for view, icon, title_key, desc_key, soon in cards:
        soon_html = (
            f"<span class='home-card-soon'>{_t(lang, 'coming_soon')}</span>" if soon else ""
        )
        st.markdown(
            f"<div class='home-card'>"
            f"<span class='home-card-icon'>{icon}</span>"
            f"<span class='home-card-body'>"
            f"<div class='home-card-title'>{_escape(_t(lang, title_key))}</div>"
            f"<div class='home-card-desc'>{_escape(_t(lang, desc_key))}</div>"
            f"</span>{soon_html}</div>",
            unsafe_allow_html=True,
        )
        if st.button(
            f"{icon}  {_t(lang, title_key)}",
            use_container_width=True,
            key=f"home_card_{view}",
        ):
            _go(view)


def _render_view_header(lang: str, title_key: str) -> None:
    """Shared header for sub-views: back-to-menu + title + language toggle."""
    top_l, top_r = st.columns([3, 1])
    with top_l:
        if st.button(f"⬅ {_t(lang, 'back_to_menu')}", key=f"back_{title_key}"):
            _go(_VIEW_HOME)
    with top_r:
        _render_lang_toggle(lang)
    st.markdown(f"<div class='shop-title'>{_t(lang, title_key)}</div>", unsafe_allow_html=True)


def _render_inventory_view(lang: str, realm_id: str) -> None:
    """Inventory List view (Button 1): live search + paginated part cards."""
    _render_view_header(lang, "title")

    if not realm_id:
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

    # Reset how many cards are shown whenever the search term changes, so a new
    # search always starts at the top with the first page.
    if st.session_state.get("shop_last_term") != term:
        st.session_state["shop_last_term"] = term
        st.session_state["shop_visible_count"] = _PAGE_SIZE
    visible_count = int(st.session_state.get("shop_visible_count", _PAGE_SIZE))

    # Fetch one extra row beyond what we show so we know whether a "Show more"
    # button is warranted without a separate count query.
    fetch_limit = min(visible_count + 1, _MAX_RESULTS + 1)
    try:
        items = _search_inventory(realm_id, term, fetch_limit)
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

    has_more = len(items) > visible_count
    visible_items = items[:visible_count]

    shown = len(visible_items)
    if has_more:
        st.caption(f"{_t(lang, 'showing')} {shown}+ {_t(lang, 'results')}")
    else:
        st.caption(f"{_t(lang, 'showing')} {shown} {_t(lang, 'results')}")

    # When the list is narrowed by a search to a manageable size, render each
    # card with a real interactive "+" offering Start new / Add to current
    # invoice. When browsing the whole catalog, render fast batched HTML so the
    # phone stays smooth (per-row widgets are far heavier than HTML).
    _INTERACTIVE_MAX = 25
    if term and shown <= _INTERACTIVE_MAX:
        _show_cart_flash()
        for item in visible_items:
            card_col, add_col = st.columns([6, 1])
            with card_col:
                st.markdown(_card_html(item, lang), unsafe_allow_html=True)
            with add_col:
                _render_add_popover(item, lang)
    else:
        cards_html = "".join(_card_html(item, lang) for item in visible_items)
        st.markdown(cards_html, unsafe_allow_html=True)

    if has_more:
        if st.button(
            f"\u2b07\ufe0f {_t(lang, 'show_more')}",
            use_container_width=True,
            key="shop_show_more",
        ):
            st.session_state["shop_visible_count"] = min(
                visible_count + _PAGE_SIZE, _MAX_RESULTS
            )
            st.rerun()


def _render_add_popover(item: dict[str, Any], lang: str) -> None:
    """The inventory "+" affordance: choose Start new or Add to current invoice."""
    item_id = str(item.get("qbo_item_id") or "")
    with st.popover("➕", use_container_width=True):
        if st.button(
            f"🧾 {_t(lang, 'create_new_invoice')}",
            key=f"new_inv_{item_id}",
            use_container_width=True,
        ):
            st.session_state["shop_cart"] = []
            _cart_add(item)
            _go(_VIEW_NEW_INVOICE)
        if st.button(
            f"➕ {_t(lang, 'add_to_current')}",
            key=f"add_cur_{item_id}",
            use_container_width=True,
        ):
            _cart_add(item)
            st.session_state["shop_cart_flash"] = _t(lang, "added_toast")
            st.rerun()


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_recent_invoices(realm_id: str, limit: int) -> list[dict[str, Any]]:
    qbo_client, _, _ = build_services()
    return fetch_recent_invoices(qbo_client, realm_id, limit=limit)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_next_invoice_number(realm_id: str) -> int | None:
    qbo_client, _, _ = build_services()
    return next_invoice_number(qbo_client, realm_id)


def _render_history_view(lang: str, realm_id: str) -> None:
    """Invoice History view (Button 3): read-only list of recent QBO invoices."""
    _render_view_header(lang, "history_title")

    if not realm_id:
        st.info(_t(lang, "not_connected"))
        return

    try:
        with st.spinner(_t(lang, "history_loading")):
            invoices = _cached_recent_invoices(realm_id, 50)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Invoice history load failed: %s", exc)
        st.error(_t(lang, "history_error"))
        return

    if not invoices:
        st.info(_t(lang, "history_empty"))
        return

    st.markdown("".join(_invoice_html(inv, lang) for inv in invoices), unsafe_allow_html=True)


def _invoice_html(inv: dict[str, Any], lang: str) -> str:
    doc = str(inv.get("DocNumber") or "—").strip()
    txn_date = str(inv.get("TxnDate") or "").strip()
    customer = ""
    ref = inv.get("CustomerRef")
    if isinstance(ref, dict):
        customer = str(ref.get("name") or "").strip()
    total = _fmt_price(inv.get("TotalAmt"))
    balance_raw = inv.get("Balance")
    balance = _fmt_price(balance_raw)

    badges = []
    if total:
        badges.append(f"<span class='badge badge-price'>{_t(lang, 'invoice_total')}: {total}</span>")
    try:
        is_paid = float(balance_raw or 0) <= 0
    except (TypeError, ValueError):
        is_paid = False
    if is_paid:
        badges.append(f"<span class='badge badge-stock'>{_t(lang, 'invoice_paid')}</span>")
    elif balance:
        badges.append(f"<span class='badge badge-stock-zero'>{_t(lang, 'invoice_balance')}: {balance}</span>")

    customer_html = f"<div class='inv-customer'>{_escape(customer)}</div>" if customer else ""
    return (
        f"<div class='inv-card'>"
        f"<div class='inv-top'>"
        f"<span class='inv-no'>{_t(lang, 'invoice_no')} #{_escape(doc)}</span>"
        f"<span class='inv-date'>{_escape(txn_date)}</span>"
        f"</div>{customer_html}"
        f"<div class='inv-amounts'>{''.join(badges)}</div>"
        f"</div>"
    )


def _render_new_invoice_view(lang: str, realm_id: str) -> None:
    """New Invoice view (Button 2): build a cart and submit it for review.

    The shop manager searches parts, taps + to add them, adjusts quantities, and
    taps "Finish invoice". This does NOT post to QuickBooks - it writes a pending
    draft to the Supabase review queue for accounting.
    """
    _render_view_header(lang, "card_new_invoice")

    if not realm_id:
        st.info(_t(lang, "not_connected"))
        return

    _show_cart_flash()

    # Auto-suggested next invoice number (teaser; accounting assigns the final).
    try:
        next_no = _cached_next_invoice_number(realm_id)
    except Exception:  # noqa: BLE001
        next_no = None
    if next_no:
        st.success(f"{_t(lang, 'next_invoice_hint')}: **#{next_no}**")

    # --- Add parts: compact interactive search (kept small so it stays fast). ---
    add_term = st.text_input(
        _t(lang, "cart_search"),
        key="invoice_add_search",
        placeholder=_t(lang, "search_placeholder"),
    ).strip()
    if add_term:
        try:
            matches = _search_inventory(realm_id, add_term, 25)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Invoice add-search failed: %s", exc)
            matches = []
        for item in matches:
            label_col, add_col = st.columns([4, 1])
            with label_col:
                sku = str(item.get("sku") or "").strip()
                name = str(item.get("name") or "").strip()
                price = _fmt_price(item.get("sales_price"))
                bits = " · ".join(b for b in (f"{_t(lang, 'sku')} {sku}" if sku else "", name, price) if b)
                st.markdown(bits or name or "—")
            with add_col:
                if st.button("➕", key=f"add_{item.get('qbo_item_id')}", use_container_width=True):
                    _cart_add(item)
                    st.session_state["shop_cart_flash"] = _t(lang, "added_toast")
                    st.rerun()

    st.markdown(f"<div class='shop-title'>{_t(lang, 'cart_title')}</div>", unsafe_allow_html=True)
    cart = _cart()
    if not cart:
        st.info(_t(lang, "cart_empty"))
        return

    # --- Cart line items with quantity steppers + remove. ---
    total = 0.0
    for idx, line in enumerate(cart):
        line_total = float(line.get("unit_price") or 0) * int(line.get("qty") or 0)
        total += line_total
        name_col, qty_col, rm_col = st.columns([3, 2, 1])
        with name_col:
            sku = str(line.get("sku") or "").strip()
            head = f"{_t(lang, 'sku')} {sku} · " if sku else ""
            st.markdown(f"**{head}{_escape(str(line.get('name') or ''))}**")
            st.caption(f"{_fmt_price(line.get('unit_price'))} · {_fmt_price(line_total)}")
        with qty_col:
            new_qty = st.number_input(
                _t(lang, "qty"),
                min_value=1,
                step=1,
                value=int(line.get("qty") or 1),
                key=f"qty_{idx}_{line.get('qbo_item_id')}",
            )
            if int(new_qty) != int(line.get("qty") or 1):
                line["qty"] = int(new_qty)
                st.rerun()
        with rm_col:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            if st.button("🗑", key=f"rm_{idx}_{line.get('qbo_item_id')}", help=_t(lang, "remove")):
                cart.pop(idx)
                st.rerun()

    st.markdown(f"### {_t(lang, 'invoice_total_label')}: {_fmt_price(total)}")

    # --- Invoice metadata + submit. ---
    customer = st.text_input(_t(lang, "customer"), key="invoice_customer")
    truck_unit = st.text_input(_t(lang, "truck_unit"), key="invoice_truck")
    notes = st.text_area(_t(lang, "notes"), key="invoice_notes", height=80)

    st.caption(_t(lang, "finish_help"))
    finish_col, clear_col = st.columns([3, 1])
    with finish_col:
        if st.button(f"✅ {_t(lang, 'finish_invoice')}", use_container_width=True, type="primary"):
            try:
                submit_invoice_draft(
                    realm_id=realm_id,
                    proposed_doc_number=str(next_no or ""),
                    customer_name=customer,
                    truck_unit=truck_unit,
                    notes=notes,
                    line_items=[
                        {
                            "qbo_item_id": str(line.get("qbo_item_id") or ""),
                            "sku": str(line.get("sku") or ""),
                            "name": str(line.get("name") or ""),
                            "qty": int(line.get("qty") or 0),
                            "unit_price": float(line.get("unit_price") or 0),
                            "line_total": round(
                                float(line.get("unit_price") or 0) * int(line.get("qty") or 0), 2
                            ),
                        }
                        for line in cart
                    ],
                    total=total,
                    submitted_by=str(st.session_state.get("shop_user") or ""),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Invoice draft submit failed: %s", exc)
                st.error(_t(lang, "finish_err"))
            else:
                st.session_state["shop_cart"] = []
                st.session_state["shop_cart_flash"] = _t(lang, "finish_ok")
                st.rerun()
    with clear_col:
        if st.button(f"🧹 {_t(lang, 'clear_invoice')}", use_container_width=True):
            st.session_state["shop_cart"] = []
            st.rerun()


def _cart() -> list[dict[str, Any]]:
    cart = st.session_state.get("shop_cart")
    if not isinstance(cart, list):
        cart = []
        st.session_state["shop_cart"] = cart
    return cart


def _cart_add(item: dict[str, Any]) -> None:
    """Add a part to the cart, or bump its quantity if already present."""
    cart = _cart()
    item_id = str(item.get("qbo_item_id") or "")
    for line in cart:
        if str(line.get("qbo_item_id") or "") == item_id:
            line["qty"] = int(line.get("qty") or 0) + 1
            return
    cart.append(
        {
            "qbo_item_id": item_id,
            "sku": str(item.get("sku") or ""),
            "name": str(item.get("name") or ""),
            "unit_price": float(item.get("sales_price") or 0),
            "qty": 1,
        }
    )


def _show_cart_flash() -> None:
    msg = st.session_state.pop("shop_cart_flash", None)
    if msg:
        st.success(msg)



def _render_scan_view(lang: str, realm_id: str) -> None:
    """Scan Document view (Button 4): placeholder for future OCR matching."""
    _render_view_header(lang, "card_scan")
    st.info(f"📷 {_t(lang, 'coming_soon')}")

