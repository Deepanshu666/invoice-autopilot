"""The invoice-to-decision pipeline.

run_pipeline() is a generator: it yields an event as each stage starts, as each
check completes, and when each stage ends, so the UI can show the run live.
The final event carries the full result (decision, reasons, audit trail).

Decision rule (deliberately simple and explainable):
  * any failed check with severity "reject"  -> REJECTED (returned to vendor, with a message)
  * any failed check with severity "review"  -> NEEDS_REVIEW (routed to a named queue)
  * otherwise                                -> APPROVED (warnings are kept on the record)
"""
from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timedelta

from rapidfuzz import fuzz

from . import config, llm
from .extract import normalize_invoice_number, parse_fields, read_document
from .store import Store

STAGES = [
    ("intake", "Receive & fingerprint"),
    ("extract", "Read the invoice"),
    ("validate", "Validate fields & arithmetic"),
    ("vendor", "Verify vendor"),
    ("duplicates", "Duplicate detection"),
    ("po_match", "Match to purchase order"),
    ("decide", "Decision"),
]


def fmt(x: float | None) -> str:
    return "—" if x is None else f"${x:,.2f}"


def mask(acct: str | None) -> str:
    return f"••••{acct[-4:]}" if acct else "—"


class Ctx:
    """Mutable state shared across stages for one run."""

    def __init__(self, store: Store, data: bytes, filename: str, today: date):
        self.store, self.data, self.filename, self.today = store, data, filename, today
        self.checks: list[dict] = []
        self.fields: dict = {}
        self.doc: dict = {}
        self.vendor: dict | None = None
        self.po: dict | None = None
        self.po_is_inferred = False
        self.po_candidates: list[dict] = []
        self.file_hash = hashlib.sha256(data).hexdigest()
        self.inv_norm: str | None = None

    def check(self, stage, label, status, detail, severity=None, queue=None, vendor_msg=None, **extra):
        c = {"stage": stage, "label": label, "status": status, "detail": detail, "severity": severity,
             "queue": queue, "vendor_msg": vendor_msg, **extra}
        self.checks.append(c)
        return c


# ====================================================================== stages
def stage_intake(x: Ctx):
    kb = len(x.data) / 1024
    yield x.check("intake", "File received", "pass", f"{x.filename} · {kb:,.1f} KB")
    yield x.check("intake", "Fingerprint", "info", f"SHA-256 {x.file_hash[:16]}… (used to catch re-sent files)")


def stage_extract(x: Ctx):
    try:
        x.doc = read_document(x.data, x.filename)
    except Exception as e:  # unreadable / corrupt file
        x.doc = {"text": "", "method": "failed", "pages": 0, "ocr_confidence": None}
        yield x.check("extract", "Read document", "fail", f"Could not open the file: {e}", "reject", config.QUEUES["ap"],
                      vendor_msg="We could not open the attached file. Please resend the invoice as a PDF.")
        return
    d = x.doc
    yield x.check("extract", "Reading method", "info", f"{d['method']} · {d['pages']} page(s)")
    if d["ocr_confidence"] is not None:
        ok = d["ocr_confidence"] >= config.OCR_CONFIDENCE_MIN
        yield x.check("extract", "OCR quality", "pass" if ok else "fail",
                      f"Mean word confidence {d['ocr_confidence']:.0f}% (minimum {config.OCR_CONFIDENCE_MIN}%)",
                      None if ok else "review", None if ok else config.QUEUES["ap"])
    x.fields = parse_fields(d["text"], x.today)
    f = x.fields

    if llm.provider():
        try:
            ai = llm.extract_with_llm(d["text"])
            disagreements = []
            for k in ("invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total"):
                a, r = ai.get(k), f.get(k)
                if a in (None, ""):
                    continue
                if r in (None, ""):
                    f[k] = a
                    f["sources"][k] = "AI extraction"
                    continue
                same = (abs(float(a) - float(r)) < 0.01) if isinstance(r, (int, float)) else \
                    normalize_invoice_number(str(a)) == normalize_invoice_number(str(r))
                if not same:
                    disagreements.append(f"{k}: rules read '{r}', AI read '{a}'")
            if disagreements:
                yield x.check("extract", "AI cross-check", "fail", "; ".join(disagreements), "review", config.QUEUES["ap"])
            else:
                yield x.check("extract", "AI cross-check", "pass", f"{llm.provider().title()} reading agrees with rule-based reading")
        except Exception as e:
            yield x.check("extract", "AI cross-check", "warn", f"AI extraction unavailable ({type(e).__name__}); continued with rules")

    found = [k for k in ("invoice_number", "invoice_date", "po_number", "vendor_name", "total") if f.get(k)]
    yield x.check("extract", "Fields extracted", "pass" if len(found) >= 3 else "warn",
                  f"{len(found)}/5 key fields found; {len(f.get('line_items', []))} line item(s)")
    for note in f.get("notes", []):
        yield x.check("extract", "Interpretation", "warn", note)


