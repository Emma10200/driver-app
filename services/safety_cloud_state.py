"""Shared cloud-backed JSON state for the Safety Paperwork Portal.

The staff Streamlit app and the scheduled email-reply ingester run in *different
processes* (the GitHub Action runner has no access to Streamlit Cloud's ephemeral
local filesystem). Anything they both need to see — the recipient link directory
and the inbox processed/unmatched state — must live in Supabase Storage so it is
shared and durable.

This module keeps that integration tiny and dependency-injectable: a single JSON
blob per logical "state file" under ``safety-uploads/live/state/``. Volume is low
(safety paperwork requests), so last-write-wins on a whole-file blob is acceptable
for v1. When Supabase is not configured (local dev / tests) every call is a no-op
that reports ``enabled == False`` so callers transparently fall back to local
JSON files.
"""

from __future__ import annotations

import json
from typing import Any

from submission_storage import (
    JSON_MIME,
    _read_supabase_bytes,
    _save_file_map_to_supabase,
    _supabase_enabled,
)

# All safety portal cloud state lives under this prefix so it sits next to the
# uploaded documents (``safety-uploads/live/document_uploads/...``) and is easy
# to find/inspect in the Supabase dashboard.
STATE_PREFIX = "safety-uploads/live/state"


def cloud_state_enabled() -> bool:
    """Return True when Supabase storage is configured for shared state."""
    try:
        return bool(_supabase_enabled())
    except Exception:
        return False


def _state_path(name: str) -> str:
    safe_name = str(name or "").strip().strip("/")
    if not safe_name:
        raise ValueError("State file name is required.")
    if not safe_name.endswith(".json"):
        safe_name = f"{safe_name}.json"
    return f"{STATE_PREFIX}/{safe_name}"


def read_state(name: str) -> dict[str, Any]:
    """Read a JSON state object from Supabase. Returns ``{}`` when absent/disabled."""
    return read_state_path(_state_path(name))


def read_state_path(relative_path: str) -> dict[str, Any]:
    """Read a JSON state object from an explicit Supabase relative path."""
    if not cloud_state_enabled():
        return {}
    try:
        raw = _read_supabase_bytes(relative_path)
    except FileNotFoundError:
        return {}
    except Exception:
        # Never let a transient Supabase read break the caller; they fall back
        # to local state. The ingester logs its own failures separately.
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(name: str, data: dict[str, Any]) -> bool:
    """Persist a JSON state object to Supabase. Returns True when written."""
    if not cloud_state_enabled():
        return False
    relative_path = _state_path(name)
    prefix, _, file_name = relative_path.rpartition("/")
    payload = json.dumps(data, indent=2, sort_keys=True, default=str).encode("utf-8")
    try:
        _save_file_map_to_supabase(prefix, {file_name: (payload, JSON_MIME)})
    except Exception:
        return False
    return True
