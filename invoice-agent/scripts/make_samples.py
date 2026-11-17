"""Generate realistic test invoices for the demo.

Three different vendor layouts (classic, modern, letter-style), plus one
scanned image-only invoice. Run:  python scripts/make_samples.py
"""
from __future__ import annotations

import io
import random
from pathlib import Path

from PIL import Image, ImageFilter
from pdf2image import convert_from_bytes
from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
W, H = LETTER
BUYER = ["Northwind Retail Inc.", "Attn: Accounts Payable", "500 Market Street, Suite 1200", "San Francisco, CA 94105"]


def money(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------- layout A
def layout_classic(c: canvas.Canvas, d: dict) -> None:
    """Classic layout: vendor top-left, meta top-right, ruled item table."""
    accent = HexColor(d.get("accent", "#1f4e79"))
    c.setFillColor(accent)
    c.rect(0, H - 18, W, 18, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, H - 70, d["vendor"])
    c.setFont("Helvetica", 9)
    y = H - 86
    for line in d["vendor_addr"]:
        c.drawString(50, y, line)
        y -= 12
    c.drawString(50, y, f"EIN: {d['tax_id']}")

    c.setFont("Helvetica-Bold", 22)
    c.setFillColor(accent)
    c.drawRightString(W - 50, H - 70, "INVOICE")
    c.setFillColor(black)
    c.setFont("Helvetica", 10)
    meta = [("Invoice No:", d.get("inv_no")), ("Invoice Date:", d.get("date")),
            ("Due Date:", d.get("due")), ("PO Number:", d.get("po"))]
    y = H - 92
    for label, val in meta:
        if not val:
            continue
        c.drawRightString(W - 140, y, label)
        c.drawRightString(W - 50, y, val)
        y -= 14

    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, H - 190, "BILL TO")
    c.setFont("Helvetica", 10)
    y = H - 204
    for line in BUYER:
        c.drawString(50, y, line)
        y -= 13

    # table
    y = H - 290
    c.setFillColor(HexColor("#e8eef5"))
    c.rect(45, y - 6, W - 90, 20, fill=1, stroke=0)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(52, y, "Description")
    c.drawRightString(370, y, "Qty")
    c.drawRightString(465, y, "Unit Price")
    c.drawRightString(W - 52, y, "Amount")
    c.setFont("Helvetica", 10)
    y -= 24
    for desc, qty, unit in d["lines"]:
        c.drawString(52, y, desc)
        c.drawRightString(370, y, f"{qty:g}")
        c.drawRightString(465, y, money(unit))
        c.drawRightString(W - 52, y, money(qty * unit))
        y -= 18
    c.line(45, y + 6, W - 45, y + 6)
    sub = sum(q * u for _, q, u in d["lines"])
    tax = round(sub * d.get("tax_rate", 0), 2)
    y -= 12
    rows = [("Subtotal", sub)]
    if d.get("tax_rate"):
        rows.append((f"Sales Tax ({d['tax_rate'] * 100:g}%)", tax))
    rows.append(("Total Due", sub + tax))
    for label, val in rows:
        bold = label == "Total Due"
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawRightString(465, y, label)
        c.drawRightString(W - 52, y, money(val))
        y -= 18

    y -= 30
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Payment details")
    c.setFont("Helvetica", 10)
    for line in [f"Bank: {d['bank']}", f"Account No: {d['acct']}", f"Routing No: {d['routing']}",
                 f"Terms: {d.get('terms', 'Net 30')}"]:
        y -= 14
        c.drawString(50, y, line)
    if d.get("note"):
        y -= 30
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(50, y, d["note"])
    c.setFont("Helvetica", 8)
    c.drawCentredString(W / 2, 40, f"Thank you for your business. Questions? {d['email']}")


