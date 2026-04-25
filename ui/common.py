"""Shared UI helpers for the Streamlit driver application."""

from __future__ import annotations

import html
import os
import subprocess
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import streamlit as st
import streamlit.components.v1 as components

from config import (
    PHASE_LABELS,
)
from runtime_context import get_active_company_profile, is_test_mode_active
from services.draft_service import autosave_draft
from services.error_log_service import log_application_error
from services.test_mode_service import render_admin_test_tools


BASE_STYLES = """
<style>
    /* Prevent iOS Safari from auto-zooming when a field is focused. */
    div[data-testid="stTextInput"] input,
    div[data-testid="stTextArea"] textarea,
    div[data-testid="stDateInput"] input,
    div[data-testid="stNumberInput"] input,
    div[data-testid="stSelectbox"] div[role="combobox"],
    div[data-testid="stMultiSelect"] div[role="combobox"] {
        font-size: 16px !important;
    }
    /* Stop the on-screen keyboard from popping up on dropdowns. The
       BaseWeb select wraps an <input> we don't actually want users typing
       into -- the wrapper div handles taps and opens the menu. Making
       the input itself inert (no caret, no pointer events) keeps the
       dropdown tappable while preventing the soft keyboard from eating
       half the screen on mobile. Trade-off: keyboard typeahead-search
       on desktop is also disabled; arrow keys + click still work. */
    div[data-baseweb="select"] input {
        caret-color: transparent !important;
        pointer-events: none !important;
    }
    /* Touch-friendly primary buttons on mobile. */
    .stButton > button {
        min-height: 2.75rem;
    }
    /* Hide Streamlit framework chrome that looks out of place in a
       branded portal. The header is intentionally left visible so the
       sidebar collapse control continues to work. */
    #MainMenu { visibility: hidden; }
    footer { visibility: hidden; }
    /* Red asterisk on required labels */
    div[data-testid="stTextInput"] label p:has(~ *),
    div[data-testid="stSelectbox"] label p:has(~ *) {
        font-weight: 600;
    }
    .missing-field-wrapper {
        border: 2px solid #ff4b4b;
        background: rgba(255, 75, 75, 0.06);
        border-radius: 8px;
        padding: 0.35rem 0.5rem 0.15rem;
        margin-bottom: 0.35rem;
    }
    .missing-field-note {
        color: #ff4b4b;
        font-weight: 600;
        font-size: 0.78rem;
        margin: 0.2rem 0 0.15rem;
    }
    .missing-field {
        background: rgba(255, 75, 75, 0.1);
        border: 2px solid #ff4b4b;
        border-radius: 8px;
        padding: 0.5rem 0.8rem;
        margin-bottom: 0.3rem;
        font-size: 0.9rem;
        color: var(--text-color);
    }
    .missing-field-header {
        color: #ff4b4b;
        font-weight: 700;
        font-size: 1rem;
        margin-bottom: 0.3rem;
    }
    .app-header {
        text-align: center;
        padding: 1rem 1rem 0.9rem;
        margin-bottom: 0.5rem;
        border: 1px solid color-mix(in srgb, var(--primary-color) 22%, transparent);
        border-radius: 8px;
        background: var(--secondary-background-color);
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .app-header h1 {
        color: var(--text-color);
        margin-bottom: 0.15rem;
    }
    .app-header p {
        color: color-mix(in srgb, var(--text-color) 82%, transparent);
        margin: 0;
        font-size: 0.92rem;
    }
    .app-header h3 {
        color: color-mix(in srgb, var(--text-color) 70%, transparent);
        margin-top: 0.55rem;
        margin-bottom: 0;
        font-size: 0.92rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    .eeo-notice {
        background: color-mix(in srgb, var(--primary-color) 8%, var(--secondary-background-color));
        color: var(--text-color);
        padding: 0.8rem;
        border-radius: 8px;
        margin: 0.5rem 0;
        font-size: 0.85rem;
        line-height: 1.5;
        border-left: 4px solid var(--primary-color);
    }
    .app-version-footer {
        margin-top: 2rem;
        padding-bottom: 0.35rem;
        text-align: center;
        font-size: 0.72rem;
        color: color-mix(in srgb, var(--text-color) 62%, transparent);
    }
    /* Keep the sidebar open/close control visible at all times instead of
       fading until hovered. Applies to both the collapsed and expanded states. */
    [data-testid="stSidebarCollapsedControl"],
    [data-testid="stSidebarCollapseButton"],
    button[kind="headerNoPadding"][aria-label="Open sidebar"],
    button[kind="headerNoPadding"][aria-label="Close sidebar"] {
        opacity: 1 !important;
        visibility: visible !important;
    }
</style>
"""

