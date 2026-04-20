"""Helpers for saving submitted applications to local storage or Supabase."""

from __future__ import annotations

import io
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

try:
    import streamlit as st
    from streamlit.errors import StreamlitSecretNotFoundError
except ImportError:  # pragma: no cover - defensive import for non-Streamlit tooling
    st = None

    class StreamlitSecretNotFoundError(Exception):
        """Fallback used when Streamlit is unavailable."""

VALID_BACKENDS = {"auto", "local", "supabase", "both"}
DEFAULT_BUCKET = "driver-applications"
PDF_MIME = "application/pdf"


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "submission"


def _json_default(value: Any):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _candidate_names(name: str) -> list[str]:
    return [name, name.lower()]


def _get_streamlit_secret(name: str) -> str | None:
    if st is None:
        return None

    try:
        secrets = st.secrets

        for candidate in _candidate_names(name):
            if candidate in secrets:
                return str(secrets[candidate])

        for section_name in ("app", "supabase", "storage"):
            section = secrets.get(section_name, {})
            for candidate in _candidate_names(name):
                if candidate in section:
                    return str(section[candidate])
    except StreamlitSecretNotFoundError:
        return None

    return None


def _get_secret(name: str, default: str | None = None) -> str | None:
    streamlit_value = _get_streamlit_secret(name)
    if streamlit_value:
        return streamlit_value

    for candidate in _candidate_names(name):
        env_value = os.getenv(candidate)
        if env_value:
            return env_value

    return default


def _get_backend() -> str:
    backend = (_get_secret("SUBMISSION_STORAGE_BACKEND", "auto") or "auto").strip().lower()
    return backend if backend in VALID_BACKENDS else "auto"


def _get_supabase_settings() -> dict[str, str]:
    return {
        "url": (_get_secret("SUPABASE_URL", "") or "").strip(),
        "key": (_get_secret("SUPABASE_SERVICE_KEY") or _get_secret("SUPABASE_KEY", "") or "").strip(),
        "bucket": (_get_secret("SUPABASE_BUCKET", DEFAULT_BUCKET) or DEFAULT_BUCKET).strip(),
        "table": (_get_secret("SUPABASE_TABLE", "") or "").strip(),
    }


def _supabase_enabled() -> bool:
    settings = _get_supabase_settings()
    return bool(settings["url"] and settings["key"])


def get_submission_destination_summary(local_base_dir: Path) -> str:
    backend = _get_backend()
    settings = _get_supabase_settings()

    if backend == "local":
        return f"the local folder `{local_base_dir}`"
    if backend == "supabase":
        return f"Supabase bucket `{settings['bucket']}`"
    if backend == "both":
        return f"both Supabase bucket `{settings['bucket']}` and the local folder `{local_base_dir}`"
    if _supabase_enabled():
        return f"Supabase bucket `{settings['bucket']}`"
    return f"the local folder `{local_base_dir}`"


def _build_submission_key(form_data: dict[str, Any]) -> tuple[str, str]:
    submission_timestamp = form_data.get("final_submission_timestamp", datetime.now().isoformat())
    timestamp = datetime.fromisoformat(submission_timestamp).strftime("%Y%m%d_%H%M%S")
    applicant_name = f"{form_data.get('last_name', 'driver')}_{form_data.get('first_name', '')}"
    submission_key = f"{timestamp}_{_slugify(applicant_name)}"
    return submission_timestamp, submission_key


def _build_payload(
    form_data: dict[str, Any],
    employers: list[dict[str, Any]],
    licenses: list[dict[str, Any]],
    accidents: list[dict[str, Any]],
    violations: list[dict[str, Any]],
) -> dict[str, Any]:
    submission_timestamp, submission_key = _build_submission_key(form_data)
    return {
        "submission_key": submission_key,
        "submitted_at": submission_timestamp,
        "form_data": form_data,
        "employers": employers,
        "licenses": licenses,
        "accidents": accidents,
        "violations": violations,
    }


def _build_file_map(payload: dict[str, Any], artifacts: dict[str, bytes]) -> dict[str, tuple[bytes, str]]:
    file_map = {
        "submission.json": (
            json.dumps(payload, indent=2, default=_json_default).encode("utf-8"),
            "application/json",
        ),
        "application.pdf": (artifacts["application_pdf"], PDF_MIME),
        "fcra_disclosure.pdf": (artifacts["fcra_pdf"], PDF_MIME),
        "psp_disclosure.pdf": (artifacts["psp_pdf"], PDF_MIME),
        "clearinghouse_release.pdf": (artifacts["clearinghouse_pdf"], PDF_MIME),
    }

    if artifacts.get("california_pdf"):
        file_map["california_disclosure.pdf"] = (artifacts["california_pdf"], PDF_MIME)

    return file_map


