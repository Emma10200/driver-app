#!/usr/bin/env python3
"""Sync active + inactive GPS provider units into Supabase.

This builds a provider-unit archive that is independent of the dispatch board.
It does not replace ``assets_history``; it records which units/devices exist or
existed on each GPS provider so matching/billing can reason about deactivated
units that no longer appear in active board/cache sheets.

Examples:
    python scripts/sync_gps_provider_units.py --dry-run
    python scripts/sync_gps_provider_units.py --provider all --history-lookback-days 180
    python scripts/sync_gps_provider_units.py --provider gpstab

Before first use, run migration:
    supabase/migrations/0023_gps_provider_units.sql
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

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backfill_anytrek_history import (  # noqa: E402
    CHUNK_DAYS,
    EROAD_BASE,
    TRACK888_HOSQL_URL,
    _canonical_trailer_id,
    _format_anytrek_time,
    _gpstab_api_keys,
    _gpstab_get_vehicles,
    _load_secrets,
    _track888_auth_with_company,
    _track888_authenticate,
    _track888_companies,
    fetch_anytrek_chunk,
)

BATCH_SIZE = 500


class RestClient:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def upsert(self, table: str, rows: list[dict[str, Any]], *, on_conflict: str) -> int:
        if not rows:
            return 0
        total = 0
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            resp = requests.post(
                f"{self.url}/rest/v1/{table}",
                headers={**self.headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                params={"on_conflict": on_conflict},
                json=batch,
                timeout=60,
            )
            if not resp.ok:
                raise RuntimeError(f"upsert {table} failed: HTTP {resp.status_code} {resp.text[:500]}")
            total += len(batch)
        return total

    def rpc(self, function_name: str, payload: dict[str, Any]) -> Any:
        resp = requests.post(
            f"{self.url}/rest/v1/rpc/{function_name}",
            headers=self.headers,
            json=payload,
            timeout=120,
        )
        if not resp.ok:
            raise RuntimeError(f"rpc {function_name} failed: HTTP {resp.status_code} {resp.text[:500]}")
        try:
            return resp.json()
        except ValueError:
            return None

    def select_all(self, table: str, *, params: dict[str, Any], page_size: int = 1000, hard_cap: int = 200000) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while offset < hard_cap:
            page_params = dict(params)
            page_params["limit"] = str(page_size)
            page_params["offset"] = str(offset)
            resp = requests.get(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers,
                params=page_params,
                timeout=90,
            )
            if not resp.ok:
                raise RuntimeError(f"select {table} failed: HTTP {resp.status_code} {resp.text[:500]}")
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError(f"select {table} returned unexpected response: {str(batch)[:500]}")
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return rows

    def patch(self, table: str, payload: dict[str, Any], *, filters: dict[str, str]) -> None:
        resp = requests.patch(
            f"{self.url}/rest/v1/{table}",
            headers={**self.headers, "Prefer": "return=minimal"},
            params=filters,
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            raise RuntimeError(f"patch {table} failed: HTTP {resp.status_code} {resp.text[:500]}")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _parse_dt(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return None
    return text


def _status_text(row: dict[str, Any]) -> str:
    status = _first(
        row,
        "status", "Status", "state", "State", "vehicleStatus", "active", "isActive", "enabled", "disabled",
        "deleted", "isDeleted", "archived", "deactivated",
    )
    if isinstance(status, bool):
        return "active" if status else "inactive"
    return _text(status)


def _is_active(row: dict[str, Any]) -> bool | None:
    for key in ("active", "isActive", "enabled"):
        if key in row and isinstance(row.get(key), bool):
            return bool(row.get(key))
    for key in ("disabled", "deleted", "isDeleted", "archived", "deactivated"):
        if key in row and isinstance(row.get(key), bool):
            return not bool(row.get(key))

    status = _status_text(row).lower()
    if not status:
        return None
    inactive_tokens = ("inactive", "deactivated", "disabled", "deleted", "archived", "retired", "terminated")
    active_tokens = ("active", "enabled", "available", "online")
    if any(token in status for token in inactive_tokens):
        return False
    if any(token in status for token in active_tokens):
        return True
    return None


def _provider_row(
    *,
    provider: str,
    provider_account: str = "",
    provider_unit_id: str,
    asset_type: str,
    asset_id: str = "",
    display_name: str = "",
    status: str = "",
    is_active: bool | None = None,
    last_history_at: str | None = None,
    history_lookback_days: int | None = None,
    last_position_lat: float | None = None,
    last_position_lon: float | None = None,
    source: str,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_unit_id = _text(provider_unit_id) or _text(asset_id) or _text(display_name)
    asset_id = _text(asset_id)
    display_name = _text(display_name) or asset_id or provider_unit_id
    return {
        "provider": _text(provider) or "unknown",
        "provider_account": _text(provider_account),
        "provider_unit_id": provider_unit_id,
        "asset_type": asset_type if asset_type in ("truck", "trailer", "unknown") else "unknown",
        "asset_id": asset_id,
        "canonical_asset_id": asset_id,
        "display_name": display_name,
        "status": _text(status),
        "is_active": is_active,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "last_history_at": last_history_at,
        "history_lookback_days": history_lookback_days,
        "last_position_lat": last_position_lat,
        "last_position_lon": last_position_lon,
        "source": source,
        "raw": raw or {},
    }


def sync_gpstab(secrets: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account_label, api_key in _gpstab_api_keys(secrets):
        print(f"[GPSTab] Fetching roster for {account_label}...")
        vehicles = _gpstab_get_vehicles(api_key)
        print(f"  Found {len(vehicles)} vehicles")
        for vehicle in vehicles:
            provider_unit_id = _first(vehicle, "id", "Id", "vehicleInternalId")
            display_id = _first(vehicle, "vehicleid", "vehicleId", "name", "number", "unit")
            if not provider_unit_id and not display_id:
                continue
            rows.append(_provider_row(
                provider="GPSTab",
                provider_account=account_label,
                provider_unit_id=_text(provider_unit_id) or _text(display_id),
                asset_type="truck",
                asset_id=_text(display_id),
                display_name=_text(display_id),
                status=_status_text(vehicle),
                is_active=_is_active(vehicle),
                source="provider_roster",
                raw=vehicle,
            ))
    return rows


def sync_track888(secrets: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for company in _track888_companies(secrets):
        print(f"[888 ELD] Fetching roster for {company['name']}...")
        token = _track888_authenticate(company["user"], company["password"])
        if not token:
            token = _track888_auth_with_company(company["user"], company["password"], company["company_id"])
        if not token:
            print("  Authentication failed; skipping.")
            continue
        vehicles = _track888_fetch_collection_bounded(token, company["user"], "vehicles", company["company_id"], limit=1000)
        print(f"  Found {len(vehicles)} vehicles")
        for vehicle in vehicles:
            provider_unit_id = _first(vehicle, "_id", "id", "vehicleId", "vehicle_id")
            display_id = _first(vehicle, "name", "vehicleName", "number", "unit", "displayName")
            if not provider_unit_id and not display_id:
                continue
            rows.append(_provider_row(
                provider="888 ELD",
                provider_account=company["name"],
                provider_unit_id=_text(provider_unit_id) or _text(display_id),
                asset_type="truck",
                asset_id=_text(display_id),
                display_name=_text(display_id),
                status=_status_text(vehicle),
                is_active=_is_active(vehicle),
                source="provider_roster",
                raw=vehicle,
            ))
    return rows


def _track888_fetch_collection_bounded(token: str, user: str, collection: str, company_id: str, limit: int = 5000) -> list[dict[str, Any]]:
    """Fetch a Track Mile/HOSQL collection with bounded timeouts.

    The older backfill helper is intentionally broad and can silently return no
    rows if one URL variant stalls. For provider-unit roster sync we want the
    same response handling as the successful probe script.
    """
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
            response = requests.get(url, headers=headers, timeout=25)
            if not response.ok:
                continue
            data = response.json()
            if isinstance(data, list):
                return [row for row in data if isinstance(row, dict)]
            if isinstance(data, dict):
                for key in ("data", "items", "docs", "Data", "Items"):
                    if isinstance(data.get(key), list):
                        return [row for row in data[key] if isinstance(row, dict)]
        except Exception:
            continue
    return []


def sync_eroad(secrets: dict[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label in ["EROAD_PRESTIGE_KEY", "EROAD_XPRESS_KEY"]:
        api_key = secrets.get(label, "").strip()
        if not api_key:
            continue
        print(f"[EROAD] Fetching /vehicles roster for {label}...")
        headers = {"ApiKey": api_key, "Accept": "application/json"}
        fetched: list[dict[str, Any]] = []
        offset = 0
        page_size = 20
        for _ in range(50):
            try:
                resp = requests.get(
                    f"{EROAD_BASE}/vehicles",
                    headers=headers,
                    params={"offset": offset, "limit": page_size},
                    timeout=45,
                )
            except Exception as exc:
                print(f"  Error: {exc}")
                break
            if not resp.ok:
                print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
                break
            data = resp.json()
            page = []
            if isinstance(data, list):
                page = data
            elif isinstance(data, dict):
                for key in ("vehicles", "results", "items", "data"):
                    if isinstance(data.get(key), list):
                        page = data[key]
                        break
            if not isinstance(page, list) or not page:
                break
            fetched.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
            time.sleep(0.25)
        print(f"  Found {len(fetched)} vehicles")
        for vehicle in fetched:
            provider_unit_id = _first(vehicle, "id", "vehicleId", "uuid")
            display_id = _first(vehicle, "name", "displayName", "registration", "fleetId", "number")
            if not provider_unit_id and not display_id:
                continue
            rows.append(_provider_row(
                provider="EROAD",
                provider_account=label,
                provider_unit_id=_text(provider_unit_id) or _text(display_id),
                asset_type="truck",
                asset_id=_text(display_id),
                display_name=_text(display_id),
                status=_status_text(vehicle),
                is_active=_is_active(vehicle),
                source="provider_roster",
                raw=vehicle,
            ))
    return rows


def sync_anytrek_discovery(secrets: dict[str, str], days: int) -> list[dict[str, Any]]:
    api_key = secrets.get("ANYTREK_API_KEY", "").strip()
    if not api_key or days <= 0:
        return []

    print(f"[Anytrek] Discovering trailer units from {days} days of transactions...")
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    latest: dict[str, dict[str, Any]] = {}
    chunk_start = start
    while chunk_start < now:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), now)
        transactions = fetch_anytrek_chunk(api_key, chunk_start, chunk_end)
        for tx in transactions:
            vehicle_name = _text(tx.get("vehicleName"))
            canonical_id = _canonical_trailer_id(vehicle_name)
            if not canonical_id:
                continue
            device_id = _text(tx.get("deviceId"))
            provider_unit_id = device_id or canonical_id
            recorded_at = _parse_dt(tx.get("createTime") or tx.get("reportTime"))
            current = latest.get(provider_unit_id)
            if current and recorded_at and current.get("last_history_at") and recorded_at <= current["last_history_at"]:
                continue
            latest[provider_unit_id] = {
                "provider_unit_id": provider_unit_id,
                "canonical_id": canonical_id,
                "vehicle_name": vehicle_name,
                "last_history_at": recorded_at,
                "lat": tx.get("lat"),
                "lon": tx.get("lng"),
                "tx": tx,
            }
        chunk_start = chunk_end
        time.sleep(0.25)

    rows: list[dict[str, Any]] = []
    for item in latest.values():
        lat = item.get("lat")
        lon = item.get("lon")
        rows.append(_provider_row(
            provider="Anytrek",
            provider_account="",
            provider_unit_id=item["provider_unit_id"],
            asset_type="trailer",
            asset_id=item["canonical_id"],
            display_name=item["vehicle_name"],
            status="history_seen",
            is_active=None,
            last_history_at=item.get("last_history_at"),
            history_lookback_days=days,
            last_position_lat=float(lat) if lat not in (None, "") else None,
            last_position_lon=float(lon) if lon not in (None, "") else None,
            source="anytrek_transactions",
            raw=item["tx"],
        ))
    print(f"  Discovered {len(rows)} Anytrek trailer units")
    return rows


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["provider"], row["provider_account"], row["provider_unit_id"])
        existing = out.get(key)
        if not existing:
            out[key] = row
            continue
        # Prefer rows with an asset_id and latest last_history_at.
        if row.get("asset_id") and not existing.get("asset_id"):
            out[key] = {**existing, **row}
        elif row.get("last_history_at") and str(row.get("last_history_at")) > str(existing.get("last_history_at") or ""):
            out[key] = {**existing, **row}
    return list(out.values())


def infer_provider_units_from_hour_tracks(client: RestClient, days: int) -> list[dict[str, Any]]:
    """Infer history-seen units from compact asset_hour_tracks.

    This is the practical fallback when aggregating millions of raw
    ``assets_history`` rows in SQL hits statement timeouts. It captures every
    unit with hourly GPS tracks in the lookback window and stores the latest
    provider/source/last ping available from the compact rollup.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))
    print(f"Inferring provider units from asset_hour_tracks ({start.date()} → {end.date()})...")
    rows = client.select_all(
        "asset_hour_tracks",
        params={
            "select": "asset_type,asset_id,provider,source,last_source_recorded_at,centroid_lat,centroid_lon,address,job_id",
            "hour_start": f"gte.{start.isoformat()}",
            "order": "hour_start.desc",
        },
        page_size=1000,
        hard_cap=200000,
    )
    latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        asset_type = _text(row.get("asset_type"))
        asset_id = _text(row.get("asset_id"))
        if asset_type not in ("truck", "trailer") or not asset_id:
            continue
        provider = _text(row.get("provider")) or _text(row.get("source")) or "history"
        source = _text(row.get("source")) or "asset_hour_tracks"
        key = (provider, source, asset_type, asset_id)
        recorded_at = _parse_dt(row.get("last_source_recorded_at"))
        current = latest.get(key)
        if current and str(recorded_at or "") <= str(current.get("last_history_at") or ""):
            continue
        latest[key] = _provider_row(
            provider=provider,
            provider_account=source,
            provider_unit_id=asset_id,
            asset_type=asset_type,
            asset_id=asset_id,
            display_name=asset_id,
            status="history_seen",
            is_active=None,
            last_history_at=recorded_at,
            history_lookback_days=days,
            last_position_lat=float(row["centroid_lat"]) if row.get("centroid_lat") is not None else None,
            last_position_lon=float(row["centroid_lon"]) if row.get("centroid_lon") is not None else None,
            source="asset_hour_tracks",
            raw={
                "track_source": source,
                "job_id": row.get("job_id"),
                "address": row.get("address"),
                "history_inferred_from": "asset_hour_tracks",
            },
        )
    inferred = list(latest.values())
    print(f"  Inferred {len(inferred)} history-seen provider-unit rows from asset_hour_tracks.")
    return inferred