def stage_validate(x: Ctx):
    f = x.fields
    missing = [lbl for k, lbl in (("invoice_number", "invoice number"), ("invoice_date", "invoice date"),
                                  ("total", "total amount")) if not f.get(k)]
    if missing:
        yield x.check("validate", "Mandatory fields", "fail", "Missing: " + ", ".join(missing), "reject", config.QUEUES["ap"],
                      summary=f"Missing {', '.join(missing)} — sent back to the vendor for reissue",
                      vendor_msg=f"The invoice is missing its {', '.join(missing)}. These are required for us to record and "
                                 "pay it (and to prevent duplicate payments). Please reissue the invoice with them included.")
    else:
        yield x.check("validate", "Mandatory fields", "pass", "Invoice number, date and total present")

    if f.get("line_items_sum") is not None and f["sources"].get("subtotal") == "printed":
        diff = abs(f["line_items_sum"] - f["subtotal"])
        if diff > config.ROUNDING_TOLERANCE:
            yield x.check("validate", "Line items add up", "fail",
                          f"Lines sum to {fmt(f['line_items_sum'])} but subtotal says {fmt(f['subtotal'])}",
                          "review", config.QUEUES["ap"])
        else:
            yield x.check("validate", "Line items add up", "pass", f"Lines sum to {fmt(f['line_items_sum'])} = subtotal")
    if all(f["sources"].get(k) == "printed" for k in ("subtotal", "tax", "total")):
        diff = abs(f["subtotal"] + f["tax"] - f["total"])
        if diff > config.ROUNDING_TOLERANCE:
            yield x.check("validate", "Subtotal + tax = total", "fail",
                          f"{fmt(f['subtotal'])} + {fmt(f['tax'])} ≠ {fmt(f['total'])}", "review", config.QUEUES["ap"])
        else:
            yield x.check("validate", "Subtotal + tax = total", "pass",
                          f"{fmt(f['subtotal'])} + {fmt(f['tax'])} = {fmt(f['total'])}")
    elif f.get("tax_inclusive"):
        yield x.check("validate", "Embedded tax", "info",
                      f"Total is tax-inclusive; tax derived as {fmt(f.get('tax'))} (total {fmt(f.get('total'))} − lines {fmt(f.get('subtotal'))})")

    if f.get("invoice_date"):
        d = date.fromisoformat(f["invoice_date"])
        if d > x.today + timedelta(days=config.MAX_FUTURE_DAYS):
            yield x.check("validate", "Invoice date", "fail", f"Dated in the future ({d:%d %b %Y})", "review", config.QUEUES["ap"])
        elif (x.today - d).days > config.MAX_INVOICE_AGE_DAYS:
            yield x.check("validate", "Invoice date", "fail", f"Invoice is {(x.today - d).days} days old (limit {config.MAX_INVOICE_AGE_DAYS})",
                          "review", config.QUEUES["ap"])
        else:
            yield x.check("validate", "Invoice date", "pass", f"{d:%d %b %Y} · {(x.today - d).days} days ago")


