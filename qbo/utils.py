from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def normalize_key(value: Any) -> str:
    if value is None:
        return ""
    return "".join(ch for ch in str(value).strip().lower() if ch.isalnum())


def normalize_company_name(value: Any) -> str:
    return normalize_key(value)


def safe_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_source_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    if not text:
        return ""

    formats = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y/%m/%d",
        "%m-%d-%Y",
        "%m-%d-%y",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def parse_optional_date(value: Any) -> str:
    return parse_source_date(value)


def add_days_to_iso_date(iso_date: str, days_to_add: int) -> str:
    if not iso_date:
        return ""
    base = datetime.strptime(iso_date, "%Y-%m-%d")
    return (base + timedelta(days=int(days_to_add))).strftime("%Y-%m-%d")


def most_recent_friday() -> str:
    today = datetime.now()
    days_since_friday = (today.weekday() - 4) % 7
    target = today - timedelta(days=days_since_friday)
    return target.strftime("%Y-%m-%d")
