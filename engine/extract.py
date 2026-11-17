"""Read an invoice (text PDF, scanned PDF or image) and pull out structured fields.

Strategy:
  1. Use the PDF's text layer when it has one (exact, fast).
  2. Otherwise OCR the page images with Tesseract and record OCR confidence.
  3. Parse fields with label-synonym rules that cope with different vendor layouts
     ("Invoice No" / "Invoice #" / "Bill No", "Total Due" / "Amount due" / "Grand Total" ...).
  4. Derive what is missing but computable (subtotal from lines, embedded tax) and
     record *how* each value was obtained, so every number can be explained.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta

from dateutil import parser as dateparser

MONEY = r"\$?\s?((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})"


# ------------------------------------------------------------------ reading
def read_document(data: bytes, filename: str) -> dict:
    """Return {'text', 'method', 'pages', 'ocr_confidence'}."""
    name = filename.lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff")):
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        text, conf = _ocr_images([img])
        return {"text": text, "method": "OCR (image upload)", "pages": 1, "ocr_confidence": conf}

    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if len(re.sub(r"\s", "", text)) >= 40:
        return {"text": text, "method": "PDF text layer", "pages": pages, "ocr_confidence": None}

    from pdf2image import convert_from_bytes
    images = convert_from_bytes(data, dpi=300)
    text, conf = _ocr_images(images)
    return {"text": text, "method": "OCR (scanned PDF, no text layer)", "pages": pages, "ocr_confidence": conf}


def _ocr_images(images) -> tuple[str, float]:
    import pytesseract
    texts, confs = [], []
    for img in images:
        img = img.convert("L")
        texts.append(pytesseract.image_to_string(img, config="--psm 4"))
        data = pytesseract.image_to_data(img, config="--psm 4", output_type=pytesseract.Output.DICT)
        confs += [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
    conf = round(sum(confs) / len(confs), 1) if confs else 0.0
    return "\n".join(texts), conf


# ------------------------------------------------------------------ helpers
def _money(s: str) -> float:
    return float(s.replace(",", ""))


def _last_money(line: str) -> float | None:
    found = re.findall(MONEY, line)
    return _money(found[-1]) if found else None


def normalize_invoice_number(s: str | None) -> str | None:
    """'INV-00871', 'inv 871', 'INV/871' all become 'INV871'."""
    if not s:
        return None
    s = re.sub(r"[^A-Z0-9]", "", s.upper())
    return re.sub(r"\d+", lambda m: str(int(m.group())), s)


def normalize_po(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(\d{4,})", s)
    return f"PO-{m.group(1)}" if m else None


DATE_PATTERNS = [
    r"\d{4}-\d{2}-\d{2}",
    r"\d{1,2}[/.]\d{1,2}[/.]\d{4}",
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}",
]
DATE_RE = re.compile("|".join(f"(?:{p})" for p in DATE_PATTERNS), re.I)


def parse_date(raw: str, today: date | None = None) -> tuple[str | None, str | None]:
    """Parse a date; resolve DD/MM vs MM/DD ambiguity. Returns (iso, note)."""
    today = today or date.today()
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", raw.strip())
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        cands = []
        for d_, mo, label in ((b, a, "MM/DD"), (a, b, "DD/MM")):
            try:
                cands.append((date(y, mo, d_), label))
            except ValueError:
                pass
        if len(cands) == 2 and cands[0][0] != cands[1][0]:
            not_future = [c for c in cands if c[0] <= today + timedelta(days=3)]
            pick = max(not_future or cands, key=lambda c: c[0])
            other = [c for c in cands if c is not pick][0]
            note = (f"'{raw}' is ambiguous: read as {pick[0]:%d %b %Y} ({pick[1]}) rather than "
                    f"{other[0]:%d %b %Y} ({other[1]}) because it is the most recent date not in the future")
            return pick[0].isoformat(), note
        if cands:
            return cands[0][0].isoformat(), None
        return None, f"Could not parse date '{raw}'"
    try:
        return dateparser.parse(raw, fuzzy=True).date().isoformat(), None
    except (ValueError, OverflowError):
        return None, f"Could not parse date '{raw}'"


def _find_labeled_date(lines: list[str], labels: list[str], today: date | None, skip: str | None = None):
    for lab in labels:  # labels in priority order
        for line in lines:
            low = line.lower()
            if skip and skip in low:
                continue
            i = low.find(lab)
            if i >= 0:
                m = DATE_RE.search(line[i + len(lab):])
                if m:
                    iso, note = parse_date(m.group(0), today)
                    return iso, m.group(0), note
    return None, None, None


# ------------------------------------------------------------------ parsing
TOTAL_LABELS = [r"grand\s*total", r"total\s*due", r"amount\s*due", r"balance\s*due", r"invoice\s*total",
                r"total\s*amount", r"total\s*\(incl", r"^\s*total\b"]
SUBTOTAL_LABELS = [r"sub-?\s?total", r"net\s*amount", r"total\s*before\s*tax", r"taxable\s*amount"]
TAX_LABELS = [r"sales\s*tax", r"^\s*tax\b", r"\bvat\b", r"\bgst\b", r"\btax\s*\(", r"\btax\s*\$"]
NOT_TAX = re.compile(r"tax\s*(id|invoice|no\b|number)|incl", re.I)

HEADER_RE = re.compile(r"\b(qty|quantity|hours|units)\b.*\b(amount|total|rate|price)\b", re.I)
LINE_RE = re.compile(rf"^(?P<desc>.*?[A-Za-z].*?)\s+(?P<qty>\d+(?:\.\d+)?)\s+{MONEY}\s+{MONEY}\s*$")
LINE_SIMPLE_RE = re.compile(rf"^(?P<desc>.*?[A-Za-z]{{3}}.*?)\s+{MONEY}\s*$")


def parse_fields(text: str, today: date | None = None) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    f: dict = {"sources": {}, "notes": []}
    src = f["sources"]

    # --- invoice number
    m = re.search(r"(?i:invoice\s*(?:no\.?|number|num|#)|inv\.?\s*(?:no\.?|#)|bill\s*(?:no\.?|number|#))"
                  r"\s*[:#.]?\s*([A-Z]{0,5}[ \-/]?[A-Z0-9][A-Z0-9\-/]*)", text)
    if m and re.search(r"\d", m.group(1)):
        f["invoice_number"] = m.group(1).strip()
        src["invoice_number"] = "printed"

    # --- dates
    iso, raw, note = _find_labeled_date(lines, ["invoice date", "date issued", "issue date", "dated", "date:"], today, skip="due")
    if iso:
        f["invoice_date"], f["invoice_date_raw"] = iso, raw
        src["invoice_date"] = "printed"
        if note:
            f["notes"].append(note)
            src["invoice_date"] = "printed (ambiguous format resolved)"
    due, _, _ = _find_labeled_date(lines, ["due date", "payment due", "due:"], today)
    if due:
        f["due_date"] = due

    # --- PO reference
    m = re.search(r"(?i)\b(?:purchase\s*order|p\.?o\.?)\s*(?:no\.?|number|ref(?:erence)?|#)?\s*[:#\-]?\s*(?:PO[\s\-]*)?(\d{4,})", text)
    if m and not re.search(r"(?i)box\s*$", text[max(0, m.start() - 4):m.start() + 6]):
        f["po_number"] = normalize_po(m.group(1))
        src["po_number"] = "printed"

    # --- vendor identifiers
    m = re.search(r"(?i)(?:EIN|Tax\s*ID|TIN|Tax\s*No\.?|VAT\s*No\.?)\s*[:#]?\s*(\d{2}-?\d{7})", text)
    if m:
        f["tax_id"] = m.group(1) if "-" in m.group(1) else f"{m.group(1)[:2]}-{m.group(1)[2:]}"
    m = re.search(r"(?i)(?:account\s*(?:no\.?|number|#)|a/c\s*(?:no\.?)?|acct\.?\s*(?:no\.?)?)\s*[:#.]?\s*(\d[\d \-]{5,24}\d)", text)
    if m:
        f["bank_account"] = re.sub(r"\D", "", m.group(1))
    m = re.search(r"(?i)(?:routing|aba)\s*(?:no\.?|number|#)?\s*(?:routing)?\s*[:#]?\s*(\d{9})", text)
    if m:
        f["routing_number"] = m.group(1)
    f["mentions_bank_change"] = bool(re.search(r"(?i)(bank(ing)?\s*details?|account)\s*(have|has)?\s*changed|new\s*bank", text))

    # vendor name guess: signature line, else first meaningful line
    m = re.search(r"(?im)^\s*for\s+([A-Z][\w&.,' ]{3,60})$", text)
    if m:
        f["vendor_name"] = m.group(1).strip()
    else:
        for l in lines[:5]:
            cand = re.sub(r"(?i)\b(tax\s+)?invoice\b", "", l).strip(" -|")
            if len(cand) > 3 and not re.search(r"\d{3,}", cand):
                f["vendor_name"] = cand
                break

    # --- currency
    if re.search(r"\bUSD\b|\$", text):
        f["currency"] = "USD"
    elif re.search(r"\bEUR\b|€", text):
        f["currency"] = "EUR"
    elif re.search(r"\bGBP\b|£", text):
        f["currency"] = "GBP"
    elif re.search(r"\bINR\b|₹", text):
        f["currency"] = "INR"

    # --- line items (between table header and first totals line)
    items, in_table = [], False
    stop_re = re.compile("|".join(TOTAL_LABELS + SUBTOTAL_LABELS + TAX_LABELS), re.I)
    for l in lines:
        if HEADER_RE.search(l):
            in_table = True
            continue
        if not in_table:
            continue
        if stop_re.search(l):
            break
        m = LINE_RE.match(l)
        if m:
            qty, unit, amt = float(m.group("qty")), _money(m.group(3)), _money(m.group(4))
            items.append({"description": m.group("desc").strip(" -|"), "quantity": qty, "unit_price": unit, "amount": amt})
            continue
        m = LINE_SIMPLE_RE.match(l)
        if m:
            amt = _money(m.group(2))
            items.append({"description": m.group("desc").strip(" -|"), "quantity": 1.0, "unit_price": amt, "amount": amt})
    f["line_items"] = items

    # --- totals
    for l in lines:
        low = l.lower()
        val = _last_money(l)
        if val is None:
            continue
        if "subtotal" not in f and any(re.search(p, low) for p in SUBTOTAL_LABELS):
            f["subtotal"] = val
            src["subtotal"] = "printed"
        elif "tax" not in f and any(re.search(p, low) for p in TAX_LABELS) and not NOT_TAX.search(low):
            f["tax"] = val
            src["tax"] = "printed"
        elif any(re.search(p, low) for p in TOTAL_LABELS) and not re.search(r"sub-?\s?total|line\s*total", low):
            f["total"] = val
            src["total"] = "printed"
            if "incl" in low:
                f["tax_inclusive"] = True
                rate = re.search(r"(\d+(?:\.\d+)?)\s*%", l)
                if rate:
                    f["stated_tax_rate"] = float(rate.group(1)) / 100

    # --- derive missing values (and say so)
    line_sum = round(sum(i["amount"] for i in items), 2) if items else None
    f["line_items_sum"] = line_sum
    if "subtotal" not in f and line_sum is not None:
        f["subtotal"] = line_sum
        src["subtotal"] = "derived: sum of line items"
    if "tax" not in f and f.get("total") is not None and f.get("subtotal") is not None:
        f["tax"] = round(f["total"] - f["subtotal"], 2)
        src["tax"] = "derived: total minus subtotal" + (" (tax embedded in total)" if f.get("tax_inclusive") else "")
    if "total" not in f and f.get("subtotal") is not None:
        f["total"] = round(f["subtotal"] + (f.get("tax") or 0), 2)
        src["total"] = "derived: subtotal + tax"
    return f


FIELD_LABELS = {
    "vendor_name": "Vendor (as printed)", "tax_id": "Vendor tax ID", "invoice_number": "Invoice number",
    "invoice_date": "Invoice date", "due_date": "Due date", "po_number": "PO reference", "currency": "Currency",
    "subtotal": "Subtotal", "tax": "Tax", "total": "Total", "bank_account": "Remit-to account",
    "routing_number": "Routing number",
}