def _identify_vendor(x: Ctx):
    vendors = x.store.vendors
    text = x.doc.get("text", "").lower()
    by_tax = vendors[vendors.tax_id == x.fields.get("tax_id", "~")]
    best, best_score = None, 0
    for _, v in vendors.iterrows():
        names = [v["name"]] + [a for a in v["aliases"].split("|") if a]
        printed = x.fields.get("vendor_name", "")
        # Trust the printed vendor name first; only scan the whole text when no name could be read.
        if printed:
            s = max(fuzz.token_sort_ratio(n.lower(), printed.lower()) for n in names)
        else:
            s = max([fuzz.partial_ratio(n.lower(), text) for n in names if len(n) > 8] or [0])
        if s > best_score:
            best, best_score = v.to_dict(), s
    tax_vendor = by_tax.iloc[0].to_dict() if len(by_tax) else None
    return tax_vendor, best, best_score


def stage_vendor(x: Ctx):
    f = x.fields
    tax_v, name_v, score = _identify_vendor(x)
    name_ok = name_v is not None and score >= config.VENDOR_NAME_MATCH_MIN
    if tax_v and name_ok and tax_v["vendor_id"] != name_v["vendor_id"]:
        x.vendor = tax_v
        yield x.check("vendor", "Vendor identity", "fail",
                      f"Tax ID {f.get('tax_id')} belongs to {tax_v['name']} but the name on the invoice looks like "
                      f"{name_v['name']} — conflicting identity", "review", config.QUEUES["fraud"])
    elif tax_v:
        x.vendor = tax_v
        yield x.check("vendor", "Vendor identity", "pass",
                      f"{tax_v['name']} ({tax_v['vendor_id']}) — tax ID {f['tax_id']} matches vendor master"
                      + (f"; name match {score:.0f}%" if name_ok else ""))
    elif name_ok:
        x.vendor = name_v
        yield x.check("vendor", "Vendor identity", "warn",
                      f"Matched {name_v['name']} ({name_v['vendor_id']}) by name ({score:.0f}%); no tax ID on invoice to confirm")
    else:
        printed = f.get("vendor_name") or "unknown"
        yield x.check("vendor", "Vendor identity", "fail",
                      f"'{printed}' is not on the approved vendor list (closest: {name_v['name'] if name_v else '—'}, {score:.0f}%)",
                      "reject", config.QUEUES["vendor"], summary="Vendor is not on the approved vendor list",
                      vendor_msg="We could not find your company on our approved vendor list. Invoices can only be paid to "
                                 "onboarded vendors — please contact your requester at Northwind to complete vendor onboarding.")
        return
    v = x.vendor
    if v["status"] != "active":
        yield x.check("vendor", "Vendor status", "fail", f"Vendor is {v['status'].upper()}: {v['status_reason']}", "reject",
                      config.QUEUES["vendor"], summary=f"{v['name']} is {v['status']} — {v['status_reason']}",
                      vendor_msg="Your vendor account with Northwind is currently on hold, so we cannot process this invoice. "
                                 "Please contact our Vendor Management team to resolve the account status.")
    else:
        yield x.check("vendor", "Vendor status", "pass", f"Active · terms {v['payment_terms']}")

    acct = f.get("bank_account")
    if not acct:
        yield x.check("vendor", "Bank details", "info", f"No bank details on invoice — will pay account on file {mask(v['bank_account'])}")
    elif acct != v["bank_account"] or (f.get("routing_number") and f["routing_number"] != v["routing_number"]):
        why = " The invoice itself announces a change of bank details — a classic payment-redirection pattern." if f.get("mentions_bank_change") else ""
        yield x.check("vendor", "Bank details", "fail",
                      f"Remit-to account {mask(acct)} (routing {f.get('routing_number', '—')}) does not match vendor master "
                      f"{mask(v['bank_account'])} (routing {v['routing_number']}).{why} Verify by calling {v['phone']} "
                      "(number on file, not on the invoice) before any payment or master-data change.",
                      "review", config.QUEUES["fraud"], fraud=True,
                      summary=f"Bank details differ from vendor master ({mask(acct)} vs {mask(v['bank_account'])}) — possible payment-redirection fraud")
    else:
        yield x.check("vendor", "Bank details", "pass", f"Remit-to account {mask(acct)} matches vendor master")


