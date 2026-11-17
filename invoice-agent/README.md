# Invoice Autopilot — PS-1 (Finance / AP)

A working process that takes a vendor invoice (clean PDF, scanned PDF or image) and produces a **clear, reasoned decision** — *Approved*, *Needs review* (routed to a named queue) or *Rejected* (with a drafted message to the vendor) — with every step visible.

- **Live run view:** each of the 7 stages lights up as it executes, showing every check and its result.
- **Dashboard:** history, touchless rate, value approved, review queue, "why invoices were stopped", and a full audit trail for any run.
- **Human in the loop:** reviewers approve or reject from the app; their note is stored and approved invoices update the PO ledger.

## The process

```
Invoice ─► 1 Receive & fingerprint ─► 2 Read (text layer, else OCR) ─► 3 Validate fields & arithmetic
        ─► 4 Verify vendor (identity, status, bank details) ─► 5 Duplicate detection
        ─► 6 Match to PO (explicit or inferred; line, tax, cumulative amount) ─► 7 Decide
                                                 ├─ all pass ............ APPROVED → payment run
                                                 ├─ any "review" fail ... NEEDS REVIEW → AP / Procurement / AP Manager (fraud)
                                                 └─ any "blocking" fail . REJECTED → message to vendor
```

**AI reads, rules decide.** Extraction is rules + OCR by default and can be AI-assisted (see below). Every decision comes from explicit, configurable rules in [`engine/config.py`](engine/config.py), so each one can be explained and audited.

## Test invoices (in `samples/`, listed in demo order)

| # | File | What it tests | Expected |
|---|---|---|---|
| Happy | `01_happy_path_acme.pdf` | Clean, itemised, matches PO-4501 line by line | **Approved** |
| Edge 1 | `05_disguised_duplicate_acme.pdf` | Same invoice re-sent a week later as `INV 871` (was `INV-00871`), new date, new look | **Rejected** — duplicate (numbers normalise to `INV871`) |
| Edge 2 | `02_…part1.pdf` then `03_…part2.pdf` | Two partial invoices against one $40k PO; each is fine alone, together they overbill by $3,500 | Part 1 **Approved**, part 2 **Needs review** → Procurement |
| Edge 3 | `04_scanned_no_po_delta.pdf` | Image-only scan, no PO reference, ambiguous date `06/11/2026` | OCR → date resolved → PO **inferred** (PO-4504, 100/100) → **Needs review**, one-click confirm |
| Edge 4 | `06_bank_details_changed_cobalt.pdf` | Everything matches, but the remit-to bank account has "changed" | **Needs review** → AP Manager fraud check (call-back to number on file) |
| Bonus | `07_…fulcrum.pdf` | $180 shipping not on PO, tax embedded in total | **Approved** with notes (within 2% / $500 tolerance) |
| Bonus | `08_blocked_vendor_evergreen.pdf` | Valid PO, vendor failed compliance | **Rejected** |
| Bonus | `09_missing_invoice_no_and_date_delta.pdf` | No invoice number or date | **Rejected** — vendor asked to reissue |

Order matters for the stateful cases (duplicate and split billing): run them in the order shown. Use **Reset demo data** in the sidebar to start over.

## Run locally

```bash
# system tools for OCR / PDF rendering
#   macOS:   brew install tesseract poppler
#   Ubuntu:  sudo apt-get install tesseract-ocr poppler-utils
#   Windows: install Tesseract (UB Mannheim build) and Poppler, add both to PATH
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py                                   # opens http://localhost:8501
python -m pytest -q                                    # 17 end-to-end tests
```

## Deploy for a live link (free, ~5 minutes)

1. Create a new GitHub repository and push this folder to it.
2. Go to **share.streamlit.io** → sign in with GitHub → **Create app** → pick the repo, branch `main`, main file `app.py`.
3. Deploy. `packages.txt` installs Tesseract and Poppler automatically; `requirements.txt` installs the Python packages.
4. Send the app URL to your hiring coordinator. Open it once before any demo — free apps sleep after a period of inactivity and take ~30 seconds to wake.

## Optional: AI-assisted extraction

The app runs fully without an API key. To add an AI cross-check, in Streamlit Cloud open **App → Settings → Secrets** and add:

```toml
ANTHROPIC_API_KEY = "..."   # or OPENAI_API_KEY = "..."
# LLM_MODEL = "..."         # optional override of the default model
```

The AI reading then fills any field the rules missed, and any disagreement between AI and rules becomes a review flag rather than being silently trusted. *This path is written defensively but was not exercised during the build (no key available); test it once before relying on it in a demo.*

## Project layout

```
app.py                 Streamlit app: Process invoice · Dashboard · POs & vendors · How it works
ui.py                  Live stage cards, decision banner, result tabs, reviewer actions
engine/pipeline.py     The 7-stage pipeline (generator → live events) and decision logic
engine/extract.py      Text-layer / OCR reading and field parsing (label synonyms, derived values)
engine/store.py        Vendor master, POs, PO lines (CSV) + run history and PO ledger (SQLite)
engine/config.py       All business rules and thresholds
engine/llm.py          Optional AI extraction (Anthropic or OpenAI)
data/*.csv             Vendor master, purchase orders, PO lines
samples/               Test invoices — regenerate with: python scripts/make_samples.py
tests/                 End-to-end tests for every scenario
docs/DEMO_SCRIPT.md    5-minute video script + live-interview plan + likely questions
```

## Assumptions

- Buyer is Northwind Retail Inc.; POs are in USD and compared **pre-tax**; tax must equal the PO's tax rate.
- Only approved invoices consume PO balance; pending ones do not.
- An inferred PO is never auto-paid — a human confirms it.
- A bank-detail change is never accepted from an invoice; it is verified out of band.
- Invoices without a number or date are returned, because the number is what prevents double payment.
