#!/usr/bin/env python3
"""Probe GPS provider API structures without printing secrets.

The goal is to answer: which providers expose active/inactive/all-unit rosters,
what fields come back, and whether 60-day history/transactions can reveal units
that are no longer on the dispatch board.

Writes a sanitized JSON report to gps_reports/.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backfill_anytrek_history import (  # noqa: E402
    ANYTREK_TX_URL,
    EROAD_BASE,
    MAX_COUNT_PER_CALL,
    TRACK888_HOSQL_URL,
    _format_anytrek_time,
    _gpstab_api_keys,
    _gpstab_fetch_vehicle_history,
    _gpstab_get_vehicles,
    _load_secrets,
    _track888_auth_with_company,
    _track888_authenticate,
    _track888_companies,
    _track888_fetch_collection,
)

SENSITIVE_KEY_RE = re.compile(r"(key|token|secret|password|authorization|apikey|api_key|access)", re.I)
MAX_SAMPLE_CHARS = 20_000


def _redact(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in list(value.items())[:80]}
    if isinstance(value, list):
        return [_redact(v, key) for v in value[:5]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "…"
    return value


def _shape(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): _shape(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        if not value:
            return []
        return [_shape(value[0], depth + 1)]
    return type(value).__name__


def _sample_collection(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    keys = sorted({str(k) for item in items[: min(len(items), 50)] if isinstance(item, dict) for k in item.keys()})
    return {
        "count": len(items),
        "keys_seen_first_50": keys,
        "sample": [_redact(item) for item in items[:limit]],
        "sample_shape": _shape(items[0]) if items else None,
    }


def _safe_error(exc: Exception) -> dict[str, Any]:
    return {"error": type(exc).__name__, "message": str(exc)[:500]}


def probe_gpstab(secrets: dict[str, str], days: int, sample_limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {"accounts": {}}
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=min(days, 7))  # one sample history call can be huge; cap probe history span
    for account_label, api_key in _gpstab_api_keys(secrets):
        print(f"[probe] GPSTab {account_label}: roster", flush=True)
        account: dict[str, Any] = {}
        try:
            vehicles = _gpstab_get_vehicles(api_key)
            account["vehicles"] = _sample_collection(vehicles, sample_limit)
            if vehicles:
                first = vehicles[0]
                vehicle_id = first.get("id")
                display_id = first.get("vehicleid") or first.get("vehicleId")
                account["history_probe"] = {"vehicle_id": vehicle_id, "display_id": display_id, "days_requested": (now - start).days}
                if vehicle_id:
                    print(f"[probe] GPSTab {account_label}: sample history for {display_id or vehicle_id}", flush=True)
                    items = _gpstab_fetch_vehicle_history(api_key, int(vehicle_id), start, now)
                    account["history_probe"].update(_sample_collection(items, sample_limit))
        except Exception as exc:
            account.update(_safe_error(exc))
        out["accounts"][account_label] = account
    return out


def _eroad_get(api_key: str, path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    resp = requests.get(
        f"{EROAD_BASE}{path}",
        headers={"ApiKey": api_key, "Accept": "application/json"},
        params=params or {},
        timeout=45,
    )
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:1000]
    return resp.status_code, body


def _extract_items(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in ("results", "items", "data", "vehicles", "Items", "Data"):
            if isinstance(body.get(key), list):
                return [x for x in body[key] if isinstance(x, dict)]
    return []


def probe_eroad(secrets: dict[str, str], sample_limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {"accounts": {}}
    for label in ["EROAD_PRESTIGE_KEY", "EROAD_XPRESS_KEY"]:
        api_key = secrets.get(label, "").strip()
        if not api_key:
            continue
        account: dict[str, Any] = {}
        try:
            for endpoint, params in [
                ("/vehicles", {"firstResult": 0, "maxResult": 5, "limit": 5}),
                ("/vehicleCurrentState", {"firstResult": 0, "maxResult": 5}),
            ]:
                print(f"[probe] EROAD {label}: {endpoint}", flush=True)
                code, body = _eroad_get(api_key, endpoint, params)
                items = _extract_items(body)
                account[endpoint] = {
                    "http_status": code,
                    "top_level_shape": _shape(body),
                    "items": _sample_collection(items, sample_limit),
                }
        except Exception as exc:
            account.update(_safe_error(exc))
        out["accounts"][label] = account
    return out


def _track888_fetch_collection_probe(token: str, user: str, collection: str, company_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "user": user,
        "Content-Type": "application/json",
    }
    urls = []
    if company_id:
        urls.append(f"{TRACK888_HOSQL_URL}/{collection}?%24limit={limit}&companyId={company_id}")
    urls.append(f"{TRACK888_HOSQL_URL}/{collection}?%24limit={limit}")
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            if not resp.ok:
                continue
            data = resp.json()
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if isinstance(data, dict):
                for key in ("data", "items", "docs", "Data", "Items"):
                    if isinstance(data.get(key), list):
                        return [x for x in data[key] if isinstance(x, dict)]
        except Exception:
            continue
    return []


def probe_track888(secrets: dict[str, str], sample_limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {"companies": {}}
    for company in _track888_companies(secrets):
        print(f"[probe] 888 ELD {company['name']}: authenticate", flush=True)
        item: dict[str, Any] = {"company_id_prefix": company["company_id"][:10] + "…"}
        try:
            token = _track888_authenticate(company["user"], company["password"])
            if not token:
                token = _track888_auth_with_company(company["user"], company["password"], company["company_id"])
            item["authenticated"] = bool(token)
            if token:
                for collection in ["vehicles", "vehicle_statuses", "latest_vehicle_statuses"]:
                    print(f"[probe] 888 ELD {company['name']}: {collection}", flush=True)
                    rows = _track888_fetch_collection_probe(token, company["user"], collection, company["company_id"], limit=1000)
                    item[collection] = _sample_collection(rows, sample_limit)
        except Exception as exc:
            item.update(_safe_error(exc))
        out["companies"][company["name"]] = item
    return out


def fetch_anytrek_window(api_key: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    params = {
        "key": api_key,
        "startTime": _format_anytrek_time(start),
        "endTime": _format_anytrek_time(end),
        "count": MAX_COUNT_PER_CALL,
    }
    resp = requests.post(f"{ANYTREK_TX_URL}?{urlencode(params)}", timeout=180)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def probe_anytrek(secrets: dict[str, str], days: int, sample_limit: int, max_chunks: int) -> dict[str, Any]:
    api_key = secrets.get("ANYTREK_API_KEY", "").strip()
    if not api_key:
        return {"configured": False}
    out: dict[str, Any] = {"configured": True, "days_requested": days, "chunks": []}
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    unique_names: set[str] = set()
    unique_devices: set[str] = set()
    total = 0
    chunk_start = start
    chunks_done = 0
    while chunk_start < now and chunks_done < max_chunks:
        chunk_end = min(chunk_start + timedelta(days=1), now)
        chunk: dict[str, Any] = {"start": chunk_start.isoformat(), "end": chunk_end.isoformat()}
        try:
            print(f"[probe] Anytrek transactions: {chunk_start.date()} to {chunk_end.date()}", flush=True)
            rows = fetch_anytrek_window(api_key, chunk_start, chunk_end)
            total += len(rows)
            for row in rows:
                if row.get("vehicleName"):
                    unique_names.add(str(row.get("vehicleName")))
                if row.get("deviceId"):
                    unique_devices.add(str(row.get("deviceId")))
            chunk.update(_sample_collection(rows, sample_limit))
        except Exception as exc:
            chunk.update(_safe_error(exc))
        out["chunks"].append(chunk)
        chunk_start = chunk_end
        chunks_done += 1
        time.sleep(0.25)
    out["transactions_seen_in_probe_chunks"] = total
    out["unique_vehicle_names_in_probe_chunks"] = len(unique_names)
    out["unique_device_ids_in_probe_chunks"] = len(unique_devices)
    out["vehicle_name_sample"] = sorted(unique_names)[:100]
    out["note"] = "Probe samples first chunks only by default; sync script can scan full 60 days."
    return out


def probe_motive(secrets: dict[str, str]) -> dict[str, Any]:
    # Motive OAuth token is currently managed by Apps Script OAuth2 Script Properties,
    # not by this Python env in existing code. Report env availability without trying
    # to mint a token outside the established OAuth flow.
    keys = [k for k in ("MOTIVE_ACCESS_TOKEN", "MOTIVE_CLIENT_ID", "MOTIVE_CLIENT_SECRET") if secrets.get(k)]
    return {
        "configured_keys_present": keys,
        "python_probe_supported": "MOTIVE_ACCESS_TOKEN" in keys,
        "note": "Existing Motive integration uses Apps Script OAuth2 service; Python env normally cannot call it unless an access token is added.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe GPS provider API structures safely.")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--provider", choices=["all", "gpstab", "track888", "eroad", "anytrek", "motive"], default="all")
    parser.add_argument("--sample-limit", type=int, default=2)
    parser.add_argument("--anytrek-max-chunks", type=int, default=3, help="Probe first N one-day chunks; use 60 to sample every day.")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    secrets = _load_secrets()
    configured = sorted(k for k, v in secrets.items() if v and not SENSITIVE_KEY_RE.search(k))
    configured_sensitive = sorted(k for k, v in secrets.items() if v and SENSITIVE_KEY_RE.search(k))
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_requested": args.days,
        "configured_non_sensitive_keys": configured,
        "configured_sensitive_key_names": configured_sensitive,
        "providers": {},
    }

    if args.provider in ("all", "gpstab"):
        report["providers"]["gpstab"] = probe_gpstab(secrets, args.days, args.sample_limit)
    if args.provider in ("all", "track888"):
        report["providers"]["track888"] = probe_track888(secrets, args.sample_limit)
    if args.provider in ("all", "eroad"):
        report["providers"]["eroad"] = probe_eroad(secrets, args.sample_limit)
    if args.provider in ("all", "anytrek"):
        report["providers"]["anytrek"] = probe_anytrek(secrets, args.days, args.sample_limit, args.anytrek_max_chunks)
    if args.provider in ("all", "motive"):
        report["providers"]["motive"] = probe_motive(secrets)

    out_path = Path(args.out) if args.out else ROOT / "gps_reports" / f"gps_provider_api_probe_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, default=str)
    if len(payload) > MAX_SAMPLE_CHARS * 10:
        # This should rarely happen because samples are capped, but avoid absurd files.
        report["truncated_warning"] = "Report was large; samples are capped in script."
        payload = json.dumps(report, indent=2, default=str)
    out_path.write_text(payload, encoding="utf-8")

    print(f"Wrote probe report: {out_path}")
    for provider, data in report["providers"].items():
        print(f"\n[{provider}]")
        print(json.dumps(data, indent=2, default=str)[:MAX_SAMPLE_CHARS])


if __name__ == "__main__":
    main()
