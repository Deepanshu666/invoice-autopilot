"""End-to-end tests: every sample invoice, in demo order, gets the expected decision.

Run:  python -m pytest -q
"""
from datetime import date
from pathlib import Path

import pytest

from engine.pipeline import process
from engine.store import Store

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
TODAY = date(2026, 11, 17)

# (file prefix, expected decision, expected route, text that must appear in the headline)
DEMO_ORDER = [
    ("01", "APPROVED", "Payment run", "PO-4501"),
    ("05", "REJECTED", "Returned to vendor", "Duplicate of INV-00871"),
    ("02", "APPROVED", "Payment run", "PO-4502"),
    ("03", "NEEDS_REVIEW", "Procurement - PO owner", "overbill PO-4502 by $3,500.00"),
    ("04", "NEEDS_REVIEW", "AP Specialist", "PO-4504"),
    ("06", "NEEDS_REVIEW", "AP Manager - fraud check", "possible payment-redirection fraud"),
    ("07", "APPROVED", "Payment run", "PO-4506"),
    ("08", "REJECTED", "Returned to vendor", "blocked"),
    ("09", "REJECTED", "Returned to vendor", "Missing invoice number, invoice date"),
]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    s = Store(db_path=tmp_path_factory.mktemp("db") / "runs.db")
    s.reset()
    return s


def _file(prefix):
    return next(SAMPLES.glob(f"{prefix}_*.pdf"))


@pytest.mark.parametrize("prefix,decision,route,needle", DEMO_ORDER)
def test_demo_sequence(store, prefix, decision, route, needle):
    f = _file(prefix)
    r = process(store, f.read_bytes(), f.name, today=TODAY)
    assert r["decision"] == decision, r["headline"]
    assert r["route_to"] == route
    assert needle in r["headline"]


def test_split_billing_ledger(store):
    po = store.po("PO-4502")
    assert po["billed"] == 24000.0 and po["remaining"] == 16000.0  # second invoice is pending, not billed


def test_exact_resend_is_caught(store):
    f = _file("01")
    r = process(store, f.read_bytes(), f.name, today=TODAY, save=False)
    assert r["decision"] == "REJECTED" and "already processed" in r["headline"]


def test_reviewer_confirms_inferred_po(store):
    runs = store.runs()
    delta = runs[runs.filename.str.startswith("04_")].iloc[0]
    store.review(int(delta.id), True, "PO confirmed with Facilities", po_number="PO-4504")
    assert store.po("PO-4504")["remaining"] == 0


def test_ambiguous_date_resolved():
    from engine.extract import parse_date
    iso, note = parse_date("06/11/2026", today=TODAY)
    assert iso == "2026-11-06" and "ambiguous" in note


def test_invoice_number_normalisation():
    from engine.extract import normalize_invoice_number as n
    assert n("INV-00871") == n("inv 871") == n("INV/871") == "INV871"


def _render(**overrides):
    import sys
    sys.path.insert(0, str(SAMPLES.parent / "scripts"))
    import make_samples as m
    return m.render(dict(m.ACME, layout="modern", inv_no="X-1", date="Nov 10, 2026", due="Dec 10, 2026",
                         po="PO-4501", lines=m.ACME_LINES, tax_rate=0.085) | overrides)


def test_unknown_vendor_rejected(tmp_path):
    s = Store(db_path=tmp_path / "r.db")
    pdf = _render(vendor="Zenith Paper Traders", tax_id="11-2223334", acct="1234567890")
    r = process(s, pdf, "u.pdf", today=TODAY, save=False)
    assert r["decision"] == "REJECTED" and "approved vendor list" in r["headline"]


def test_unit_price_above_po_flagged(tmp_path):
    s = Store(db_path=tmp_path / "r.db")
    pdf = _render(lines=[("HP 58A toner cartridge", 12, 95.00)])
    r = process(s, pdf, "p.pdf", today=TODAY, save=False)
    assert r["decision"] == "NEEDS_REVIEW" and "Price above PO" in r["headline"]


def test_corrupt_file_rejected(tmp_path):
    r = process(Store(db_path=tmp_path / "r.db"), b"not a pdf", "broken.pdf", today=TODAY, save=False)
    assert r["decision"] == "REJECTED"
