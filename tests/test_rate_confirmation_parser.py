from __future__ import annotations

from services.rate_confirmation_parser import (
    extract_rate_items,
    extract_total_rate,
    identify_broker,
    parse_rate_confirmation,
)

ARRIVE_TEXT = """
7701 Metropolis Dr | Bldg 15 Austin, TX 78744
Have your driver call in for dispatch at (512) 236-5545 and reference the Arrive order 9160858
Rate Details
LineHaul $4,500.00
Total $4,500.00
Pickup #1
Pickup Address Appointment Ref/PO# Commodity Weight
International Distribution Corp
13103 BAYPARK RD
Pasadena, TX 77507
Jul 2, 2026
13:00 CDT
Delivery #1
Delivery Address
SOME WAREHOUSE
Ontario, CA 91761
Jul 4, 2026
09:00 PDT
"""

TQL_TEXT = """
TQL RATE CONFIRMATION FOR PO# 37299080
Total: $1,000.00 USD
Pick-up Location
Date
Time
Kingsburg, CA
7/1/2026
Appt 14:00
Delivery Location
Date
Time
San Diego, CA
7/2/2026
FCFS 08:00 to 14:00
"""

PAM_TEXT = """
06/30/26 Load Confirmation Agreement
13:42:28 P.A.M.Transport Supply Chain Solutions-Willard Ofc
Scheduled pick up:  6/30/26 0800 -to-  6/30/26 1600
Scheduled delivery:  7/01/26 0900 -to-  7/01/26 0900
Shipper: RAYVEN INC. Consignee: NORKA
Address: 431 GRIGGS ST N 3001 E NEWBERRY ST
SAINT PAUL, MN 55104-4108 APPLETON, WI 54915-3007
MILES    260.00 FLAT RATE    850.00
NET------>    850.00
"""

RXO_TEXT = """
Load Confirmation
23328073
Carrier Pay Breakdown
LNH | Line Haul | Flat $6000.00
LM  | Lumper | Flat $285.00
Total Carrier Pay $6285.00
STOP DETAIL
Type Scheduled Arrival Scheduled Departure
Pick 07/01/26 W M BARR
Memphis, TN 38113
Drop 07/03/26 WAREHOUSE
San Bernardino, CA 92408
"""


def test_identify_known_brokers() -> None:
    assert identify_broker(ARRIVE_TEXT) == "Arrive Logistics"
    assert identify_broker(TQL_TEXT) == "TQL"
    assert identify_broker(PAM_TEXT) == "P.A.M. Transport"
    assert identify_broker(RXO_TEXT, "Fwd: RXO load") == "RXO"


def test_generic_broker_fallback_skips_own_companies() -> None:
    text = "Rate Confirmation\nPrestige Transportation Inc\nAcme Freight Solutions LLC\nTotal $500.00"
    assert identify_broker(text) == "Acme Freight Solutions LLC"


def test_arrive_full_parse() -> None:
    parsed = parse_rate_confirmation(ARRIVE_TEXT)
    assert parsed["broker_name"] == "Arrive Logistics"
    assert parsed["rate_amount"] == 4500.0
    assert "Pasadena, TX" in parsed["pickup_summary"]
    assert "Ontario, CA" in parsed["delivery_summary"]
    assert parsed["pickup_at"].startswith("2026-07-02")
    assert parsed["delivery_at"].startswith("2026-07-04")
    assert parsed["parse_status"] == "parsed"


def test_tql_parse_pickup_delivery_rate() -> None:
    parsed = parse_rate_confirmation(TQL_TEXT)
    assert parsed["rate_amount"] == 1000.0
    assert "Kingsburg, CA" in parsed["pickup_summary"]
    assert "San Diego, CA" in parsed["delivery_summary"]


def test_pam_two_column_and_net_rate() -> None:
    parsed = parse_rate_confirmation(PAM_TEXT)
    assert parsed["rate_amount"] == 850.0
    assert "Saint Paul, MN" in parsed["pickup_summary"]
    assert "Appleton, WI" in parsed["delivery_summary"]


def test_rxo_stop_detail_fallback_and_itemized() -> None:
    parsed = parse_rate_confirmation(RXO_TEXT)
    assert parsed["rate_amount"] == 6285.0
    assert "Memphis, TN" in parsed["pickup_summary"]
    assert "San Bernardino, CA" in parsed["delivery_summary"]
    items = {item["description"]: item["amount"] for item in parsed["rate_items"]}
    assert items.get("Line Haul") == 6000.0
    assert items.get("Lumper") == 285.0


def test_image_only_pdf_needs_ocr() -> None:
    parsed = parse_rate_confirmation("")
    assert parsed["parse_status"] == "needs_ocr"


def test_total_prefers_grand_total_over_linehaul() -> None:
    text = "LineHaul $3,866.34\nFuel Surcharge $933.66\nTotal $4,800.00"
    assert extract_total_rate(text) == 4800.0


def test_itemized_rate_lines() -> None:
    text = "Line Haul 3500.00 Flat Rate 1 $3,500.00 USD\nOverweight 150.00 Flat Rate 1 $150.00 USD\nOut of Route Miles 125.00 Flat Rate 1 $125.00 USD"
    items = extract_rate_items(text)
    descriptions = {item["description"] for item in items}
    assert "Line Haul" in descriptions
    assert "Overweight" in descriptions
