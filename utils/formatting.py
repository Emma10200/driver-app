"""Formatting helpers for user-entered values."""

from __future__ import annotations


def normalize_digits(value: str | None) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def format_ssn(value: str | None) -> str:
    digits = normalize_digits(value)[:9]
    if len(digits) <= 3:
        return digits
    if len(digits) <= 5:
        return f"{digits[:3]}-{digits[3:]}"
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
