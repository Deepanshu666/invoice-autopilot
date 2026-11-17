"""Business rules in one place, so AP can see (and change) exactly what the process enforces."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SAMPLES_DIR = ROOT / "samples"
DB_PATH = DATA_DIR / "runs.db"

BUYER_NAME = "Northwind Retail Inc."
BASE_CURRENCY = "USD"

# Price tolerance: an invoice may exceed the PO's remaining balance by up to
# 2% AND no more than $500 and still be auto-approved (covers freight, rounding).
TOLERANCE_PCT = 0.02
TOLERANCE_ABS = 500.00

# Line-level unit price variance allowed before a line is flagged.
UNIT_PRICE_TOLERANCE_PCT = 0.02

# Arithmetic checks (sum of lines vs subtotal, subtotal + tax vs total).
ROUNDING_TOLERANCE = 1.00

# Duplicate detection: same vendor + same total within this many days.
DUPLICATE_WINDOW_DAYS = 30

# Dates
MAX_INVOICE_AGE_DAYS = 180
MAX_FUTURE_DAYS = 3

# Matching thresholds (0-100, rapidfuzz scores)
VENDOR_NAME_MATCH_MIN = 85
LINE_DESC_MATCH_MIN = 70

# OCR quality below this mean word confidence means a human should check the read.
OCR_CONFIDENCE_MIN = 70

# Who each kind of exception is routed to.
QUEUES = {
    "fraud": "AP Manager - fraud check",
    "po": "Procurement - PO owner",
    "ap": "AP Specialist",
    "vendor": "Vendor Management",
}