PLACEHOLDER_OPTION = "Select one..."
VERSION_ENV_KEYS = (
    "APP_VERSION",
    "GIT_COMMIT_SHA",
    "GITHUB_SHA",
    "RENDER_GIT_COMMIT",
    "COMMIT_SHA",
)
REPO_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def _resolve_runtime_version() -> str:
    for key in VERSION_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value[:7]

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        git_version = result.stdout.strip()
        if git_version:
            return git_version
    except Exception:
        pass

    git_head = REPO_ROOT / ".git" / "HEAD"
    try:
        if git_head.exists():
            head_value = git_head.read_text(encoding="utf-8").strip()
            if head_value.startswith("ref: "):
                ref_name = head_value.split(" ", 1)[1].strip()
                ref_path = REPO_ROOT / ".git" / ref_name
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()[:7]
            elif head_value:
                return head_value[:7]
    except Exception:
        pass

    return "unknown"


def display_value(value: Any, default: str = "—") -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value).strip()
    return text if text else default


def summary_item(label: str, value: Any, default: str = "—") -> None:
    st.markdown(f"- **{label}:** {display_value(value, default)}")


MISSING_FIELDS_STATE_KEY = "_missing_field_keys"
MISSING_FIELDS_HEADER_KEY = "_missing_fields_header"
MISSING_FIELDS_LABELS_KEY = "_missing_field_labels"


