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
import re
from typing import Any
from urllib.parse import quote_plus

import streamlit as st

from qbo.shop_inventory_sync import (
    build_services,
    resolve_shop_realm_id,
    sync_shop_inventory,
)
from qbo.shop_customer_sync import sync_shop_customers
from qbo.shop_invoices import (
    custom_field_map,
    custom_field_items,
    fetch_invoice_by_id,
    fetch_recent_invoices,
    next_invoice_number,
)
from qbo.shop_invoice_history_sync import sync_shop_invoice_history
from qbo.shop_attachment_sync import sync_shop_attachments
from qbo.shop_invoice_attachments import (
    build_attachment_index,
    download_attachment_bytes,
    index_key,
    list_entity_attachments,
    list_invoice_attachments,
)
from qbo.shop_inventory_adjustment_sync import sync_shop_inventory_adjustments
from qbo.shop_purchase_history_sync import sync_shop_purchase_history
from services.qbo_supabase import SupabaseRestClient
from services.shop_customer_cache import customer_names, last_customer_sync
from services.shop_inventory_adjustment_cache import (
    last_inventory_adjustment_sync,
    list_adjustments_with_item,
    list_cached_inventory_adjustments,
)
from services.shop_invoice_history_cache import (
    get_cached_invoice,
    last_invoice_history_sync,
    list_cached_invoices,
    list_invoices_with_item,
)
from services.shop_purchase_history_cache import (
    get_cached_purchase,
    last_purchase_history_sync,
    list_cached_purchases,
    list_purchases_with_item,
)
from services.shop_invoice_queue import (
    delete_draft,
    finalize_invoice_draft,
    get_draft,
    list_drafts,
    list_recent_drafts,
    save_invoice_draft,
    submit_invoice_draft,
)
from submission_storage import get_runtime_secret

logger = logging.getLogger(__name__)

_PAGE_SIZE = 250  # how many cards on first load and per "Show more"
_INVENTORY_PAGE = 60  # inventory shows fewer per page since each row has an Add button
_MAX_RESULTS = 6000  # hard ceiling so a runaway list can't lock up the phone
_SEARCH_CACHE_TTL = 60  # seconds
_REALM_CACHE_TTL = 600  # seconds
_INVOICE_CACHE_TTL = 120  # seconds
_LABOR_ITEM_NAME = "labor gts"
_LABOR_MECHANICS = ("Alex", "Rafi", "Danko")
_DEFAULT_SHOP_APP_URL = "https://driver-application.streamlit.app/?shop=1"
_SHOP_BUILD_LABEL = "Shop app build 2026-06-17.04 (part history server-side item filter + GIN index = instant first open)"

# How many cached transactions part history scans per type. Big enough to cover
# a multi-year shop's full Bill/Purchase/Invoice/Adjustment history (reads are
# paginated past PostgREST's 1000-row server cap and cached for a short TTL).
_PART_HISTORY_SCAN = 20000

