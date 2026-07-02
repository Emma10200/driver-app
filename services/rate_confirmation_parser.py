"""Heuristic parser for rate-confirmation PDF text.

Free-first extraction strategy:
- Layer 1 (this module): pypdf text-layer + regex heuristics tuned against the
  real broker corpus in rate_conf_samples/pdfs (Arrive, CHR, RXO, TQL, Echo,
  Schneider, DSV, BlueGrace, Priority1, AIT, ShipArdent, PortX, HD Shipping,
  PRIMO, ...).
- Layer 2 (future): Tesseract OCR fallback for scanned/image-only documents.

Targets: broker name, pickup, delivery, total rate (90-95%), itemized rate
(best effort).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Broker registry: strongest signal wins (order matters).
# ---------------------------------------------------------------------------
BROKER_SIGNATURES: list[tuple[str, str]] = [
    (r"arrive\s*order|arrivelogistics\.com|ARRIVEnow", "Arrive Logistics"),
    (r"C\.?H\.?\s*Robinson|chrobinson\.com", "C.H. Robinson"),
    (r"\bRXO\b|rxo\.com", "RXO"),
    (r"TQL RATE CONFIRMATION|\bTQL\b|tql\.com", "TQL"),
    (r"Echo Global Logistics|echo\.com", "Echo Global Logistics"),
    (r"Schneider National|schneider\.com|FreightPower", "Schneider"),
    (r"DSV Road|dsv\.com", "DSV Road"),
    (r"BlueGrace|bluegracegroup\.com", "BlueGrace Logistics"),
    (r"Priority\s*1|priority1\.com", "Priority 1"),
    (r"AIT Truckload|AIT WORLDWIDE|aitworldwide\.com", "AIT Worldwide"),
    (r"Ship\s*ARdent|shipardent\.com", "Ship ARdent"),
    (r"PortX|portxlogistics\.com", "PortX Logistics"),
    (r"HD Shipping|hdships\.com", "HD Shipping Solutions"),
    (r"DBA PRIMO|heyprimo\.com|Logistics Freight Solutions", "PRIMO (Logistics Freight Solutions)"),
    (r"Coyote Logistics|coyote\.com", "Coyote Logistics"),
    (r"J\.?B\.?\s*Hunt|jbhunt\.com", "J.B. Hunt"),
    (r"Landstar", "Landstar"),
    (r"Uber Freight|uberfreight", "Uber Freight"),
    (r"Werner Enterprises|werner\.com", "Werner"),
    (r"Trident Transport|tridenttransport\.com", "Trident Transport"),
    (r"Sunset Transportation|sunsettrans\.com", "Sunset Transportation"),
    (r"England Logistics|englandlogistics\.com", "England Logistics"),
    (r"Traffix|traffix\.com", "Traffix"),
    (r"Redwood Logistics|redwoodlogistics\.com", "Redwood Logistics"),
    (r"Trinity Logistics|trinitylogistics\.com", "Trinity Logistics"),
    (r"Nolan Transportation|ntgfreight\.com|\bNTG\b", "NTG"),
    (r"MoLo Solutions|shipmolo\.com", "MoLo Solutions"),
    (r"Arch(?:er)?\s*Logistics", "Arch Logistics"),
    (r"AFN\b|loadafn\.com", "AFN Logistics"),
    (r"Spot Inc|spotinc\.com", "Spot Freight"),
    (r"Axle Logistics|axlelogistics\.com", "Axle Logistics"),
    (r"Steam Logistics|steamlogistics\.com", "Steam Logistics"),
    (r"Logistic Dynamics|ldi\.?load|logisticdynamics", "Logistic Dynamics"),
    (r"AMX Logistics|amxtrucking\.com", "AMX Logistics"),
    (r"ST\.?\s*FREIGHT", "ST. Freight"),
    (r"Team TILT|tiltgroup", "TILT Group"),
    (r"King of Freight|kingoffreight\.com", "King of Freight"),
    (r"Total Quality Logistics", "TQL"),
    (r"Worldwide Express|wwex\.com", "Worldwide Express"),
    (r"GlobalTranz|globaltranz\.com", "GlobalTranz"),
    (r"Loadsmart|loadsmart\.com", "Loadsmart"),
    (r"Transfix|transfix\.io", "Transfix"),
    (r"Convoy\b", "Convoy"),
    (r"SPI Logistics|spi3pl\.com", "SPI Logistics"),
    (r"Magellan Transport|notify Magellan|call Magellan", "Magellan Transport Logistics"),
    (r"Roadmaster|roadmastergroup\.com", "Roadmaster Group"),
    (r"Big Valley Transportation|bigvalleytransportation\.com", "Big Valley Transportation"),
    (r"DSF LOGISTICS", "DSF Logistics"),
    (r"ELI Solutions|elberta\.net", "ELI Solutions"),
    (r"P\.?A\.?M\.?\s*Transport|pamtransport", "P.A.M. Transport"),
    (r"Trident\b", "Trident Transport"),
    (r"Sunset Transportation", "Sunset Transportation"),
]

# Our own companies must never be reported as the broker.
_OWN_COMPANY_RE = re.compile(r"(?i)prestige?\b|prestig\b|xpress\s*trans")
_GENERIC_BROKER_LINE_RE = re.compile(
    r"(?im)^\s*([A-Z][A-Za-z0-9 .,'&\-]{2,50}?(?:Logistics|Transportation|Transport|Freight|Truckload|Shipping|Solutions|Group)(?:[ ,]*(?:Inc|LLC|Co|Corp)\.?,?)?)\s*$"
)

# ---------------------------------------------------------------------------
# Section markers (pickup vs delivery chunking)
# ---------------------------------------------------------------------------
PICKUP_MARKER_RE = re.compile(
    r"(?im)^\s*(?:"
    r"pick\s*up\s*#?\s*1\b"
    r"|pickup\s*#?\s*1\b"
    r"|shipper\s*#?\s*1\b"
    r"|shipper pickup"
    r"|shipper information"
    r"|stop\s*1\s*[-(]?\s*(?:pick|pickup)"
    r"|pu\s*\d\s+name"
    r"|pick-?up location"
    r"|origin\b"
    r"|route pickup"
    r"|1\s+Pickup\b"
    r"|\d\s+Pick\b"
    r"|scheduled pick\s*up"
    r"|shipper\s*:"
    r"|\bPICK\b"
    r")"
)
DELIVERY_MARKER_RE = re.compile(
    r"(?im)^\s*(?:"
    r"delivery\s*#?\s*\d?\b"
    r"|receiver\s*#?\s*\d\b"
    r"|consignee delivery"
    r"|consignee information"
    r"|consignee\s*\d\b"
    r"|stop\s*\d\s*[-(]?\s*(?:drop|delivery|del\b)"
    r"|so\s*\d\s+name"
    r"|delivery location"
    r"|destination\b"
    r"|drop\s*#?\s*1\b"
    r"|\d\s+Delivery\b"
    r"|\d\s+Drop\b"
    r"|scheduled delivery"
    r"|\bSTOP\b"
    r")"
)
# Two-column layouts (PRIMO, PAM) put the delivery header mid-line next to the
# shipper header. Only strong tokens are safe to match mid-line.
MIDLINE_DELIVERY_RE = re.compile(
    r"(?i)(?:consignee information|consignee\s*:|consignee delivery|receiver\s*#?\s*\d)"
)

CITY_STATE_RE = re.compile(
    r"([A-Z][A-Za-z .'\-]{2,40}?),\s*([A-Z]{2})[ ,]*(\d{5})?(?:-\d{4})?\b"
)
# All-caps "GLENDALE AZ 85307" style (no comma) used by ELI/DSF/AIT/Roadmaster.
CITY_STATE_NOCOMMA_RE = re.compile(
    r"\b([A-Z][A-Z .'\-]{2,30}?)\s+([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b"
)
# Jammed "PLEASANT GROVECA 95668" (missing space before state) from AFN/GlobalTranz.
CITY_STATE_JAMMED_RE = re.compile(
    r"\b([A-Z][A-Z .'\-]{2,28}?)([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b"
)
DATE_RE = re.compile(
    r"((?:Mon|Tues?|Wed(?:nes)?|Thu(?:rs)?|Fri|Sat(?:ur)?|Sun)[a-z]*,?\s+)?"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{2,4})"
)
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(AM|PM|am|pm)?\b")

MONEY_RE = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")
TOTAL_PATTERNS = [
    re.compile(r"(?i)total\s+carrier\s+pay\W*\$?\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?i)total\s+rate\W*\$?\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?i)total\s+cost\s+USD\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?i)total\s+pay:?\s*\$?\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?is)\btotal\b\s*:?\s*[_\s]{0,40}\$\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?i)\btotal\b\s*[:\s]\s*\$\s?([\d,]+(?:\.\d{2})?)\s*(?:USD)?"),
    re.compile(r"(?i)\btotal\b\s*:?\s*([\d,]+\.\d{2})\s*USD"),
    re.compile(r"(?i)\btotal\b\s*\$\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?i)agreed\s+amount\W{0,20}\$\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?i)net\s+freight\s+charges\s+USD\s?([\d,]+(?:\.\d{2})?)"),
    re.compile(r"(?i)NET-{2,}>\s*([\d,]+\.\d{2})"),
    re.compile(r"(?i)line\s*haul\s*:?\s*\$?\s?([\d,]+(?:\.\d{2})?)\s*(?:USD)?"),
    re.compile(r"(?i)flat\s+rate\s+([\d,]+\.\d{2})"),
]
# Itemized rate lines: "<description> ... $<amount>"
RATE_ITEM_RE = re.compile(
    r"(?im)^\s*(?:[A-Z]{2,4}\s*\|\s*)?"
    r"(line\s*haul|linehaul|fuel surcharge|fuel|lumper|detention|layover|drop trailer"
    r"|stop\s*off|tarp|overweight|out of route miles|hazardous material|hazmat"
    r"|tanker endorsement|freight charge|team|extra stop|unloading|loading|accessorial"
    r"|carrier freight pay|flat rate|freight\s*-\s*linehaul)"
    r"[^$\n]{0,60}\$\s?([\d,]+(?:\.\d{2})?)"
)

_EXCLUDED_CITY_WORDS = {
    "po box", "suite", "bldg", "building", "attn", "dept", "inc", "llc",
    "stone park", "melrose park", "fontana",  # carrier HQ cities, not stops
}


def identify_broker(*texts: str) -> str:
    for text in texts:
        if not text:
            continue
        for pattern, name in BROKER_SIGNATURES:
            if re.search(pattern, text, re.IGNORECASE):
                return name
    # Generic fallback: first company-looking line that isn't our own carrier.
    for text in texts:
        if not text:
            continue
        for match in _GENERIC_BROKER_LINE_RE.finditer(text[:2500]):
            candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ,")
            if _OWN_COMPANY_RE.search(candidate):
                continue
            if len(candidate) > 4:
                return candidate
    return ""


def _parse_date(raw: str) -> str:
    raw = raw.strip().rstrip(",")
    for fmt in ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.replace(".", ""), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _city_state_matches(chunk: str) -> list[tuple[int, str]]:
    """All (position, 'City, ST') matches, comma / no-comma / jammed formats."""
    found: list[tuple[int, str]] = []
    seen_positions: set[int] = set()
    for pattern, title_case in ((CITY_STATE_RE, False), (CITY_STATE_NOCOMMA_RE, True), (CITY_STATE_JAMMED_RE, True)):
        for match in pattern.finditer(chunk):
            if match.start() in seen_positions:
                continue
            city = match.group(1).strip(" ,")
            low = city.lower()
            if any(word in low for word in _EXCLUDED_CITY_WORDS) or len(city) < 3:
                continue
            if title_case or city.isupper():
                city = city.title()
            label = f"{city}, {match.group(2)}"
            found.append((match.start(), label))
            seen_positions.add(match.start())
    found.sort(key=lambda item: item[0])
    return found


def _first_city_state(chunk: str, *, prefer_last_on_line: bool = False) -> str:
    matches = _city_state_matches(chunk)
    if not matches:
        return ""
    if not prefer_last_on_line:
        return matches[0][1]
    # Two-column layouts (PRIMO) put "pickup city ... delivery city" on one
    # line; the delivery is the LAST city on the first line that has cities.
    first_pos = matches[0][0]
    line_end = chunk.find("\n", first_pos)
    if line_end == -1:
        line_end = len(chunk)
    on_first_line = [m for m in matches if m[0] < line_end]
    return on_first_line[-1][1]


def _first_date(chunk: str) -> str:
    match = DATE_RE.search(chunk)
    return _parse_date(match.group(2)) if match else ""


def _stop_summary(chunk: str, *, prefer_last_on_line: bool = False) -> tuple[str, str]:
    """Return (summary 'City, ST — YYYY-MM-DD', iso_date) from a section chunk."""
    city = _first_city_state(chunk, prefer_last_on_line=prefer_last_on_line)
    date = _first_date(chunk)
    if city and date:
        return f"{city} — {date}", date
    if date and not city:
        return date, date
    return city or "", date


def extract_total_rate(text: str) -> float | None:
    for pattern in TOTAL_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            try:
                # The last "Total" occurrence is usually the grand total.
                return float(matches[-1].replace(",", ""))
            except ValueError:
                continue
    return None


def extract_rate_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for match in RATE_ITEM_RE.finditer(text):
        desc = re.sub(r"\s+", " ", match.group(1)).strip().title()
        try:
            amount = float(match.group(2).replace(",", ""))
        except ValueError:
            continue
        key = (desc.lower(), amount)
        if key in seen:
            continue
        seen.add(key)
        items.append({"description": desc, "amount": amount})
    return items[:12]


def _pickup_delivery_chunks(text: str) -> tuple[str, str]:
    """Slice the document into a pickup chunk and a delivery chunk."""
    pickup_match = PICKUP_MARKER_RE.search(text)
    search_from = pickup_match.end() if pickup_match else 0
    delivery_match = DELIVERY_MARKER_RE.search(text, search_from)
    midline_match = MIDLINE_DELIVERY_RE.search(text)
    # Prefer the line-anchored delivery marker; fall back to a strong mid-line
    # token (two-column layouts like PRIMO/PAM put both headers on one line).
    delivery_start = None
    if delivery_match:
        delivery_start = delivery_match.start()
    elif midline_match:
        delivery_start = midline_match.start()
    if pickup_match and delivery_start is not None:
        between = text[pickup_match.start(): delivery_start]
        if 0 <= delivery_start - pickup_match.start() < 100 and "\n" not in between:
            # Two-column layout: both headers share one line (PRIMO/PAM).
            # Same window for both; pickup takes the first city per line,
            # delivery takes the last.
            window = text[pickup_match.start(): pickup_match.start() + 1200]
            return window, window
        pickup_end = min(delivery_start, pickup_match.start() + 1200)
        if pickup_end <= pickup_match.start():
            pickup_end = pickup_match.start() + 1200
        return (
            text[pickup_match.start(): pickup_end][:1200],
            text[delivery_start: delivery_start + 1200],
        )
    if pickup_match:
        return text[pickup_match.start(): pickup_match.start() + 1200], ""
    if delivery_start is not None:
        return "", text[delivery_start: delivery_start + 1200]
    return "", ""


# Echo-style inline route: "from LOS ANGELES, CA to DOWNEY, CA"
INLINE_ROUTE_RE = re.compile(
    r"(?i)from\s+([A-Z][A-Za-z .'\-]{2,40},\s*[A-Z]{2})\s+to\s+([A-Z][A-Za-z .'\-]{2,40},\s*[A-Z]{2})"
)


_SHIPPER_CONSIGNEE_LINE_RE = re.compile(r"(?im)^.*shipper\b.*consignee.*$")


def _two_column_city_fallback(text: str) -> tuple[str, str]:
    """'Shipper: X  Consignee: Y' column layouts: first city vs last city."""
    match = _SHIPPER_CONSIGNEE_LINE_RE.search(text)
    if not match:
        return "", ""
    window = text[match.start(): match.start() + 700]
    cities = _city_state_matches(window)
    if not cities:
        return "", ""
    if len(cities) == 1:
        return cities[0][1], ""
    return cities[0][1], cities[-1][1]


def _is_date_only(summary: str) -> bool:
    return bool(summary) and not re.search(r"[A-Za-z]", summary)


def _stop_detail_fallback(text: str) -> tuple[str, str, str, str]:
    """RXO-style 'STOP DETAIL' tables: first city is pickup, last is delivery.

    Returns (pickup_summary, pickup_date, delivery_summary, delivery_date).
    """
    idx = text.upper().find("STOP DETAIL")
    if idx == -1:
        return "", "", "", ""
    region = text[idx: idx + 3000]
    cities = _city_state_matches(region)
    if not cities:
        return "", "", "", ""
    dates = [(m.start(), _parse_date(m.group(2))) for m in DATE_RE.finditer(region)]
    dates = [(pos, d) for pos, d in dates if d]
    pickup_city = cities[0][1]
    delivery_city = cities[-1][1] if len(cities) > 1 else ""
    pickup_date = dates[0][1] if dates else ""
    delivery_date = dates[-1][1] if len(dates) > 1 else ""
    pickup = f"{pickup_city} — {pickup_date}" if pickup_city and pickup_date else pickup_city
    delivery = f"{delivery_city} — {delivery_date}" if delivery_city and delivery_date else delivery_city
    return pickup, pickup_date, delivery, delivery_date


def parse_rate_confirmation(text: str, *, subject: str = "", quoted_body: str = "") -> dict[str, Any]:
    """Parse broker/pickup/delivery/rate fields from rate-confirmation text.

    Returns a dict with keys: broker_name, pickup_summary, delivery_summary,
    pickup_at, delivery_at, rate_amount, rate_items, parse_status.
    """
    text = text or ""
    result: dict[str, Any] = {
        "broker_name": identify_broker(text, subject, quoted_body),
        "pickup_summary": "",
        "delivery_summary": "",
        "pickup_at": None,
        "delivery_at": None,
        "rate_amount": None,
        "rate_items": [],
        "parse_status": "not_started",
    }
    if not text.strip():
        result["parse_status"] = "needs_ocr"
        return result

    result["rate_amount"] = extract_total_rate(text)
    result["rate_items"] = extract_rate_items(text)

    pickup_chunk, delivery_chunk = _pickup_delivery_chunks(text)
    pickup_summary, pickup_date = _stop_summary(pickup_chunk)
    delivery_summary, delivery_date = _stop_summary(delivery_chunk, prefer_last_on_line=True)

    if not pickup_summary or not delivery_summary:
        fb_pickup, fb_pickup_date, fb_delivery, fb_delivery_date = _stop_detail_fallback(text)
        if not pickup_summary and fb_pickup:
            pickup_summary, pickup_date = fb_pickup, fb_pickup_date
        if not delivery_summary and fb_delivery:
            delivery_summary, delivery_date = fb_delivery, fb_delivery_date

    # Schedule lines sometimes give only dates; cities live in a separate
    # shipper/consignee column block (PAM/AIT style).
    if _is_date_only(pickup_summary) or _is_date_only(delivery_summary) or not pickup_summary or not delivery_summary:
        col_pickup, col_delivery = _two_column_city_fallback(text)
        if col_pickup and (not pickup_summary or _is_date_only(pickup_summary)):
            pickup_summary = f"{col_pickup} — {pickup_date}" if pickup_date else col_pickup
        if col_delivery and (not delivery_summary or _is_date_only(delivery_summary)):
            delivery_summary = f"{col_delivery} — {delivery_date}" if delivery_date else col_delivery

    if not pickup_summary and not delivery_summary:
        inline = INLINE_ROUTE_RE.search(text)
        if inline:
            pickup_summary = inline.group(1).strip()
            delivery_summary = inline.group(2).strip()

    result["pickup_summary"] = pickup_summary
    result["delivery_summary"] = delivery_summary
    result["pickup_at"] = f"{pickup_date}T00:00:00+00:00" if pickup_date else None
    result["delivery_at"] = f"{delivery_date}T00:00:00+00:00" if delivery_date else None

    core_found = sum(
        1 for key in ("broker_name", "pickup_summary", "delivery_summary")
        if result[key]
    ) + (1 if result["rate_amount"] else 0)
    if core_found >= 3:
        result["parse_status"] = "parsed"
    elif core_found >= 1:
        result["parse_status"] = "needs_review"
    else:
        result["parse_status"] = "text_extracted"
    return result
