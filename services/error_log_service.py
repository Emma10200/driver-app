"""Centralized application error logging (Supabase-first, local fallback)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import streamlit as st
except ImportError:  # pragma: no cover - defensive fallback for non-Streamlit tooling
    st = None

from submission_storage import get_runtime_secret

LOCAL_LOG_FILE = Path(__file__).resolve().parent.parent / "submissions" / "_error_logs" / "app_errors.jsonl"


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _session_context() -> dict[str, Any]:
    if st is None:
        return {}

    try:
        return {
            "company_slug": st.session_state.get("company_slug"),
            "current_page": st.session_state.get("current_page"),
            "draft_id": st.session_state.get("draft_id"),
            "submitted": bool(st.session_state.get("submitted")),
            "test_mode": bool(st.session_state.get("test_mode")),
        }
    except Exception:
        return {}


def _supabase_settings() -> dict[str, str]:
    return {
        "url": (_safe_str(get_runtime_secret("SUPABASE_URL", ""))).strip(),
        "key": (_safe_str(get_runtime_secret("SUPABASE_SERVICE_KEY", "") or get_runtime_secret("SUPABASE_KEY", ""))).strip(),
        "table": (_safe_str(get_runtime_secret("ERROR_LOG_TABLE", ""))).strip(),
    }


def _write_local_log(payload: dict[str, Any]) -> None:
    LOCAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCAL_LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def log_application_error(
    *,
    code: str,
    user_message: str,
    technical_details: str | None = None,
    severity: str = "error",
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort error logging. Never raises."""

    payload: dict[str, Any] = {
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "code": code,
        "severity": severity,
        "user_message": user_message,
        "technical_details": _safe_str(technical_details),
        "context": {
            **_session_context(),
            **(extra or {}),
        },
    }

    settings = _supabase_settings()
    if settings["url"] and settings["key"] and settings["table"]:
        try:
            response = requests.post(
                f"{settings['url'].rstrip('/')}/rest/v1/{settings['table']}",
                headers={
                    "apikey": settings["key"],
                    "Authorization": f"Bearer {settings['key']}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            return
        except Exception:
            # Fall back to local file when remote logging is unavailable.
            pass

    try:
        _write_local_log(payload)
    except Exception:
        # Final fallback: print so deployment logs still capture failures.
        print("[app-error-log-fallback]", json.dumps(payload, default=str))
