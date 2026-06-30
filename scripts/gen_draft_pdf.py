"""One-shot script: generate PDFs from a draft JSON file."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import COMPANY_PROFILES
from pdf_generator import (
    generate_application_pdf,
    generate_california_disclosure_pdf,
    generate_clearinghouse_pdf,
    generate_fcra_pdf,
    generate_psp_pdf,
)


def main():
    draft_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    company_slug = sys.argv[2] if len(sys.argv) > 2 else "pg"

    if not draft_path or not draft_path.exists():
        print(f"Usage: python {Path(__file__).name} <draft.json> [company_slug]")
        sys.exit(1)

    company = COMPANY_PROFILES.get(company_slug)
    if not company:
        print(f"Unknown company slug: {company_slug}. Available: {', '.join(COMPANY_PROFILES)}")
        sys.exit(1)

    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    form_data = payload.get("form_data", {})
    employers = payload.get("employers", [])
    licenses = payload.get("licenses", [])
    accidents = payload.get("accidents", [])
    violations = payload.get("violations", [])

    first = form_data.get("first_name", "unknown")
    last = form_data.get("last_name", "unknown")
    safe_name = f"{first}_{last}".replace(" ", "_")

    out_dir = Path(draft_path).parent / f"{safe_name}_{company_slug}_pdfs"
    out_dir.mkdir(exist_ok=True)

    # Application PDF
    app_bytes = generate_application_pdf(form_data, employers, licenses, accidents, violations, company=company)
    (out_dir / "application.pdf").write_bytes(app_bytes)
    print(f"  -> {out_dir / 'application.pdf'}")

    # FCRA PDF
    fcra_bytes = generate_fcra_pdf(form_data, company=company)
    (out_dir / "fcra_disclosure.pdf").write_bytes(fcra_bytes)
    print(f"  -> {out_dir / 'fcra_disclosure.pdf'}")

    # California Disclosure PDF (if applicable)
    if form_data.get("ca_applicable"):
        ca_bytes = generate_california_disclosure_pdf(form_data, company=company)
        (out_dir / "california_disclosure.pdf").write_bytes(ca_bytes)
        print(f"  -> {out_dir / 'california_disclosure.pdf'}")

    # PSP PDF
    psp_bytes = generate_psp_pdf(form_data, company=company)
    (out_dir / "psp_disclosure.pdf").write_bytes(psp_bytes)
    print(f"  -> {out_dir / 'psp_disclosure.pdf'}")

    # Clearinghouse PDF
    ch_bytes = generate_clearinghouse_pdf(form_data, company=company)
    (out_dir / "clearinghouse.pdf").write_bytes(ch_bytes)
    print(f"  -> {out_dir / 'clearinghouse.pdf'}")

    print(f"\nAll PDFs saved to: {out_dir}")


if __name__ == "__main__":
    main()