def stage_duplicates(x: Ctx):
    f = x.fields
    x.inv_norm = normalize_invoice_number(f.get("invoice_number"))
    seen = x.store.hash_seen(x.file_hash)
    if seen:
        yield x.check("duplicates", "Same file", "fail",
                      f"This exact file was already processed as run #{seen['id']} ({seen['status'].replace('_', ' ').lower()})",
                      "reject", config.QUEUES["ap"], duplicate_of=seen["id"],
                      summary=f"Exact same file already processed as run #{seen['id']}",
                      vendor_msg=f"This invoice was already received and is being processed (our ref #{seen['id']}). "
                                 "No action needed — please don't resend.")
        return
    yield x.check("duplicates", "Same file", "pass", "File not seen before")
    if not x.vendor:
        yield x.check("duplicates", "Invoice number", "info", "Skipped — vendor unknown")
        return
    priors = x.store.prior_invoices(x.vendor["vendor_id"])
    if x.inv_norm:
        same = [p for p in priors if p["invoice_number_norm"] == x.inv_norm]
        if same:
            p = same[0]
            yield x.check("duplicates", "Invoice number", "fail",
                          f"'{f['invoice_number']}' is the same invoice as '{p['invoice_number']}' (run #{p['id']}, "
                          f"{p['status'].replace('_', ' ').lower()}, {fmt(p['total'])}) — both normalise to {x.inv_norm}",
                          "reject", config.QUEUES["ap"], duplicate_of=p["id"],
                          summary=f"Duplicate of {p['invoice_number']} (run #{p['id']}) — '{f['invoice_number']}' is the same number ({x.inv_norm}), already {p['status'].replace('_', ' ').lower()}",
                          vendor_msg=f"Invoice {p['invoice_number']} for {fmt(p['total'])} has already been received and "
                                     f"is {('approved for payment' if 'APPROVED' in p['status'] else 'under review')}. "
                                     "We have not processed this copy, to avoid paying twice. No action is needed.")
            return
        yield x.check("duplicates", "Invoice number", "pass", f"No prior invoice {x.inv_norm} from this vendor")
    if f.get("total") is not None and f.get("invoice_date"):
        d = date.fromisoformat(f["invoice_date"])
        near = [p for p in priors if p["total"] is not None and abs(p["total"] - f["total"]) < 0.01 and p["invoice_date"]
                and abs((date.fromisoformat(p["invoice_date"]) - d).days) <= config.DUPLICATE_WINDOW_DAYS]
        if near:
            p = near[0]
            yield x.check("duplicates", "Same amount, close date", "fail",
                          f"Run #{p['id']} ({p['invoice_number']}) has the same total {fmt(p['total'])} within "
                          f"{config.DUPLICATE_WINDOW_DAYS} days — possible duplicate with a new number",
                          "review", config.QUEUES["ap"], duplicate_of=p["id"],
                          summary=f"Possible duplicate of {p['invoice_number']} (run #{p['id']}): same amount, new number")
        else:
            yield x.check("duplicates", "Same amount, close date", "pass", "No same-amount invoice in the last 30 days")


