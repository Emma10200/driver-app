from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


_SIGNATURE_VERSION = 1


def _normalize_code(value: Any) -> str:
    """Normalize a money-code/ref value for exact-batch duplicate detection."""
    return " ".join(str(value or "").strip().upper().split())


def _normalize_amount(value: Any) -> str:
    """Normalize currency values to cents without float string drift."""
    if value in (None, ""):
        return "0.00"
    try:
        amount = Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        amount = Decimal("0.00")
    return f"{amount:.2f}"


def money_code_batch_entry(draft: dict[str, Any]) -> dict[str, str]:
    """Return the canonical code/amount pair used for money-code batch identity."""
    code = _normalize_code(draft.get("DocNumber"))
    amount = _normalize_amount(_draft_amount(draft))
    return {"code": code, "amount": amount}


def build_money_code_batch_signature(drafts: list[dict[str, Any]], realm_id: str) -> dict[str, Any]:
    """
    Build an order-independent, multiplicity-preserving fingerprint for a money-code import batch.

    The identity is intentionally limited to the whole-batch multiset of money-code/ref numbers
    and amounts, scoped to the QBO realm. It does not reject a single money-code use by itself;
    only the same full structure is considered a duplicate.
    """
    entries = [money_code_batch_entry(draft) for draft in drafts or []]
    entries = [entry for entry in entries if entry["code"] and entry["amount"] != "0.00"]
    entries.sort(key=lambda entry: (entry["code"], entry["amount"]))
    payload = {
        "version": _SIGNATURE_VERSION,
        "template_type": "money_codes",
        "realm_id": str(realm_id or "").strip(),
        "entries": entries,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest() if entries and payload["realm_id"] else ""
    total = sum((Decimal(entry["amount"]) for entry in entries), Decimal("0.00"))
    return {
        **payload,
        "fingerprint": fingerprint,
        "entry_count": len(entries),
        "total_amount": f"{total:.2f}",
    }


def _draft_amount(draft: dict[str, Any]) -> Any:
    total = Decimal("0.00")
    for line in draft.get("Line") or []:
        if not isinstance(line, dict):
            continue
        try:
            total += Decimal(str(line.get("Amount") or "0").replace(",", ""))
        except (InvalidOperation, ValueError):
            continue
    if total:
        return total
    return draft.get("TotalAmt") or "0"
