"""Company configuration and shared constants for the driver application."""

from __future__ import annotations

from dataclasses import dataclass

COMPANY_NAME = "PRESTIGE TRANSPORTATION INC."
COMPANY_ADDRESS = "8622 Hemlock Ave."
COMPANY_CITY_STATE_ZIP = "Fontana, CA 92335"
COMPANY_PHONE = "(877) 549-9529"
COMPANY_FAX = "(888) 283-6538"
COMPANY_EMAIL = "safety@prestigecalifornia.com"


@dataclass(frozen=True)
class CompanyProfile:
    slug: str
    name: str
    address: str = ""
    city_state_zip: str = ""
    phone: str = ""
    fax: str = ""
    email: str = ""
    brand_color: str = "#3E6FA3"


DEFAULT_COMPANY_SLUG = "prestige"
COMPANY_PROFILES: dict[str, CompanyProfile] = {
    "prestige": CompanyProfile(
        slug="prestige",
        name=COMPANY_NAME,
        address=COMPANY_ADDRESS,
        city_state_zip=COMPANY_CITY_STATE_ZIP,
        phone=COMPANY_PHONE,
        fax=COMPANY_FAX,
        email=COMPANY_EMAIL,
        brand_color="#3E6FA3",
    ),
    "side-xpress": CompanyProfile(
        slug="side-xpress",
        name="Xpress Trans, Inc",
        address="2905 W. Lake St.",
        city_state_zip="Melrose Park, IL 60160",
        phone="708-356-4420",
        fax="630-303-9721",
        email="safety@xpresstransinc.com",
        brand_color="#3F8356",
    ),
}

# Application phases
PHASE_LABELS = {
    1: "Personal Information",
    2: "Company Questions & Driving Experience",
    3: "Licenses & Endorsements",
    4: "Employment History (10 Years)",
    5: "Education & Trucking School",
    6: "FMCSR Disqualifications & Records",
    7: "Certifications & Signature",
    8: "FCRA Disclosure (Standalone)",
    9: "California Disclosure",
    10: "PSP Disclosure (Standalone)",
    11: "Clearinghouse Release (Standalone)",
    12: "Review & Submit",
}

# Equipment types for driving experience
EQUIPMENT_TYPES = [
    "Straight Truck",
    "Tractor and Semi-Trailer",
    "Tractor - Two Trailers",
    "Tanker",
    "Hazmat",
    "Other",
]

# Trailer types
TRAILER_TYPES = [
    "Van",
    "Flatbed",
    "Reefer",
    "Tanker",
    "Container",
    "Other",
]

# Trailer lengths
TRAILER_LENGTHS = [
    "28 feet or less",
    "29 to 40 feet",
    "41 to 52 feet",
    "53 feet or more",
]

# License classes
LICENSE_CLASSES = ["Class A", "Class B", "Class C"]

# US States
US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

# Position types (independent contractor language)
POSITION_TYPES = [
    "Owner Operator",
]

LICENSE_COUNTRIES = ["US", "Canada", "Mexico"]

AREAS_DRIVEN_OPTIONS = [
    "OTR",
    "Regional",
    "Local",
    "Dedicated",
    "Other",
]

TRUCK_TYPES = [
    "Straight Truck",
    "Day Cab",
    "Sleeper",
    "Tractor-Trailer",
    "Box Truck",
    "Container Chassis",
    "Flatbed Tractor",
    "Other",
]

DRIVING_EQUIPMENT_OPTIONS = [
    "Dry Van",
    "Reefer",
    "Tanker",
    "Flatbed",
    "Container",
    "Chassis",
    "Box",
    "Straight Trailer",
    "Double Trailer",
    "Other",
]

OFFICE_LOCATIONS = [
    "California Office",
    "Chicago Office",
]

MOBILE_CARRIERS = [
    "AT&T",
    "Verizon",
    "T-Mobile",
    "Cricket Wireless",
    "Boost Mobile",
    "Metro by T-Mobile",
    "UScellular",
    "Xfinity Mobile",
    "Spectrum Mobile",
    "Google Fi",
    "Mint Mobile",
    "Consumer Cellular",
    "Other",
]

# How did you hear about us options
REFERRAL_SOURCES = [
    "Internet Search",
    "Craigslist",
    "Indeed",
    "Driver Referral",
    "Social Media",
    "Job Board",
    "Other",
]
