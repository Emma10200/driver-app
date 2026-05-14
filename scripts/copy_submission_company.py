"""Generate a company-branded copy of an existing submitted application.

The source can be either a local submission folder / ``submission.json`` file,
or a Supabase storage prefix such as::

    companies/xpress/live/submissions/20260420_151243_driver-emma

The copied packet keeps the applicant data intact, updates the company metadata
to the requested target company, regenerates all PDFs with the target company
header/disclosure language, and saves the result under the target company's
``companies/<slug>/<mode>/submissions`` namespace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import submission_storage
from config import COMPANY_PROFILES, COMPANY_SLUG_ALIASES, CompanyProfile
from pdf_generator import (
    generate_application_pdf,
    generate_california_disclosure_pdf,
    generate_clearinghouse_pdf,
    generate_fcra_pdf,
    generate_psp_pdf,
)


DEFAULT_OUTPUT_ROOT = REPO_ROOT / "submissions"
VALID_COPY_MODES = {"preserve", "live", "test-mode"}
VALID_STORAGE_BACKENDS = {"local", "supabase", "both", "auto"}


def resolve_company_slug(value: str) -> str:
    """Resolve a user-provided company slug/alias to a configured company.

    Unlike the live app URL resolver, this function raises on unknown values so
    a typo in a maintenance script cannot silently generate a Prestige copy.
    """

    raw_slug = str(value or "").strip().lower().replace("_", "-")
    slug = COMPANY_SLUG_ALIASES.get(raw_slug, raw_slug)
    if slug not in COMPANY_PROFILES:
        choices = ", ".join(sorted(COMPANY_PROFILES))
        aliases = ", ".join(sorted(alias for alias, target in COMPANY_SLUG_ALIASES.items() if target in COMPANY_PROFILES))
        raise ValueError(
            f"Unknown company {value!r}. Use one of: {choices}. Accepted aliases include: {aliases}."
        )
    return slug


def _submission_json_path(source_path: str | Path) -> Path:
    path = Path(source_path).expanduser().resolve()
    if path.is_dir():
        path = path / "submission.json"
    if not path.exists():
        raise FileNotFoundError(f"Could not find submission JSON at {path}.")
    if path.name != "submission.json":
        raise ValueError(f"Expected a submission folder or submission.json file, got {path}.")
    return path


def load_local_submission_payload(source_path: str | Path) -> dict[str, Any]:
    json_path = _submission_json_path(source_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Submission JSON at {json_path} did not contain an object.")
    return payload


def load_remote_submission_payload(remote_prefix: str) -> dict[str, Any]:
    payload = submission_storage.read_remote_submission_payload(remote_prefix)
    if payload is None:
        raise FileNotFoundError(
            "Could not read submission.json from Supabase prefix "
            f"{remote_prefix!r}. Check SUPABASE_URL/SUPABASE_SERVICE_KEY and the prefix."
        )
    return payload


def _submission_parts(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    form_data = deepcopy(payload.get("form_data") or {})
    if not isinstance(form_data, dict):
        raise ValueError("Submission payload is missing a valid form_data object.")

    def copied_list(key: str) -> list[dict[str, Any]]:
        value = deepcopy(payload.get(key) or [])
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)]

    return (
        form_data,
        copied_list("employers"),
        copied_list("licenses"),
        copied_list("accidents"),
        copied_list("violations"),
        copied_list("uploaded_documents"),
    )


def infer_copy_mode(payload: dict[str, Any], *, source_hint: str = "", requested_mode: str = "preserve") -> str:
    requested_mode = str(requested_mode or "preserve").strip().lower()
    if requested_mode not in VALID_COPY_MODES:
        raise ValueError(f"Mode must be one of: {', '.join(sorted(VALID_COPY_MODES))}.")
    if requested_mode != "preserve":
        return requested_mode

    form_data = payload.get("form_data") if isinstance(payload.get("form_data"), dict) else {}
    if form_data.get("test_mode"):
        return "test-mode"
    if "test-mode" in str(source_hint or "").replace("\\", "/").lower().split("/"):
        return "test-mode"
    return "live"


def build_copied_form_data(
    payload: dict[str, Any],
    *,
    target_company: CompanyProfile,
    mode: str,
) -> dict[str, Any]:
    form_data, *_ = _submission_parts(payload)
    source_company_slug = str(form_data.get("company_slug") or "").strip()
    source_company_name = str(form_data.get("company_name") or "").strip()

    form_data.update(
        {
            "company_slug": target_company.slug,
            "company_name": target_company.name,
            "test_mode": mode == "test-mode",
            "copied_from_company_slug": source_company_slug,
            "copied_from_company_name": source_company_name,
            "copied_from_submission_key": str(payload.get("submission_key") or ""),
            "company_copy_created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return form_data


def build_company_copy_artifacts(
    *,
    form_data: dict[str, Any],
    employers: list[dict[str, Any]],
    licenses: list[dict[str, Any]],
    accidents: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    target_company: CompanyProfile,
) -> dict[str, bytes | None]:
    return {
        "application_pdf": generate_application_pdf(
            form_data,
            employers,
            licenses,
            accidents,
            violations,
            company=target_company,
        ),
        "fcra_pdf": generate_fcra_pdf(form_data, company=target_company),
        "california_pdf": generate_california_disclosure_pdf(form_data, company=target_company)
        if form_data.get("ca_applicable")
        else None,
        "psp_pdf": generate_psp_pdf(form_data, company=target_company),
        "clearinghouse_pdf": generate_clearinghouse_pdf(form_data, company=target_company),
    }


def _clear_storage_secret_cache() -> None:
    cache_clear = getattr(getattr(submission_storage, "_get_secret", None), "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def _force_storage_backend(backend: str) -> str | None:
    backend = str(backend or "local").strip().lower()
    if backend not in VALID_STORAGE_BACKENDS:
        raise ValueError(f"Storage backend must be one of: {', '.join(sorted(VALID_STORAGE_BACKENDS))}.")
    previous_backend = os.environ.get("SUBMISSION_STORAGE_BACKEND")
    os.environ["SUBMISSION_STORAGE_BACKEND"] = backend
    _clear_storage_secret_cache()
    return previous_backend


def _restore_storage_backend(previous_backend: str | None) -> None:
    if previous_backend is None:
        os.environ.pop("SUBMISSION_STORAGE_BACKEND", None)
    else:
        os.environ["SUBMISSION_STORAGE_BACKEND"] = previous_backend
    _clear_storage_secret_cache()


def copy_submission_payload_to_company(
    payload: dict[str, Any],
    *,
    target_company_slug: str,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    mode: str = "live",
    backend: str = "local",
    dry_run: bool = False,
) -> dict[str, Any]:
    mode = str(mode or "live").strip().lower()
    if mode not in {"live", "test-mode"}:
        raise ValueError("Resolved copy mode must be either 'live' or 'test-mode'.")

    target_slug = resolve_company_slug(target_company_slug)
    target_company = COMPANY_PROFILES[target_slug]
    source_form_data, employers, licenses, accidents, violations, uploaded_documents = _submission_parts(payload)
    source_company_slug = str(source_form_data.get("company_slug") or "").strip()
    storage_namespace = f"companies/{target_slug}/{mode}"

    form_data = build_copied_form_data(payload, target_company=target_company, mode=mode)

    if dry_run:
        return {
            "dry_run": True,
            "source_company_slug": source_company_slug,
            "target_company_slug": target_slug,
            "target_company_name": target_company.name,
            "storage_namespace": storage_namespace,
            "backend": backend,
            "would_write_files": [
                "submission.json",
                "application.pdf",
                "fcra_disclosure.pdf",
                "psp_disclosure.pdf",
                "clearinghouse_release.pdf",
                *(["california_disclosure.pdf"] if form_data.get("ca_applicable") else []),
            ],
        }

    artifacts = build_company_copy_artifacts(
        form_data=form_data,
        employers=employers,
        licenses=licenses,
        accidents=accidents,
        violations=violations,
        target_company=target_company,
    )

    previous_backend = _force_storage_backend(backend)
    try:
        result = submission_storage.save_submission_bundle(
            form_data=form_data,
            employers=employers,
            licenses=licenses,
            accidents=accidents,
            violations=violations,
            uploaded_documents=uploaded_documents,
            artifacts=artifacts,
            local_base_dir=Path(output_root).expanduser().resolve(),
            storage_namespace=storage_namespace,
        )
    finally:
        _restore_storage_backend(previous_backend)
    result.update(
        {
            "source_company_slug": source_company_slug,
            "target_company_slug": target_slug,
            "target_company_name": target_company.name,
            "storage_namespace": storage_namespace,
        }
    )
    return result


def generate_company_copy(
    *,
    target_company_slug: str,
    source_path: str | Path | None = None,
    remote_prefix: str | None = None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    mode: str = "preserve",
    backend: str = "local",
    dry_run: bool = False,
) -> dict[str, Any]:
    if bool(source_path) == bool(remote_prefix):
        raise ValueError("Provide exactly one of source_path or remote_prefix.")

    if remote_prefix:
        payload = load_remote_submission_payload(remote_prefix)
        source_hint = remote_prefix
    else:
        assert source_path is not None
        payload = load_local_submission_payload(source_path)
        source_hint = str(source_path)

    resolved_mode = infer_copy_mode(payload, source_hint=source_hint, requested_mode=mode)
    return copy_submission_payload_to_company(
        payload,
        target_company_slug=target_company_slug,
        output_root=output_root,
        mode=resolved_mode,
        backend=backend,
        dry_run=dry_run,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a company-branded copy of an existing submitted driver application.",
    )
    parser.add_argument(
        "source",
        nargs="?",
        help="Local submission folder or submission.json file. Omit when using --remote-prefix.",
    )
    parser.add_argument(
        "--remote-prefix",
        help="Supabase prefix containing submission.json, for example companies/xpress/live/submissions/<submission-key>.",
    )
    parser.add_argument(
        "--to",
        "--target-company",
        dest="target_company",
        required=True,
        help="Target company slug or alias, such as prestige, xpress, pg, or prestig.",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(VALID_COPY_MODES),
        default="preserve",
        help="Destination mode. preserve keeps test-mode copies in test-mode and otherwise writes live.",
    )
    parser.add_argument(
        "--backend",
        choices=sorted(VALID_STORAGE_BACKENDS),
        default="local",
        help="Where to save the copied packet. Use supabase/both only when secrets are configured.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Local root folder used when backend is local or both.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print what would be generated without writing files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if bool(args.source) == bool(args.remote_prefix):
        parser.error("Provide exactly one local source or --remote-prefix.")

    try:
        result = generate_company_copy(
            source_path=args.source,
            remote_prefix=args.remote_prefix,
            target_company_slug=args.target_company,
            output_root=args.output_root,
            mode=args.mode,
            backend=args.backend,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should return a clean one-line failure
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI use
    raise SystemExit(main())