def _score_po(x: Ctx, po: dict) -> tuple[float, list[str]]:
    f, why, score = x.fields, [], 0.0
    sub = f.get("subtotal") or 0
    if po["status"] != "open" or po["remaining"] <= 0:
        return 0, ["PO closed or fully billed"]
    if abs(sub - po["remaining"]) <= 0.01:
        score += 55
        why.append(f"amount {fmt(sub)} equals remaining balance")
    elif sub <= po["remaining"] * (1 + config.TOLERANCE_PCT):
        score += 30 if po["allow_partial"] == "yes" else 15
        why.append(f"amount fits within remaining {fmt(po['remaining'])}")
    lines = f.get("line_items") or []
    if lines and po["lines"]:
        hits = sum(1 for l in lines if max(fuzz.token_set_ratio(l["description"].lower(), pl["description"].lower())
                                           for pl in po["lines"]) >= config.LINE_DESC_MATCH_MIN)
        score += 35 * hits / len(lines)
        why.append(f"{hits}/{len(lines)} lines match PO lines")
    if f.get("invoice_date") and f["invoice_date"] >= po["issue_date"]:
        score += 10
        why.append("invoice dated after PO issue")
    return round(score), why


def stage_po_match(x: Ctx):
    f = x.fields
    if not x.vendor:
        yield x.check("po_match", "PO lookup", "info", "Skipped — vendor unknown")
        return
    po_ref = f.get("po_number")
    if po_ref:
        po = x.store.po(po_ref)
        if not po:
            yield x.check("po_match", "PO lookup", "fail", f"{po_ref} does not exist in the procurement system",
                          "review", config.QUEUES["po"])
            return
        x.po = po
        if po["vendor_id"] != x.vendor["vendor_id"]:
            owner = x.store.vendor(po["vendor_id"])
            yield x.check("po_match", "PO lookup", "fail",
                          f"{po_ref} belongs to {owner['name'] if owner else po['vendor_id']}, not {x.vendor['name']}",
                          "review", config.QUEUES["po"])
            return
        yield x.check("po_match", "PO lookup", "pass", f"{po_ref} found — {po['description']} (requester {po['requester']})")
    else:
        cands = []
        for po in x.store.pos_for_vendor(x.vendor["vendor_id"]):
            s, why = _score_po(x, po)
            cands.append({"po_number": po["po_number"], "score": s, "why": why, "description": po["description"]})
        cands.sort(key=lambda c: -c["score"])
        x.po_candidates = cands
        if cands and cands[0]["score"] >= 60 and (len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= 15):
            best = cands[0]
            x.po = x.store.po(best["po_number"])
            x.po_is_inferred = True
            others = "; ".join(f"{c['po_number']} {c['score']}" for c in cands[1:]) or "none"
            yield x.check("po_match", "PO lookup", "fail",
                          f"No PO on invoice. Best match {best['po_number']} (confidence {best['score']}/100: "
                          f"{', '.join(best['why'])}). Other candidates: {others}. Inferred matches are never auto-paid — "
                          "confirm the PO to approve.", "review", config.QUEUES["ap"], suggested_po=best["po_number"],
                          summary=f"No PO on invoice — best match {best['po_number']} ({best['score']}/100) needs a human to confirm")
        else:
            listing = ", ".join(f"{c['po_number']} ({c['score']})" for c in cands) or "vendor has no POs"
            yield x.check("po_match", "PO lookup", "fail",
                          f"No PO on invoice and no confident match ({listing})", "reject", config.QUEUES["po"], summary="No PO on invoice and no matching PO found",
                          vendor_msg="The invoice does not reference a Northwind purchase order and we could not match it to "
                                     "one. Please reissue it quoting the PO number you received from your requester.")
            return

    po = x.po
    if po["status"] != "open":
        yield x.check("po_match", "PO status", "fail", f"{po['po_number']} is {po['status'].upper()} — no further billing allowed",
                      "review", config.QUEUES["po"])
    if f.get("currency") and f["currency"] != po["currency"]:
        yield x.check("po_match", "Currency", "fail", f"Invoice in {f['currency']}, PO in {po['currency']}", "review", config.QUEUES["po"])

    # ---- line level
    lines = f.get("line_items") or []
    bundled = (len(lines) == 1 and len(po["lines"]) > 1 and max(
        fuzz.token_set_ratio(lines[0]["description"].lower(), pl["description"].lower()) for pl in po["lines"])
        < config.LINE_DESC_MATCH_MIN)
    if bundled:
        yield x.check("po_match", "Line items", "info", "Invoice is bundled into one line; matched at total level only")
    else:
        for l in lines:
            best = max(po["lines"], key=lambda pl: fuzz.token_set_ratio(l["description"].lower(), pl["description"].lower()))
            sim = fuzz.token_set_ratio(l["description"].lower(), best["description"].lower())
            if sim < config.LINE_DESC_MATCH_MIN:
                yield x.check("po_match", f"Line: {l['description'][:40]}", "warn",
                              f"Not on PO ({fmt(l['amount'])}) — allowed only if the total stays within tolerance")
                continue
            var = (l["unit_price"] - best["unit_price"]) / best["unit_price"]
            if var > config.UNIT_PRICE_TOLERANCE_PCT:
                yield x.check("po_match", f"Line: {l['description'][:40]}", "fail",
                              f"Unit price {fmt(l['unit_price'])} vs PO {fmt(best['unit_price'])} (+{var:.1%})",
                              "review", config.QUEUES["po"],
                              summary=f"Price above PO: {l['description'][:40]} at {fmt(l['unit_price'])} vs {fmt(best['unit_price'])} agreed (+{var:.1%})")
            elif l["quantity"] > best["quantity"] and po["allow_partial"] != "yes":
                yield x.check("po_match", f"Line: {l['description'][:40]}", "fail",
                              f"Qty {l['quantity']:g} exceeds PO qty {best['quantity']:g}", "review", config.QUEUES["po"])
            else:
                yield x.check("po_match", f"Line: {l['description'][:40]}", "pass",
                              f"{l['quantity']:g} × {fmt(l['unit_price'])} matches PO line {best['line_no']}")

    # ---- tax
    if f.get("tax") is not None and f.get("subtotal") is not None:
        expected = round(f["subtotal"] * po["tax_rate"], 2)
        if abs(expected - f["tax"]) > config.ROUNDING_TOLERANCE:
            yield x.check("po_match", "Tax", "fail",
                          f"Tax charged {fmt(f['tax'])}, expected {fmt(expected)} at {po['tax_rate']:.1%}", "review", config.QUEUES["ap"])
        else:
            yield x.check("po_match", "Tax", "pass", f"{fmt(f['tax'])} = {po['tax_rate']:.1%} of {fmt(f['subtotal'])}")

    # ---- amount vs remaining balance (cumulative, so split invoices are handled)
    sub = f.get("subtotal") or 0
    prior = x.store.approved_invoices_for_po(po["po_number"])
    after = po["billed"] + sub
    over = after - po["amount"]
    hist = f" Previously billed {fmt(po['billed'])} across {len(prior)} invoice(s)." if prior else ""
    allowed = min(po["amount"] * config.TOLERANCE_PCT, config.TOLERANCE_ABS)
    x.po_snapshot = {"amount": po["amount"], "billed_before": po["billed"], "this_invoice": sub,
                     "billed_after": after, "remaining_after": po["amount"] - after}
    if over <= 0.005:
        partial = after < po["amount"] - 0.005
        note = f" Partial billing — {fmt(po['amount'] - after)} remains open." if partial else " PO now fully billed."
        status = "warn" if partial and po["allow_partial"] != "yes" else "pass"
        extra = " (PO is not flagged for partial billing — noted)" if status == "warn" else ""
        yield x.check("po_match", "Amount vs PO", status,
                      f"{fmt(sub)} brings total billed to {fmt(after)} of {fmt(po['amount'])}.{hist}{note}{extra}")
    elif over <= allowed + 0.005:
        yield x.check("po_match", "Amount vs PO", "warn",
                      f"Over PO by {fmt(over)} ({over / po['amount']:.2%}) — within tolerance "
                      f"({config.TOLERANCE_PCT:.0%} / {fmt(config.TOLERANCE_ABS)} max).{hist}")
    else:
        yield x.check("po_match", "Amount vs PO", "fail",
                      f"{fmt(sub)} would bring total billed to {fmt(after)} against a PO of {fmt(po['amount'])} — "
                      f"over by {fmt(over)} ({over / po['amount']:.1%}), tolerance is {fmt(allowed)}.{hist} "
                      "Needs a PO amendment from the requester or a corrected invoice.",
                      "review", config.QUEUES["po"], overbilled=over,
                      summary=f"Would overbill {po['po_number']} by {fmt(over)} ({over / po['amount']:.1%})"
                              + (" across split invoices" if prior else ""))


