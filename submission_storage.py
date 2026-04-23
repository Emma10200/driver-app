"""Helpers for saving draft and submitted applications to local storage or Supabase."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Mapping
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional on hosted environments

    def load_dotenv(*_args, **_kwargs):
        return False


try:
    import streamlit as st
    from streamlit.errors import StreamlitSecretNotFoundError
except ImportError:  # pragma: no cover - defensive import for non-Streamlit tooling
    st = None

    class StreamlitSecretNotFoundError(Exception):
        """Fallback used when Streamlit is unavailable."""


load_dotenv()

VALID_BACKENDS = {"auto", "local", "supabase", "both"}
DEFAULT_BUCKET = "driver-applications"
PDF_MIME = "application/pdf"
JSON_MIME = "application/json"
DRAFTS_DIRNAME = "drafts"
UPLOADS_DIRNAME = "uploads"


def _join_relative_prefix(*parts: str | None) -> str:
    return "/".join(
        str(part).strip("/") for part in parts if str(part or "").strip("/")
    )


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

        for section in secrets.values():
            if not isinstance(section, Mapping):
                continue

            for candidate in _candidate_names(name):
                if candidate in section:
                    return str(section[candidate])
    except StreamlitSecretNotFoundError:
        return None

    return None


@lru_cache(maxsize=128)
def _get_secret(name: str, default: str | None = None) -> str | None:
    streamlit_value = _get_streamlit_secret(name)
    if streamlit_value:
        return streamlit_value

    for candidate in _candidate_names(name):
        env_value = os.getenv(candidate)
        if env_value:
            return env_value

    return default


def get_runtime_secret(name: str, default: str | None = None) -> str | None:
    return _get_secret(name, default)


def _get_backend() -> str:
    backend = (
        (_get_secret("SUBMISSION_STORAGE_BACKEND", "auto") or "auto").strip().lower()
    )
    return backend if backend in VALID_BACKENDS else "auto"


def _get_supabase_settings() -> dict[str, str]:
    return {
        "url": (_get_secret("SUPABASE_URL", "") or "").strip(),
        "key": (
            _get_secret("SUPABASE_SERVICE_KEY") or _get_secret("SUPABASE_KEY", "") or ""
        ).strip(),
        "bucket": (
            _get_secret("SUPABASE_BUCKET", DEFAULT_BUCKET) or DEFAULT_BUCKET
        ).strip(),
        "table": (_get_secret("SUPABASE_TABLE", "") or "").strip(),
    }


def _supabase_enabled() -> bool:
    settings = _get_supabase_settings()
    return bool(settings["url"] and settings["key"])


def _build_supabase_headers() -> dict[str, str]:
    settings = _get_supabase_settings()
    if not settings["url"] or not settings["key"]:
        raise RuntimeError(
            "Supabase storage is selected but SUPABASE_URL / SUPABASE_SERVICE_KEY is missing."
        )

    return {
        "apikey": settings["key"],
        "Authorization": f"Bearer {settings['key']}",
    }


def _save_file_map_locally(
    local_base_dir: Path,
    relative_prefix: str,
    file_map: dict[str, tuple[bytes, str]],
) -> dict[str, Any]:
    target_dir = local_base_dir / relative_prefix
    target_dir.mkdir(parents=True, exist_ok=True)

    for file_name, (content, _) in file_map.items():
        (target_dir / file_name).write_bytes(content)

    return {
        "backend": "local",
        "location_label": str(target_dir),
        "files": list(file_map.keys()),
    }


def _save_file_map_to_supabase(
    relative_prefix: str,
    file_map: dict[str, tuple[bytes, str]],
) -> dict[str, Any]:
    settings = _get_supabase_settings()
    base_url = settings["url"].rstrip("/")
    bucket = settings["bucket"] or DEFAULT_BUCKET
    headers = _build_supabase_headers()

    for file_name, (content, content_type) in file_map.items():
        remote_path = f"{relative_prefix}/{file_name}".strip("/")
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

    return {
        "backend": "supabase",
        "location_label": f"{bucket}/{relative_prefix}".rstrip("/"),
        "files": list(file_map.keys()),
    }


def _save_file_map(
    *,
    local_base_dir: Path,
    local_relative_prefix: str,
    supabase_relative_prefix: str,
    file_map: dict[str, tuple[bytes, str]],
) -> dict[str, Any]:
    backend = _get_backend()
    supabase_ready = _supabase_enabled()

    if backend == "auto":
        if supabase_ready:
            return _save_file_map_to_supabase(supabase_relative_prefix, file_map)
        return _save_file_map_locally(local_base_dir, local_relative_prefix, file_map)

    if backend == "local":
        return _save_file_map_locally(local_base_dir, local_relative_prefix, file_map)

    if backend == "supabase":
        return _save_file_map_to_supabase(supabase_relative_prefix, file_map)

    local_result = _save_file_map_locally(
        local_base_dir, local_relative_prefix, file_map
    )
    supabase_result = _save_file_map_to_supabase(supabase_relative_prefix, file_map)
    warnings = []
    warnings.extend(local_result.get("warnings", []))
    warnings.extend(supabase_result.get("warnings", []))
    return {
        "backend": "both",
        "location_label": f"{supabase_result['location_label']} and {local_result['location_label']}",
        "files": list(file_map.keys()),
        "warnings": warnings,
    }


def _read_local_bytes(local_base_dir: Path, relative_path: str) -> bytes:
    target_file = local_base_dir / relative_path
    if not target_file.exists():
        raise FileNotFoundError(f"No local file found at `{target_file}`.")
    return target_file.read_bytes()


def _read_supabase_bytes(relative_path: str) -> bytes:
    settings = _get_supabase_settings()
    headers = _build_supabase_headers()
    bucket = settings["bucket"] or DEFAULT_BUCKET
    download_url = f"{settings['url'].rstrip('/')}/storage/v1/object/{bucket}/{quote(relative_path)}"
    response = requests.get(download_url, headers=headers, timeout=60)
    if response.status_code == 404:
        raise FileNotFoundError(
            f"No Supabase file found at `{bucket}/{relative_path}`."
        )
    response.raise_for_status()
    return response.content


def _draft_relative_prefix(draft_id: str, storage_namespace: str = "") -> str:
    return _join_relative_prefix(storage_namespace, DRAFTS_DIRNAME, draft_id)


def get_submission_destination_summary(
    local_base_dir: Path, storage_namespace: str = ""
) -> str:
    backend = _get_backend()
    settings = _get_supabase_settings()
    local_target = (
        local_base_dir / storage_namespace if storage_namespace else local_base_dir
    )
    bucket_target = _join_relative_prefix(settings["bucket"], storage_namespace)

    if backend == "local":
        return f"the local folder `{local_target}`"
    if backend == "supabase":
        return f"Supabase bucket `{bucket_target or settings['bucket']}`"
    if backend == "both":
        return f"both Supabase bucket `{bucket_target or settings['bucket']}` and the local folder `{local_target}`"
    if _supabase_enabled():
        return f"Supabase bucket `{bucket_target or settings['bucket']}`"
    return f"the local folder `{local_target}`"


def _build_submission_key(form_data: dict[str, Any]) -> tuple[str, str]:
    submission_timestamp = form_data.get(
        "final_submission_timestamp", datetime.now().isoformat()
    )
    timestamp = datetime.fromisoformat(submission_timestamp).strftime("%Y%m%d_%H%M%S")
    applicant_name = (
        f"{form_data.get('last_name', 'driver')}_{form_data.get('first_name', '')}"
    )
    submission_key = f"{timestamp}_{_slugify(applicant_name)}"
    return submission_timestamp, submission_key


def _build_payload(
    form_data: dict[str, Any],
    employers: list[dict[str, Any]],
    licenses: list[dict[str, Any]],
    accidents: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    uploaded_documents: list[dict[str, Any]] | None = None,
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
        "uploaded_documents": uploaded_documents or [],
    }


def _build_file_map(
    payload: dict[str, Any], artifacts: dict[str, bytes]
) -> dict[str, tuple[bytes, str]]:
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


def _save_locally(
    local_base_dir: Path,
    submission_key: str,
    file_map: dict[str, tuple[bytes, str]],
    storage_namespace: str = "",
) -> dict[str, Any]:
    submission_dir = (
        local_base_dir / storage_namespace / submission_key
        if storage_namespace
        else local_base_dir / submission_key
    )
    submission_dir.mkdir(parents=True, exist_ok=True)

    for file_name, (content, _) in file_map.items():
        (submission_dir / file_name).write_bytes(content)

    return {
        "backend": "local",
        "location_label": str(submission_dir),
        "submission_key": submission_key,
        "files": list(file_map.keys()),
    }


def _save_to_supabase(
    payload: dict[str, Any],
    submission_key: str,
    file_map: dict[str, tuple[bytes, str]],
    storage_namespace: str = "",
) -> dict[str, Any]:
    settings = _get_supabase_settings()
    base_url = settings["url"].rstrip("/")
    bucket = settings["bucket"] or DEFAULT_BUCKET
    remote_prefix = _join_relative_prefix(
        storage_namespace, "submissions", submission_key
    )
    headers = _build_supabase_headers()

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
        except (
            requests.RequestException
        ) as exc:  # pragma: no cover - depends on user schema
            warnings.append(
                f"Supabase file upload worked, but metadata table insert failed: {exc}"
            )

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
    uploaded_documents: list[dict[str, Any]] | None = None,
    storage_namespace: str = "",
) -> dict[str, Any]:
    payload = _build_payload(
        form_data, employers, licenses, accidents, violations, uploaded_documents
    )
    file_map = _build_file_map(payload, artifacts)
    submission_key = payload["submission_key"]
    backend = _get_backend()
    supabase_ready = _supabase_enabled()

    if backend == "auto":
        if supabase_ready:
            return _save_to_supabase(
                payload, submission_key, file_map, storage_namespace
            )
        return _save_locally(
            local_base_dir, submission_key, file_map, storage_namespace
        )

    if backend == "local":
        return _save_locally(
            local_base_dir, submission_key, file_map, storage_namespace
        )

    if backend == "supabase":
        return _save_to_supabase(payload, submission_key, file_map, storage_namespace)

    local_result = _save_locally(
        local_base_dir, submission_key, file_map, storage_namespace
    )
    supabase_result = _save_to_supabase(
        payload, submission_key, file_map, storage_namespace
    )
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


def save_draft_bundle(
    *,
    draft_id: str,
    draft_payload: dict[str, Any],
    local_base_dir: Path,
    storage_namespace: str = "",
) -> dict[str, Any]:
    relative_prefix = _draft_relative_prefix(draft_id, storage_namespace)
    file_map = {
        "draft.json": (
            json.dumps(draft_payload, indent=2, default=_json_default).encode("utf-8"),
            JSON_MIME,
        )
    }
    result = _save_file_map(
        local_base_dir=local_base_dir,
        local_relative_prefix=relative_prefix,
        supabase_relative_prefix=relative_prefix,
        file_map=file_map,
    )
    result["draft_id"] = draft_id
    return result


def load_draft_bundle(
    *, draft_id: str, local_base_dir: Path, storage_namespace: str = ""
) -> dict[str, Any]:
    draft_id = draft_id.strip()
    if not draft_id:
        raise ValueError("Draft ID is required.")

    relative_path = f"{_draft_relative_prefix(draft_id, storage_namespace)}/draft.json"
    backend = _get_backend()
    read_order: list[str]

    if backend == "local":
        read_order = ["local"]
    elif backend == "supabase":
        read_order = ["supabase", "local"]
    elif backend == "both":
        read_order = ["supabase", "local"]
    else:
        read_order = (
            ["supabase", "local"] if _supabase_enabled() else ["local", "supabase"]
        )

    last_error: Exception | None = None
    for target in read_order:
        try:
            if target == "local":
                raw_bytes = _read_local_bytes(local_base_dir, relative_path)
            else:
                raw_bytes = _read_supabase_bytes(relative_path)
            return json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error

    raise FileNotFoundError(f"No draft found for `{draft_id}`.")


def save_supporting_documents(
    *,
    draft_id: str,
    documents: list[dict[str, Any]],
    local_base_dir: Path,
    storage_namespace: str = "",
) -> dict[str, Any]:
    if not documents:
        return {
            "backend": _get_backend(),
            "location_label": "",
            "draft_id": draft_id,
            "documents": [],
            "warnings": [],
        }

    relative_prefix = _join_relative_prefix(
        _draft_relative_prefix(draft_id, storage_namespace), UPLOADS_DIRNAME
    )
    file_map: dict[str, tuple[bytes, str]] = {}
    metadata: list[dict[str, Any]] = []

    for index, document in enumerate(documents, start=1):
        file_name = (
            str(document.get("file_name") or f"document-{index}").strip()
            or f"document-{index}"
        )
        content = document.get("content", b"")
        content_bytes = content if isinstance(content, bytes) else bytes(content)
        content_digest = str(document.get("content_digest") or "")
        suffix = Path(file_name).suffix
        base_name = Path(file_name).stem or f"document-{index}"
        stored_name = (
            f"{_slugify(base_name)}-{content_digest[:12] or index}{suffix.lower()}"
        )
        file_map[stored_name] = (
            content_bytes,
            str(document.get("content_type") or "application/octet-stream"),
        )
        metadata.append(
            {
                "file_name": file_name,
                "stored_name": stored_name,
                "content_type": str(
                    document.get("content_type") or "application/octet-stream"
                ),
                "size_bytes": int(document.get("size_bytes") or len(content_bytes)),
                "content_digest": content_digest,
                "storage_path": f"{relative_prefix}/{stored_name}",
            }
        )

    result = _save_file_map(
        local_base_dir=local_base_dir,
        local_relative_prefix=relative_prefix,
        supabase_relative_prefix=relative_prefix,
        file_map=file_map,
    )
    result["draft_id"] = draft_id
    result["documents"] = metadata
    return result
