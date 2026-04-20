"""
Company configuration and constants for Prestige Transportation Inc.
"""

COMPANY_NAME = "PRESTIGE TRANSPORTATION INC."
COMPANY_ADDRESS = "8622 Hemlock Ave."
COMPANY_CITY_STATE_ZIP = "Fontana, CA 92335"
COMPANY_PHONE = "(877) 549-9529"
COMPANY_FAX = "(888) 283-6538"
COMPANY_EMAIL = "safety@prestigecalifornia.com"

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
    "Independent Contractor Driver",
    "Owner Operator",
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