def stage_decide(x: Ctx):
    fails = [c for c in x.checks if c["status"] == "fail"]
    rejects = [c for c in fails if c["severity"] == "reject"]
    reviews = [c for c in fails if c["severity"] == "review"]
    warns = [c for c in x.checks if c["status"] == "warn"]
    f = x.fields
    if rejects:
        decision = "REJECTED"
        lead = rejects[0]
        headline = lead.get("summary") or f"{lead['label']}: {lead['detail']}"
        route = "Returned to vendor"
        action = "Send the vendor message below; nothing is posted or paid."
    elif reviews:
        decision = "NEEDS_REVIEW"
        reviews.sort(key=lambda c: (not c.get("fraud"), STAGE_ORDER[c["stage"]]))
        lead = reviews[0]
        headline = lead.get("summary") or f"{lead['label']}: {lead['detail']}"
        route = lead["queue"]
        action = {
            config.QUEUES["fraud"]: "Do not pay. Call the vendor on the number in the vendor master to verify, then approve or reject.",
            config.QUEUES["po"]: "Ask the PO owner to amend the PO or request a corrected invoice / credit note.",
        }.get(route, "Check the flagged item(s) and approve or reject.")
        if lead.get("suggested_po"):
            action = f"Confirm {lead['suggested_po']} is the right PO, then approve — the match is pre-filled."
    else:
        decision = "APPROVED"
        po = x.po["po_number"] if x.po else "PO"
        due = f.get("due_date")
        if not due and x.vendor and f.get("invoice_date"):
            import re as _re
            m = _re.search(r"(\d+)", x.vendor.get("payment_terms", ""))
            if m:
                due = (date.fromisoformat(f["invoice_date"]) + timedelta(days=int(m.group(1)))).isoformat()
                f["due_date"] = due
                f["sources"]["due_date"] = f"derived: invoice date + {x.vendor['payment_terms']}"
        due = due or "per terms"
        lead = None
        headline = f"Approved for payment — {fmt(f.get('total'))} against {po}, due {due}, to account on file"
        if warns:
            headline += f" ({len(warns)} note{'s' if len(warns) > 1 else ''})"
        route = "Payment run"
        action = "Scheduled in the next payment run. No human touch needed."
    vendor_msg = None
    if decision == "REJECTED":
        bits = [c["vendor_msg"] for c in rejects if c.get("vendor_msg")]
        if bits:
            inv = f.get("invoice_number") or "(no number)"
            vendor = x.vendor["name"] if x.vendor else (f.get("vendor_name") or "Vendor")
            to = x.vendor["email"] if x.vendor else "the sender"
            vendor_msg = {
                "to": to,
                "subject": f"Invoice {inv} — action needed",
                "body": f"Hello {vendor} team,\n\nThank you for your invoice {inv}"
                        f"{' for ' + fmt(f.get('total')) if f.get('total') else ''}. We were unable to process it:\n\n"
                        + "\n".join(f"• {b}" for b in bits)
                        + "\n\nIf you have questions, reply to this email.\n\nNorthwind Retail · Accounts Payable",
            }
    lead_detail = lead["detail"] if decision != "APPROVED" else None
    x.outcome = dict(decision=decision, headline=headline, reason_detail=lead_detail, route_to=route, next_action=action, vendor_message=vendor_msg)
    tone = {"APPROVED": "pass", "NEEDS_REVIEW": "warn", "REJECTED": "fail"}[decision]
    yield x.check("decide", decision.replace("_", " ").title(), tone,
                  f"{len(rejects)} blocking, {len(reviews)} for review, {len(warns)} note(s) → {route}")