# ---------------------------------------------------------------- layout B
def layout_modern(c: canvas.Canvas, d: dict) -> None:
    """Modern layout: 'Tax Invoice', different labels, remit-to block on the right."""
    accent = HexColor(d.get("accent", "#0f766e"))
    c.setFont("Helvetica-Bold", 26)
    c.setFillColor(accent)
    c.drawString(50, H - 72, "Tax Invoice")
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 50, H - 60, d["vendor"])
    c.setFont("Helvetica", 9)
    y = H - 74
    for line in d["vendor_addr"] + [f"Tax ID {d['tax_id']}"]:
        c.drawRightString(W - 50, y, line)
        y -= 11

    c.setFont("Helvetica", 10)
    y = H - 130
    for label, val in [("Invoice #", d.get("inv_no")), ("Date issued", d.get("date")),
                       ("Payment due", d.get("due")), ("Your PO ref", d.get("po"))]:
        if not val:
            continue
        c.setFillColor(HexColor("#6b7280"))
        c.drawString(50, y, label)
        c.setFillColor(black)
        c.drawString(140, y, val)
        y -= 14

    c.setFillColor(HexColor("#6b7280"))
    c.drawString(330, H - 130, "Billed to")
    c.setFillColor(black)
    y = H - 144
    for line in BUYER:
        c.drawString(330, y, line)
        y -= 13

    y = H - 250
    c.setStrokeColor(accent)
    c.setLineWidth(1.5)
    c.line(50, y + 14, W - 50, y + 14)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Item")
    c.drawRightString(380, y, "Qty")
    c.drawRightString(470, y, "Rate")
    c.drawRightString(W - 50, y, "Line total")
    c.line(50, y - 6, W - 50, y - 6)
    c.setFont("Helvetica", 10)
    y -= 24
    for desc, qty, unit in d["lines"]:
        c.drawString(50, y, desc)
        c.drawRightString(380, y, f"{qty:g}")
        c.drawRightString(470, y, money(unit))
        c.drawRightString(W - 50, y, money(qty * unit))
        y -= 18
    sub = sum(q * u for _, q, u in d["lines"])
    tax = round(sub * d.get("tax_rate", 0), 2)
    y -= 10
    for label, val, bold in [("Sub-total", sub, False), ("Tax", tax, False),
                             ("Amount due (USD)", sub + tax, True)]:
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11 if bold else 10)
        c.drawRightString(470, y, label)
        c.drawRightString(W - 50, y, money(val))
        y -= 18

    y -= 30
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Remit to")
    c.setFont("Helvetica", 10)
    for line in [d["bank"], f"Account Number: {d['acct']}", f"ABA Routing: {d['routing']}"]:
        y -= 14
        c.drawString(50, y, line)
    if d.get("note"):
        y -= 28
        c.setFont("Helvetica-Bold", 10)
        c.setFillColor(HexColor("#b91c1c"))
        c.drawString(50, y, d["note"])
        c.setFillColor(black)
    c.setFont("Helvetica", 8)
    c.drawString(50, 40, f"{d['vendor']} · {d['email']}")


