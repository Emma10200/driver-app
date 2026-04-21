"""Shared UI helpers for the Streamlit driver application."""

from __future__ import annotations

import os
import subprocess
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from config import (
    PHASE_LABELS,
)
from runtime_context import get_active_company_profile, is_test_mode_active
from services.draft_service import autosave_draft, render_draft_sidebar
from services.error_log_service import log_application_error


BASE_STYLES = """
<style>
    /* Red asterisk on required labels */
    div[data-testid="stTextInput"] label p:has(~ *),
    div[data-testid="stSelectbox"] label p:has(~ *) {
        font-weight: 600;
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
        padding: 1.15rem 1rem;
        margin-bottom: 0.5rem;
        border: 1px solid color-mix(in srgb, var(--primary-color) 35%, transparent);
        border-radius: 14px;
        border-bottom-width: 3px;
        background: linear-gradient(
            135deg,
            color-mix(in srgb, var(--primary-color) 12%, transparent),
            color-mix(in srgb, var(--primary-color) 4%, var(--background-color))
        );
    }
    .app-header h1 {
        color: var(--text-color);
        margin-bottom: 0.2rem;
    }
    .app-header p {
        color: color-mix(in srgb, var(--text-color) 82%, transparent);
        margin: 0;
    }
    .app-header h3 {
        color: var(--primary-color);
        margin-top: 0.55rem;
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


def show_missing_fields(
    missing_fields: list[str],
    header_text: str = "Please complete the required fields:",
) -> None:
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


def _open_sidebar_via_js() -> None:
    components.html(
        """
        <script>
        const parentWindow = window.parent;
        const parentDocument = parentWindow.document;

        function tryOpenSidebar() {
            const openButton = parentDocument.querySelector('button[aria-label="Open sidebar"]');
            if (openButton) {
                openButton.click();
            }
        }

        [0, 60, 140].forEach((delay) => {
            parentWindow.setTimeout(tryOpenSidebar, delay);
        });
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


def render_save_draft_button(button_key: str, label: str = "💾 Save Draft") -> None:
    if not st.button(label, key=button_key, use_container_width=True):
        return

    result = autosave_draft()
    if result and result.get("ok"):
        st.success(f"Draft saved. Resume later with code `{st.session_state.draft_id}`.")
        _open_sidebar_via_js()
    else:
        st.warning("The form is still open, but the secure draft save did not complete.")


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
    location_parts = [part for part in [company.address, company.city_state_zip] if part]
    contact_parts: list[str] = []
    if company.phone:
        contact_parts.append(f"Phone: {company.phone}")
    if company.email:
        contact_parts.append(f"Email: {company.email}")

    st.markdown(BASE_STYLES, unsafe_allow_html=True)
    render_draft_sidebar()
    _sync_browser_autofill_via_js()
    st.markdown(
        f"""
<div class="app-header">
    <h1>{company.name}</h1>
    {f'<p>{" | ".join(location_parts)}</p>' if location_parts else ''}
    {f'<p>{" | ".join(contact_parts)}</p>' if contact_parts else ''}
    <h3>Independent Contractor Driver Application</h3>
</div>
""",
        unsafe_allow_html=True,
    )

    if is_test_mode_active():
        st.warning(
            "Safe test mode is active. This session uses fake applicant data, stores records in a separate test namespace, "
            "and tags internal notification emails as [TEST]."
        )


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