def _missing_field_state() -> dict[str, Any]:
    state = st.session_state.get(MISSING_FIELDS_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        st.session_state[MISSING_FIELDS_STATE_KEY] = state
    return state


def record_missing_fields(
    missing: list[tuple[str, str]],
    header_text: str = "Please complete the required fields:",
) -> None:
    """Store keyed missing-field info so the next render can highlight inline.

    Each entry is a (field_key, human_label) pair. The field_key matches the
    key passed to missing_field_wrapper() on the widget.
    """
    if not missing:
        clear_missing_fields()
        return

    st.session_state[MISSING_FIELDS_STATE_KEY] = {key: label for key, label in missing}
    st.session_state[MISSING_FIELDS_HEADER_KEY] = header_text
    st.session_state[MISSING_FIELDS_LABELS_KEY] = [label for _, label in missing]
    log_application_error(
        code="validation_missing_fields",
        user_message=header_text,
        technical_details=", ".join(label for _, label in missing),
        severity="warning",
        extra={"missing_fields": [label for _, label in missing]},
    )


def clear_missing_fields() -> None:
    for key in (MISSING_FIELDS_STATE_KEY, MISSING_FIELDS_HEADER_KEY, MISSING_FIELDS_LABELS_KEY):
        if key in st.session_state:
            del st.session_state[key]


def show_missing_fields(
    missing_fields: list[str],
    header_text: str = "Please complete the required fields:",
) -> None:
    """Legacy helper: shows a top-of-page summary warning without keyed
    inline highlighting. Prefer record_missing_fields() on pages migrated
    to missing_field_wrapper(). Kept so unmigrated pages keep working."""
    if not missing_fields:
        return

    bullet_list = "\n".join(f"- {field}" for field in missing_fields)
    log_application_error(
        code="validation_missing_fields",
        user_message=header_text,
        technical_details=", ".join(missing_fields),
        severity="warning",
        extra={"missing_fields": missing_fields},
    )
    st.warning(f"{header_text}\n\n{bullet_list}")


def mark_missing(field_key: str) -> bool:
    """Render an inline red "Please complete this field" note immediately
    above a widget when the previous submission flagged field_key as missing.

    Call this just before the widget:
        mark_missing("first_name")
        first_name = st.text_input("First Name *", ...)

    Returns True when the field is currently flagged (mostly useful for tests).
    The rendered marker carries data-missing-field=<key> so the banner's
    scroll-into-view picks up the first one automatically.
    """
    if field_key not in _missing_field_state():
        return False

    st.markdown(
        f'<div class="missing-field-note" data-missing-field="{field_key}">'
        '⚠ Please complete this field'
        '</div>',
        unsafe_allow_html=True,
    )
    return True


def render_missing_fields_banner() -> None:
    """Render the summary banner at the top of the page for fields currently
    flagged as missing. Also scrolls to the first highlighted field."""
    labels = st.session_state.get(MISSING_FIELDS_LABELS_KEY) or []
    if not labels:
        return

    header = st.session_state.get(MISSING_FIELDS_HEADER_KEY) or "Please complete the required fields:"
    bullet_list = "\n".join(f"- {label}" for label in labels)
    st.warning(f"{header}\n\n{bullet_list}")

    components.html(
        """
        <script>
        const parentWindow = window.parent;
        const parentDocument = parentWindow.document;
        function scrollToFirstMissing() {
            const target = parentDocument.querySelector('[data-missing-field]');
            if (target && typeof target.scrollIntoView === 'function') {
                target.scrollIntoView({ block: 'center', behavior: 'smooth' });
                return true;
            }
            return false;
        }
        [60, 180, 360].forEach((delay) => {
            parentWindow.setTimeout(scrollToFirstMissing, delay);
        });
        </script>
        """,
        height=0,
    )


def show_user_error(
    message: str,
    *,
    code: str,
    technical_details: str | None = None,
    severity: str = "error",
    extra: dict[str, Any] | None = None,
) -> None:
    log_application_error(
        code=code,
        user_message=message,
        technical_details=technical_details,
        severity=severity,
        extra=extra,
    )
    st.warning(message)


def _wire_back_button_shim(current_page: int) -> None:
    """Map the phone/browser back gesture to the app's prev_page() flow.

    Streamlit doesn't register its page changes in browser history, so the
    default "back" gesture exits the app entirely. We push a synthetic
    history entry for the current step and listen for popstate — when the
    user swipes/clicks back, we click the in-page "← Back" button the
    current step renders (key suffix "_back"), which calls prev_page()
    and reruns.

    We deliberately do NOT push history on step 1: there is no in-app
    Back button there and we want the browser's back gesture to exit the
    app normally rather than trapping the driver inside it.
    """
    if current_page <= 1:
        return
    components.html(
        f"""
        <script>
        (function() {{
            const parentWindow = window.parent;
            const parentDocument = parentWindow.document;
            const pageLabel = "drv-page-{current_page}";

            if (parentWindow.history.state && parentWindow.history.state.drvPage === pageLabel) {{
                // Already tagged this step.
            }} else {{
                try {{
                    parentWindow.history.pushState({{ drvPage: pageLabel }}, "", parentWindow.location.href);
                }} catch (e) {{ /* ignore */ }}
            }}

            if (parentDocument.body && parentDocument.body.dataset.drvBackBound !== '1') {{
                parentDocument.body.dataset.drvBackBound = '1';
                parentWindow.addEventListener('popstate', () => {{
                    const selectors = [
                        'button[kind="secondary"]',
                        'button[data-testid="baseButton-secondary"]',
                        'button'
                    ];
                    for (const sel of selectors) {{
                        const buttons = parentDocument.querySelectorAll(sel);
                        for (const btn of buttons) {{
                            const text = (btn.innerText || '').trim();
                            if (text.startsWith('← Back')) {{
                                btn.click();
                                // Re-push so the next popstate is also caught.
                                try {{
                                    parentWindow.history.pushState(
                                        {{ drvPage: pageLabel }}, "", parentWindow.location.href
                                    );
                                }} catch (e) {{ /* ignore */ }}
                                return;
                            }}
                        }}
                    }}
                }});
            }}
        }})();
        </script>
        """,
        height=0,
    )


def _sync_browser_autofill_via_js() -> None:
    components.html(
        """
        <script>
        const parentWindow = window.parent;
        const parentDocument = parentWindow.document;
        const INPUT_SELECTOR = [
            'input[type="text"]',
            'input[type="email"]',
            'input[type="tel"]',
            'input[type="search"]',
            'input[type="password"]',
            'input:not([type])',
            'textarea'
        ].join(',');
        const BUTTON_SELECTOR = 'button, [role="button"], input[type="submit"]';

        // Mimic the user's "click in, click out" gesture exactly.
        // Streamlit's text_input commits on the native blur event, so we
        // programmatically focus + blur every filled input. We do it via
        // a real focus()/blur() pair so React's synthetic event system
        // and Streamlit's onBlur handler both fire normally.
        function commitAutofilledInputs() {
            const previouslyFocused = parentDocument.activeElement;
            const inputs = parentDocument.querySelectorAll(INPUT_SELECTOR);
            inputs.forEach((input) => {
                const value = (input.value ?? '').trim();
                if (!value) { return; }
                if (input.disabled || input.readOnly) { return; }
                try {
                    input.focus({ preventScroll: true });
                    input.blur();
                } catch (e) { /* ignore */ }
            });
            if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
                try { previouslyFocused.focus({ preventScroll: true }); } catch (e) {}
            }
        }

        if (parentDocument.body && parentDocument.body.dataset.autofillButtonSyncBound !== '1') {
            parentDocument.body.dataset.autofillButtonSyncBound = '1';

            // Also run when the user presses Enter inside a form field.
            parentDocument.addEventListener('keydown', (event) => {
                if (event.key === 'Enter') {
                    commitAutofilledInputs();
                }
            }, true);

            // Catch Chrome/Edge autofill the moment it happens via the
            // animationstart event fired by the :-webkit-autofill style.
            try {
                const style = parentDocument.createElement('style');
                style.textContent = `
                    @keyframes onAutoFillStart { from {} to {} }
                    input:-webkit-autofill { animation-name: onAutoFillStart; animation-duration: 1ms; }
                `;
                parentDocument.head.appendChild(style);
                parentDocument.addEventListener('animationstart', (event) => {
                    if (event.animationName === 'onAutoFillStart' && event.target) {
                        // Defer so the browser finishes filling all fields first.
                        setTimeout(commitAutofilledInputs, 50);
                    }
                }, true);
            } catch (e) { /* ignore */ }
        }
        </script>
        """,
        height=0,
    )


def render_save_draft_button(
    button_key: str,
    label: str = "Save Draft",
    *,
    on_before_save: Callable[[], bool] | None = None,
) -> None:
    """Render the save-draft button plus inline post-save panel."""
    active_key = st.session_state.get("_save_draft_panel_for_key")
    if active_key and active_key != button_key:
        st.session_state["_save_draft_panel_for_key"] = None

    if st.button(label, key=button_key, use_container_width=True):
        if on_before_save and not on_before_save():
            return
        result = autosave_draft()
        if result and result.get("ok"):
            st.session_state["_save_draft_panel_for_key"] = button_key
            st.session_state["_save_draft_panel_error"] = None
        else:
            st.session_state["_save_draft_panel_for_key"] = None
            st.session_state["_save_draft_panel_error"] = (
                "The form is still open, but the secure draft save did not complete."
            )

    active_key = st.session_state.get("_save_draft_panel_for_key")
    error_msg = st.session_state.get("_save_draft_panel_error")

    if active_key == button_key:
        _render_save_draft_panel(panel_key=button_key)
    elif error_msg and active_key is None:
        st.warning(error_msg)


def _render_save_draft_panel(panel_key: str) -> None:
    """Inline post-save panel: copy resume link plus email form."""
    from services.draft_service import build_resume_url_snippet
    from services.notification_service import send_resume_link_email
    from submission_storage import get_runtime_secret

    snippet = build_resume_url_snippet()
    if not snippet:
        return

    st.success("Draft saved. Use the link below to return later.")

    components.html(
        f"""
        <div style="font-family: sans-serif; margin-bottom: 0.6rem;">
          <div style="font-size: 0.78rem; color: #444; margin-bottom: 0.25rem;">
            Return link:
          </div>
          <div style="display: flex; gap: 0.4rem; align-items: stretch;">
            <input id="drv-resume-url-{panel_key}" readonly
              style="flex:1; padding: 0.55rem 0.6rem; font-size: 0.85rem;
                     border: 1px solid #ccc; border-radius: 6px; background: #fafafa;" />
            <button id="drv-resume-copy-{panel_key}" type="button"
              style="padding: 0.55rem 0.9rem; font-size: 0.85rem;
                     border: 1px solid #ccc; border-radius: 6px; background: #fff; cursor: pointer;">
              Copy link
            </button>
          </div>
          <div id="drv-resume-copy-msg-{panel_key}"
               style="font-size: 0.72rem; color: #3a8a49; margin-top: 0.2rem; min-height: 0.9rem;"></div>
        </div>
        <script>
        (function() {{
          const p = window.parent;
          const loc = p.location;
          const suffix = {snippet!r};
          const fullUrl = loc.origin + loc.pathname + suffix;
          const input = document.getElementById('drv-resume-url-{panel_key}');
          const btn = document.getElementById('drv-resume-copy-{panel_key}');
          const msg = document.getElementById('drv-resume-copy-msg-{panel_key}');
          input.value = fullUrl;
          btn.addEventListener('click', () => {{
            input.select();
            p.navigator.clipboard.writeText(fullUrl).then(() => {{
              msg.textContent = 'Copied!';
              setTimeout(() => msg.textContent = '', 2000);
            }}).catch(() => {{
              p.document.execCommand('copy');
              msg.textContent = 'Copied!';
              setTimeout(() => msg.textContent = '', 2000);
            }});
          }});
        }})();
        </script>
        """,
        height=110,
    )

    prefilled = str(st.session_state.form_data.get("email") or "").strip()
    email_key = f"{panel_key}_email_input"
    st.caption("Or email the link to yourself for later use:")
    col_a, col_b = st.columns([3, 1])
    with col_a:
        to_email = st.text_input(
            "Email link to",
            value=prefilled,
            key=email_key,
            placeholder="you@example.com",
            label_visibility="collapsed",
        )
    with col_b:
        send_clicked = st.button("Send Email", key=f"{panel_key}_email_send", use_container_width=True)

    if send_clicked:
        to_email = (to_email or "").strip()
        if not to_email or "@" not in to_email:
            st.warning("Enter a valid email address.")
        else:
            company = get_active_company_profile()
            base_url = (get_runtime_secret("APP_BASE_URL", "") or "").strip()
            base_url = f"{base_url.rstrip('/')}/" if base_url else ""
            resume_url = (base_url + snippet) if base_url else snippet
            result = send_resume_link_email(
                to_email=to_email,
                resume_url=resume_url,
                company_name=company.name,
                is_relative=not bool(base_url),
            )
            if result.get("status") == "sent":
                st.success(f"Link sent to {to_email}.")
            elif result.get("status") == "disabled":
                st.info("Email is not configured on this deployment. Copy the link above instead.")
            else:
                st.warning("We couldn't send the email right now. Please copy the link above.")


def default_california_applicability() -> bool:
    state = (st.session_state.form_data.get("state") or "").upper()
    preferred_office = (
        st.session_state.form_data.get("preferred_office")
        or st.session_state.form_data.get("applying_location")
        or ""
    ).lower()
    return state == "CA" or "california" in preferred_office or "fontana" in preferred_office


def selectbox_with_placeholder(
    label: str,
    options: list[str],
    current_value: str | None = None,
    *,
    key: str | None = None,
    placeholder: str = PLACEHOLDER_OPTION,
    help: str | None = None,
    disabled: bool = False,
) -> str:
    # For binary Yes/No questions, render as a horizontal radio instead of a
    # dropdown. On mobile, selectbox triggers the on-screen keyboard (for option
    # search), which eats half the screen for a two-choice answer.
    if sorted(options) == ["No", "Yes"]:
        ordered = list(options)
        radio_index: int | None
        if current_value in ordered:
            radio_index = ordered.index(current_value)
        else:
            radio_index = None
        selected = st.radio(
            label,
            ordered,
            index=radio_index,
            key=key,
            help=help,
            disabled=disabled,
            horizontal=True,
        )
        return selected if selected in ordered else ""

    display_options = [placeholder, *options]
    selected_value = current_value if current_value in options else None
    index = display_options.index(selected_value) if selected_value else 0
    selected = st.selectbox(
        label,
        display_options,
        index=index,
        key=key,
        help=help,
        disabled=disabled,
    )
    return "" if selected == placeholder else selected


def render_app_shell() -> None:
    company = get_active_company_profile()
    company_name = html.escape(company.name)
    location_parts = [
        html.escape(part) for part in [company.address, company.city_state_zip] if part
    ]
    contact_parts: list[str] = []
    if company.phone:
        contact_parts.append(f"Phone: {html.escape(company.phone)}")
    if company.email:
        contact_parts.append(f"Email: {html.escape(company.email)}")

    st.markdown(BASE_STYLES, unsafe_allow_html=True)
    brand_color = (company.brand_color or "").strip()
    if brand_color:
        st.markdown(
            f"<style>:root {{ --primary-color: {brand_color}; }}</style>",
            unsafe_allow_html=True,
        )
    if st.session_state.get("admin_tools_enabled"):
        with st.sidebar:
            render_admin_test_tools()
    _sync_browser_autofill_via_js()
    st.markdown(
        f"""
<div class="app-header">
    <h1>{company_name}</h1>
    {f'<p>{" | ".join(location_parts)}</p>' if location_parts else ''}
    {f'<p>{" | ".join(contact_parts)}</p>' if contact_parts else ''}
    <h3>Driver Application</h3>
</div>
""",
        unsafe_allow_html=True,
    )

    if is_test_mode_active():
        st.warning(
            "Safe test mode is active. This session uses fake applicant data, stores records in a separate test namespace, "
            "and tags internal notification emails as [TEST]."
        )

    render_missing_fields_banner()


def render_eeo_notice() -> None:
    st.markdown(
        """
<div class="eeo-notice">
In compliance with Federal and State equal opportunity laws, qualified applicants are
considered for all positions without regard to race, color, religion, sex, national origin,
age, marital status, veteran status, non-job related disability, or any other protected group status.
</div>
""",
        unsafe_allow_html=True,
    )


def render_version_footer() -> None:
    if not (is_test_mode_active() or st.session_state.get("admin_tools_enabled")):
        return
    st.markdown(
        f'<div class="app-version-footer">Build {_resolve_runtime_version()}</div>',
        unsafe_allow_html=True,
    )


def render_progress_bar() -> int:
    total_pages = len(PHASE_LABELS)
    if st.session_state.submitted:
        progress = 1.0
        progress_text = "Application complete"
    else:
        display_page = min(max(st.session_state.current_page, 1), total_pages)
        progress = display_page / total_pages
        progress_text = f"Step {display_page} of {total_pages}: {PHASE_LABELS.get(display_page, '')}"

    st.progress(progress, text=progress_text)
    return 99 if st.session_state.submitted else st.session_state.current_page


def scroll_to_top_on_page_change(page: int) -> None:
    if st.session_state.get("last_rendered_page") == page:
        return

    if page == 1:
        st.session_state.last_rendered_page = page
        return

    components.html(
        """
        <script>
        const parentWindow = window.parent;
        const parentDocument = parentWindow.document;
        const selectors = [
            'section.main',
            'main',
            '[data-testid="stMain"]',
            '[data-testid="stAppViewContainer"]',
            '[data-testid="stAppViewBlockContainer"]'
        ];

        function resetScroll(target) {
            if (!target) {
                return;
            }

            if (typeof target.scrollTo === 'function') {
                target.scrollTo({ top: 0, left: 0, behavior: 'auto' });
            }
            target.scrollTop = 0;
            target.scrollLeft = 0;
        }

        function scrollEverythingToTop() {
            selectors.forEach((selector) => {
                parentDocument.querySelectorAll(selector).forEach(resetScroll);
            });

            resetScroll(parentDocument.documentElement);
            resetScroll(parentDocument.body);
            parentWindow.scrollTo({ top: 0, left: 0, behavior: 'auto' });

            const blockContainer = parentDocument.querySelector('[data-testid="stAppViewBlockContainer"]');
            if (blockContainer && typeof blockContainer.scrollIntoView === 'function') {
                blockContainer.scrollIntoView({ block: 'start', behavior: 'auto' });
            }
        }

        [0, 40, 120, 240].forEach((delay) => {
            parentWindow.setTimeout(scrollEverythingToTop, delay);
        });
        </script>
        """,
        height=0,
    )
    st.session_state.last_rendered_page = page