# ---------------------------------------------------------------- layout C
def layout_letter(c: canvas.Canvas, d: dict) -> None:
    """Letter-style: centred letterhead, 'Bill No', tax may be embedded in total."""
    c.setFont("Times-Bold", 20)
    c.drawCentredString(W / 2, H - 60, d["vendor"].upper())
    c.setFont("Times-Roman", 10)
    c.drawCentredString(W / 2, H - 76, " | ".join(d["vendor_addr"]))
    c.drawCentredString(W / 2, H - 89, f"TIN {d['tax_id']}  |  {d['email']}")
    c.line(50, H - 98, W - 50, H - 98)

    c.setFont("Times-Roman", 11)
    y = H - 125
    left = [("Bill No:", d.get("inv_no")), ("Dated:", d.get("date")), ("Purchase Order:", d.get("po"))]
    for label, val in left:
        if not val:
            continue
        c.drawString(50, y, f"{label} {val}")
        y -= 15
    y -= 10
    c.drawString(50, y, "To:")
    for line in BUYER:
        y -= 14
        c.drawString(70, y, line)

    y -= 30
    c.drawString(50, y, d.get("intro", "We are pleased to submit our bill for the following:"))
    y -= 26
    c.setFont("Times-Bold", 11)
    c.drawString(50, y, "Particulars")
    c.drawRightString(380, y, "Qty")
    c.drawRightString(470, y, "Rate")
    c.drawRightString(W - 50, y, "Amount")
    c.setFont("Times-Roman", 11)
    y -= 20
    for desc, qty, unit in d["lines"]:
        c.drawString(50, y, desc)
        c.drawRightString(380, y, f"{qty:g}")
        c.drawRightString(470, y, money(unit))
        c.drawRightString(W - 50, y, money(qty * unit))
        y -= 17
    c.line(300, y + 5, W - 50, y + 5)
    sub = sum(q * u for _, q, u in d["lines"])
    tax = round(sub * d.get("tax_rate", 0), 2)
    y -= 14
    c.setFont("Times-Bold", 12)
    if d.get("tax_rate") and d.get("embed_tax"):
        c.drawRightString(470, y, f"Total (incl. {d['tax_rate'] * 100:g}% sales tax)")
    else:
        c.drawRightString(470, y, "Grand Total")
    c.drawRightString(W - 50, y, money(sub + tax))

    y -= 50
    c.setFont("Times-Roman", 11)
    c.drawString(50, y, "Kindly remit payment to:")
    for line in [f"{d['bank']}", f"A/C No. {d['acct']}", f"Routing {d['routing']}"]:
        y -= 14
        c.drawString(70, y, line)
    y -= 40
    c.drawString(50, y, "For " + d["vendor"])
    c.drawString(50, y - 30, "Authorised Signatory")


LAYOUTS = {"classic": layout_classic, "modern": layout_modern, "letter": layout_letter}


def render(d: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    c.setTitle(f"Invoice {d.get('inv_no') or ''}")
    c.setAuthor(d["vendor"])
    LAYOUTS[d["layout"]](c, d)
    c.showPage()
    c.save()
    return buf.getvalue()


def scanify(pdf_bytes: bytes, seed: int = 7) -> bytes:
    """Turn a clean PDF into an image-only 'scan': grey, rotated, noisy, no text layer."""
    rnd = random.Random(seed)
    img = convert_from_bytes(pdf_bytes, dpi=200)[0].convert("L")
    img = img.rotate(0.7, expand=True, fillcolor=255, resample=Image.BICUBIC)
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.004)):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = rnd.choice([90, 140, 170])
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = img.point(lambda v: int(v * 0.93 + 8))  # paper tint
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=200)
    return out.getvalue()


ACME = dict(vendor="Acme Office Supplies Inc.", vendor_addr=["1450 Industrial Way", "Oakland, CA 94607"],
            tax_id="94-3127781", bank="First National Bank", acct="4021 8834 1902", routing="121000358",
            email="billing@acmeoffice.example")
BRIGHT = dict(vendor="BrightPath Logistics LLC", vendor_addr=["88 Harbor Blvd", "Richmond, CA 94804"],
              tax_id="27-4410938", bank="Pacific Commerce Bank", acct="7730019245", routing="122000661",
              email="ar@brightpath.example", accent="#7c3aed")
COBALT = dict(vendor="Cobalt Cloud Services Inc.", vendor_addr=["300 Hamilton Ave", "Palo Alto, CA 94301"],
              tax_id="46-2201775", email="finance@cobaltcloud.example", accent="#1d4ed8")
DELTA = dict(vendor="Delta Facilities Management Co.", vendor_addr=["2200 Zanker Rd", "San Jose, CA 95131"],
             tax_id="82-1190346", bank="Bay Area Credit Union", acct="3309871122", routing="321180379",
             email="accounts@deltafm.example")
FULCRUM = dict(vendor="Fulcrum IT Hardware Ltd", vendor_addr=["17 Commerce Pkwy", "Pleasanton, CA 94588"],
               tax_id="33-9021457", bank="Western Business Bank", acct="6644 3109 87", routing="121137522",
               email="invoices@fulcrumit.example")
