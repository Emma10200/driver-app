from __future__ import annotations

from typing import Any

from .api_client import QboClient


def normalize_for_match(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).upper()
    out_chars: list[str] = []
    for ch in text:
        out_chars.append(ch if ch.isalnum() else " ")
    return " ".join("".join(out_chars).split())


def _normalize_cache_name(value: str) -> str:
    return "".join(value.split()).lower()


def _match_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    words_a = [word for word in a.split(" ") if word]
    words_b = [word for word in b.split(" ") if word]
    if not words_a or not words_b:
        return 0.0
    matches = 0
    for word_a in words_a:
        for word_b in words_b:
            if word_a == word_b or word_a in word_b or word_b in word_a:
                matches += 1
                break
    return matches / max(len(words_a), len(words_b))


def _escape_qbo_string(value: str) -> str:
    return value.replace("'", "\\'")


class EntityLookupService:
    def __init__(self, qbo_client: QboClient) -> None:
        self._qbo = qbo_client
        self._runtime: dict[tuple[str, str, str], str | None] = {}

    def resolve_entity(self, type_name: str, name: str, realm_id: str) -> str | None:
        if not name or not realm_id:
            return None
        runtime_key = (realm_id, type_name, normalize_for_match(name))
        if runtime_key in self._runtime:
            return self._runtime[runtime_key]
        resolved = self._query_entity(type_name=type_name, name=name, realm_id=realm_id)
        self._runtime[runtime_key] = resolved
        return resolved

    def resolve_account(self, account_name: str, realm_id: str) -> str | None:
        if not account_name or not realm_id:
            return None
        runtime_key = (realm_id, "Account", normalize_for_match(account_name))
        if runtime_key in self._runtime:
            return self._runtime[runtime_key]

        search_term = account_name.split(":")[-1].strip() if ":" in account_name else account_name
        safe_name = _escape_qbo_string(search_term)
        result = self._qbo.query(
            "SELECT Id, Name, FullyQualifiedName FROM Account WHERE Name = '" + safe_name + "'",
            realm_id=realm_id,
        )
        accounts = (result.get("QueryResponse") or {}).get("Account") or []
        matched_id: str | None = None
        for account in accounts:
            if account.get("FullyQualifiedName") == account_name:
                matched_id = account.get("Id")
                break
            if account.get("Name") == account_name:
                matched_id = account.get("Id")
                break
        if matched_id is None and len(accounts) == 1 and ":" not in account_name:
            matched_id = accounts[0].get("Id")
        self._runtime[runtime_key] = matched_id
        return matched_id

    def invalidate(self) -> None:
        self._runtime.clear()

    def _query_entity(self, type_name: str, name: str, realm_id: str) -> str | None:
        field_name = "Name" if type_name in ("Item", "Term") else "DisplayName"
        safe_name = _escape_qbo_string(name)
        sql_exact = f"SELECT Id, {field_name} FROM {type_name} WHERE {field_name} = '{safe_name}'"
        result = self._qbo.query(sql_exact, realm_id=realm_id)
        rows = (result.get("QueryResponse") or {}).get(type_name) or []
        if rows:
            return rows[0].get("Id")

        first_word = name.strip().split()[0] if name.strip() else ""
        if len(first_word) < 3:
            return None
        safe_first = _escape_qbo_string(first_word)
        sql_like = f"SELECT Id, {field_name} FROM {type_name} WHERE {field_name} LIKE '{safe_first}%'"
        try:
            like_result = self._qbo.query(sql_like, realm_id=realm_id)
        except RuntimeError:
            return None
        like_rows = (like_result.get("QueryResponse") or {}).get(type_name) or []
        if not like_rows:
            return None

        target_norm = normalize_for_match(name)
        best_id: str | None = None
        best_score = 0.0
        for row in like_rows:
            row_name = row.get(field_name) or ""
            score = _match_score(target_norm, normalize_for_match(row_name))
            if score > best_score:
                best_score = score
                best_id = row.get("Id")
        if best_id and best_score >= 0.5:
            return best_id
        return None