def canonicalize_provider_trailer_aliases(client: RestClient) -> int:
    """Fold base trailer IDs into unique dashed canonical IDs in gps_provider_units.

    Example: if archive contains both ``4400`` and ``4400-14``, and there is only
    one dashed ID for prefix ``4400``, rows with fleet-facing ``asset_id=4400``
    are updated to ``asset_id=canonical_asset_id=4400-14``. Provider-native
    identifiers (device IDs) are left unchanged.
    """
    rows = client.select_all(
        "gps_provider_units",
        params={
            "select": "provider,provider_account,provider_unit_id,asset_type,asset_id,canonical_asset_id,display_name,raw",
            "asset_type": "eq.trailer",
            "order": "asset_id.asc",
        },
        page_size=1000,
        hard_cap=100000,
    )
    dashed_by_prefix: dict[str, set[str]] = {}
    for row in rows:
        for value in (_text(row.get("asset_id")), _text(row.get("canonical_asset_id")), _text(row.get("display_name"))):
            match = re.match(r"^(\d{2,})-\d+", value)
            if match:
                dashed_by_prefix.setdefault(match.group(1), set()).add(value)

    alias_map = {prefix: next(iter(ids)) for prefix, ids in dashed_by_prefix.items() if len(ids) == 1}
    changed = 0
    for row in rows:
        asset_id = _text(row.get("asset_id"))
        if not re.fullmatch(r"\d{2,}", asset_id):
            continue
        canonical = alias_map.get(asset_id)
        if not canonical or canonical == asset_id:
            continue
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        raw = dict(raw)
        raw["alias_canonicalized_from"] = asset_id
        raw["alias_canonicalized_to"] = canonical
        client.patch(
            "gps_provider_units",
            {
                "asset_id": canonical,
                "canonical_asset_id": canonical,
                "display_name": canonical,
                "raw": raw,
            },
            filters={
                "provider": f"eq.{row['provider']}",
                "provider_account": f"eq.{row['provider_account']}",
                "provider_unit_id": f"eq.{row['provider_unit_id']}",
            },
        )
        changed += 1
    if changed:
        print(f"Canonicalized {changed} gps_provider_units trailer alias row(s).")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync GPS provider unit archive, including inactive/deactivated units.")
    parser.add_argument("--provider", choices=["all", "gpstab", "track888", "eroad", "anytrek"], default="all")
    parser.add_argument("--history-lookback-days", type=int, default=180, help="Days of assets_history to fold into provider archive.")
    parser.add_argument("--anytrek-discovery-days", type=int, default=0, help="Optional direct Anytrek transaction discovery window; 0 skips direct discovery.")
    parser.add_argument("--skip-history-infer", action="store_true", help="Do not call refresh_gps_provider_units_from_history().")
    parser.add_argument("--history-infer-source", choices=["auto", "raw", "hour-tracks", "none"], default="auto", help="How to infer history-seen units after provider roster sync.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    secrets = _load_secrets()
    supabase_url = secrets.get("SUPABASE_URL", "")
    supabase_key = secrets.get("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_KEY not found in .env, secrets.toml, or environment.")

    client = RestClient(supabase_url, supabase_key)
    rows: list[dict[str, Any]] = []

    if args.provider in ("all", "gpstab"):
        rows.extend(sync_gpstab(secrets))
    if args.provider in ("all", "track888"):
        rows.extend(sync_track888(secrets))
    if args.provider in ("all", "eroad"):
        rows.extend(sync_eroad(secrets))
    if args.provider in ("all", "anytrek") and args.anytrek_discovery_days > 0:
        rows.extend(sync_anytrek_discovery(secrets, args.anytrek_discovery_days))

    rows = _dedupe_rows(rows)
    print(f"\nPrepared {len(rows)} provider roster/archive rows.")
    if args.dry_run:
        print(json.dumps(rows[:20], indent=2, default=str))
        if len(rows) > 20:
            print(f"... {len(rows) - 20} more rows omitted")
    elif rows:
        written = client.upsert(
            "gps_provider_units",
            rows,
            on_conflict="provider,provider_account,provider_unit_id",
        )
        print(f"Upserted {written} rows into gps_provider_units.")

    if not args.skip_history_infer and args.history_infer_source != "none":
        print(f"\nRefreshing provider archive from history ({args.history_lookback_days} days, source={args.history_infer_source})...")
        if args.dry_run:
            print("DRY RUN: skipped history refresh.")
        elif args.history_infer_source in ("hour-tracks", "auto"):
            inferred_rows = infer_provider_units_from_hour_tracks(client, args.history_lookback_days)
            written = client.upsert(
                "gps_provider_units",
                inferred_rows,
                on_conflict="provider,provider_account,provider_unit_id",
            )
            print(f"History inference upserted {written} asset-hour-track provider-unit row(s).")
        else:
            result = client.rpc(
                "refresh_gps_provider_units_from_history",
                {"lookback_days": max(1, int(args.history_lookback_days))},
            )
            print(f"History refresh affected {result} provider-unit row(s).")

    if not args.dry_run:
        canonicalize_provider_trailer_aliases(client)


if __name__ == "__main__":
    main()