EVERGREEN = dict(vendor="Evergreen Catering Co.", vendor_addr=["9 Ferry Plaza", "San Francisco, CA 94111"],
                 tax_id="61-7781203", bank="Golden Gate Bank", acct="8812003341", routing="121042882",
                 email="hello@evergreencatering.example")

ACME_LINES = [("A4 copy paper (case of 10 reams)", 40, 42.50), ("HP 58A toner cartridge", 12, 89.00),
              ("Ergonomic mesh office chair", 4, 289.00)]

SAMPLES = [
    ("01_happy_path_acme.pdf", dict(ACME, layout="classic", inv_no="INV-00871", date="2026-11-03",
                                    due="2026-12-03", po="PO-4501", tax_rate=0.085, lines=ACME_LINES)),
    ("02_split_billing_brightpath_part1.pdf",
     dict(BRIGHT, layout="modern", inv_no="BPL-2026-1107", date="Nov 4, 2026", due="Dec 19, 2026", po="PO-4502",
          lines=[("Freight & logistics services - October (partial billing 1 of 2)", 1, 24000.00)])),
    ("03_split_billing_brightpath_part2.pdf",
     dict(BRIGHT, layout="modern", inv_no="BPL-2026-1188", date="Nov 12, 2026", due="Dec 27, 2026", po="PO-4502",
          lines=[("Freight & logistics services - November (partial billing 2 of 2)", 1, 19500.00)])),
    ("04_scanned_no_po_delta.pdf",
     dict(DELTA, layout="letter", inv_no="DFM-3391", date="06/11/2026", po=None,
          intro="Bill for facilities services rendered at your SF office, November 2026:",
          lines=[("HVAC preventive maintenance", 1, 2400.00), ("Deep cleaning - office floors", 4, 350.00)])),
    ("05_disguised_duplicate_acme.pdf", dict(ACME, layout="classic", inv_no="INV 871", date="10 November 2026",
                                             due="10 December 2026", po="PO-4501", tax_rate=0.085,
                                             lines=ACME_LINES, accent="#334155",
                                             note="Reminder: this invoice is now past our internal follow-up date.")),
    ("06_bank_details_changed_cobalt.pdf",
     dict(COBALT, layout="modern", inv_no="CCS-88213", date="Nov 9, 2026", due="Dec 9, 2026", po="PO-4503",
          bank="Meridian Digital Bank", acct="9087661200 34", routing="026073150",
          lines=[("Managed cloud hosting - annual plan", 1, 18000.00)],
          note="IMPORTANT: Our banking details have changed. Please update your records and pay to the account above.")),
    ("07_within_tolerance_tax_included_fulcrum.pdf",
     dict(FULCRUM, layout="letter", inv_no="FIT/26/0442", date="2026-11-08", po="PO-4506", tax_rate=0.085,
          embed_tax=True, lines=[("Dell Latitude 5450 laptop", 10, 1250.00), ("Shipping & handling", 1, 180.00)])),
    ("08_blocked_vendor_evergreen.pdf",
     dict(EVERGREEN, layout="classic", inv_no="EC-1209", date="2026-11-05", due="2026-11-20", po="PO-4508",
          tax_rate=0.085, accent="#15803d", terms="Net 15",
          lines=[("Leadership offsite catering (2 days)", 1, 2600.00)])),
    ("09_missing_invoice_no_and_date_delta.pdf",
     dict(DELTA, layout="letter", inv_no=None, date=None, po="PO-4507",
          lines=[("HVAC preventive maintenance", 1, 2400.00), ("Deep cleaning - office floors", 4, 350.00),
                 ("Window cleaning - exterior", 1, 450.00)])),
]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, d in SAMPLES:
        pdf = render(d)
        if "scanned" in name:
            pdf = scanify(pdf)
        (OUT / name).write_bytes(pdf)
        print("wrote", name)


if __name__ == "__main__":
    main()