def _save_locally(local_base_dir: Path, submission_key: str, file_map: dict[str, tuple[bytes, str]]) -> dict[str, Any]:
    submission_dir = local_base_dir / submission_key
    submission_dir.mkdir(parents=True, exist_ok=True)

    for file_name, (content, _) in file_map.items():
        (submission_dir / file_name).write_bytes(content)

    return {
        "backend": "local",
        "location_label": str(submission_dir),
        "submission_key": submission_key,
        "files": list(file_map.keys()),
    }


def _save_to_supabase(payload: dict[str, Any], submission_key: str, file_map: dict[str, tuple[bytes, str]]) -> dict[str, Any]:
    settings = _get_supabase_settings()
    if not settings["url"] or not settings["key"]:
        raise RuntimeError("Supabase storage is selected but SUPABASE_URL / SUPABASE_SERVICE_KEY is missing.")

    base_url = settings["url"].rstrip("/")
    bucket = settings["bucket"] or DEFAULT_BUCKET
    remote_prefix = f"submissions/{submission_key}"
    headers = {
        "apikey": settings["key"],
        "Authorization": f"Bearer {settings['key']}",
    }

    for file_name, (content, content_type) in file_map.items():
        remote_path = f"{remote_prefix}/{file_name}"
        upload_url = f"{base_url}/storage/v1/object/{bucket}/{quote(remote_path)}"
        response = requests.post(
            upload_url,
            headers={
                **headers,
                "Content-Type": content_type,
                "x-upsert": "true",
            },
            data=io.BytesIO(content).getvalue(),
            timeout=60,
        )
        response.raise_for_status()

    warnings: list[str] = []
    if settings["table"]:
        try:
            metadata_url = f"{base_url}/rest/v1/{settings['table']}"
            response = requests.post(
                metadata_url,
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={
                    "submission_key": submission_key,
                    "submitted_at": payload["submitted_at"],
                    "applicant_name": " ".join(
                        part
                        for part in [
                            payload["form_data"].get("first_name", "").strip(),
                            payload["form_data"].get("last_name", "").strip(),
                        ]
                        if part
                    ),
                    "applicant_email": payload["form_data"].get("email", ""),
                    "storage_prefix": remote_prefix,
                    "payload": payload,
                    "files": list(file_map.keys()),
                },
                timeout=60,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - depends on user schema
            warnings.append(f"Supabase file upload worked, but metadata table insert failed: {exc}")

    return {
        "backend": "supabase",
        "location_label": f"{bucket}/{remote_prefix}",
        "submission_key": submission_key,
        "files": list(file_map.keys()),
        "warnings": warnings,
    }


def save_submission_bundle(
    *,
    form_data: dict[str, Any],
    employers: list[dict[str, Any]],
    licenses: list[dict[str, Any]],
    accidents: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    artifacts: dict[str, bytes],
    local_base_dir: Path,
) -> dict[str, Any]:
    payload = _build_payload(form_data, employers, licenses, accidents, violations)
    file_map = _build_file_map(payload, artifacts)
    submission_key = payload["submission_key"]
    backend = _get_backend()
    supabase_ready = _supabase_enabled()

    if backend == "auto":
        if supabase_ready:
            return _save_to_supabase(payload, submission_key, file_map)
        return _save_locally(local_base_dir, submission_key, file_map)

    if backend == "local":
        return _save_locally(local_base_dir, submission_key, file_map)

    if backend == "supabase":
        return _save_to_supabase(payload, submission_key, file_map)

    local_result = _save_locally(local_base_dir, submission_key, file_map)
    supabase_result = _save_to_supabase(payload, submission_key, file_map)
    warnings = []
    warnings.extend(local_result.get("warnings", []))
    warnings.extend(supabase_result.get("warnings", []))
    return {
        "backend": "both",
        "location_label": f"{supabase_result['location_label']} and {local_result['location_label']}",
        "submission_key": submission_key,
        "files": list(file_map.keys()),
        "warnings": warnings,
    }