STAGE_ORDER = {k: i for i, (k, _) in enumerate(STAGES)}
STAGE_FUNCS = {"intake": stage_intake, "extract": stage_extract, "validate": stage_validate, "vendor": stage_vendor,
               "duplicates": stage_duplicates, "po_match": stage_po_match, "decide": stage_decide}


def run_pipeline(store: Store, data: bytes, filename: str, today: date | None = None, save: bool = True):
    """Yield events: stage_start, check, stage_end, done(result)."""
    x = Ctx(store, data, filename, today or date.today())
    x.po_snapshot = None
    total_busy = 0.0
    stage_log = []
    for key, label in STAGES:
        if key != "decide" and x.doc.get("method") == "failed":
            continue
        yield {"type": "stage_start", "stage": key, "label": label}
        n_before = len(x.checks)
        # Time only the work done inside the stage, not time the UI spends between events.
        busy, gen = 0.0, STAGE_FUNCS[key](x)
        while True:
            s0 = time.perf_counter()
            try:
                c = next(gen)
            except StopIteration:
                busy += time.perf_counter() - s0
                break
            busy += time.perf_counter() - s0
            yield {"type": "check", "stage": key, "check": c}
        stage_checks = x.checks[n_before:]
        status = "fail" if any(c["status"] == "fail" for c in stage_checks) else \
            "warn" if any(c["status"] == "warn" for c in stage_checks) else "pass"
        ms = max(1, int(busy * 1000))
        total_busy += busy
        stage_log.append({"stage": key, "label": label, "status": status, "ms": ms})
        yield {"type": "stage_end", "stage": key, "label": label, "status": status, "ms": ms}

    f = x.fields
    result = {
        "filename": filename, "file_hash": x.file_hash, "processed_at": datetime.now().isoformat(timespec="seconds"),
        "method": x.doc.get("method"), "ocr_confidence": x.doc.get("ocr_confidence"), "text": x.doc.get("text", ""),
        "fields": {k: v for k, v in f.items()}, "invoice_number_norm": x.inv_norm,
        "vendor_id": x.vendor["vendor_id"] if x.vendor else None, "vendor_name": x.vendor["name"] if x.vendor else f.get("vendor_name"),
        # Only an explicit PO is recorded against the ledger; an inferred one waits for the reviewer to confirm.
        "po_number": x.po["po_number"] if x.po and not x.po_is_inferred else None,
        "suggested_po": x.po["po_number"] if x.po and x.po_is_inferred else None,
        "po_candidates": x.po_candidates, "po_snapshot": x.po_snapshot,
        "checks": x.checks, "stages": stage_log, "duration_ms": max(1, int(total_busy * 1000)),
        **x.outcome,
    }
    if save:
        result["run_id"] = store.save_run(result)
    yield {"type": "done", "result": result}


def process(store: Store, data: bytes, filename: str, today: date | None = None, save: bool = True) -> dict:
    """Run the pipeline to completion and return the result (for scripts/tests)."""
    for ev in run_pipeline(store, data, filename, today, save):
        if ev["type"] == "done":
            return ev["result"]
    raise RuntimeError("pipeline produced no result")