# Invoice History: the default (unfiltered) list shows a clean recent window so
# old outlier invoice numbers (e.g. a one-off #152285 from 2025) don't float to
# the top. When the user searches/filters, load a multi-year window (past
# PostgREST's 1000-row cap) so search can reach old invoices.
_INVOICE_HISTORY_DEFAULT = 500
_INVOICE_HISTORY_WINDOW = 8000
_INVOICE_HISTORY_RENDER_CAP = 400

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
        "full_resync_title": "Part missing? Full re-sync",
        "full_resync_help": "Re-pulls every active part from QuickBooks (not just recent changes). Use this if a part you expect is not showing up.",
        "full_resync_btn": "Re-pull all parts from QuickBooks",
        "full_resync_short": "Re-pull",
        "negatives_short": "Negatives",
        "negatives_short_on": "Negatives ✓",
        "too_many_results": "Showing the maximum number of parts. Type to narrow the list.",
        "add_part_label": "Add a part",
        "select_part_placeholder": "Select part",
        "add_part_help": "Search by part number, description or SKU, then pick to add.",
        "add_parts_panel_title": "➕ Add parts to this invoice",
        "add_parts_panel_help": "Search/pick a part below, then tap Add to invoice.",
        "li_line": "Line",
        "li_unit": "Rate",
        "li_ext": "Amount",
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
        "card_purchase_history": "Purchase History",
        "card_purchase_history_desc": "See where parts were bought",
        "card_scan": "Scan Document",
        "card_scan_desc": "Snap a part to find it",
        "coming_soon": "Coming soon",
        "sync_all": "Sync all",
        "sync_all_title": "Sync inventory, invoices, and customers",
        "sync_all_running": "Checking QuickBooks for inventory, invoice, and customer changes…",
        "sync_all_done": "Sync complete",
        "sync_all_error": "Some syncs failed",
        # Invoice history.
        "history_title": "Invoice History",
        "history_loading": "Loading invoices…",
        "history_empty": "No invoices yet.",
        "history_error": "Could not load invoices right now. Please try again shortly.",
        "history_search_placeholder": "Search unit, VIN, or customer...",
        "purchase_history_title": "Purchase History",
        "purchase_history_search_placeholder": "Search part, vendor, or document...",
        "purchase_history_empty": "No purchase history found yet. Tap Refresh purchases.",
        "purchase_history_refresh": "Refresh purchases",
        "purchase_history_full_refresh": "Re-pull all purchases",
        "purchase_history_refreshing": "Checking QuickBooks for purchase changes…",
        "purchase_history_refresh_done": "Purchase history updated",
        "purchase_history_refresh_none": "Purchase history already up to date",
        "oil_changes_only": "Oil changes only",
        "unpaid_only": "Unpaid only",
        "history_no_filter_results": "No invoices match those filters.",
        "copy_to_draft": "Copy invoice to draft",
        "copy_to_draft_ok": "Copied to a new draft. Review and finish when ready.",
        "history_refresh": "Refresh invoices",
        "history_refreshing": "Checking QuickBooks for invoice changes…",
        "history_refresh_done": "Invoice history updated",
        "history_refresh_none": "Invoice history already up to date",
        "invoice_no": "Invoice",
        "invoice_total": "Total",
        "invoice_balance": "Balance",
        "invoice_paid": "Paid",
        "next_invoice_hint": "Next invoice number will be",
        "view_details": "View details",
        "view_docs": "Documents",
        "docs_title": "Scanned Documents",
        "docs_loading": "Loading documents…",
        "docs_none": "No documents attached to this invoice in QuickBooks.",
        "docs_none_short": "No documents",
        "docs_error": "Could not load documents from QuickBooks. Please try again.",
        "docs_download": "Download",
        "docs_preview": "Preview",
        "docs_unsupported": "Preview not available — use Download to view this file.",
        "history_render_cap": "Showing newest {shown} of {total}. Search to find older invoices.",
        "unit": "Unit",
        "vin": "VIN",
        "miles": "Miles",
        "line_items": "Line items",
        "li_item": "Item",
        "li_desc": "Description",
        "li_qty": "Qty",
        "li_rate": "Rate",
        "li_amount": "Amount",
        "no_lines": "No line items on this invoice.",
        "detail_error": "Could not open this invoice. Please go back and try again.",
        "refresh_invoices": "Refresh invoices",
        "invoices_never": "Invoices not synced yet. Tap Refresh invoices.",
        # New invoice / cart.
        "add": "Add",
        "add_to_invoice": "Add to invoice",
        "create_new_invoice": "Start new invoice",
        "add_to_current": "Add to current invoice",
        "add_to_draft": "Add to draft",
        "more_drafts": "+{n} more drafts — open Invoice History to pick one.",
        "added_toast": "Added to invoice",
        "low_stock_warn": "⚠️ No stock on hand — adding this may go negative.",
        "negatives_only": "⚠️ Show negative stock",
        "negatives_on": "✖ Showing negative stock (worst $ first)",
        "neg_value": "Shortage",
        "on_drafts": "On drafts",
        "on_hand_label": "📦 In stock now",
        "qty_to_add": "Qty to invoice",
        "confirm_remove_q": "Remove this part from the invoice?",
        "confirm_remove_yes": "Remove",
        "confirm_remove_no": "Keep",
        "labor_details_title": "Labor details (optional)",
        "labor_work": "Work done",
        "labor_mechanic": "Mechanic",
        "labor_hours": "Hours",
        "add_labor_detail": "Add labor detail",
        "no_negatives": "No negative-stock parts. 🎉",
        "cart_title": "Current Invoice",
        "cart_empty": "No parts added yet. Search and tap + to add parts.",
        "cart_search": "Search parts to add",
        "qty": "Qty",
        "remove": "Remove",
        "customer": "Customer / Company",
        "truck_unit": "Truck / Unit #",
        "unit_short": "Unit",
        "notes": "Notes",
        "invoice_total_label": "Invoice total",
        "finish_invoice": "Finish invoice",
        "finish_help": "Sends this invoice to accounting for review. It is NOT final yet.",
        "finish_ok": "Invoice sent to accounting for review.",
        "finish_err": "Could not submit the invoice. Please try again.",
        "save_draft": "Save draft",
        "draft_ok": "Draft saved.",
        "draft_err": "Could not save draft. Please try again.",
        "draft_autosaved": "📝 Draft — saved automatically (not in QuickBooks yet)",
        "drafts_title": "📝 Your drafts",
        "drafts_help": "Saved here only — NOT in QuickBooks yet",
        "qbo_invoices_title": "✅ In QuickBooks",
        "edit_draft": "Edit",
        "delete_draft": "Delete",
        "prior_invoice": "Last invoice for this unit",
        "no_drafts": "No saved drafts.",
        "next": "Next",
        "suggestions": "Suggestions",
        "confirm_customer": "Confirm customer",
        "skip_customer": "Skip customer",
        "edit_header": "Edit unit/VIN/customer",
        "choose_customer": "Choose customer",
        "customer_suggestions": "Suggested customers",
        "customer_not_listed": "Customer not listed",
        "customer_search": "Search existing customers",
        "refresh_customers": "Refresh customers",
        "customers_refreshing": "Checking QuickBooks for customer changes…",
        "customers_refresh_done": "Customer list updated",
        "customers_refresh_none": "Customer list already up to date",
        "new_customer": "New customer name",
        "use_customer": "Use this customer",
        "header_ready": "Invoice header locked in — add parts below.",
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
        "full_resync_title": "Липсва част? Пълна синхронизация",
        "full_resync_help": "Изтегля отново всички активни части от QuickBooks (не само последните промени). Използвайте, ако липсва част.",
        "full_resync_btn": "Изтегли всички части от QuickBooks",
        "full_resync_short": "Обнови",
        "negatives_short": "Отриц.",
        "negatives_short_on": "Отриц. ✓",
        "too_many_results": "Показан е максималният брой части. Пишете, за да стесните списъка.",
        "add_part_label": "Добавете част",
        "select_part_placeholder": "Изберете част",
        "add_part_help": "Търсете по номер, описание или SKU, след което изберете.",
        "add_parts_panel_title": "➕ Добавете части към фактурата",
        "add_parts_panel_help": "Търсете/изберете част долу, после натиснете Добави.",
        "li_line": "Ред",
        "li_unit": "Цена",
        "li_ext": "Сума",
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
        "card_purchase_history": "История на покупки",
        "card_purchase_history_desc": "Виж откъде са купени части",
        "card_scan": "Сканирай документ",
        "card_scan_desc": "Снимай част, за да я намериш",
        "coming_soon": "Очаквайте скоро",
        "sync_all": "Синхронизирай всичко",
        "sync_all_title": "Синхронизирай наличност, фактури и клиенти",
        "sync_all_running": "Проверка за промени в QuickBooks…",
        "sync_all_done": "Синхронизацията е готова",
        "sync_all_error": "Някои синхронизации не успяха",
        # Invoice history.
        "history_title": "История на фактурите",
        "history_loading": "Зареждане на фактури…",
        "history_empty": "Все още няма фактури.",
        "history_error": "Фактурите не могат да се заредят сега. Опитайте отново.",
        "history_search_placeholder": "Търси номер, VIN или клиент...",
        "purchase_history_title": "История на покупки",
        "purchase_history_search_placeholder": "Търси част, доставчик или документ...",
        "purchase_history_empty": "Няма история на покупки. Натиснете обновяване.",
        "purchase_history_refresh": "Обнови покупки",
        "purchase_history_full_refresh": "Изтегли всички покупки",
        "purchase_history_refreshing": "Проверка за промени в покупките…",
        "purchase_history_refresh_done": "Историята на покупки е обновена",
        "purchase_history_refresh_none": "Историята на покупки вече е актуална",
        "oil_changes_only": "Само смяна на масло",
        "unpaid_only": "Само неплатени",
        "history_no_filter_results": "Няма фактури с тези филтри.",
        "copy_to_draft": "Копирай във чернова",
        "copy_to_draft_ok": "Копирано в нова чернова. Прегледайте и завършете.",
        "history_refresh": "Обнови фактурите",
        "history_refreshing": "Проверка за промени във фактурите…",
        "history_refresh_done": "Историята е обновена",
        "history_refresh_none": "Историята вече е актуална",
        "invoice_no": "Фактура",
        "invoice_total": "Общо",
        "invoice_balance": "Остатък",
        "invoice_paid": "Платена",
        "next_invoice_hint": "Следващ номер на фактура ще бъде",
        "view_details": "Виж детайли",
        "view_docs": "Документи",
        "docs_title": "Сканирани документи",
        "docs_loading": "Зареждане на документи…",
        "docs_none": "Няма прикачени документи към тази фактура в QuickBooks.",
        "docs_none_short": "Няма документи",
        "docs_error": "Документите не можаха да се заредят от QuickBooks. Опитайте отново.",
        "docs_download": "Изтегли",
        "docs_preview": "Преглед",
        "docs_unsupported": "Няма визуализация — използвайте Изтегли, за да видите файла.",
        "history_render_cap": "Показани са най-новите {shown} от {total}. Търсете, за да намерите по-стари фактури.",
        "unit": "Номер",
        "vin": "VIN",
        "miles": "Мили",
        "line_items": "Редове",
        "li_item": "Артикул",
        "li_desc": "Описание",
        "li_qty": "Кол.",
        "li_rate": "Цена",
        "li_amount": "Сума",
        "no_lines": "Няма редове в тази фактура.",
        "detail_error": "Фактурата не може да се отвори. Върнете се и опитайте отново.",
        "refresh_invoices": "Обнови фактурите",
        "invoices_never": "Фактурите още не са синхронизирани. Натиснете Обнови фактурите.",
        # New invoice / cart.
        "add": "Добави",
        "add_to_invoice": "Добави към фактура",
        "create_new_invoice": "Започни нова фактура",
        "add_to_current": "Добави към текущата фактура",
        "add_to_draft": "Добави към чернова",
        "more_drafts": "+{n} още чернови — отворете История на фактури.",
        "added_toast": "Добавено към фактурата",
        "low_stock_warn": "⚠️ Няма наличност — добавянето може да стане отрицателно.",
        "negatives_only": "⚠️ Покажи отрицателна наличност",
        "negatives_on": "✖ Показана отрицателна наличност",
        "neg_value": "Недостиг",
        "on_drafts": "В чернови",
        "on_hand_label": "📦 Налично сега",
        "qty_to_add": "Кол. за фактура",
        "confirm_remove_q": "Да премахна ли тази част от фактурата?",
        "confirm_remove_yes": "Премахни",
        "confirm_remove_no": "Запази",
        "labor_details_title": "Детайли за труда (по избор)",
        "labor_work": "Извършена работа",
        "labor_mechanic": "Механик",
        "labor_hours": "Часове",
        "add_labor_detail": "Добави труд",
        "no_negatives": "Няма части с отрицателна наличност. 🎉",
        "cart_title": "Текуща фактура",
        "cart_empty": "Още няма добавени части. Търсете и натиснете +, за да добавите.",
        "cart_search": "Търсете части за добавяне",
        "qty": "Кол.",
        "remove": "Премахни",
        "customer": "Клиент / Фирма",
        "truck_unit": "Камион / Номер",
        "unit_short": "Единица",
        "notes": "Бележки",
        "invoice_total_label": "Обща сума",
        "finish_invoice": "Завърши фактурата",
        "finish_help": "Изпраща фактурата към счетоводството за преглед. ОЩЕ НЕ е окончателна.",
        "finish_ok": "Фактурата е изпратена към счетоводството.",
        "finish_err": "Фактурата не може да бъде изпратена. Опитайте отново.",
        "save_draft": "Запази чернова",
        "draft_ok": "Черновата е запазена.",
        "draft_err": "Черновата не може да бъде запазена. Опитайте отново.",
        "draft_autosaved": "📝 Чернова — автоматично запазена (още не е в QuickBooks)",
        "drafts_title": "📝 Вашите чернови",
        "drafts_help": "Само тук — ОЩЕ НЕ са в QuickBooks",
        "qbo_invoices_title": "✅ В QuickBooks",
        "edit_draft": "Редактирай",
        "delete_draft": "Изтрий",
        "prior_invoice": "Последна фактура за този номер",
        "no_drafts": "Няма запазени чернови.",
        "next": "Напред",
        "suggestions": "Предложения",
        "confirm_customer": "Потвърди клиента",
        "skip_customer": "Пропусни клиента",
        "edit_header": "Редактирай номер/VIN/клиент",
        "choose_customer": "Избери клиент",
        "customer_suggestions": "Предложени клиенти",
        "customer_not_listed": "Клиентът не е в списъка",
        "customer_search": "Търси съществуващи клиенти",
        "refresh_customers": "Обнови клиентите",
        "customers_refreshing": "Проверка за промени в клиентите…",
        "customers_refresh_done": "Списъкът с клиенти е обновен",
        "customers_refresh_none": "Списъкът с клиенти вече е актуален",
        "new_customer": "Име на нов клиент",
        "use_customer": "Използвай този клиент",
        "header_ready": "Данните са готови — добавете части долу.",
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

  /* Part-picker dropdown (Add part to invoice): let long part numbers wrap onto
     multiple lines instead of being clipped with an ellipsis, and break on the
     slashes that separate the many part-number aliases. Targets the BaseWeb
     listbox options so it only affects open dropdown menus. */
  ul[role="listbox"] li,
  div[data-baseweb="popover"] li[role="option"],
  div[data-baseweb="menu"] li {
      white-space: normal !important;
      height: auto !important;
      min-height: 2.6rem !important;
      line-height: 1.35 !important;
      overflow-wrap: anywhere !important;
      word-break: break-word !important;
      padding-top: 0.5rem !important;
      padding-bottom: 0.5rem !important;
  }

  /* Sticky search bar: the search box pins to the top and stays as a slim strip
     while the part list scrolls underneath it, so the shop user can keep typing
     without scrolling back up. We mark the search box with a zero-height anchor
     just before it, then make the text input's own element container sticky. */
  .sticky-search-anchor { display: block; height: 0; margin: 0; padding: 0; }
  div[data-testid="stElementContainer"]:has(.sticky-search-anchor) {
      height: 0; min-height: 0; margin: 0; padding: 0;
  }
  div[data-testid="stElementContainer"]:has(.sticky-search-anchor) + div[data-testid="stElementContainer"] {
      position: -webkit-sticky;
      position: sticky;
      top: 0;
      z-index: 1000;
      background: #F7F8FA;
      padding: 0.5rem 0 0.55rem;
      margin-bottom: 0.2rem;
      box-shadow: 0 8px 10px -9px rgba(16, 24, 40, 0.3);
  }
  div[data-testid="stElementContainer"]:has(.sticky-search-anchor) + div[data-testid="stElementContainer"] input {
      height: 2.6rem !important;
      font-size: 1.05rem !important;
      padding: 0.5rem 0.85rem !important;
  }

  /* Sticky sub-page navigation: keep Back + Menu visible while scrolling long
     inventory/history/detail pages. The hidden anchor marks the horizontal
     button row immediately after it. */
  .sticky-shop-nav-anchor { display: block; height: 0; margin: 0; padding: 0; }
  div[data-testid="stElementContainer"]:has(.sticky-shop-nav-anchor) {
      height: 0; min-height: 0; margin: 0; padding: 0;
  }
  div[data-testid="stElementContainer"]:has(.sticky-shop-nav-anchor) + div[data-testid="stHorizontalBlock"] {
      position: -webkit-sticky;
      position: sticky;
      top: 0;
      z-index: 1600;
      background: #F7F8FA;
      padding: 0.45rem 0 0.5rem;
      box-shadow: 0 8px 10px -9px rgba(16, 24, 40, 0.35);
  }

  /* Inventory row: a native bordered container is the part "card", and the green
     + popover sits on the right edge INSIDE that same card (where the old static
     placeholder was). The inner card HTML renders bare so we don't box-in-a-box. */
  div[data-testid="stVerticalBlockBorderWrapper"] {
      border: 1px solid #e4e8ec !important;
      border-radius: 14px !important;
      background: #ffffff !important;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05) !important;
      padding: 0.6rem 0.55rem 0.6rem 0.85rem !important;
      margin: 0.55rem 0 !important;
  }
  .part-card-bare { padding: 0; margin: 0; background: transparent; }

  /* Invoice parts-step header card: prominent number + customer, then the
     unit/VIN/miles as clearly bordered chips (no longer a faint gray caption). */
  .inv-head-card {
      border: 1px solid #e4e8ec;
      border-radius: 14px;
      background: #ffffff;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
      padding: 0.7rem 0.9rem;
      margin: 0.1rem 0 0.4rem;
  }
  .inv-head-top {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.2rem 0.7rem;
  }
  .inv-head-no {
      font-size: 1.7rem;
      font-weight: 800;
      letter-spacing: -0.01em;
      color: #1f2933;
      line-height: 1.1;
  }
  .inv-head-cust {
      font-size: 1.15rem;
      font-weight: 650;
      color: #3a4652;
      overflow-wrap: anywhere;
  }
  .inv-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 0.4rem;
      margin-top: 0.55rem;
  }
  .inv-chip {
      display: inline-flex;
      align-items: baseline;
      gap: 0.35rem;
      background: #f3f6fa;
      border: 1px solid #dde5ee;
      border-radius: 9px;
      padding: 0.28rem 0.55rem;
  }
  .inv-chip-k {
      font-size: 0.78rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      color: #5b6a7d;
  }
  .inv-chip-v {
      font-size: 1.05rem;
      font-weight: 750;
      color: #1f2933;
      overflow-wrap: anywhere;
  }

  /* Invoice Add Parts panel: make the part-entry area visually obvious without
     making the phone screen feel heavy. */
  .add-parts-title {
      font-size: 1.22rem;
      font-weight: 800;
      color: #1f2933;
      line-height: 1.25;
      margin-bottom: 0.15rem;
  }

  /* Add-popover "Add to draft" section label. */
  .popover-section {
      font-size: 0.82rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      color: #5b6a7d;
      margin: 0.55rem 0 0.15rem;
  }

  /* QuickBooks-style invoice line items. */
  .cli-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.6rem;
      margin-bottom: 0.1rem;
  }
  .cli-line {
      font-size: 0.82rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      color: #5b6a7d;
      background: #eef1f5;
      border-radius: 6px;
      padding: 0.1rem 0.45rem;
  }
  .cli-amount {
      font-size: 1.25rem;
      font-weight: 800;
      color: #1f2933;
  }
  .cli-name {
      font-size: 1.12rem;
      font-weight: 650;
      line-height: 1.3;
      color: #1f2933;
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.45rem;
      overflow-wrap: anywhere;
      word-break: break-word;
  }
  .cli-sku {
      font-size: 0.95rem;
      font-weight: 800;
      color: #1a55b0;
      background: #eaf1fb;
      border: 1px solid #d3e2f6;
      border-radius: 7px;
      padding: 0.08rem 0.45rem;
      white-space: nowrap;
  }
  .cli-desc {
      font-size: 0.98rem;
      color: #54606e;
      margin-top: 0.15rem;
      line-height: 1.35;
  }
  /* "In stock now" chip: visually distinct from the Qty-to-invoice stepper so
     the user never confuses current stock with what they're adding. */
  .cli-onhand {
      display: inline-block;
      margin-top: 0.45rem;
      font-size: 0.98rem;
      font-weight: 600;
      color: #1b7a43;
      background: #e8f5ed;
      border: 1px solid #cdead8;
      border-radius: 8px;
      padding: 0.3rem 0.6rem;
  }
  .cli-onhand b { font-weight: 800; }
  .cli-onhand-low {
      color: #b42318;
      background: #fdecea;
      border-color: #f7d4cf;
  }
  .li-rate-label {
      font-size: 0.82rem;
      font-weight: 600;
      color: #5b6a7d;
      margin-bottom: 0.15rem;
  }
  .li-rate-value {
      font-size: 1.1rem;
      font-weight: 700;
      color: #1f2933;
      padding-top: 0.35rem;
  }

  /* Buttons: flat, professional, full-width and easy to tap. */
  div[data-testid="stButton"] > button,
  div[data-testid="stLinkButton"] > a {
      border-radius: 12px !important;
      font-size: 1.08rem !important;
      font-weight: 600 !important;
      padding: 0.7rem 1rem !important;
      min-height: 3rem !important;
  }
  /* Inventory "Add" popover trigger: a full-width green bar beneath the card. */
  div[data-testid="stPopover"] button,
  [data-testid="stPopover"] button,
  div[data-testid="stPopover"] > div > button,
  div[data-testid="stPopover"] button[aria-haspopup="dialog"] {
      background: #2f9e54 !important;
      color: #ffffff !important;
      border: 1px solid #248045 !important;
      border-radius: 10px !important;
      font-size: 1.05rem !important;
      font-weight: 700 !important;
      width: 100% !important;
      min-height: 2.8rem !important;
      padding: 0.35rem 0.5rem !important;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.1) !important;
  }
  /* Home-only compact "open in app" square. Hidden in PWA/standalone contexts
     where the page is already inside an app-like wrapper. */
  .open-app-wrap { display: flex; justify-content: flex-end; }
  .open-app-square {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 2.75rem;
      height: 2.75rem;
      border-radius: 10px;
      border: 1px solid #cfd6de;
      background: #ffffff;
      color: #1f2933;
      text-decoration: none !important;
      font-size: 1.35rem;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
  }
  @media all and (display-mode: standalone) {
      .open-app-wrap { display: none !important; }
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
      overflow-wrap: anywhere;
      word-break: break-word;
  }
    .part-detail-link { color: #1f2933 !important; text-decoration: none !important; }
    .part-detail-link:active, .part-detail-link:hover { text-decoration: underline !important; }
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
  /* Web search affordance: small magnifying glass in the top-right of each part
     card. Pure HTML link, so it reliably stays where widget columns could not. */
  .part-web-search {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 2.1rem;
      height: 2.1rem;
      border-radius: 10px;
      font-size: 1.15rem;
      font-weight: 700;
      line-height: 1;
      color: #1a55b0 !important;
      background: #eaf1fb;
      border: 1px solid #d3e2f6;
      cursor: pointer;
      user-select: none;
      text-decoration: none !important;
  }
  .part-web-search:hover { background: #dceafc; border-color: #bfd6f2; }
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
  .badge-draft { background: #fbf0dc; color: #8a5a09; border-color: #f0dcb4; }

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
  .inv-meta { font-size: 0.95rem; color: #6b7682; margin-top: 0.25rem; }
  .inv-amounts { margin-top: 0.5rem; display: flex; flex-wrap: wrap; gap: 0.4rem; }

  /* Invoice detail line items. */
  .li-card {
      border: 1px solid #e9edf1; border-radius: 12px;
      padding: 0.7rem 0.9rem; margin: 0.4rem 0;
      background: #fbfcfd;
  }
  .li-name { font-size: 1.08rem; font-weight: 700; color: #1f2933; }
  .li-desc { font-size: 0.98rem; color: #616e7c; margin-top: 0.2rem; line-height: 1.35; }
    .inv-meta { font-size: 0.98rem; color: #52606d; margin-top: 0.25rem; line-height: 1.35; }
    .li-card {
            border: 1px solid #e4e8ec; border-radius: 12px;
            padding: 0.8rem 0.95rem; margin: 0.45rem 0;
            background: #ffffff;
    }
    .li-name { font-size: 1.08rem; font-weight: 700; color: #1f2933; }
    .li-desc { font-size: 0.98rem; color: #616e7c; margin-top: 0.25rem; line-height: 1.35; }

  /* Draft vs QuickBooks distinction. Drafts use a warm amber treatment to make
     it obvious they are NOT yet in QuickBooks. */
  .drafts-banner {
      margin: 0.4rem 0 0.2rem; font-size: 1.2rem; font-weight: 800; color: #92400e;
  }
  .drafts-sub {
      display: block; font-size: 0.95rem; font-weight: 600; color: #b45309; margin-top: 0.1rem;
  }
  .draft-card {
      border: 1px solid #f5d9a8; border-left: 6px solid #f59e0b; border-radius: 12px;
      padding: 0.75rem 0.95rem; margin: 0.45rem 0; background: #fffbeb;
  }
  .draft-top { display: flex; justify-content: space-between; align-items: center; gap: 0.6rem; }
  .draft-badge {
      font-size: 0.78rem; font-weight: 800; letter-spacing: 0.02em; text-transform: uppercase;
      color: #92400e; background: #fde68a; border-radius: 999px; padding: 0.15rem 0.5rem;
  }
</style>
"""


# Desktop-only override. Layered on top of _MOBILE_CSS for non-phone clients so
# accounting gets a wider working area while the shop manager's phone keeps the
# narrow, thumb-friendly 680px column.
_DESKTOP_CSS = """
<style>
  .block-container { max-width: 1180px; }
</style>
"""


def _is_mobile_client() -> bool:
    """Best-effort phone detection from the request User-Agent.

    Streamlit has no native viewport API, but the User-Agent header is a
    reliable enough signal to widen the layout for desktop while keeping the
    phone-first sizing for the shop manager. Defaults to mobile (the safer,
    narrower layout) when the header is missing.
    """
    try:
        headers = getattr(st.context, "headers", None)
        user_agent = (headers.get("User-Agent") if headers else "") or ""
    except Exception:  # noqa: BLE001 - context not available in every runtime
        return True
    user_agent = user_agent.lower()
    if not user_agent:
        return True
    mobile_tokens = ("mobi", "android", "iphone", "ipod", "windows phone", "blackberry")
    return any(token in user_agent for token in mobile_tokens)


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


@st.cache_data(ttl=_REALM_CACHE_TTL, show_spinner=False)
def _all_active_parts(realm_id: str) -> list[dict[str, Any]]:
    """Load every active part once (one query, cached) for live type-to-filter.

    The inventory live-search dropdown filters this in the browser per keystroke,
    so there are no per-keystroke database queries.
    """
    if not realm_id:
        return []
    supabase = SupabaseRestClient()
    rows = supabase.select_all(
        "shop_inventory",
        select="qbo_item_id,sku,name,fully_qualified_name,sales_description,purchase_description,sales_price,qty_on_hand,reorder_point,purchase_cost",
        filters={"realm_id": f"eq.{realm_id}", "active": "eq.true"},
        order="sku.asc,name.asc",
    )
    return rows


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _draft_quantities(realm_id: str) -> dict[str, float]:
    """Total quantity of each part committed across all OPEN drafts.

    Lets the inventory card show "X on drafts" without touching the real
    QuickBooks-backed on-hand number (drafts are not posted to QBO yet). Keyed by
    ``qbo_item_id``. Cached briefly so it does not re-query on every keystroke.
    """
    totals: dict[str, float] = {}
    if not realm_id:
        return totals
    try:
        drafts = list_drafts(realm_id, limit=200)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load drafts for quantities: %s", exc)
        return totals
    for draft in drafts:
        for li in draft.get("line_items") or []:
            item_id = str(li.get("qbo_item_id") or "")
            if not item_id:
                continue
            try:
                qty = float(li.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty:
                totals[item_id] = totals.get(item_id, 0.0) + qty
    return totals


@st.cache_data(ttl=_REALM_CACHE_TTL, show_spinner=False)
def _active_part_options(realm_id: str) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Return (labels, label->item) for the live-filter dropdown."""
    labels: list[str] = []
    by_label: dict[str, dict[str, Any]] = {}
    for item in _all_active_parts(realm_id):
        sku = str(item.get("sku") or "").strip()
        name = str(item.get("name") or "").strip()
        desc = str(item.get("sales_description") or item.get("purchase_description") or "").strip()
        # Part number / item name leads (most important); SKU and description
        # follow so they are still searchable but less prominent. The selectbox
        # filters on this label text, so every searchable field stays in it.
        label = " · ".join(
            b for b in (name, desc, f"SKU {sku}" if sku else "") if b
        ) or name or sku
        # Keep labels unique so selection maps to exactly one part.
        if label in by_label:
            label = f"{label}  ·  [{item.get('qbo_item_id')}]"
        labels.append(label)
        by_label[label] = item
    return labels, by_label


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


def _card_html(
    item: dict[str, Any],
    lang: str,
    *,
    show_shortage: bool = False,
    bare: bool = False,
    draft_qty: float = 0.0,
) -> str:
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

    # Tier 1 (most prominent): part number (QBO item name) + SKU shelf reference.
    sku_html = (
        f"<span class='part-sku'>{_t(lang, 'sku')} {_escape(sku)}</span>" if sku else ""
    )
    search_query = " ".join(
        value
        for value in (
            str(item.get("name") or item.get("fully_qualified_name") or "").strip(),
            description,
        )
        if value
    )
    search_url = f"https://www.google.com/search?q={quote_plus(search_query)}" if search_query else "https://www.google.com/"
    search_html = (
        f"<a class='part-web-search' href='{_escape(search_url)}' target='_blank' "
        f"rel='noopener noreferrer' title='Search web for this part' aria-label='Search web for this part'>🔎</a>"
    )
    header_html = (
        f"<div class='part-header'>"
        f"<span class='part-head-main'>"
        f"<span class='part-name'>{_escape(name)}</span>{sku_html}"
        f"</span>"
        f"{search_html}"
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
    if show_shortage:
        shortage = _part_shortage_value(item)
        if shortage < 0:
            badges.append(
                f"<span class='badge badge-stock-zero'>{_t(lang, 'neg_value')}: "
                f"{_fmt_price(shortage)}</span>"
            )
    # Only shown when this part is sitting on one or more open drafts (rare), so
    # the shop user knows some are already spoken for. Hidden otherwise.
    if draft_qty and draft_qty > 0:
        badges.append(
            f"<span class='badge badge-draft'>{_t(lang, 'on_drafts')}: "
            f"{_fmt_qty(draft_qty)}</span>"
        )

    # NOTE: keep this HTML flat (no leading indentation). Streamlit's Markdown
    # renderer treats 4+ leading spaces as a code block and would print the raw
    # tags instead of rendering them.
    badges_html = f"<div class='part-badges'>{''.join(badges)}</div>"
    wrapper = "part-card-bare" if bare else "part-card"
    return f"<div class='{wrapper}'>{header_html}{meta_html}{badges_html}</div>"


def _cart_line_html(
    lang: str,
    line_no: int,
    line: dict[str, Any],
    unit_price: float,
    line_total: float,
    on_hand: Any = None,
) -> str:
    """One invoice line rendered QuickBooks-style: # · Item · Description · Amount.

    Flat HTML (no leading indentation) so Streamlit's Markdown renderer does not
    treat it as a code block. The qty stepper / remove button are rendered by the
    caller as real widgets directly beneath this header.
    """
    sku = str(line.get("sku") or "").strip()
    name = str(line.get("name") or "").strip() or "—"
    desc = str(line.get("description") or "").strip()
    sku_html = f"<span class='cli-sku'>{_t(lang, 'sku')} {_escape(sku)}</span>" if sku else ""
    desc_html = f"<div class='cli-desc'>{_escape(desc)}</div>" if desc else ""

    # Current stock on hand, shown as its own clearly-labelled chip so it is never
    # confused with the quantity being added to the invoice. Red when none/short.
    on_hand_html = ""
    if on_hand is not None:
        oh_qty = _fmt_qty(on_hand)
        if oh_qty is not None:
            try:
                low = float(on_hand) <= 0
            except (TypeError, ValueError):
                low = False
            cls = "cli-onhand cli-onhand-low" if low else "cli-onhand"
            on_hand_html = (
                f"<div class='{cls}'>{_t(lang, 'on_hand_label')}: <b>{oh_qty}</b></div>"
            )

    return (
        f"<div class='cli-head'>"
        f"<span class='cli-line'>{_t(lang, 'li_line')} {line_no}</span>"
        f"<span class='cli-amount'>{_fmt_price(line_total)}</span>"
        f"</div>"
        f"<div class='cli-name'>{_escape(name)}{sku_html}</div>"
        f"{desc_html}"
        f"{on_hand_html}"
    )


def _is_labor_line(line: dict[str, Any]) -> bool:
    """True for the Labor GTS line item only."""
    haystack = " ".join(
        str(line.get(key) or "") for key in ("name", "description", "item_name")
    ).lower()
    return _LABOR_ITEM_NAME in haystack


def _render_labor_details_editor(lang: str, line: dict[str, Any], idx: int, item_id: str) -> None:
    """Optional mechanic/work breakdown for Labor GTS invoice lines."""
    details = line.get("labor_details")
    if not isinstance(details, list):
        details = []
        line["labor_details"] = details

    with st.expander(_t(lang, "labor_details_title"), expanded=bool(details)):
        remove_index: int | None = None
        for detail_idx, detail in enumerate(details):
            if not isinstance(detail, dict):
                detail = {}
                details[detail_idx] = detail
            mech = str(detail.get("mechanic") or _LABOR_MECHANICS[0])
            work = str(detail.get("work") or "")
            hours = float(detail.get("hours") or 0)
            cols = st.columns([1.1, 1.5, 0.85, 0.45], vertical_alignment="bottom")
            with cols[0]:
                selected_mech = st.selectbox(
                    _t(lang, "labor_mechanic"),
                    list(_LABOR_MECHANICS),
                    index=list(_LABOR_MECHANICS).index(mech) if mech in _LABOR_MECHANICS else 0,
                    key=f"labor_mech_{idx}_{detail_idx}_{item_id}",
                )
            with cols[1]:
                selected_work = st.text_input(
                    _t(lang, "labor_work"),
                    value=work,
                    key=f"labor_work_{idx}_{detail_idx}_{item_id}",
                    placeholder="Shock absorbers",
                )
            with cols[2]:
                selected_hours = st.number_input(
                    _t(lang, "labor_hours"),
                    min_value=0.0,
                    step=0.25,
                    value=max(0.0, hours),
                    key=f"labor_hours_{idx}_{detail_idx}_{item_id}",
                )
            with cols[3]:
                if st.button("🗑", key=f"labor_rm_{idx}_{detail_idx}_{item_id}", help=_t(lang, "remove")):
                    remove_index = detail_idx
            detail["mechanic"] = selected_mech
            detail["work"] = selected_work.strip()
            detail["hours"] = float(selected_hours or 0)

        if remove_index is not None:
            details.pop(remove_index)
            st.rerun()

        if st.button(
            f"➕ {_t(lang, 'add_labor_detail')}",
            key=f"labor_add_{idx}_{item_id}",
            use_container_width=True,
        ):
            details.append({"mechanic": _LABOR_MECHANICS[0], "work": "", "hours": 0.0})
            st.rerun()


def _escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _run_refresh(realm_id: str, lang: str, *, force_full: bool = False) -> None:
    """Run an inventory sync, then refresh the view.

    Normal refresh pulls only QBO items changed since the last sync (delta).
    ``force_full=True`` ignores the saved cursor and re-pulls every active item
    from QuickBooks - use it to recover parts a delta run might have missed.
    Upserts into Supabase, clears the cached reads so the list shows fresh data,
    and flashes a short status. Failures are surfaced without crashing.
    """
    with st.spinner(_t(lang, "refreshing")):
        result = sync_shop_inventory(realm_id, force_full=force_full)

    # Invalidate cached reads so the list + freshness stamp reflect the sync.
    _search_inventory.clear()
    _all_active_parts.clear()
    _active_part_options.clear()
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


def _run_sync_all(realm_id: str, lang: str) -> None:
    """Run all shop reference-data delta syncs from the home page.

    Each sync uses its own Supabase high-water cursor (QBO LastUpdatedTime), so
    after the initial full pulls this is gentle: it only asks QuickBooks for
    changed Inventory Items, Invoices, Purchases, Inventory Adjustments, and Customers.
    """
    if not realm_id:
        st.warning(_t(lang, "not_connected"))
        return
    with st.spinner(_t(lang, "sync_all_running")):
        inventory = sync_shop_inventory(realm_id)
        invoices = sync_shop_invoice_history(realm_id)
        purchases = sync_shop_purchase_history(realm_id)
        adjustments = sync_shop_inventory_adjustments(realm_id)
        customers = sync_shop_customers(realm_id)
        attachments = sync_shop_attachments(realm_id)

    _search_inventory.clear()
    _all_active_parts.clear()
    _active_part_options.clear()
    _last_synced.clear()
    _cached_recent_invoices.clear()
    _cached_invoice_detail.clear()
    _cached_invoice_history_synced_at.clear()
    _cached_recent_purchases.clear()
    _cached_purchase_history_synced_at.clear()
    _cached_recent_adjustments.clear()
    _cached_inventory_adjustment_synced_at.clear()
    _cached_customer_names.clear()
    _cached_customer_search.clear()
    _cached_customer_synced_at.clear()
    _cached_attachment_index.clear()
    _cached_part_history_events.clear()
    _cached_invoices_with_item.clear()
    _cached_purchases_with_item.clear()
    _cached_adjustments_with_item.clear()

    failures = [r for r in (inventory, invoices, purchases, customers) if r.status != "success"]
    if failures:
        st.error(
            f"{_t(lang, 'sync_all_error')}: "
            + "; ".join(getattr(result, "message", "") for result in failures)
        )
        return

    changed = (
        int(getattr(inventory, "items_upserted", 0) or 0)
        + int(getattr(invoices, "invoices_upserted", 0) or 0)
        + int(getattr(purchases, "purchases_upserted", 0) or 0)
        + int(getattr(adjustments, "adjustments_upserted", 0) or 0)
        + int(getattr(customers, "customers_upserted", 0) or 0)
    )
    st.success(f"{_t(lang, 'sync_all_done')} (+{changed})")
    if getattr(adjustments, "status", "") == "skipped":
        st.warning(getattr(adjustments, "message", "Run migration 0011 to enable inventory adjustments."))
    elif getattr(adjustments, "status", "") not in ("", "success"):
        st.warning(f"Inventory adjustment sync issue: {getattr(adjustments, 'message', '')}")
    if getattr(attachments, "status", "") not in ("", "success"):
        st.warning(
            "Document index not cached: "
            f"{getattr(attachments, 'message', '')}. "
            "Run migration 0012, then Sync All again."
        )


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
_VIEW_PURCHASE_HISTORY = "purchase_history"
_VIEW_INVOICE_DETAIL = "invoice"
_VIEW_INVOICE_DOCS = "invoice_docs"
_VIEW_PURCHASE_DETAIL = "purchase_detail"
_VIEW_PART_DETAIL = "part"
_VIEW_SCAN = "scan"
_VALID_VIEWS = {
    _VIEW_HOME,
    _VIEW_INVENTORY,
    _VIEW_NEW_INVOICE,
    _VIEW_HISTORY,
    _VIEW_PURCHASE_HISTORY,
    _VIEW_INVOICE_DETAIL,
    _VIEW_INVOICE_DOCS,
    _VIEW_PURCHASE_DETAIL,
    _VIEW_PART_DETAIL,
    _VIEW_SCAN,
}


def _go(view: str, *, record: bool = True) -> None:
    """Navigate by writing the view into the URL (single source of truth).

    Using a query param (``v``) instead of session_state makes navigation
    deterministic: a bare ``?shop=1`` always shows the home menu, even if a
    previous session left a different view selected. Sub-views are shareable /
    refresh-safe because the URL carries the state.
    """
    if record:
        _push_nav_history(view)

    # Mark that this view change came from an in-app button. If the app is opened
    # cold from a stale saved URL like ?shop=1&v=inventory, _current_view() will
    # ignore that stale view and force the Home menu instead.
    st.session_state["shop_allow_url_view"] = view != _VIEW_HOME
    if view == _VIEW_HOME:
        try:
            del st.query_params["v"]
        except (KeyError, Exception):  # noqa: BLE001 - tolerate API differences
            st.query_params["v"] = _VIEW_HOME
    else:
        st.query_params["v"] = view
    st.rerun()


def _nav_snapshot() -> dict[str, str]:
    """Current view/query snapshot for the in-app Back button."""
    snap = {"view": _current_view()}
    part_id = _query_param_value("part_id").strip()
    if part_id:
        snap["part_id"] = part_id
    invoice_id = (_query_param_value("invoice_id") or str(st.session_state.get("shop_invoice_id") or "")).strip()
    if invoice_id:
        snap["invoice_id"] = invoice_id
    return snap


def _push_nav_history(next_view: str) -> None:
    current = _nav_snapshot()
    if current.get("view") == next_view and next_view != _VIEW_PART_DETAIL:
        return
    stack = list(st.session_state.get("shop_nav_stack") or [])
    if not stack or stack[-1] != current:
        stack.append(current)
    st.session_state["shop_nav_stack"] = stack[-25:]


def _restore_nav_snapshot(snap: dict[str, str]) -> None:
    view = str(snap.get("view") or _VIEW_HOME)
    st.session_state["shop_allow_url_view"] = view != _VIEW_HOME
    for key in ("part_id", "invoice_id"):
        try:
            del st.query_params[key]
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
    if view == _VIEW_HOME:
        try:
            del st.query_params["v"]
        except Exception:  # noqa: BLE001
            pass
    else:
        st.query_params["v"] = view
    if snap.get("part_id"):
        st.query_params["part_id"] = snap["part_id"]
    if snap.get("invoice_id"):
        st.session_state["shop_invoice_id"] = snap["invoice_id"]
    st.rerun()


def _go_back(default_view: str = _VIEW_HOME) -> None:
    stack = list(st.session_state.get("shop_nav_stack") or [])
    if stack:
        snap = stack.pop()
        st.session_state["shop_nav_stack"] = stack
        _restore_nav_snapshot(snap)
        return
    _go(default_view, record=False)


def _open_part_detail(part_id: str) -> None:
    """Open part detail through Streamlit state instead of a raw HTML link."""
    part_id = str(part_id or "").strip()
    if not part_id:
        return
    _push_nav_history(_VIEW_PART_DETAIL)
    st.session_state["shop_allow_url_view"] = True
    st.query_params["v"] = _VIEW_PART_DETAIL
    st.query_params["part_id"] = part_id
    st.rerun()


def _open_invoice_detail(invoice_id: str, *, return_part_id: str = "") -> None:
    """Open invoice detail and optionally remember the part to return to."""
    invoice_id = str(invoice_id or "").strip()
    if not invoice_id:
        return
    st.session_state["shop_invoice_id"] = invoice_id
    if return_part_id:
        st.session_state["shop_invoice_return_part_id"] = str(return_part_id or "").strip()
    else:
        st.session_state.pop("shop_invoice_return_part_id", None)
    _go(_VIEW_INVOICE_DETAIL)


def _open_invoice_documents(invoice_id: str) -> None:
    """Open the scanned-documents view for an invoice."""
    invoice_id = str(invoice_id or "").strip()
    if not invoice_id:
        return
    st.session_state["shop_invoice_id"] = invoice_id
    _go(_VIEW_INVOICE_DOCS)


def _open_purchase_detail(txn_id: str, *, return_part_id: str = "") -> None:
    """Open the purchase/bill detail view (line items + documents)."""
    txn_id = str(txn_id or "").strip()
    if not txn_id:
        return
    st.session_state["shop_purchase_id"] = txn_id
    if return_part_id:
        st.session_state["shop_purchase_return_part_id"] = str(return_part_id or "").strip()
    else:
        st.session_state.pop("shop_purchase_return_part_id", None)
    _go(_VIEW_PURCHASE_DETAIL)


def _open_app_square_html(lang: str) -> str:
    """Small home-page-only app link.

    Rendered as raw HTML instead of ``st.link_button`` so it can be a compact
    square rather than a full-width button.
    """
    return (
        f"<div class='open-app-wrap'>"
        f"<a class='open-app-square' href='{_escape(_shop_app_url())}' "
        f"title='{_escape(_t(lang, 'open_app'))}' aria-label='{_escape(_t(lang, 'open_app'))}'>📱</a>"
        f"</div>"
    )


def _current_view() -> str:
    """Resolve the active view from the URL, defaulting to home."""
    raw = st.query_params.get("v", _VIEW_HOME)
    if isinstance(raw, list):
        raw = raw[0] if raw else _VIEW_HOME
    if raw not in _VALID_VIEWS or raw == _VIEW_HOME:
        return _VIEW_HOME
    if raw == _VIEW_PART_DETAIL and _query_param_value("part_id"):
        st.session_state["shop_allow_url_view"] = True
        return raw
    if not st.session_state.get("shop_allow_url_view"):
        try:
            del st.query_params["v"]
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass
        return _VIEW_HOME
    return raw


def _query_param_value(name: str) -> str:
    raw = st.query_params.get(name, "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return str(raw or "")


def _wire_shop_back_button(view: str) -> None:
    """Map the phone/browser back gesture to the in-app back button.

    On any sub-view we push a synthetic history entry and listen for popstate.
    When the user taps the phone's back key, we click the in-page back button
    (text starting with "⬅") instead of letting the browser exit the app, so
    back naturally walks Detail -> History -> Home. On the home menu we do
    nothing, so back there exits the app normally.
    """
    if view == _VIEW_HOME:
        return
    st.iframe(
        f"""
        <script>
        (function() {{
            const pw = window.parent;
            const pd = pw.document;
            const tag = "shop-{view}";
            if (!(pw.history.state && pw.history.state.shopView === tag)) {{
                try {{ pw.history.pushState({{ shopView: tag }}, "", pw.location.href); }} catch (e) {{}}
            }}
            if (pd.body && pd.body.dataset.shopBackBound !== '1') {{
                pd.body.dataset.shopBackBound = '1';
                pw.addEventListener('popstate', () => {{
                    const buttons = pd.querySelectorAll('button');
                    for (const btn of buttons) {{
                        const text = (btn.innerText || '').trim();
                        if (text.startsWith('⬅')) {{
                            btn.click();
                            try {{ pw.history.pushState({{ shopView: tag }}, "", pw.location.href); }} catch (e) {{}}
                            return;
                        }}
                    }}
                }});
            }}
        }})();
        </script>
        """,
        height=1,
    )


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
            (_VIEW_PURCHASE_HISTORY, "🧾", _t(lang, "card_purchase_history")),
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
    if not _is_mobile_client():
        # Desktop (accounting) gets a wider, roomier layout; phones keep the
        # phone-first 680px column so the shop manager's view is unchanged.
        st.markdown(_DESKTOP_CSS, unsafe_allow_html=True)

    lang = st.session_state.get("shop_lang", "en")

    # Visible build stamp at the very top (sidebar is collapsed by default) so we
    # can confirm at a glance whether Streamlit Cloud is serving the new code.
    st.caption(_SHOP_BUILD_LABEL)

    # The active view comes from the URL (?...&v=inventory), so a bare ?shop=1
    # always lands on the home menu regardless of any prior session state.
    view = _current_view()

    _wire_shop_back_button(view)
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
    elif view == _VIEW_PURCHASE_HISTORY:
        _render_purchase_history_view(lang, realm_id)
    elif view == _VIEW_INVOICE_DETAIL:
        _render_invoice_detail_view(lang, realm_id)
    elif view == _VIEW_INVOICE_DOCS:
        _render_invoice_documents_view(lang, realm_id)
    elif view == _VIEW_PURCHASE_DETAIL:
        _render_purchase_detail_view(lang, realm_id)
    elif view == _VIEW_PART_DETAIL:
        _render_part_detail_view(lang, realm_id)
    elif view == _VIEW_NEW_INVOICE:
        _render_new_invoice_view(lang, realm_id)
    elif view == _VIEW_SCAN:
        _render_scan_view(lang, realm_id)


def _render_home_view(lang: str, realm_id: str) -> None:
    """The four-button shop menu shown at ?shop=1 (buttons only, no cards)."""
    header_col, sync_col, app_col, lang_col = st.columns([3, 0.65, 0.65, 0.7])
    with header_col:
        st.markdown(f"<div class='home-title'>🔧 {_t(lang, 'home_title')}</div>", unsafe_allow_html=True)
    with sync_col:
        if st.button("🔄", help=_t(lang, "sync_all_title"), use_container_width=True, key="home_sync_all"):
            _run_sync_all(realm_id, lang)
    with app_col:
        st.markdown(_open_app_square_html(lang), unsafe_allow_html=True)
    with lang_col:
        _render_lang_toggle(lang)
    st.markdown(f"<div class='home-sub'>{_t(lang, 'home_subtitle')}</div>", unsafe_allow_html=True)

    cards = [
        (_VIEW_INVENTORY, "📦", "card_inventory", "card_inventory_desc", False),
        (_VIEW_NEW_INVOICE, "🧾", "card_new_invoice", "card_new_invoice_desc", False),
        (_VIEW_HISTORY, "📜", "card_history", "card_history_desc", False),
        (_VIEW_PURCHASE_HISTORY, "🧾", "card_purchase_history", "card_purchase_history_desc", False),
        (_VIEW_SCAN, "📷", "card_scan", "card_scan_desc", True),
    ]
    for view, icon, title_key, desc_key, soon in cards:
        label = f"{icon}  {_t(lang, title_key)}"
        if soon:
            label += f"  ·  {_t(lang, 'coming_soon')}"
        if st.button(
            label,
            use_container_width=True,
            key=f"home_nav_{view}",
            help=_t(lang, desc_key),
        ):
            if view == _VIEW_NEW_INVOICE:
                _start_new_invoice()
            else:
                _go(view)

def _render_view_header(
    lang: str, title_key: str, *, title_override: str = "", show_title: bool = True
) -> None:
    """Shared header for sub-views: back-to-menu + title + language toggle.

    ``title_override`` lets a caller show literal text (e.g. an invoice number)
    instead of a translated key. ``show_title=False`` renders only the back +
    language controls (caller draws its own heading).
    """
    st.markdown("<span class='sticky-shop-nav-anchor'></span>", unsafe_allow_html=True)
    back_col, menu_col, lang_col = st.columns([1.15, 1.1, 0.8])
    with back_col:
        if st.button("⬅ Back", key=f"back_one_{title_key}", use_container_width=True):
            _go_back(_VIEW_HOME)
    with menu_col:
        if st.button("🏠 Menu", key=f"menu_{title_key}", use_container_width=True):
            st.session_state["shop_nav_stack"] = []
            _go(_VIEW_HOME, record=False)
    with lang_col:
        _render_lang_toggle(lang)
    if show_title:
        title = title_override or _t(lang, title_key)
        st.markdown(f"<div class='shop-title'>{_escape(title)}</div>", unsafe_allow_html=True)


def _render_inventory_view(lang: str, realm_id: str) -> None:
    """Inventory List view (Button 1): type-to-filter parts, no dropdown."""
    _render_view_header(lang, "title")

    if not realm_id:
        st.info(_t(lang, "not_connected"))
        return

    _show_refresh_flash()

    # All active parts loaded once (cached); we filter this list in Python as the
    # search term changes. No dropdown - just a text box with the list below.
    try:
        parts = _all_active_parts(realm_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load parts: %s", exc)
        parts = []

    # Compact action buttons ABOVE the search bar (smaller, less awkward). The
    # search bar itself stays pinned to the top as you scroll the list.
    negatives_on = bool(st.session_state.get("shop_negatives_only"))
    refresh_col, neg_col, resync_col = st.columns([1, 1.3, 1.1])
    with refresh_col:
        if st.button(
            f"\U0001f504 {_t(lang, 'refresh')}",
            use_container_width=True,
            key="shop_refresh_btn",
        ):
            _run_refresh(realm_id, lang)
    with neg_col:
        neg_label = "⚠️ " + (_t(lang, "negatives_short_on") if negatives_on else _t(lang, "negatives_short"))
        if st.button(neg_label, use_container_width=True, key="shop_neg_toggle"):
            st.session_state["shop_negatives_only"] = not negatives_on
            st.rerun()
    with resync_col:
        if st.button(
            f"⟳ {_t(lang, 'full_resync_short')}",
            use_container_width=True,
            key="shop_full_resync",
            help=_t(lang, "full_resync_help"),
        ):
            _run_refresh(realm_id, lang, force_full=True)
    negatives_on = bool(st.session_state.get("shop_negatives_only"))

    # Sticky search bar: rendered directly in the main column (NOT inside its own
    # sub-container) so its sticky scroll context is the whole page, which is what
    # makes it actually pin to the top while the list scrolls underneath.
    st.markdown("<span class='sticky-search-anchor'></span>", unsafe_allow_html=True)
    term = st.text_input(
        _t(lang, "search_label"),
        key="shop_search_term",
        placeholder=_t(lang, "search_placeholder"),
        label_visibility="collapsed",
    ).strip()

    last_run = _last_synced(realm_id)
    freshness = _fmt_user_datetime(last_run) if last_run else _t(lang, "never")
    st.caption(f"{_t(lang, 'updated')}: {freshness}")
    if negatives_on:
        items = _negative_parts(parts)
        if not items:
            st.success(_t(lang, "no_negatives"))
            return
    else:
        items = _filter_parts(parts, term)
        if not items:
            st.info(_t(lang, "no_results") if term else _t(lang, "type_to_search"))
            return
        items = _sort_inventory(items)

    # Render the FULL result set (search already covers every cached part, not
    # just a page). Capped only as a safety valve against a pathological catalog.
    visible_items = items[:_MAX_RESULTS]
    has_more = len(items) > len(visible_items)
    shown = len(visible_items)
    st.caption(f"{_t(lang, 'showing')} {shown}{'+' if has_more else ''} {_t(lang, 'results')}")

    # Every part is a bordered card with a full-width green + bar beneath it.
    _show_cart_flash()
    try:
        draft_qtys = _draft_quantities(realm_id)
    except Exception:  # noqa: BLE001 - badge is a nice-to-have, never block the list
        draft_qtys = {}
    for item in visible_items:
        with st.container(border=True):
            st.markdown(
                _card_html(
                    item,
                    lang,
                    show_shortage=negatives_on,
                    bare=True,
                    draft_qty=draft_qtys.get(str(item.get("qbo_item_id") or ""), 0.0),
                ),
                unsafe_allow_html=True,
            )
            part_id = str(item.get("qbo_item_id") or "").strip()
            if part_id and st.button(
                "📜 History",
                key=f"part_history_{part_id}",
                use_container_width=True,
                help="Open this part's bought and sold history.",
            ):
                _open_part_detail(part_id)
            _render_add_popover(item, lang, realm_id)

    if has_more:
        st.caption(_t(lang, "too_many_results"))


def _render_part_detail_view(lang: str, realm_id: str) -> None:
    """Part detail: stock card + chronological sold/purchased transaction history."""
    _render_view_header(lang, "title", title_override="Part detail")
    if st.button(f"⬅ {_t(lang, 'card_inventory')}", key="part_detail_back_inventory", use_container_width=True):
        _go(_VIEW_INVENTORY)
    if not realm_id:
        st.info(_t(lang, "not_connected"))
        return
    part_id = _query_param_value("part_id").strip()
    if not part_id:
        st.info("No part selected.")
        return
    part = _find_active_part(realm_id, part_id)
    if not part:
        st.error("Could not find that part in the synced inventory cache.")
        return

    st.markdown(_card_html(part, lang), unsafe_allow_html=True)
    st.markdown("<div class='shop-title'>Part history</div>", unsafe_allow_html=True)

    with st.spinner("Loading part history…"):
        events = _cached_part_history_events(realm_id, part_id)

    if not events:
        st.info("No sales or purchase history found for this synced part yet.")
        return
    # Surface the most recent vendor this part was bought from.
    last_buy = next((e for e in events if str(e.get("kind") or "") == "bought"), None)
    if last_buy:
        vendor = str(last_buy.get("name") or "—")
        st.caption(f"🛒 Last purchased from {vendor} on {_fmt_user_date(last_buy.get('date'))}")
    has_purchases = any(str(e.get("kind") or "") == "bought" for e in events)
    has_adjustments = any(str(e.get("kind") or "") == "adjusted" for e in events)
    if not has_purchases:
        st.caption("No cached purchase rows found for this part yet. Run Purchase History refresh after applying the purchase-history migration.")
    if not has_adjustments:
        st.caption("No cached inventory adjustments found for this part yet. Run the 0011 adjustment SQL migration, then Sync All.")
    events = _filter_part_history_events(events, part_id)
    if not events:
        st.info("No part history matches the selected filters.")
        return
    current_part_id = str(part.get("qbo_item_id") or "").strip()
    # One cached scan of all attachments so each transaction can offer a direct
    # scan download without a per-row QBO call (same fast path as the history list).
    with st.spinner(_t(lang, "docs_loading")):
        doc_index = _cached_attachment_index(realm_id)
    for idx, ev in enumerate(events[:250]):
        st.markdown(_part_event_html(ev), unsafe_allow_html=True)
        kind = str(ev.get("kind") or "")
        invoice_id = str(ev.get("invoice_id") or "").strip()
        purchase_id = str(ev.get("purchase_id") or "").strip()
        if kind == "sold" and invoice_id:
            doc = str(ev.get("doc") or invoice_id).strip()
            open_col, docs_col = st.columns(2)
            with open_col:
                if st.button(
                    f"🔍 Open invoice {doc}",
                    key=f"part_sale_invoice_{invoice_id}_{idx}",
                    use_container_width=True,
                ):
                    _open_invoice_detail(invoice_id, return_part_id=current_part_id)
            with docs_col:
                atts = doc_index.get(index_key("Invoice", invoice_id), []) if doc_index else None
                _render_doc_indicator(
                    lang, "Invoice", invoice_id, atts, key=f"part_sale_docs_{invoice_id}_{idx}"
                )
        elif kind == "bought" and purchase_id:
            doc = str(ev.get("doc") or purchase_id).strip()
            etype = str(ev.get("purchase_type") or "Purchase")
            open_col, docs_col = st.columns(2)
            with open_col:
                if st.button(
                    f"🔍 Open purchase {doc}",
                    key=f"part_buy_purchase_{purchase_id}_{idx}",
                    use_container_width=True,
                ):
                    _open_purchase_detail(purchase_id, return_part_id=current_part_id)
            with docs_col:
                atts = doc_index.get(index_key(etype, purchase_id), []) if doc_index else None
                _render_doc_indicator(
                    lang, etype, purchase_id, atts, key=f"part_buy_docs_{purchase_id}_{idx}"
                )


def _filter_part_history_events(events: list[dict[str, Any]], part_id: str) -> list[dict[str, Any]]:
    """Filter part history by independently selectable event-type toggles.

    No selected toggles means no filter (show all). Selecting one, two, or all
    three limits the list to exactly those history types.
    """
    safe_part_id = _collapse_alnum(part_id) or "part"
    st.caption("Filter history")
    sold_col, bought_col, adjusted_col = st.columns(3)
    with sold_col:
        show_sold = st.toggle("Sold", key=f"part_hist_filter_sold_{safe_part_id}")
    with bought_col:
        show_bought = st.toggle("Bought", key=f"part_hist_filter_bought_{safe_part_id}")
    with adjusted_col:
        show_adjusted = st.toggle("Adjusted", key=f"part_hist_filter_adjusted_{safe_part_id}")

    selected: set[str] = set()
    if show_sold:
        selected.add("sold")
    if show_bought:
        selected.add("bought")
    if show_adjusted:
        selected.add("adjusted")
    if not selected:
        return events
    return [ev for ev in events if str(ev.get("kind") or "") in selected]


def _find_active_part(realm_id: str, part_id: str) -> dict[str, Any] | None:
    for part in _all_active_parts(realm_id):
        if str(part.get("qbo_item_id") or "") == str(part_id or ""):
            return part
    return None


def _part_match_names(part: dict[str, Any]) -> set[str]:
    return {
        str(value or "").strip().lower()
        for value in (part.get("name"), part.get("fully_qualified_name"))
        if str(value or "").strip()
    }


def _part_match_name_keys(part: dict[str, Any]) -> set[str]:
    """Exact Product/Service name keys for invoice line matching.

    Sold history must match the invoice line's QBO Product/Service item, not the
    line description. We normalize case and harmless spacing/punctuation only;
    we do not do contains/fuzzy matching here because that creates false
    invoice hits where the clicked part is not actually on the invoice.
    """
    return {
        _exact_item_name_key(value)
        for value in (part.get("name"), part.get("fully_qualified_name"))
        if _exact_item_name_key(value)
    }


def _part_sales_events(realm_id: str, part: dict[str, Any]) -> list[dict[str, Any]]:
    part_id = str(part.get("qbo_item_id") or "")
    events: list[dict[str, Any]] = []
    if not part_id:
        return events
    for inv in _cached_invoices_with_item(realm_id, part_id):
        raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else {}
        # Match against the raw QBO invoice lines so the authoritative
        # ItemRef.value (QBO Item Id) is always available, even for rows cached
        # before the flattened item_id existed. Fall back to flattened lines only
        # if the raw payload is somehow missing.
        lines = [
            line for line in (raw.get("Line") or [])
            if str(line.get("DetailType") or "") == "SalesItemLineDetail"
        ] or (inv.get("line_items") or [])
        vehicle = _invoice_vehicle_values(inv)
        for line in lines:
            if not _sales_line_matches_part(line, part, part_id):
                continue
            qty, rate, amount = _line_item_qty_rate_amount(line)
            events.append(
                {
                    "kind": "sold",
                    "invoice_id": str(inv.get("qbo_invoice_id") or inv.get("Id") or ""),
                    "date": str(inv.get("txn_date") or inv.get("TxnDate") or ""),
                    "doc": str(inv.get("doc_number") or inv.get("DocNumber") or ""),
                    "name": _invoice_customer_name(inv),
                    "unit": vehicle.get("unit", ""),
                    "qty": qty,
                    "rate": rate,
                    "amount": amount,
                    "memo": str(line.get("description") or line.get("Description") or ""),
                }
            )
    return events


def _sales_line_matches_part(line: dict[str, Any], part: dict[str, Any], part_id: str) -> bool:
    # Exact QBO Item Id match only. The invoice line's Product/Service ItemRef
    # value must equal the clicked inventory item's QBO Id. No name or
    # description fallbacks: those produced invoices where the part was only
    # mentioned but not actually the line item that was sold.
    _, item_id = _sales_line_item_ref(line)
    return bool(part_id and item_id and item_id == part_id)


def _part_purchase_events(realm_id: str, part: dict[str, Any]) -> list[dict[str, Any]]:
    part_id = str(part.get("qbo_item_id") or "")
    events: list[dict[str, Any]] = []
    if not part_id:
        return events
    for doc in _cached_purchases_with_item(realm_id, part_id):
        entity = str(doc.get("qbo_txn_type") or "Purchase")
        vendor = _purchase_vendor_name(doc)
        date = str(doc.get("txn_date") or doc.get("TxnDate") or "")
        doc_no = str(doc.get("doc_number") or doc.get("DocNumber") or doc.get("qbo_txn_id") or doc.get("Id") or "")
        # Match raw QBO lines so the item-based ItemRef.value is always present.
        # Account-based expense lines have no ItemRef and therefore never match a
        # specific inventory item (which is correct: nothing was bought as that
        # Product/Service).
        raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
        lines = (raw.get("Line") or []) or (doc.get("line_items") or [])
        for line in lines:
            if not _purchase_line_matches_part(line, part, part_id):
                continue
            qty, rate, amount = _purchase_line_qty_rate_amount(line)
            events.append(
                {
                    "kind": "bought",
                    "purchase_id": str(doc.get("qbo_txn_id") or doc.get("Id") or ""),
                    "purchase_type": entity,
                    "date": date,
                    "doc": doc_no,
                    "name": vendor,
                    "qty": qty,
                    "rate": rate,
                    "amount": amount,
                    "memo": str(line.get("Description") or entity),
                }
            )
    return events


def _part_adjustment_events(realm_id: str, part: dict[str, Any]) -> list[dict[str, Any]]:
    part_id = str(part.get("qbo_item_id") or "")
    events: list[dict[str, Any]] = []
    if not part_id:
        return events
    try:
        docs = _cached_adjustments_with_item(realm_id, part_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("Inventory adjustment cache unavailable: %s", exc)
        return []
    for doc in docs:
        account = str(doc.get("adjust_account_name") or "Inventory adjustment")
        date = str(doc.get("txn_date") or doc.get("TxnDate") or doc.get("AdjustmentDate") or "")
        doc_no = str(doc.get("doc_number") or doc.get("DocNumber") or doc.get("ReferenceNumber") or doc.get("qbo_adjustment_id") or doc.get("Id") or "")
        memo = str(doc.get("reason") or doc.get("private_note") or doc.get("PrivateNote") or "")
        # Match raw QBO ItemAdjustmentLineDetail lines by exact ItemRef.value.
        raw = doc.get("raw") if isinstance(doc.get("raw"), dict) else {}
        lines = (raw.get("Line") or []) or (doc.get("line_items") or [])
        for line in lines:
            if not _adjustment_line_matches_part(line, part, part_id):
                continue
            qty = _adjustment_line_qty_diff(line)
            line_memo = str(line.get("description") or line.get("Description") or memo or "")
            events.append(
                {
                    "kind": "adjusted",
                    "date": date,
                    "doc": doc_no,
                    "name": account,
                    "qty": qty,
                    "rate": None,
                    "amount": None,
                    "memo": line_memo,
                }
            )
    return events


def _adjustment_line_matches_part(line: dict[str, Any], part: dict[str, Any], part_id: str) -> bool:
    # Exact QBO Item Id match only (inventory adjustment lines always carry an
    # ItemRef). No name/description fallbacks.
    _, item_id = _adjustment_line_item_ref(line)
    return bool(part_id and item_id and item_id == part_id)


def _purchase_line_matches_part(line: dict[str, Any], part: dict[str, Any], part_id: str) -> bool:
    # Exact QBO Item Id match only. Item-based expense lines carry an ItemRef;
    # account-based lines do not and correctly never match a specific item.
    _, item_id = _purchase_line_item_ref(line)
    return bool(part_id and item_id and item_id == part_id)


def _line_text_matches_part(line: dict[str, Any], part: dict[str, Any]) -> bool:
    blob = _line_item_search_text(line)
    haystack = blob.lower()
    haystack_norm = _collapse_alnum(blob)
    needles = [
        str(part.get("name") or ""),
        str(part.get("fully_qualified_name") or ""),
        str(part.get("sku") or ""),
    ]
    for raw in needles:
        needle = raw.strip().lower()
        needle_norm = _collapse_alnum(raw)
        if needle and needle in haystack:
            return True
        if needle_norm and len(needle_norm) >= 4 and needle_norm in haystack_norm:
            return True
    return False


def _exact_item_name_key(value: Any) -> str:
    """Case-insensitive exact-ish key for QBO Product/Service names.

    This is intentionally stricter than search matching: it ignores casing and
    punctuation/spacing differences, but it never treats one item name as a
    substring of another.
    """
    return _collapse_alnum(str(value or ""))


def _sales_line_item_ref(line: dict[str, Any]) -> tuple[str, str]:
    if line.get("item_name") or line.get("item_id"):
        return str(line.get("item_name") or "").strip(), str(line.get("item_id") or "").strip()
    detail = line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    if isinstance(item_ref, dict):
        return str(item_ref.get("name") or "").strip(), str(item_ref.get("value") or "").strip()
    return "", ""


def _adjustment_line_item_ref(line: dict[str, Any]) -> tuple[str, str]:
    if line.get("item_name") or line.get("item_id"):
        return str(line.get("item_name") or "").strip(), str(line.get("item_id") or "").strip()
    detail = line.get("ItemAdjustmentLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    if isinstance(item_ref, dict):
        return str(item_ref.get("name") or "").strip(), str(item_ref.get("value") or "").strip()
    return "", ""


def _adjustment_line_qty_diff(line: dict[str, Any]) -> Any:
    if "qty_diff" in line:
        return line.get("qty_diff")
    detail = line.get("ItemAdjustmentLineDetail") or {}
    return detail.get("QtyDiff") if isinstance(detail, dict) else None


def _line_item_name(line: Any) -> str:
    if not isinstance(line, dict):
        return ""
    if line.get("item_name") or line.get("name"):
        return str(line.get("item_name") or line.get("name") or "").strip()
    detail = line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    return str(item_ref.get("name") or "").strip() if isinstance(item_ref, dict) else ""


def _line_item_qty_rate_amount(line: dict[str, Any]) -> tuple[Any, Any, Any]:
    if "item_name" in line or "unit_price" in line:
        return line.get("qty"), line.get("unit_price"), line.get("amount")
    detail = line.get("SalesItemLineDetail") or {}
    return detail.get("Qty"), detail.get("UnitPrice"), line.get("Amount")


def _purchase_line_item_ref(line: dict[str, Any]) -> tuple[str, str]:
    if line.get("item_name") or line.get("item_id"):
        return str(line.get("item_name") or "").strip(), str(line.get("item_id") or "").strip()
    detail = line.get("ItemBasedExpenseLineDetail") or line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    if isinstance(item_ref, dict):
        return str(item_ref.get("name") or "").strip(), str(item_ref.get("value") or "").strip()
    return "", ""


def _purchase_line_qty_rate_amount(line: dict[str, Any]) -> tuple[Any, Any, Any]:
    if "item_name" in line or "item_id" in line or "unit_price" in line:
        return line.get("qty"), line.get("unit_price"), line.get("amount")
    detail = line.get("ItemBasedExpenseLineDetail") or line.get("SalesItemLineDetail") or {}
    return detail.get("Qty"), detail.get("UnitPrice"), line.get("Amount")


def _purchase_vendor_name(doc: dict[str, Any]) -> str:
    if doc.get("vendor_name"):
        return str(doc.get("vendor_name") or "")
    for key in ("EntityRef", "VendorRef"):
        ref = doc.get(key)
        if isinstance(ref, dict) and ref.get("name"):
            return str(ref.get("name") or "")
    return "Vendor"


def _part_event_sort_key(ev: dict[str, Any]) -> tuple[int, str]:
    """Sort part-history events newest-first by a numeric date key.

    Converts the event date to a YYYYMMDD integer so ordering is correct
    regardless of the underlying string format and so events with a missing or
    unparseable date sort to the bottom instead of jumping to the top. The doc
    number is a stable tiebreak within the same day.
    """
    raw = str(ev.get("date") or "").strip()
    digits = re.sub(r"\D+", "", raw.split("T", 1)[0].split(" ", 1)[0])[:8]
    try:
        key = int(digits) if len(digits) == 8 else 0
    except ValueError:
        key = 0
    return (key, str(ev.get("doc") or ""))


def _part_event_html(ev: dict[str, Any]) -> str:
    kind = str(ev.get("kind") or "")
    is_sale = kind == "sold"
    is_adjustment = kind == "adjusted"
    badge = "Sold" if is_sale else "Adjusted" if is_adjustment else "Bought"
    badge_cls = "badge-price" if is_sale else "badge-draft" if is_adjustment else "badge-stock"
    who = str(ev.get("name") or "—")
    unit = str(ev.get("unit") or "").strip()
    qty = _fmt_qty(ev.get("qty")) or "—"
    amount = _fmt_price(ev.get("amount"))
    rate = _fmt_price(ev.get("rate"))
    qty_label = "Qty Δ" if is_adjustment else "Qty"
    bits = [f"{qty_label}: {qty}"]
    if rate:
        bits.append(f"Rate: {rate}")
    if amount:
        bits.append(f"Amount: {amount}")
    doc = str(ev.get("doc") or "")
    date = str(ev.get("date") or "")
    memo = str(ev.get("memo") or "")
    header_bits = [_short_history_date(date), who]
    if unit:
        header_bits.append(f"Unit {unit}")
    header = " · ".join(bit for bit in header_bits if bit)
    return (
        f"<div class='inv-card'>"
        f"<div class='inv-top'><span class='inv-no'>{_escape(header or '—')}</span>"
        f"<span class='badge {badge_cls}'>{badge}</span></div>"
        f"<div class='inv-meta'>{' · '.join(_escape(bit) for bit in bits)}"
        f"{(' · Doc: ' + _escape(doc)) if doc else ''}</div>"
        f"{f'<div class=\'li-desc\'>{_escape(memo)}</div>' if memo else ''}"
        f"</div>"
    )


def _short_history_date(value: Any) -> str:
    """Return compact M/D/YY for QBO date strings, else a readable fallback."""
    return _fmt_user_date(value)


def _fmt_user_date(value: Any) -> str:
    """Return compact M/D/YY for shop-facing dates."""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    # QBO cache dates are usually YYYY-MM-DD; raw LastUpdated-style values may
    # include a time suffix. Keep only the date portion for this mobile card.
    date_part = raw.split("T", 1)[0].split(" ", 1)[0]
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_part)
    if match:
        year, month, day = match.groups()
        return f"{int(month)}/{int(day)}/{year[-2:]}"
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", date_part)
    if match:
        month, day, year = match.groups()
        return f"{int(month)}/{int(day)}/{year[-2:]}"
    return date_part[:10] if len(date_part) > 10 else date_part


def _fmt_user_datetime(value: Any) -> str:
    """Return compact M/D/YY plus short time when a time is present."""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    date = _fmt_user_date(raw)
    match = re.search(r"(?:T|\s)(\d{1,2}):(\d{2})", raw)
    if not match:
        return date
    hour = int(match.group(1))
    minute = match.group(2)
    suffix = "a" if hour < 12 else "p"
    hour12 = hour % 12 or 12
    return f"{date} {hour12}:{minute}{suffix}"


def _part_shortage_value(item: dict[str, Any]) -> float:
    """Negative dollar shortage = qty_on_hand * unit cost, for qty < 0.

    Uses purchase_cost (cost basis) with sales_price as a fallback. Returns 0 for
    non-negative quantities. More negative = bigger problem.
    """
    try:
        qty = float(item.get("qty_on_hand"))
    except (TypeError, ValueError):
        return 0.0
    if qty >= 0:
        return 0.0
    cost = item.get("purchase_cost")
    if cost in (None, ""):
        cost = item.get("sales_price")
    try:
        unit_cost = float(cost)
    except (TypeError, ValueError):
        unit_cost = 0.0
    return qty * unit_cost  # negative number


def _negative_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only negative-stock parts, sorted by total dollar shortage (worst first)."""
    negs = []
    for item in parts:
        try:
            qty = float(item.get("qty_on_hand"))
        except (TypeError, ValueError):
            continue
        if qty < 0:
            negs.append(item)
    negs.sort(key=lambda it: (_part_shortage_value(it), str(it.get("name") or "").lower()))
    return negs


def _filter_parts(parts: list[dict[str, Any]], term: str) -> list[dict[str, Any]]:
    """Contains-filter parts by SKU, item name, and sales/purchase description.

    Mirrors how QuickBooks' item search behaves: a token matches if it appears
    (as a substring) in the SKU, item name, fully-qualified name, sales
    description, or purchase description. Every whitespace-separated token must
    match somewhere (AND), so "brake pad" narrows to items mentioning both.

    Light normalization collapses punctuation/spacing so a search for "00" also
    matches "0-0" and "valeo" matches "VALEO," - direct, not heavily fuzzy.

    Done in Python over the cached part list so typing filters instantly without
    a database query per keystroke. Blank term returns the whole catalog.
    """
    needle = term.strip().lower()
    if not needle:
        return parts
    tokens = [t for t in needle.split() if t]
    norm_tokens = [_collapse_alnum(t) for t in tokens]
    fields = ("sku", "name", "fully_qualified_name", "sales_description", "purchase_description")
    out: list[dict[str, Any]] = []
    for item in parts:
        haystack = " ".join(str(item.get(field) or "") for field in fields).lower()
        haystack_norm = _collapse_alnum(haystack)
        if all(
            (tok in haystack) or (ntok and ntok in haystack_norm)
            for tok, ntok in zip(tokens, norm_tokens)
        ):
            out.append(item)
    return out


def _collapse_alnum(value: str) -> str:
    """Lowercase and strip everything except letters/digits.

    Lets "00" match "0-0" and "11r225" match "11R22.5" without being so fuzzy
    that unrelated parts match.
    """
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _sort_inventory(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort the inventory list: in stock first, then by SKU, then by item name.

    1) Stock status (primary): items in stock (qty > 0, or quantity not tracked)
       come before out-of-stock items.
    2) SKU (secondary): ascending; items with no SKU sort after those that have
       one so the shelf-numbered parts lead.
    3) Item name (tertiary): alphabetical tiebreak.
    """

    def key(item: dict[str, Any]):
        qty_raw = item.get("qty_on_hand")
        try:
            qty = float(qty_raw) if qty_raw not in (None, "") else None
        except (TypeError, ValueError):
            qty = None
        out_of_stock = 1 if (qty is not None and qty <= 0) else 0
        sku = str(item.get("sku") or "").strip().lower()
        name = str(item.get("fully_qualified_name") or item.get("name") or "").strip().lower()
        return (out_of_stock, sku == "", sku, name)

    return sorted(parts, key=key)



def _render_add_popover(item: dict[str, Any], lang: str, realm_id: str) -> None:
    """The inventory "+" affordance: add the part to a draft or a new invoice."""
    item_id = str(item.get("qbo_item_id") or "")
    with st.popover("➕", use_container_width=True):
        # Low-stock warning: still allow adding, just flag that it may go negative.
        qty_raw = item.get("qty_on_hand")
        try:
            on_hand = float(qty_raw) if qty_raw is not None else None
        except (TypeError, ValueError):
            on_hand = None
        if on_hand is not None and on_hand <= 0:
            st.warning(f"{_t(lang, 'low_stock_warn')} ({_t(lang, 'in_stock')}: {_fmt_qty(qty_raw)})")

        # Primary action: start a brand-new invoice with this part on it.
        if st.button(
            f"🧾 {_t(lang, 'create_new_invoice')}",
            key=f"new_inv_{item_id}",
            use_container_width=True,
            type="primary",
        ):
            _clear_draft_derived_caches()
            for key in _INVOICE_FIELD_KEYS:
                st.session_state.pop(key, None)
            st.session_state["shop_cart"] = []
            _cart_add(item)
            _go(_VIEW_NEW_INVOICE)

        # Otherwise add it to one of the existing drafts. Show up to 5 (newest
        # first); if there are more, the count is noted so the list stays short.
        try:
            drafts = list_drafts(realm_id, limit=25)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load drafts for add popover: %s", exc)
            drafts = []
        if drafts:
            st.markdown(f"<div class='popover-section'>{_t(lang, 'add_to_draft')}</div>", unsafe_allow_html=True)
            for draft in drafts[:5]:
                draft_id = str(draft.get("id") or "")
                doc = str(draft.get("proposed_doc_number") or "—")
                customer = str(draft.get("customer_name") or "").strip()
                label = f"📝 #{doc}"
                if customer:
                    label += f" · {customer}"
                if st.button(label, key=f"add_draft_{draft_id}_{item_id}", use_container_width=True):
                    _add_item_to_draft(realm_id, draft_id, item)
            if len(drafts) > 5:
                st.caption(_t(lang, "more_drafts").format(n=len(drafts) - 5))


def _add_item_to_draft(realm_id: str, draft_id: str, item: dict[str, Any]) -> None:
    """Load a saved draft, add the part, persist, and open it in New Invoice."""
    draft = get_draft(draft_id)
    if not draft:
        st.session_state["shop_cart_flash"] = _t(st.session_state.get("shop_lang", "en"), "draft_err")
        st.rerun()
        return
    _load_draft_into_session(draft, navigate=False)
    _cart_add(item)
    # Persist immediately so the added part is saved even if he backs out.
    try:
        total = sum(
            float(li.get("unit_price") or 0) * int(li.get("qty") or 0) for li in _cart()
        )
        _autosave_draft(realm_id, total)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Autosave after add-to-draft failed: %s", exc)
    _go(_VIEW_NEW_INVOICE)


def _refresh_cart_line_stock(realm_id: str, lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refresh draft/cart line stock from the current inventory cache.

    Drafts store a snapshot of each line. If a draft is opened days later, that
    saved ``on_hand`` value can be stale (often 0). Rehydrate from the live
    synced inventory row so the cart shows the current QBO stock count.
    """
    if not realm_id or not lines:
        return lines
    try:
        parts = _all_active_parts(realm_id)
    except Exception:  # noqa: BLE001 - stale stock is better than crashing
        return lines

    def _part_stock(part: dict[str, Any]) -> Any:
        for key in ("qty_on_hand", "quantity_on_hand", "stock_qty", "on_hand"):
            if part.get(key) is not None:
                return part.get(key)
        return None

    def _line_item_id(line: dict[str, Any]) -> str:
        for key in ("qbo_item_id", "item_id", "item_ref_id", "product_service_id", "qbo_id"):
            value = line.get(key)
            if value:
                return str(value).strip()
        item_ref = line.get("ItemRef") or line.get("item_ref")
        if isinstance(item_ref, dict):
            return str(item_ref.get("value") or item_ref.get("id") or "").strip()
        return ""

    def _line_item_name(line: dict[str, Any]) -> str:
        for key in ("name", "item_name", "product_service_name", "invoice_name", "display_name"):
            value = line.get(key)
            if value:
                return str(value).strip().lower()
        item_ref = line.get("ItemRef") or line.get("item_ref")
        if isinstance(item_ref, dict):
            return str(item_ref.get("name") or "").strip().lower()
        return ""

    by_id = {str(part.get("qbo_item_id") or ""): part for part in parts if part.get("qbo_item_id")}
    by_name: dict[str, dict[str, Any]] = {}
    for part in parts:
        for name in (part.get("name"), part.get("fully_qualified_name"), part.get("invoice_name"), part.get("display_name")):
            key = str(name or "").strip().lower()
            if key and key not in by_name:
                by_name[key] = part

    for line in lines:
        if not isinstance(line, dict):
            continue
        item_id = _line_item_id(line)
        name = _line_item_name(line)
        part = by_id.get(item_id) if item_id else None
        if part is None and name:
            part = by_name.get(name)
        if part is None:
            continue
        line["qbo_item_id"] = str(part.get("qbo_item_id") or item_id)
        current_stock = _part_stock(part)
        if current_stock is not None:
            line["on_hand"] = current_stock
        if not line.get("sku"):
            line["sku"] = str(part.get("sku") or "")
    return lines


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_recent_invoices(realm_id: str, limit: int) -> list[dict[str, Any]]:
    return list_cached_invoices(realm_id, limit=limit)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_recent_purchases(realm_id: str, limit: int) -> list[dict[str, Any]]:
    return list_cached_purchases(realm_id, limit=limit)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_recent_adjustments(realm_id: str, limit: int) -> list[dict[str, Any]]:
    return list_cached_inventory_adjustments(realm_id, limit=limit)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_invoices_with_item(realm_id: str, item_id: str) -> list[dict[str, Any]]:
    """Invoices containing a part, server-side filtered via the line_items GIN index."""
    return list_invoices_with_item(realm_id, item_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_purchases_with_item(realm_id: str, item_id: str) -> list[dict[str, Any]]:
    """Purchases/bills containing a part, server-side filtered via the GIN index."""
    return list_purchases_with_item(realm_id, item_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_adjustments_with_item(realm_id: str, item_id: str) -> list[dict[str, Any]]:
    """Inventory adjustments containing a part, server-side filtered via the GIN index."""
    return list_adjustments_with_item(realm_id, item_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_part_history_events(realm_id: str, part_id: str) -> list[dict[str, Any]]:
    """Assemble a part's full sold/bought/adjusted history, newest first.

    Memoized per (realm, part): the heavy work (scanning the cached invoice,
    purchase, and adjustment history) runs once, so re-opening the same part -
    e.g. after viewing a document and tapping Back - is instant. Cleared on Sync
    All so a fresh sync is reflected.
    """
    part = _find_active_part(realm_id, part_id)
    if not part:
        return []
    sales = _part_sales_events(realm_id, part)
    purchases = _part_purchase_events(realm_id, part)
    adjustments = _part_adjustment_events(realm_id, part)
    return sorted(
        [*sales, *purchases, *adjustments],
        key=_part_event_sort_key,
        reverse=True,
    )


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_next_invoice_number(realm_id: str) -> int | None:
    qbo_client, _, _ = build_services()
    next_no = next_invoice_number(qbo_client, realm_id)

    # Also account for numbers already claimed by open drafts so two new invoices
    # started before either is posted don't collide. If a draft sits at 6948, the
    # next new invoice should be 6949 even though QBO has not seen 6948 yet.
    try:
        drafts = list_drafts(realm_id, limit=200)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load drafts for next-number: %s", exc)
        drafts = []
    highest_draft = 0
    for draft in drafts:
        doc = str(draft.get("proposed_doc_number") or "").strip()
        match = re.search(r"\d+", doc)
        if match:
            highest_draft = max(highest_draft, int(match.group(0)))

    candidates = [n for n in (next_no, highest_draft + 1 if highest_draft else None) if n]
    return max(candidates) if candidates else None


def _clear_draft_derived_caches() -> None:
    """Clear caches that depend on the open-draft set.

    Draft rows affect the proposed next invoice number and the inventory
    "On drafts" badges. Any create/update/delete/finalize of a draft should call
    this so a deleted #6948 immediately allows #6948 to be reused if QBO hasn't
    posted it yet.
    """
    try:
        _cached_next_invoice_number.clear()
        _draft_quantities.clear()
    except Exception:  # noqa: BLE001 - cache clear should never crash the UI
        pass


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_invoice_detail(realm_id: str, invoice_id: str) -> dict[str, Any] | None:
    return get_cached_invoice(realm_id, invoice_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_invoice_attachments(realm_id: str, invoice_id: str) -> list[dict[str, Any]]:
    """List QBO attachments for an invoice (live read, short-cached)."""
    qbo_client, _, _ = build_services()
    return list_invoice_attachments(qbo_client, realm_id, invoice_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_entity_attachments(realm_id: str, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
    """List QBO attachments for any entity (Invoice, Purchase, Bill…)."""
    qbo_client, _, _ = build_services()
    return list_entity_attachments(qbo_client, realm_id, entity_type, entity_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_attachment_index(realm_id: str) -> dict[str, list[dict[str, Any]]]:
    """All QBO file attachments indexed by linked entity.

    Prefers the Supabase attachment-index cache (instant; populated by Sync All),
    so the history lists can show has/no-document badges without scanning QBO on
    every page view. Falls back to a live QBO scan only when the cache is empty
    (e.g. before the first Sync All) so documents still work out of the box.
    Returns an empty dict on any failure so the lists still render.
    """
    try:
        cached = _attachment_index_from_cache(realm_id)
        if cached:
            return cached
    except Exception:  # noqa: BLE001 - cache read is best-effort; fall back to live
        logger.exception("Attachment index cache read failed; falling back to live scan")
    try:
        qbo_client, _, _ = build_services()
        return build_attachment_index(qbo_client, realm_id)
    except Exception:  # noqa: BLE001 - indicator is best-effort
        logger.exception("Attachment index build failed")
        return {}


def _attachment_index_from_cache(realm_id: str) -> dict[str, list[dict[str, Any]]]:
    """Rebuild the in-memory attachment index from the Supabase cache table."""
    if not realm_id:
        return {}
    _, _, supabase = build_services()
    rows = supabase.select_all(
        "shop_attachment_index_cache",
        select="entity_type,entity_id,attachments",
        filters={"realm_id": f"eq.{realm_id}"},
        order="entity_type",
    )
    index: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        entity_type = str(row.get("entity_type") or "").strip()
        entity_id = str(row.get("entity_id") or "").strip()
        atts = row.get("attachments")
        if entity_type and entity_id and isinstance(atts, list):
            index[index_key(entity_type, entity_id)] = atts
    return index



def _entity_has_documents(realm_id: str, entity_type: str, entity_id: str) -> bool | None:
    """True/False if the entity has attachments, or None if the index is unavailable."""
    entity_id = str(entity_id or "").strip()
    if not entity_id:
        return False
    try:
        index = _cached_attachment_index(realm_id)
    except Exception:  # noqa: BLE001
        return None
    if not index:
        return None
    return bool(index.get(index_key(entity_type, entity_id)))


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_purchase_detail(realm_id: str, txn_id: str) -> dict[str, Any] | None:
    return get_cached_purchase(realm_id, txn_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_attachment_bytes(realm_id: str, attachable_id: str, temp_download_uri: str = "") -> bytes:
    """Download one attachment's bytes.

    Prefers the ``TempDownloadUri`` captured when listing (avoids an extra QBO
    round-trip); falls back to requesting a fresh download URL if it expired.
    """
    qbo_client, _, _ = build_services()
    return download_attachment_bytes(
        qbo_client, realm_id, attachable_id, temp_download_uri=temp_download_uri
    )


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_invoice_history_synced_at(realm_id: str) -> str:
    return last_invoice_history_sync(realm_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_purchase_history_synced_at(realm_id: str) -> str:
    return last_purchase_history_sync(realm_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_inventory_adjustment_synced_at(realm_id: str) -> str:
    return last_inventory_adjustment_sync(realm_id)


@st.cache_data(ttl=_REALM_CACHE_TTL, show_spinner=False)
def _cached_customer_names(realm_id: str) -> list[str]:
    return customer_names(realm_id, "", limit=5000)


@st.cache_data(ttl=_SEARCH_CACHE_TTL, show_spinner=False)
def _cached_customer_search(realm_id: str, term: str) -> list[str]:
    return customer_names(realm_id, term, limit=25)


@st.cache_data(ttl=_REALM_CACHE_TTL, show_spinner=False)
def _cached_customer_synced_at(realm_id: str) -> str:
    return last_customer_sync(realm_id)


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_vehicle_customer_suggestions(realm_id: str, unit: str, vin: str) -> list[str]:
    unit_norm = _norm_vehicle_key(unit)
    vin_norm = _norm_vehicle_key(vin)
    if len(unit_norm) < 2 and len(vin_norm) < 5:
        return []

    scores: dict[str, int] = {}
    for inv in list_cached_invoices(realm_id, limit=1000):
        customer = str(inv.get("customer_name") or "").strip()
        if not customer:
            continue
        raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else {}
        custom_values = [value for _, value in custom_field_items(raw)]
        unit_candidates = [str(inv.get("unit") or ""), *custom_values]
        vin_candidates = [str(inv.get("vin") or ""), *custom_values]

        score = 0
        if unit_norm and any(_vehicle_match(unit_norm, c, min_len=2) for c in unit_candidates):
            score += 3
        if vin_norm and any(_vin_match(vin_norm, c) for c in vin_candidates):
            score += 5
        if score:
            scores[customer] = max(scores.get(customer, 0), score)

    return [name for name, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0].lower()))[:8]]


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _cached_vehicle_field_suggestions(realm_id: str, unit: str, vin: str) -> dict[str, list[str]]:
    """Suggest unit/VIN/miles values from cached invoice history.

    - ``units``: every distinct unit ever seen, so the unit dropdown can search
            the full list client-side; if a VIN is entered, narrow to units previously
            seen with that VIN so VIN -> Unit works the same way Unit -> VIN does.
    - ``vins`` / ``miles``: when a unit (or VIN) is already entered, narrow to the
      vehicles that match it; otherwise fall back to every distinct value so the
      dropdown is never empty.
    """
    unit_norm = _norm_vehicle_key(unit)
    vin_norm = _norm_vehicle_key(vin)
    all_units: list[str] = []
    all_vins: list[str] = []
    all_miles: list[str] = []
    seen_vin_norms: set[str] = set()
    matched_units: list[str] = []
    matched_vins: list[str] = []
    matched_miles: list[str] = []
    if not realm_id:
        return {"units": [], "vins": [], "miles": []}

    for inv in list_cached_invoices(realm_id, limit=2000):
        vehicle = _invoice_vehicle_values(inv)
        inv_unit = vehicle["unit"]
        # Normalize VIN for display: uppercase + trimmed, deduped case-insensitively
        # so "1w700482" and "1W700482" collapse to a single suggestion.
        inv_vin = vehicle["vin"].strip().upper()
        inv_vin_norm = _norm_vehicle_key(inv_vin)
        inv_miles = vehicle["miles"]

        if inv_unit and inv_unit not in all_units:
            all_units.append(inv_unit)
        if inv_vin and inv_vin_norm and inv_vin_norm not in seen_vin_norms:
            seen_vin_norms.add(inv_vin_norm)
            all_vins.append(inv_vin)
        if inv_miles and inv_miles not in all_miles:
            all_miles.append(inv_miles)

        unit_match = bool(unit_norm and _vehicle_match(unit_norm, inv_unit, min_len=2))
        vin_match = bool(vin_norm and _vin_match(vin_norm, inv_vin))
        if unit_match or vin_match:
            if inv_unit and inv_unit not in matched_units:
                matched_units.append(inv_unit)
            if inv_vin and inv_vin not in matched_vins:
                matched_vins.append(inv_vin)
            if inv_miles and inv_miles not in matched_miles:
                matched_miles.append(inv_miles)

    has_query = bool(unit_norm or vin_norm)
    units = matched_units if (has_query and matched_units) else all_units
    vins = matched_vins if (has_query and matched_vins) else all_vins
    miles = matched_miles if (has_query and matched_miles) else all_miles
    return {
        "units": units[:1000],
        "vins": vins[:1000],
        "miles": miles[:500],
    }


def _invoice_vehicle_values(inv: dict[str, Any]) -> dict[str, str]:
    """Return Unit/VIN/Miles from cache columns or raw QBO custom fields."""
    raw_invoice = inv.get("raw") if isinstance(inv.get("raw"), dict) else {}
    custom = custom_field_map(raw_invoice)
    return {
        "unit": str(inv.get("unit") or custom.get("unit") or "").strip(),
        "vin": str(inv.get("vin") or custom.get("vin") or "").strip(),
        "miles": str(inv.get("miles") or custom.get("miles") or "").strip(),
    }


def _norm_vehicle_key(value: str) -> str:
    """Normalize a unit/VIN for comparison: uppercase, alphanumeric only.

    This makes matching case-insensitive and ignores spaces/dashes, so the same
    VIN typed as ``1w7 00482`` and ``1W700482`` compare equal.
    """
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _vehicle_match(needle: str, candidate: str, *, min_len: int) -> bool:
    """Loose match for UNIT numbers (substring either direction)."""
    haystack = _norm_vehicle_key(candidate)
    if len(needle) < min_len or len(haystack) < min_len:
        return False
    return needle in haystack or haystack in needle


def _vin_match(a_norm: str, b_value: str, *, min_len: int = 5) -> bool:
    """Match VINs by suffix containment so partials map to the full VIN.

    Real VINs are 17 chars but the shop often enters only the last 6-8. So a
    match means one normalized VIN is a *suffix* of the other (e.g. ``700482``
    matches ``1GRAR06281W700482``). Suffix containment - not "share the last N
    characters" - prevents two genuinely different VINs that merely end the same
    (``ABC700482`` vs ``XYZ700482``) from being treated as the same vehicle.
    """
    b_norm = _norm_vehicle_key(b_value)
    if len(a_norm) < min_len or len(b_norm) < min_len:
        return False
    return a_norm == b_norm or a_norm.endswith(b_norm) or b_norm.endswith(a_norm)


def _render_history_view(lang: str, realm_id: str) -> None:
    """Invoice History view (Button 3): read-only list of recent QBO invoices.

    Each invoice shows its custom Unit / VIN / Miles fields and a button to open
    a full detail view with all line items.
    """
    _render_view_header(lang, "history_title")

    if not realm_id:
        st.info(_t(lang, "not_connected"))
        return

    synced_at = _cached_invoice_history_synced_at(realm_id)
    if synced_at:
        st.caption(f"{_t(lang, 'updated')}: {_fmt_user_datetime(synced_at)}")
    if st.button(f"🔄 {_t(lang, 'history_refresh')}", use_container_width=True):
        with st.spinner(_t(lang, "history_refreshing")):
            result = sync_shop_invoice_history(realm_id)
        _cached_recent_invoices.clear()
        _cached_invoice_detail.clear()
        _cached_invoice_history_synced_at.clear()
        if result.status == "success":
            if result.invoices_upserted:
                st.success(f"{_t(lang, 'history_refresh_done')} (+{result.invoices_upserted})")
            else:
                st.info(_t(lang, "history_refresh_none"))
        else:
            st.error(f"{_t(lang, 'history_error')} {result.message}")
        st.rerun()

    # --- Your drafts (in Supabase only, NOT in QuickBooks). Clear distinction. ---
    _render_drafts_section(lang, realm_id)

    st.markdown(
        f"<div class='shop-title'>{_t(lang, 'qbo_invoices_title')}</div>",
        unsafe_allow_html=True,
    )
    hist_search = st.text_input(
        _t(lang, "history_title"),
        key="invoice_history_search",
        placeholder=_t(lang, "history_search_placeholder"),
        label_visibility="collapsed",
    ).strip()
    oil_col, unpaid_col = st.columns(2)
    with oil_col:
        oil_only = st.toggle(
            f"🛢️ {_t(lang, 'oil_changes_only')}",
            key="invoice_history_oil_only",
        )
    with unpaid_col:
        unpaid_only = st.toggle(
            f"💵 {_t(lang, 'unpaid_only')}",
            key="invoice_history_unpaid_only",
        )
    has_filter = bool(hist_search or oil_only or unpaid_only)
    # Unfiltered: clean recent window (avoids old high-numbered outliers at top).
    # Filtered: wide multi-year window so search can reach old invoices.
    window = _INVOICE_HISTORY_WINDOW if has_filter else _INVOICE_HISTORY_DEFAULT
    try:
        with st.spinner(_t(lang, "history_loading")):
            invoices = _cached_recent_invoices(realm_id, window)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Invoice history load failed: %s", exc)
        st.error(_t(lang, "history_error"))
        return

    invoices = _filter_history_invoices(invoices, hist_search, oil_only, unpaid_only)

    if not invoices:
        st.info(_t(lang, "history_no_filter_results") if has_filter else _t(lang, "history_empty"))
        return

    # Keep the page responsive: render only the newest slice and tell the user to
    # search for anything older.
    total = len(invoices)
    if total > _INVOICE_HISTORY_RENDER_CAP:
        invoices = invoices[:_INVOICE_HISTORY_RENDER_CAP]
        st.caption(_t(lang, "history_render_cap").format(shown=len(invoices), total=total))

    # Build the attachment presence index once (one scan, cached) so each row can
    # show a has/no-document badge without a per-invoice API call.
    with st.spinner(_t(lang, "docs_loading")):
        doc_index = _cached_attachment_index(realm_id)

    for inv in invoices:
        st.markdown(_invoice_html(inv, lang), unsafe_allow_html=True)
        inv_id = str(inv.get("qbo_invoice_id") or inv.get("Id") or "").strip()
        if not inv_id:
            continue
        atts = doc_index.get(index_key("Invoice", inv_id), []) if doc_index else None
        docs_col, view_col = st.columns(2)
        with docs_col:
            _render_doc_indicator(lang, "Invoice", inv_id, atts, key=f"inv_docs_{inv_id}")
        with view_col:
            if st.button(
                f"🔍 {_t(lang, 'view_details')}",
                key=f"inv_detail_{inv_id}",
                use_container_width=True,
            ):
                _open_invoice_detail(inv_id)


def _render_doc_indicator(
    lang: str,
    entity_type: str,
    entity_id: str,
    atts: list[dict[str, Any]] | None,
    *,
    key: str,
) -> None:
    """Inline document control for a history row.

    ``atts`` is the attachment list from the cached index: a non-empty list means
    documents exist; an empty list means none; ``None`` means the index was
    unavailable (fall back to a button that fetches on demand).
    """
    if atts is None:
        # Index unavailable - offer the on-demand documents view.
        if st.button(f"📎 {_t(lang, 'view_docs')}", key=key, use_container_width=True):
            if entity_type == "Invoice":
                _open_invoice_documents(entity_id)
            else:
                _open_purchase_detail(entity_id)
        return
    if not atts:
        st.caption(f"📎 {_t(lang, 'docs_none_short')}")
        return
    # One file with a live URL -> one-tap download. Otherwise open the docs view.
    single_uri = str(atts[0].get("temp_download_uri") or "") if len(atts) == 1 else ""
    if single_uri:
        st.link_button(
            f"⬇ {_t(lang, 'docs_download')}",
            single_uri,
            use_container_width=True,
        )
        return
    if st.button(
        f"📎 {_t(lang, 'view_docs')} ({len(atts)})",
        key=key,
        use_container_width=True,
    ):
        if entity_type == "Invoice":
            _open_invoice_documents(entity_id)
        else:
            _open_purchase_detail(entity_id)



def _render_invoice_documents_view(lang: str, realm_id: str) -> None:
    """Show the original scanned documents attached to an invoice in QuickBooks.

    Read-only. Fetches the QBO ``Attachable`` files linked to this invoice on
    demand (no local storage), renders images inline, and offers a download for
    every file type.
    """
    _render_view_header(lang, "docs_title")

    inv_id = (_query_param_value("invoice_id") or str(st.session_state.get("shop_invoice_id") or "")).strip()
    if st.button(f"⬅ {_t(lang, 'card_history')}", key="docs_back_hist_top"):
        _go(_VIEW_HISTORY)
    if not realm_id or not inv_id:
        st.info(_t(lang, "detail_error"))
        return

    # Header card so the user knows which invoice these documents belong to.
    try:
        inv = _cached_invoice_detail(realm_id, inv_id)
    except Exception:  # noqa: BLE001 - header is best-effort
        inv = None
    if inv:
        st.markdown(_invoice_html(inv, lang), unsafe_allow_html=True)

    try:
        with st.spinner(_t(lang, "docs_loading")):
            attachments = _cached_invoice_attachments(realm_id, inv_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Invoice documents load failed: %s", exc)
        st.error(_t(lang, "docs_error"))
        return

    if not attachments:
        st.info(_t(lang, "docs_none"))
        return

    for att in attachments:
        _render_single_attachment(lang, realm_id, att)


def _render_single_attachment(lang: str, realm_id: str, att: dict[str, Any]) -> None:
    """Render one QBO attachment, download-first.

    The download control renders immediately (a direct, pre-signed QuickBooks
    URL when available) so the list never blocks on byte transfers. The inline
    preview is lazy: bytes are only pulled when the user opens the preview,
    showing a spinner while the backend fetches them.
    """
    attachable_id = str(att.get("attachable_id") or "")
    file_name = str(att.get("file_name") or "document")
    content_type = str(att.get("content_type") or "").lower()
    note = str(att.get("note") or "").strip()
    temp_uri = str(att.get("temp_download_uri") or "").strip()

    st.markdown(f"<div class='shop-title'>📎 {_escape(file_name)}</div>", unsafe_allow_html=True)
    if note:
        st.caption(note)

    # Instant, zero-wait download straight from QuickBooks' pre-signed URL. The
    # browser hits the QBO CDN directly, so this renders without any backend wait.
    if temp_uri:
        st.link_button(
            f"⬇ {_t(lang, 'docs_download')} — {file_name}",
            temp_uri,
            use_container_width=True,
        )

    name_lower = file_name.lower()
    is_image = content_type.startswith("image/") or name_lower.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
    )
    is_pdf = content_type == "application/pdf" or name_lower.endswith(".pdf")
    can_preview = is_image or is_pdf

    # Lazy preview + reliable (app-routed) download. Bytes are only fetched when
    # the user opens this expander, so the documents list paints instantly. If
    # the pre-signed URL above has expired, this path requests a fresh one.
    label = _t(lang, "docs_preview") if can_preview else _t(lang, "docs_download")
    with st.expander(f"👁 {label}", expanded=False):
        with st.spinner(_t(lang, "docs_loading")):
            try:
                data = _cached_attachment_bytes(realm_id, attachable_id, temp_uri)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Attachment download failed: %s", exc)
                data = b""
        if not data:
            st.warning(_t(lang, "docs_error"))
            return
        if is_image:
            st.image(data, use_container_width=True)
        elif is_pdf:
            _render_pdf_preview(data, file_name)
        else:
            st.info(_t(lang, "docs_unsupported"))
        st.download_button(
            f"⬇ {_t(lang, 'docs_download')} — {file_name}",
            data=data,
            file_name=file_name,
            mime=content_type or "application/octet-stream",
            use_container_width=True,
            key=f"dl_att_{attachable_id}",
        )


def _render_pdf_preview(data: bytes, file_name: str = "document.pdf") -> None:
    """Inline PDF preview.

    Chrome blocks ``data:application/pdf`` iframes for security ("This page has
    been blocked by Chrome"), so prefer Streamlit's native PDF renderer when the
    installed version supports it. The download button above is always the
    reliable fallback.
    """
    pdf_renderer = getattr(st, "pdf", None)
    if callable(pdf_renderer):
        try:
            pdf_renderer(data, height=700)
            return
        except Exception:  # noqa: BLE001 - fall through to the caption below
            logger.exception("st.pdf preview failed for %s", file_name)
    st.caption("📄 Preview not supported in this browser — use the download button below.")


def _render_documents_section(
    lang: str, realm_id: str, entity_type: str, entity_id: str
) -> None:
    """Render the scanned-document download/preview block for any QBO entity."""
    st.markdown(
        f"<div class='shop-title'>📎 {_t(lang, 'docs_title')}</div>",
        unsafe_allow_html=True,
    )
    try:
        with st.spinner(_t(lang, "docs_loading")):
            atts = _cached_entity_attachments(realm_id, entity_type, entity_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Documents load failed for %s %s: %s", entity_type, entity_id, exc)
        st.error(_t(lang, "docs_error"))
        return
    if not atts:
        st.caption(_t(lang, "docs_none_short"))
        return
    for att in atts:
        _render_single_attachment(lang, realm_id, att)


def _purchase_line_html(line: dict[str, Any]) -> str:
    """One purchase/bill line row for the purchase detail view."""
    if not isinstance(line, dict):
        return ""
    name = str(line.get("item_name") or line.get("account_name") or line.get("description") or "—")
    qty = _fmt_qty(line.get("qty")) or "—"
    rate = _fmt_price(line.get("unit_price"))
    amount = _fmt_price(line.get("amount"))
    desc = str(line.get("description") or "")
    bits = [f"Qty: {qty}"]
    if rate:
        bits.append(f"Rate: {rate}")
    if amount:
        bits.append(f"Amount: {amount}")
    return (
        f"<div class='inv-card'>"
        f"<div class='inv-top'><span class='inv-no'>{_escape(name)}</span></div>"
        f"<div class='inv-meta'>{' · '.join(_escape(bit) for bit in bits)}</div>"
        f"{f'<div class=\"li-desc\">{_escape(desc)}</div>' if desc and desc != name else ''}"
        f"</div>"
    )


def _render_purchase_detail_view(lang: str, realm_id: str) -> None:
    """Purchase/Bill detail: line items + scanned documents, with download."""
    _render_view_header(lang, "purchase_history_title")

    txn_id = str(st.session_state.get("shop_purchase_id") or "").strip()
    return_part_id = str(st.session_state.get("shop_purchase_return_part_id") or "").strip()
    back_label = "Part detail" if return_part_id else _t(lang, "card_purchase_history")
    if st.button(f"⬅ {back_label}", key="purchase_detail_back_top"):
        if return_part_id:
            _open_part_detail(return_part_id)
        else:
            _go(_VIEW_PURCHASE_HISTORY)
    if not realm_id or not txn_id:
        st.info(_t(lang, "detail_error"))
        return

    try:
        with st.spinner(_t(lang, "history_loading")):
            doc = _cached_purchase_detail(realm_id, txn_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Purchase detail load failed: %s", exc)
        doc = None
    if not doc:
        st.error(_t(lang, "detail_error"))
        return

    st.markdown(_purchase_html(doc), unsafe_allow_html=True)

    lines = doc.get("line_items") or []
    if lines:
        st.markdown(f"<div class='shop-title'>{_t(lang, 'line_items')}</div>", unsafe_allow_html=True)
        st.markdown("".join(_purchase_line_html(line) for line in lines), unsafe_allow_html=True)

    entity_type = str(doc.get("qbo_txn_type") or "Purchase")
    _render_documents_section(lang, realm_id, entity_type, txn_id)


def _render_purchase_history_view(lang: str, realm_id: str) -> None:
    """Purchase-side history: QBO Purchase + Bill docs cached in Supabase."""
    _render_view_header(lang, "purchase_history_title")

    if not realm_id:
        st.info(_t(lang, "not_connected"))
        return

    synced_at = _cached_purchase_history_synced_at(realm_id)
    if synced_at:
        st.caption(f"{_t(lang, 'updated')}: {_fmt_user_datetime(synced_at)}")
    refresh_col, full_col = st.columns(2)
    with refresh_col:
        if st.button(f"🔄 {_t(lang, 'purchase_history_refresh')}", use_container_width=True):
            _run_purchase_history_refresh(realm_id, lang, force_full=False)
    with full_col:
        if st.button(f"⟳ {_t(lang, 'purchase_history_full_refresh')}", use_container_width=True):
            _run_purchase_history_refresh(realm_id, lang, force_full=True)

    term = st.text_input(
        _t(lang, "purchase_history_title"),
        key="purchase_history_search",
        placeholder=_t(lang, "purchase_history_search_placeholder"),
        label_visibility="collapsed",
    ).strip()
    try:
        with st.spinner(_t(lang, "history_loading")):
            purchases = _cached_recent_purchases(realm_id, 1000)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Purchase history load failed: %s", exc)
        st.error(_t(lang, "history_error"))
        return
    purchases = _filter_purchase_history(purchases, term)
    if not purchases:
        st.info(_t(lang, "history_no_filter_results") if term else _t(lang, "purchase_history_empty"))
        return
    with st.spinner(_t(lang, "docs_loading")):
        doc_index = _cached_attachment_index(realm_id)
    for row in purchases:
        st.markdown(_purchase_html(row), unsafe_allow_html=True)
        txn_id = str(row.get("qbo_txn_id") or "").strip()
        if not txn_id:
            continue
        etype = str(row.get("qbo_txn_type") or "Purchase")
        atts = doc_index.get(index_key(etype, txn_id), []) if doc_index else None
        docs_col, view_col = st.columns(2)
        with docs_col:
            _render_doc_indicator(lang, etype, txn_id, atts, key=f"pur_docs_{txn_id}")
        with view_col:
            if st.button(
                f"🔍 {_t(lang, 'view_details')}",
                key=f"pur_detail_{txn_id}",
                use_container_width=True,
            ):
                _open_purchase_detail(txn_id)


def _run_purchase_history_refresh(realm_id: str, lang: str, *, force_full: bool) -> None:
    with st.spinner(_t(lang, "purchase_history_refreshing")):
        result = sync_shop_purchase_history(realm_id, force_full=force_full)
    _cached_recent_purchases.clear()
    _cached_purchase_history_synced_at.clear()
    if result.status == "success":
        if result.purchases_upserted:
            st.success(f"{_t(lang, 'purchase_history_refresh_done')} (+{result.purchases_upserted})")
        else:
            st.info(_t(lang, "purchase_history_refresh_none"))
    else:
        st.error(f"{_t(lang, 'history_error')} {result.message}")
    st.rerun()


def _filter_purchase_history(rows: list[dict[str, Any]], term: str) -> list[dict[str, Any]]:
    tokens = [tok for tok in str(term or "").lower().split() if tok]
    if not tokens:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        blob = _purchase_search_blob(row).lower()
        if all(tok in blob for tok in tokens):
            out.append(row)
    return out


def _purchase_search_blob(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("vendor_name") or ""),
        str(row.get("doc_number") or ""),
        str(row.get("qbo_txn_type") or ""),
        str(row.get("payment_type") or ""),
    ]
    for line in row.get("line_items") or []:
        parts.append(_line_item_search_text(line))
        parts.append(str(line.get("item_id") or "")) if isinstance(line, dict) else None
    return " ".join(parts)


def _purchase_html(row: dict[str, Any]) -> str:
    date = _fmt_user_date(row.get("txn_date") or "")
    vendor = str(row.get("vendor_name") or "Vendor")
    doc = str(row.get("doc_number") or row.get("qbo_txn_id") or "")
    txn_type = str(row.get("qbo_txn_type") or "Purchase")
    payment_type = str(row.get("payment_type") or "")
    total = _fmt_price(row.get("total"))
    lines = row.get("line_items") or []
    line_bits = []
    for line in lines[:6]:
        if not isinstance(line, dict):
            continue
        name = str(line.get("item_name") or line.get("account_name") or line.get("description") or "")
        qty = _fmt_qty(line.get("qty")) or "—"
        amt = _fmt_price(line.get("amount"))
        line_bits.append(f"{_escape(name or '—')} · Qty: {_escape(qty)}{(' · ' + _escape(amt)) if amt else ''}")
    lines_html = "".join(f"<div class='li-desc'>{bit}</div>" for bit in line_bits)
    label = txn_type if not payment_type else f"{txn_type} · {payment_type}"
    return (
        f"<div class='inv-card'>"
        f"<div class='inv-top'><span class='inv-no'>{_escape(date or '—')} · {_escape(vendor)}</span>"
        f"<span class='badge badge-cost'>{_escape(label)}</span></div>"
        f"<div class='inv-meta'>Doc: {_escape(doc or '—')}{(' · Total: ' + _escape(total)) if total else ''}</div>"
        f"{lines_html}"
        f"</div>"
    )


def _render_drafts_section(lang: str, realm_id: str) -> None:
    """Show shop drafts (Supabase only) with a clear 'not in QuickBooks' banner."""
    try:
        drafts = list_drafts(realm_id, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load drafts: %s", exc)
        drafts = []

    st.markdown(
        f"<div class='drafts-banner'>{_t(lang, 'drafts_title')}"
        f"<span class='drafts-sub'>{_t(lang, 'drafts_help')}</span></div>",
        unsafe_allow_html=True,
    )
    if not drafts:
        st.caption(_t(lang, "no_drafts"))
        return

    for draft in drafts:
        draft_id = str(draft.get("id") or "")
        doc = str(draft.get("proposed_doc_number") or "—")
        customer = str(draft.get("customer_name") or "")
        unit = str(draft.get("truck_unit") or "")
        total = _fmt_price(draft.get("total"))
        n_lines = len(draft.get("line_items") or [])
        st.markdown(
            f"<div class='draft-card'>"
            f"<div class='draft-top'><span class='draft-badge'>{_t(lang, 'drafts_title')}</span>"
            f"<span class='inv-no'>#{_escape(doc)}</span></div>"
            f"<div class='inv-customer'>{_escape(customer)}</div>"
            f"<div class='inv-meta'>{_t(lang, 'truck_unit')}: {_escape(unit) or '—'} · "
            f"{n_lines} {_t(lang, 'results')} · {total}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        edit_col, del_col = st.columns(2)
        with edit_col:
            if st.button(f"✏️ {_t(lang, 'edit_draft')}", key=f"draft_edit_{draft_id}", use_container_width=True):
                full = get_draft(draft_id) or draft
                _load_draft_into_session(full)
        with del_col:
            if st.button(f"🗑 {_t(lang, 'delete_draft')}", key=f"draft_del_{draft_id}", use_container_width=True):
                delete_draft(draft_id)
                _clear_draft_derived_caches()
                st.rerun()


def _filter_history_invoices(
    invoices: list[dict[str, Any]], search_term: str, oil_only: bool, unpaid_only: bool
) -> list[dict[str, Any]]:
    """Filter cached invoice rows by Unit/VIN/Customer, Oil Change, and unpaid."""
    tokens = [tok for tok in str(search_term or "").strip().lower().split() if tok]
    out: list[dict[str, Any]] = []
    for inv in invoices:
        if tokens:
            blob = _invoice_history_search_blob(inv).lower()
            if not all(tok in blob for tok in tokens):
                continue
        if oil_only and not _invoice_has_oil_change(inv):
            continue
        if unpaid_only and not _invoice_is_unpaid(inv):
            continue
        out.append(inv)
    return out


def _invoice_is_unpaid(inv: dict[str, Any]) -> bool:
    """True when cached QBO balance is greater than zero."""
    value = inv.get("balance", inv.get("Balance"))
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _invoice_history_search_blob(inv: dict[str, Any]) -> str:
    """Searchable vehicle/customer text for the history filter."""
    vehicle = _invoice_vehicle_values(inv)
    raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else inv
    customer = str(inv.get("customer_name") or "").strip()
    if not customer:
        ref = raw.get("CustomerRef") if isinstance(raw, dict) else None
        if isinstance(ref, dict):
            customer = str(ref.get("name") or "").strip()
    return " ".join(
        str(value or "")
        for value in (
            customer,
            vehicle.get("unit"),
            vehicle.get("vin"),
            vehicle.get("miles"),
        )
    )


def _invoice_has_oil_change(inv: dict[str, Any]) -> bool:
    """True when any invoice line's item/product-service name is Oil Change."""
    line_sources: list[Any] = []
    line_items = inv.get("line_items")
    if isinstance(line_items, list):
        line_sources.extend(line_items)
    raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else inv
    raw_lines = raw.get("Line") if isinstance(raw, dict) else None
    if isinstance(raw_lines, list):
        line_sources.extend(raw_lines)

    for line in line_sources:
        text = _line_item_search_text(line).lower()
        # QBO item name is expected to be "Oil Change". Contains-match is used so
        # names like "Oil Change - Diesel" still qualify.
        if "oil change" in text:
            return True
    return False


def _line_item_search_text(line: Any) -> str:
    """Best-effort item-name text extraction for normalized and raw QBO lines."""
    if not isinstance(line, dict):
        return str(line or "")
    parts: list[str] = []
    for key in ("item_id", "item_name", "name", "account_name", "description", "Description", "qty_diff"):
        value = line.get(key)
        if value:
            parts.append(str(value))
    detail = line.get("SalesItemLineDetail")
    if isinstance(detail, dict):
        item_ref = detail.get("ItemRef")
        if isinstance(item_ref, dict):
            parts.append(str(item_ref.get("name") or ""))
            parts.append(str(item_ref.get("value") or ""))
    detail = line.get("ItemAdjustmentLineDetail")
    if isinstance(detail, dict):
        item_ref = detail.get("ItemRef")
        if isinstance(item_ref, dict):
            parts.append(str(item_ref.get("name") or ""))
            parts.append(str(item_ref.get("value") or ""))
        parts.append(str(detail.get("QtyDiff") or ""))
    item_ref = line.get("ItemRef")
    if isinstance(item_ref, dict):
        parts.append(str(item_ref.get("name") or ""))
    return " ".join(part for part in parts if part)


def _invoice_html(inv: dict[str, Any], lang: str) -> str:
    doc = str(inv.get("doc_number") or inv.get("DocNumber") or "—").strip()
    txn_date = _fmt_user_date(inv.get("txn_date") or inv.get("TxnDate") or "")
    customer = str(inv.get("customer_name") or "").strip()
    if not customer:
        ref = inv.get("CustomerRef")
        if isinstance(ref, dict):
            customer = str(ref.get("name") or "").strip()
    total = _fmt_price(inv.get("total", inv.get("TotalAmt")))
    balance_raw = inv.get("balance", inv.get("Balance"))
    balance = _fmt_price(balance_raw)

    # Custom QBO fields shown as a small meta line. Prefer the actual QBO labels
    # from raw.CustomField so this works even if names are "Unit #" / "Mileage"
    # etc. Fallback to cache columns for already-synced older rows.
    raw_invoice = inv.get("raw") if isinstance(inv.get("raw"), dict) else inv
    meta_bits = [
        f"{_escape(label)}: {_escape(value)}"
        for label, value in custom_field_items(raw_invoice)
    ]
    if not meta_bits:
        fields = {
            "unit": str(inv.get("unit") or ""),
            "vin": str(inv.get("vin") or ""),
            "miles": str(inv.get("miles") or ""),
        }
        meta_bits = [
            f"{_t(lang, key)}: {_escape(value)}"
            for key, value in fields.items()
            if value
        ]
    custom_html = (
        f"<div class='inv-meta'>{' · '.join(meta_bits)}</div>" if meta_bits else ""
    )

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
        f"</div>{customer_html}{custom_html}"
        f"<div class='inv-amounts'>{''.join(badges)}</div>"
        f"</div>"
    )


def _render_invoice_detail_view(lang: str, realm_id: str) -> None:
    """Full read-only invoice detail: header, custom fields, and all line items.

    Mirrors how the invoice looks when editing in QuickBooks (item, description,
    qty, rate, amount), scrollable for long invoices.
    """
    _render_view_header(lang, "history_title")

    inv_id = (_query_param_value("invoice_id") or str(st.session_state.get("shop_invoice_id") or "")).strip()
    return_part_id = str(st.session_state.get("shop_invoice_return_part_id") or "").strip()
    back_label = "Part detail" if return_part_id else _t(lang, "card_history")
    if not realm_id or not inv_id:
        st.info(_t(lang, "detail_error"))
        if st.button(f"⬅ {back_label}", key="detail_back_hist"):
            if return_part_id:
                _open_part_detail(return_part_id)
            else:
                _go(_VIEW_HISTORY)
        return

    if st.button(f"⬅ {back_label}", key="detail_back_hist_top"):
        if return_part_id:
            _open_part_detail(return_part_id)
        else:
            _go(_VIEW_HISTORY)

    try:
        with st.spinner(_t(lang, "history_loading")):
            inv = _cached_invoice_detail(realm_id, inv_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Invoice detail load failed: %s", exc)
        inv = None
    if not inv:
        st.error(_t(lang, "detail_error"))
        return

    # Header card (reuse the list card layout) + line items below.
    st.markdown(_invoice_html(inv, lang), unsafe_allow_html=True)
    if st.button(f"📋 {_t(lang, 'copy_to_draft')}", key=f"copy_inv_{inv_id}", use_container_width=True, type="primary"):
        _copy_invoice_to_new_draft_session(inv, realm_id, lang)
        st.session_state["shop_cart_flash"] = _t(lang, "copy_to_draft_ok")
        _go(_VIEW_NEW_INVOICE)

    lines = inv.get("line_items") or []
    if not lines:
        raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else inv
        lines = [
            line for line in (raw.get("Line") or [])
            if str(line.get("DetailType") or "") == "SalesItemLineDetail"
        ]
    if lines:
        st.markdown(f"<div class='shop-title'>{_t(lang, 'line_items')}</div>", unsafe_allow_html=True)
        st.markdown("".join(_line_item_html(line, lang) for line in lines), unsafe_allow_html=True)
    else:
        st.info(_t(lang, "no_lines"))

    # Scanned supporting documents (read on demand from QBO).
    _render_documents_section(lang, realm_id, "Invoice", inv_id)


def _copy_invoice_to_new_draft_session(inv: dict[str, Any], realm_id: str, lang: str) -> None:
    """Copy a historical QBO invoice into a fresh editable shop draft session."""
    for key in _INVOICE_FIELD_KEYS:
        st.session_state.pop(key, None)
    _clear_draft_derived_caches()
    try:
        next_no = _cached_next_invoice_number(realm_id)
    except Exception:  # noqa: BLE001
        next_no = None

    vehicle = _invoice_vehicle_values(inv)
    st.session_state["invoice_doc_number"] = str(next_no or "")
    st.session_state["invoice_customer"] = _invoice_customer_name(inv)
    st.session_state["invoice_customer_is_new"] = False
    st.session_state["invoice_truck"] = vehicle.get("unit", "")
    st.session_state["invoice_vin"] = vehicle.get("vin", "")
    # Do NOT copy old miles; the new visit should enter current miles if needed.
    st.session_state["invoice_miles"] = ""
    st.session_state["invoice_notes"] = ""
    st.session_state["invoice_step"] = "parts"

    parts_by_name = _active_parts_by_invoice_name(realm_id)
    lines = inv.get("line_items") or []
    if not lines:
        lines = [
            line for line in (inv.get("Line") or [])
            if str(line.get("DetailType") or "") == "SalesItemLineDetail"
        ]
    st.session_state["shop_cart"] = [
        _history_line_to_cart_line(line, parts_by_name) for line in lines
    ]


def _invoice_customer_name(inv: dict[str, Any]) -> str:
    customer = str(inv.get("customer_name") or "").strip()
    if customer:
        return customer
    raw = inv.get("raw") if isinstance(inv.get("raw"), dict) else inv
    ref = raw.get("CustomerRef") if isinstance(raw, dict) else None
    return str(ref.get("name") or "").strip() if isinstance(ref, dict) else ""


def _active_parts_by_invoice_name(realm_id: str) -> dict[str, dict[str, Any]]:
    """Map item names/FQNs to active part rows for copying historical invoices."""
    out: dict[str, dict[str, Any]] = {}
    try:
        parts = _all_active_parts(realm_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load parts for invoice copy: %s", exc)
        return out
    for part in parts:
        for key in (part.get("name"), part.get("fully_qualified_name")):
            norm = str(key or "").strip().lower()
            if norm and norm not in out:
                out[norm] = part
    return out


def _history_line_to_cart_line(line: dict[str, Any], parts_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Convert cached/raw invoice line into the shop draft cart shape."""
    if "item_name" in line or "unit_price" in line:
        item_name = str(line.get("item_name") or line.get("name") or "").strip()
        description = str(line.get("description") or "").strip()
        qty_raw = line.get("qty")
        unit_price = float(line.get("unit_price") or 0)
    else:
        detail = line.get("SalesItemLineDetail") or {}
        item_ref = detail.get("ItemRef") or {}
        item_name = str(item_ref.get("name") or "").strip() if isinstance(item_ref, dict) else ""
        description = str(line.get("Description") or "").strip()
        qty_raw = detail.get("Qty")
        unit_price = float(detail.get("UnitPrice") or 0)
    try:
        qty = max(1, int(float(qty_raw or 1)))
    except (TypeError, ValueError):
        qty = 1
    part = parts_by_name.get(item_name.lower(), {})
    return {
        "qbo_item_id": str(part.get("qbo_item_id") or ""),
        "sku": str(part.get("sku") or ""),
        "name": str(part.get("name") or item_name),
        "description": str(description or part.get("sales_description") or part.get("purchase_description") or ""),
        "unit_price": unit_price if unit_price else float(part.get("sales_price") or 0),
        "on_hand": part.get("qty_on_hand"),
        "qty": qty,
        "labor_details": [],
    }


def _line_item_html(line: dict[str, Any], lang: str) -> str:
    if "item_name" in line or "unit_price" in line:
        item_name = str(line.get("item_name") or "").strip()
        description = str(line.get("description") or "").strip()
        qty = line.get("qty")
        rate = line.get("unit_price")
        amount = line.get("amount")
        return _line_item_flat_html(item_name, description, qty, rate, amount, lang)

    detail = line.get("SalesItemLineDetail") or {}
    item_ref = detail.get("ItemRef") or {}
    item_name = str(item_ref.get("name") or "").strip() if isinstance(item_ref, dict) else ""
    description = str(line.get("Description") or "").strip()
    qty = detail.get("Qty")
    rate = detail.get("UnitPrice")
    amount = line.get("Amount")

    return _line_item_flat_html(item_name, description, qty, rate, amount, lang)


def _line_item_flat_html(
    item_name: str,
    description: str,
    qty: Any,
    rate: Any,
    amount: Any,
    lang: str,
) -> str:

    qty_str = _fmt_qty(qty)
    rate_str = _fmt_price(rate)
    amount_str = _fmt_price(amount)

    desc_html = f"<div class='li-desc'>{_escape(description)}</div>" if description else ""
    chips = []
    if qty_str is not None:
        chips.append(f"<span class='badge badge-untracked'>{_t(lang, 'li_qty')}: {qty_str}</span>")
    if rate_str:
        chips.append(f"<span class='badge badge-price'>{_t(lang, 'li_rate')}: {rate_str}</span>")
    if amount_str:
        chips.append(f"<span class='badge badge-cost'>{_t(lang, 'li_amount')}: {amount_str}</span>")

    return (
        f"<div class='li-card'>"
        f"<div class='li-name'>{_escape(item_name or '—')}</div>"
        f"{desc_html}"
        f"<div class='part-badges'>{''.join(chips)}</div>"
        f"</div>"
    )



def _render_new_invoice_view(lang: str, realm_id: str) -> None:
    """New Invoice view (Button 2): build a cart and submit it for review.

    The shop manager searches parts, taps + to add them, adjusts quantities, and
    taps "Finish invoice". This does NOT post to QuickBooks - it writes a pending
    draft to the Supabase review queue for accounting.
    """
    if not realm_id:
        _render_view_header(lang, "card_new_invoice")
        st.info(_t(lang, "not_connected"))
        return

    _show_cart_flash()

    _ensure_invoice_defaults(realm_id)
    step = st.session_state.get("invoice_step", "vehicle")

    if step == "vehicle":
        _render_view_header(lang, "card_new_invoice")
        _render_invoice_vehicle_step(lang)
        return
    if step == "customer":
        _render_view_header(lang, "card_new_invoice")
        _render_invoice_customer_step(lang, realm_id)
        return

    # Parts step: a single prominent header card (invoice # + customer + the
    # unit/VIN/miles box) with a small pencil edit, drawn by the locked header.
    _render_view_header(lang, "card_new_invoice", show_title=False)
    _render_invoice_locked_header(lang)

    # --- Add parts: a pre-rendered dropdown (like the customer picker). Every
    # active part is an option; QuickBooks-style, you type to filter by part
    # number / description / SKU and pick one to add it as a line. ---
    try:
        part_labels, part_by_label = _active_part_options(realm_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not build part options: %s", exc)
        part_labels, part_by_label = [], {}

    nonce = int(st.session_state.get("invoice_part_nonce", 0))
    pick_key = f"invoice_part_pick_{nonce}"
    with st.container(border=True):
        st.markdown(f"<div class='add-parts-title'>{_t(lang, 'add_parts_panel_title')}</div>", unsafe_allow_html=True)
        st.caption(_t(lang, "add_parts_panel_help"))
        add_part_slot = st.empty()
        picked = st.selectbox(
            _t(lang, "add_part_label"),
            [""] + part_labels,
            key=pick_key,
            index=0,
            placeholder=_t(lang, "select_part_placeholder"),
            label_visibility="collapsed",
            accept_new_options=False,
            filter_mode="contains",
        )
        if picked and picked in part_by_label:
            with add_part_slot.container():
                item = part_by_label[picked]
                if st.button(
                    f"➕ {_t(lang, 'add_to_invoice')}",
                    key=f"add_picked_top_{nonce}",
                    use_container_width=True,
                    type="primary",
                ):
                    _cart_add(item)
                    # Bump the nonce so the dropdown resets to blank for the next part.
                    st.session_state["invoice_part_nonce"] = nonce + 1
                    st.session_state["shop_cart_flash"] = _t(lang, "added_toast")
                    st.rerun()

    st.markdown(f"<div class='shop-title'>{_t(lang, 'cart_title')}</div>", unsafe_allow_html=True)
    cart = _cart()
    _refresh_cart_line_stock(realm_id, cart)
    if not cart:
        st.info(_t(lang, "cart_empty"))
        total = 0.0
    else:
        # --- QuickBooks-style line items: # · Item / Description · Qty · Rate ·
        # Amount, each on its own clean card so it is readable on a phone. ---
        # On-hand lookup so each line can clearly show current stock (even for
        # lines restored from a saved draft, which don't carry on_hand).
        on_hand_by_id: dict[str, Any] = {}
        try:
            for p in _all_active_parts(realm_id):
                on_hand_by_id[str(p.get("qbo_item_id") or "")] = p.get("qty_on_hand")
        except Exception:  # noqa: BLE001
            on_hand_by_id = {}
        total = 0.0
        for idx, line in enumerate(cart):
            unit_price = float(line.get("unit_price") or 0)
            qty = int(line.get("qty") or 0)
            line_total = unit_price * qty
            total += line_total
            on_hand = line.get("on_hand")
            if on_hand is None:
                on_hand = on_hand_by_id.get(str(line.get("qbo_item_id") or ""))
            # Live "remaining in stock" = current on hand minus what's on this
            # invoice line, so it ticks down as he adds and back up as he removes.
            remaining = on_hand
            if on_hand is not None:
                try:
                    remaining = float(on_hand) - qty
                except (TypeError, ValueError):
                    remaining = on_hand
            item_id = str(line.get("qbo_item_id") or "")
            confirm_key = f"confirm_remove_{idx}_{item_id}"
            with st.container(border=True):
                st.markdown(
                    _cart_line_html(lang, idx + 1, line, unit_price, line_total, remaining),
                    unsafe_allow_html=True,
                )
                # Removal confirmation (triggered by the trash button or by setting
                # the quantity to 0) so a part is never dropped by accident.
                if st.session_state.get(confirm_key):
                    st.warning(_t(lang, "confirm_remove_q"))
                    yes_col, no_col = st.columns(2)
                    with yes_col:
                        if st.button(
                            f"🗑 {_t(lang, 'confirm_remove_yes')}",
                            key=f"rm_yes_{idx}_{item_id}",
                            use_container_width=True,
                            type="primary",
                        ):
                            cart.pop(idx)
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                    with no_col:
                        if st.button(
                            _t(lang, "confirm_remove_no"),
                            key=f"rm_no_{idx}_{item_id}",
                            use_container_width=True,
                        ):
                            if int(line.get("qty") or 0) <= 0:
                                line["qty"] = 1
                            st.session_state.pop(confirm_key, None)
                            st.rerun()
                    continue
                qty_col, rate_col, rm_col = st.columns([2, 2, 1], vertical_alignment="bottom")
                with qty_col:
                    new_qty = st.number_input(
                        _t(lang, "qty_to_add"),
                        min_value=0,
                        step=1,
                        value=max(0, qty),
                        key=f"qty_{idx}_{item_id}",
                    )
                    if int(new_qty) != qty:
                        if int(new_qty) <= 0:
                            # Setting qty to 0 asks to remove the part.
                            st.session_state[confirm_key] = True
                            line["qty"] = 0
                        else:
                            line["qty"] = int(new_qty)
                        st.rerun()
                with rate_col:
                    st.markdown(
                        f"<div class='li-rate-label'>{_t(lang, 'li_unit')}</div>"
                        f"<div class='li-rate-value'>{_fmt_price(unit_price)}</div>",
                        unsafe_allow_html=True,
                    )
                with rm_col:
                    st.markdown("<div class='li-rate-label'>&nbsp;</div>", unsafe_allow_html=True)
                    if st.button(
                        "🗑",
                        key=f"rm_{idx}_{item_id}",
                        help=_t(lang, "remove"),
                        use_container_width=True,
                    ):
                        st.session_state[confirm_key] = True
                        st.rerun()
                if _is_labor_line(line):
                    _render_labor_details_editor(lang, line, idx, item_id)

    st.markdown(f"### {_t(lang, 'invoice_total_label')}: {_fmt_price(total)}")

    # Perpetual autosave: writes to Supabase only when the draft actually changed.
    _autosave_draft(realm_id, total)

    st.caption(_t(lang, "finish_help"))
    finish_col, clear_col = st.columns([3, 1])
    with finish_col:
        if st.button(
            f"✅ {_t(lang, 'finish_invoice')}",
            use_container_width=True,
            type="primary",
            disabled=not bool(cart),
        ):
            _submit_shop_invoice(lang, realm_id, status="pending", total=total)
    with clear_col:
        if st.button(f"🧹 {_t(lang, 'clear_invoice')}", use_container_width=True):
            _discard_current_invoice(delete_remote=True)
            st.rerun()


def _ensure_invoice_defaults(realm_id: str) -> None:
    if "invoice_step" not in st.session_state:
        st.session_state["invoice_step"] = "vehicle"
    if not st.session_state.get("invoice_doc_number"):
        try:
            next_no = _cached_next_invoice_number(realm_id)
        except Exception:  # noqa: BLE001
            next_no = None
        if next_no:
            st.session_state["invoice_doc_number"] = str(next_no)


# Session keys that make up a single in-progress invoice draft.
_INVOICE_FIELD_KEYS = (
    "invoice_step",
    "invoice_doc_number",
    "invoice_truck",
    "invoice_vin",
    "invoice_miles",
    "invoice_notes",
    # Vehicle-step widget mirrors (see _render_invoice_vehicle_step).
    "invoice_truck_w",
    "invoice_vin_w",
    "invoice_miles_w",
    "invoice_notes_w",
    "invoice_customer",
    "invoice_customer_is_new",
    "invoice_customer_pick",
    "invoice_add_search",
    "invoice_part_nonce",
    "invoice_draft_id",
    "invoice_draft_sig",
)


def _start_new_invoice() -> None:
    """Clear any in-progress invoice so New Invoice always opens blank."""
    _clear_draft_derived_caches()
    for key in _INVOICE_FIELD_KEYS:
        st.session_state.pop(key, None)
    st.session_state["shop_cart"] = []
    _go(_VIEW_NEW_INVOICE)



def _render_invoice_vehicle_step(lang: str) -> None:
    # The vehicle widgets live ONLY on this step. Streamlit drops a widget's
    # session_state value once the widget stops rendering, so we keep the real
    # values in persistent "stable" keys and bind the widgets to separate "_w"
    # keys, seeded from the stable keys and copied back on Next.
    _VEHICLE_FIELD_PAIRS = (
        ("invoice_truck", "invoice_truck_w"),
        ("invoice_vin", "invoice_vin_w"),
        ("invoice_miles", "invoice_miles_w"),
    )
    for stable, widget in _VEHICLE_FIELD_PAIRS:
        if widget not in st.session_state:
            st.session_state[widget] = str(st.session_state.get(stable) or "")

    def _clear_vehicle_fields() -> None:
        """Clear only the selectable vehicle fields without changing invoice #.

        This runs as a button callback before Streamlit rebuilds the widgets,
        which safely resets the selectbox/text-input values and avoids disturbing
        the matching/suggestion logic.
        """
        for key in (
            "invoice_truck",
            "invoice_truck_w",
            "invoice_vin",
            "invoice_vin_w",
            "invoice_miles",
            "invoice_miles_w",
        ):
            st.session_state[key] = ""

    st.text_input(_t(lang, "invoice_no"), key="invoice_doc_number")
    unit_now = str(st.session_state.get("invoice_truck_w") or "")
    vin_now = str(st.session_state.get("invoice_vin_w") or "")
    try:
        realm_id = _cached_shop_realm_id()
        suggestions = _cached_vehicle_field_suggestions(realm_id, unit_now, vin_now)
    except Exception:  # noqa: BLE001 - suggestions are convenience only
        realm_id = ""
        suggestions = {"units": [], "vins": [], "miles": [], "customers": []}

    unit_col, vin_col, miles_col = st.columns(3)
    with unit_col:
        _vehicle_text_picker(lang, "invoice_truck_w", "truck_unit", suggestions.get("units", []))
    with vin_col:
        _vehicle_text_picker(lang, "invoice_vin_w", "vin", suggestions.get("vins", []))
    with miles_col:
        # Miles is a free-typed value (NOT a dropdown).
        st.text_input(_t(lang, "miles"), key="invoice_miles_w")

    # Show the previous miles only once a VIN is entered (a unit alone is not
    # specific enough - lots of units overlap). Match strictly on the VIN.
    if vin_now:
        try:
            prior = _last_invoice_for_unit(realm_id, "", vin_now)
        except Exception:  # noqa: BLE001
            prior = {}
        if prior.get("miles") or prior.get("doc"):
            bits = []
            if prior.get("miles"):
                bits.append(f"{_t(lang, 'miles')}: {prior['miles']}")
            if prior.get("doc"):
                bits.append(f"#{prior['doc']}")
            if prior.get("date"):
                bits.append(_fmt_user_date(prior["date"]))
            st.info(f"{_t(lang, 'prior_invoice')}: " + " · ".join(bits))

    clear_col, next_col = st.columns([1, 2])
    with clear_col:
        st.button(
            f"🧹 {_t(lang, 'clear_invoice')}",
            key="invoice_vehicle_clear",
            use_container_width=True,
            on_click=_clear_vehicle_fields,
        )
    with next_col:
        if st.button(f"➡ {_t(lang, 'next')}", use_container_width=True, type="primary"):
            # Persist the widget values into the stable keys BEFORE the widgets are
            # torn down on the next step (otherwise unit/VIN/miles/notes vanish).
            for stable, widget in _VEHICLE_FIELD_PAIRS:
                st.session_state[stable] = str(st.session_state.get(widget) or "").strip()
            st.session_state["invoice_step"] = "customer"
            st.rerun()


def _set_widget_value(state_key: str, value: str) -> None:
    st.session_state[state_key] = str(value or "")


def _vehicle_text_picker(lang: str, state_key: str, label_key: str, suggestions: list[str]) -> None:
    """Exact typed Unit/VIN input with optional historical suggestions.

    This deliberately uses ``st.text_input`` (not accept-new selectbox) so typing
    a brand-new unit like ``500`` and pressing Enter/clicking away keeps exactly
    ``500``. It never auto-selects the first fuzzy match like ``500575``. The
    suggestions popover is just a convenience: tapping a suggestion fills the
    text input via a safe callback.
    """
    current = str(st.session_state.get(state_key) or "").strip()
    st.text_input(_t(lang, label_key), key=state_key, placeholder=_t(lang, label_key))
    current = str(st.session_state.get(state_key) or "").strip()
    ranked: list[str] = []
    current_norm = _norm_vehicle_key(current)
    for raw in suggestions:
        value = str(raw or "").strip()
        if not value or value in ranked:
            continue
        value_norm = _norm_vehicle_key(value)
        if not current_norm or current_norm in value_norm or value_norm in current_norm:
            ranked.append(value)
        if len(ranked) >= 8:
            break
    if not ranked:
        return
    with st.popover(f"🔎 {_t(lang, 'suggestions')}", use_container_width=True):
        for idx, value in enumerate(ranked):
            st.button(
                value,
                key=f"veh_suggest_{state_key}_{idx}_{_collapse_alnum(value)}",
                use_container_width=True,
                on_click=_set_widget_value,
                args=(state_key, value),
            )


def _render_invoice_customer_step(lang: str, realm_id: str) -> None:
    unit = str(st.session_state.get("invoice_truck") or "")
    vin = str(st.session_state.get("invoice_vin") or "")
    st.markdown(f"### {_t(lang, 'choose_customer')}")
    st.caption(f"{_t(lang, 'truck_unit')}: {unit or '—'} · {_t(lang, 'vin')}: {vin or '—'}")

    customer_synced_at = _cached_customer_synced_at(realm_id)
    if customer_synced_at:
        st.caption(f"{_t(lang, 'updated')}: {_fmt_user_datetime(customer_synced_at)}")
    if st.button(f"🔄 {_t(lang, 'refresh_customers')}", use_container_width=True):
        with st.spinner(_t(lang, "customers_refreshing")):
            result = sync_shop_customers(realm_id)
        _cached_customer_names.clear()
        _cached_customer_search.clear()
        _cached_customer_synced_at.clear()
        if result.status == "success":
            if result.customers_upserted:
                st.success(f"{_t(lang, 'customers_refresh_done')} (+{result.customers_upserted})")
            else:
                st.info(_t(lang, "customers_refresh_none"))
        else:
            st.error(result.message)
        st.rerun()

    suggestions: list[str] = []
    try:
        suggestions = _cached_vehicle_customer_suggestions(realm_id, unit, vin)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vehicle customer suggestions failed: %s", exc)

    if suggestions:
        st.markdown(f"#### {_t(lang, 'customer_suggestions')}")
        for idx, name in enumerate(suggestions):
            clean_name = str(name or "").strip()
            if not clean_name:
                continue
            if st.button(
                f"👤 {clean_name}",
                key=f"customer_suggestion_{idx}_{_collapse_alnum(clean_name)}",
                use_container_width=True,
            ):
                st.session_state["invoice_customer"] = clean_name
                st.session_state["invoice_customer_is_new"] = False
                st.session_state["invoice_step"] = "parts"
                st.rerun()

    customer_options: list[str] = []
    try:
        for name in _cached_customer_names(realm_id):
            if name and name not in customer_options:
                customer_options.append(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cached customer lookup failed: %s", exc)

    if not customer_options:
        st.info(_t(lang, "customer_not_listed"))
    picked = st.selectbox(
        _t(lang, "customer"),
        [""] + customer_options,
        index=0,
        key="invoice_customer_pick",
        placeholder=_t(lang, "customer_search"),
        accept_new_options=True,
        filter_mode="contains",
    )
    picked_name = str(picked or "").strip()
    back_col, confirm_col = st.columns([1, 1.35])
    with back_col:
        if st.button(f"⬅ {_t(lang, 'edit_header')}", key="customer_back_vehicle", use_container_width=True):
            st.session_state["invoice_step"] = "vehicle"
            st.rerun()
    with confirm_col:
        confirm_label = _t(lang, "confirm_customer") if picked_name else _t(lang, "skip_customer")
        if st.button(f"✅ {confirm_label}", key="confirm_customer_bottom", use_container_width=True, type="primary"):
            st.session_state["invoice_customer"] = picked_name
            st.session_state["invoice_customer_is_new"] = bool(picked_name and picked_name not in customer_options)
            st.session_state["invoice_step"] = "parts"
            st.rerun()


def _render_invoice_locked_header(lang: str) -> None:
    customer = str(st.session_state.get("invoice_customer") or "").strip()
    doc = str(st.session_state.get("invoice_doc_number") or "").strip()
    unit = str(st.session_state.get("invoice_truck") or "").strip()
    vin = str(st.session_state.get("invoice_vin") or "").strip()
    miles = str(st.session_state.get("invoice_miles") or "").strip()

    # Edit is intentionally a small affordance ABOVE the whole header card, not
    # underneath the invoice number/details where it visually competes with them.
    _, edit_col = st.columns([6, 1], vertical_alignment="top")
    with edit_col:
        if st.button("✏️", key="edit_header_pencil", help=_t(lang, "edit_header")):
            st.session_state["invoice_step"] = "vehicle"
            st.rerun()

    # Prominent header card: invoice number big on the left, customer filling the
    # space to its right; below, the unit/VIN/miles in clear bordered chips.
    chips = []
    if unit:
        chips.append(
            f"<span class='inv-chip'><span class='inv-chip-k'>{_t(lang, 'unit_short')}</span>"
            f"<span class='inv-chip-v'>{_escape(unit)}</span></span>"
        )
    if vin:
        chips.append(
            f"<span class='inv-chip'><span class='inv-chip-k'>{_t(lang, 'vin')}</span>"
            f"<span class='inv-chip-v'>{_escape(vin)}</span></span>"
        )
    if miles:
        chips.append(
            f"<span class='inv-chip'><span class='inv-chip-k'>{_t(lang, 'miles')}</span>"
            f"<span class='inv-chip-v'>{_escape(miles)}</span></span>"
        )
    chips_html = f"<div class='inv-chips'>{''.join(chips)}</div>" if chips else ""
    cust_html = f"<span class='inv-head-cust'>{_escape(customer)}</span>" if customer else ""
    st.markdown(
        f"<div class='inv-head-card'>"
        f"<div class='inv-head-top'>"
        f"<span class='inv-head-no'>#{_escape(doc) or '—'}</span>"
        f"{cust_html}"
        f"</div>"
        f"{chips_html}"
        f"</div>",
        unsafe_allow_html=True,
    )

    # Helpful context: last invoice + miles for this VIN (VIN-only to avoid the
    # unit-number overlap problem).
    prior = {}
    if vin:
        try:
            realm_id = _cached_shop_realm_id()
            prior = _last_invoice_for_unit(realm_id, "", vin)
        except Exception:  # noqa: BLE001
            prior = {}
    if prior.get("doc") or prior.get("miles"):
        bits = []
        if prior.get("doc"):
            bits.append(f"#{prior['doc']}")
        if prior.get("date"):
            bits.append(_fmt_user_date(prior["date"]))
        if prior.get("miles"):
            bits.append(f"{_t(lang, 'miles')}: {prior['miles']}")
        st.info(f"{_t(lang, 'prior_invoice')}: " + " · ".join(bits))


def _submit_shop_invoice(lang: str, realm_id: str, *, status: str, total: float) -> None:
    # Make sure the latest state is persisted, then finalize that same draft row.
    _autosave_draft(realm_id, total)
    draft_id = str(st.session_state.get("invoice_draft_id") or "")
    try:
        if draft_id:
            finalize_invoice_draft(draft_id)
        else:
            submit_invoice_draft(
                realm_id=realm_id,
                proposed_doc_number=str(st.session_state.get("invoice_doc_number") or ""),
                customer_name=str(st.session_state.get("invoice_customer") or ""),
                customer_is_new=bool(st.session_state.get("invoice_customer_is_new")),
                truck_unit=str(st.session_state.get("invoice_truck") or ""),
                vin=str(st.session_state.get("invoice_vin") or "").strip().upper(),
                miles=str(st.session_state.get("invoice_miles") or ""),
                notes=str(st.session_state.get("invoice_notes") or ""),
                line_items=_invoice_line_items(),
                total=total,
                submitted_by=str(st.session_state.get("shop_user") or ""),
                status="pending",
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Invoice submit failed: %s", exc)
        st.error(_t(lang, "finish_err"))
        return
    _discard_current_invoice(delete_remote=False)
    # Draft set changed: refresh the draft-derived caches (next number, on-draft
    # badge counts) so the next invoice and the inventory badges are accurate.
    _clear_draft_derived_caches()
    st.session_state["shop_cart_flash"] = _t(lang, "finish_ok")
    st.rerun()


def _discard_current_invoice(*, delete_remote: bool) -> None:
    """Clear the in-progress invoice from the session (and optionally Supabase)."""
    if delete_remote:
        draft_id = str(st.session_state.get("invoice_draft_id") or "")
        if draft_id:
            try:
                delete_draft(draft_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not delete draft on discard: %s", exc)
    for key in _INVOICE_FIELD_KEYS:
        st.session_state.pop(key, None)
    st.session_state["shop_cart"] = []
    # A draft may have been deleted: refresh draft-derived caches.
    if delete_remote:
        _clear_draft_derived_caches()


def _load_draft_into_session(draft: dict[str, Any], *, navigate: bool = True) -> None:
    """Load a saved draft back into the New Invoice flow for editing."""
    for key in _INVOICE_FIELD_KEYS:
        st.session_state.pop(key, None)
    st.session_state["invoice_draft_id"] = str(draft.get("id") or "")
    st.session_state["invoice_doc_number"] = str(draft.get("proposed_doc_number") or "")
    st.session_state["invoice_customer"] = str(draft.get("customer_name") or "")
    st.session_state["invoice_customer_is_new"] = bool(draft.get("customer_is_new"))
    st.session_state["invoice_truck"] = str(draft.get("truck_unit") or "")
    st.session_state["invoice_vin"] = str(draft.get("vin") or "")
    st.session_state["invoice_miles"] = str(draft.get("miles") or "")
    st.session_state["invoice_notes"] = str(draft.get("notes") or "")
    st.session_state["invoice_step"] = "parts"
    cart = []
    for li in draft.get("line_items") or []:
        cart.append(
            {
                "qbo_item_id": str(li.get("qbo_item_id") or ""),
                "sku": str(li.get("sku") or ""),
                "name": str(li.get("name") or ""),
                "description": str(li.get("description") or ""),
                "labor_details": li.get("labor_details") if isinstance(li.get("labor_details"), list) else [],
                "unit_price": float(li.get("unit_price") or 0),
                "qty": int(li.get("qty") or 1),
            }
        )
    st.session_state["shop_cart"] = cart
    if navigate:
        _go(_VIEW_NEW_INVOICE)



def _cart() -> list[dict[str, Any]]:
    cart = st.session_state.get("shop_cart")
    if not isinstance(cart, list):
        cart = []
        st.session_state["shop_cart"] = cart
    return cart


def _invoice_line_items() -> list[dict[str, Any]]:
    """Normalize the current cart into stored line-item dicts."""
    return [
        {
            "qbo_item_id": str(line.get("qbo_item_id") or ""),
            "sku": str(line.get("sku") or ""),
            "name": str(line.get("name") or ""),
            "description": str(line.get("description") or ""),
            "labor_details": line.get("labor_details") if isinstance(line.get("labor_details"), list) else [],
            "qty": int(line.get("qty") or 0),
            "unit_price": float(line.get("unit_price") or 0),
            "line_total": round(float(line.get("unit_price") or 0) * int(line.get("qty") or 0), 2),
        }
        for line in _cart()
    ]


def _invoice_state_signature(line_items: list[dict[str, Any]]) -> str:
    """Stable signature of the in-progress invoice for change detection."""
    header = "|".join(
        str(st.session_state.get(key) or "")
        for key in ("invoice_doc_number", "invoice_customer", "invoice_truck", "invoice_vin", "invoice_miles", "invoice_notes")
    )
    body = ";".join(
        f"{li['qbo_item_id']}x{li['qty']}@{li['unit_price']}#{li.get('labor_details') or []}" for li in line_items
    )
    return f"{header}#{body}"


def _autosave_draft(realm_id: str, total: float) -> None:
    """Perpetually save the draft to Supabase whenever it changes.

    Event-driven (not a timer): Streamlit reruns on every interaction, and we
    only write when a signature of the invoice state actually changed. That keeps
    Supabase writes to one-per-real-change, never on idle reruns.
    """
    line_items = _invoice_line_items()
    signature = _invoice_state_signature(line_items)
    if signature == st.session_state.get("invoice_draft_sig"):
        return
    try:
        saved = save_invoice_draft(
            draft_id=str(st.session_state.get("invoice_draft_id") or "") or None,
            realm_id=realm_id,
            proposed_doc_number=str(st.session_state.get("invoice_doc_number") or ""),
            customer_name=str(st.session_state.get("invoice_customer") or ""),
            customer_is_new=bool(st.session_state.get("invoice_customer_is_new")),
            truck_unit=str(st.session_state.get("invoice_truck") or ""),
            vin=str(st.session_state.get("invoice_vin") or "").strip().upper(),
            miles=str(st.session_state.get("invoice_miles") or ""),
            notes=str(st.session_state.get("invoice_notes") or ""),
            line_items=line_items,
            total=total,
            submitted_by=str(st.session_state.get("shop_user") or ""),
        )
    except Exception as exc:  # noqa: BLE001 - autosave must never crash the page
        logger.warning("Draft autosave failed: %s", exc)
        return
    if saved.get("id"):
        st.session_state["invoice_draft_id"] = str(saved.get("id"))
    st.session_state["invoice_draft_sig"] = signature
    _clear_draft_derived_caches()


@st.cache_data(ttl=_INVOICE_CACHE_TTL, show_spinner=False)
def _last_invoice_for_unit(realm_id: str, unit: str, vin: str) -> dict[str, str]:
    """Return the MOST RECENT invoice doc/date/miles for a unit (or VIN).

    Matching is case-insensitive (normalized), and recency wins regardless of how
    the VIN was capitalized on each past invoice - so the newest reading is shown
    even if an older one used different casing.
    """
    unit_norm = _norm_vehicle_key(unit)
    vin_norm = _norm_vehicle_key(vin)
    if not realm_id or (not unit_norm and not vin_norm):
        return {}

    best: dict[str, str] = {}
    best_date = ""
    for inv in list_cached_invoices(realm_id, limit=2000):
        vehicle = _invoice_vehicle_values(inv)
        if (unit_norm and _vehicle_match(unit_norm, vehicle["unit"], min_len=2)) or (
            vin_norm and _vin_match(vin_norm, vehicle["vin"])
        ):
            date = str(inv.get("txn_date") or "")
            if date >= best_date:  # ISO dates sort lexically; newest wins
                best_date = date
                best = {
                    "doc": str(inv.get("doc_number") or ""),
                    "date": date,
                    "miles": vehicle["miles"],
                }
    return best


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
            "description": str(
                item.get("sales_description") or item.get("purchase_description") or ""
            ),
            "unit_price": float(item.get("sales_price") or 0),
            "on_hand": item.get("qty_on_hand"),
            "labor_details": [],
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

