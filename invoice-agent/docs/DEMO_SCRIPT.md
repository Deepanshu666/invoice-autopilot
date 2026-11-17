# Demo script

## Before recording or presenting

1. Open the live app and wait for it to load (free hosting sleeps when idle).
2. Sidebar → **Reset demo data**. PO balances start fresh.
3. Run speed: **Demo**.
4. Know the order: 01 → 05 → 02 → 03 → 04 → 06 (then 07/08/09 if there's time).

---

## 5-minute video (target ~4:40)

**0:00–0:30 · The problem and the approach**
> "AP teams open every invoice, find the PO, check the numbers and decide. Invoices are messy — different layouts, scans, missing PO numbers — and mistakes cost money: duplicates, overbilling, fraud. I built a process that takes an invoice and produces a decision with the reasoning visible. My design rule: *AI reads, rules decide.* Reading is flexible; the decision is explicit and auditable."

**0:30–1:20 · Happy path (01, Acme)**
- Show the preview, press **Run process**, narrate stages as they light up.
- "It reads the text layer, validates the arithmetic, confirms the vendor by tax ID, checks the bank account against the vendor master, checks for duplicates, then matches each line to PO-4501 — quantity, unit price, tax."
- Point at the green banner: *Approved, due 3 Dec, to account on file* → "touchless."

**1:20–2:00 · Edge 1: disguised duplicate (05)**
- "Same vendor re-sends a week later — new date, different number format: INV 871 instead of INV-00871."
- "A string comparison misses this. I normalise numbers, so both become INV871 → rejected, and the vendor gets a polite 'already received' message." Open **Vendor message** tab.
- "The PO check catches it too: the PO is already fully billed."

**2:00–2:50 · Edge 2: split billing (02 then 03)**
- Run 02: "First partial invoice, $24k of a $40k PO → approved, $16k remains."
- Run 03: "Second invoice is $19.5k. Alone it's under the PO — but billing is cumulative, so together they're $3,500 over. Routed to the PO owner, not rejected — the likely fix is a PO amendment." Open **PO match** tab to show the ledger strip.

**2:50–3:50 · Edge 3: scanned, no PO (04)**
- "Image-only scan. No text layer, so it switches to OCR and records confidence — 90%."
- "The date says 06/11/2026. Is that June or November? It picks 6 November and explains why."
- "There's no PO number, so it scores this vendor's open POs on amount, line descriptions and dates. PO-4504 scores 100, PO-4507 scores 50. But an inferred match is never auto-paid — a human confirms." Click **Approve** with PO-4504 prefilled.

**3:50–4:30 · Edge 4: bank details changed (06)**
- "This one looks perfect — right vendor, right PO, right amount. But the remit-to account is new, and the invoice says 'our banking details have changed'. That's the classic business-email-compromise pattern."
- "It routes to the AP Manager's fraud queue and tells them to call the number *on file*, not the one on the invoice."

**4:30–4:45 · Dashboard**
- Show the touchless rate, value on hold, review queue, "why invoices were stopped", and click a run to show its full audit trail.

---

## Live interview — run order and talking points

| Run | Invoice | Say |
|---|---|---|
| 1 | 01 Happy path | Walk every stage; explain the decision rule (reject > review > approve). |
| 2 | 05 Duplicate | Normalisation; why duplicates are *rejected* (with a message) not *reviewed*. |
| 3–4 | 02 → 03 Split billing | Cumulative ledger; only *approved* invoices consume the PO. |
| 5 | 04 Scanned, no PO | OCR + confidence, date ambiguity, PO inference scoring, human confirmation. |
| 6 | 06 Bank change | Fraud control that a "matches the PO" system misses entirely. |
| extra | 07 / 08 / 09 | Tolerance + embedded tax / blocked vendor / missing mandatory fields. |
| extra | Upload | Upload any PDF of your own to show it isn't hard-coded to the samples. |

Then: **How it works** page for the rules, and **POs & vendors** to show billed amounts changed.

---

## Questions you are likely to get

**Why rules for decisions and not an LLM?**
Money movement needs determinism and an audit trail: the same invoice must always get the same decision, and every decision must be explainable to an auditor. AI is good at reading messy documents, so that's where it fits; the app has an optional AI cross-check whose disagreements become review flags.

**How did you choose the tolerance (2% and max $500)?**
It covers freight and rounding (a common pattern) while capping the dollar exposure on large POs. It's one line in `engine/config.py`; in reality AP would set it per vendor or category.

**Why "needs review" instead of "reject" for overbilling?**
The vendor may be right (scope grew) — the fix is usually a PO amendment by the requester, so it goes to procurement. Rejection is kept for things the vendor must fix: duplicates, missing fields, unknown or blocked vendor.

**Why not auto-approve an inferred PO at 100/100 confidence?**
"No PO, no pay" is a standard control. The process does the work (finds and pre-fills the match) and the human makes a one-click decision, so the control survives without the manual search.

**What happens with a vendor not in the master?**
Rejected with a message to complete onboarding (connects to PS-2).

**How does it scale to hundreds of invoices a month?**
Each invoice takes well under a second on text PDFs and about 3 seconds with OCR. Next step would be an email-inbox trigger (e.g. a mailbox watcher) and a real database; the pipeline is already a function that takes bytes and returns a decision.

**What would you build next?**
1. Email intake (auto-pull PDFs from the AP inbox).
2. Per-vendor extraction templates learned from reviewer corrections.
3. Goods-receipt matching (3-way match: PO + receipt + invoice).
4. ERP posting (NetSuite/QuickBooks) for approved invoices, and Slack notifications for review queues.
5. Approval limits by amount (e.g. > $25k needs a second approver).

**Known limitations (say these before they're found)**
- Extraction rules handle the common label variants and the 3 sample layouts plus arbitrary uploads; very unusual layouts would need AI extraction turned on.
- Single-page focus in the samples (multi-page PDFs are read, but line-item tables spanning pages aren't specially handled).
- SQLite storage resets if the free host restarts; fine for a demo, not for production.
