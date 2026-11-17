"""Invoice Autopilot — PDF to a reasoned AP decision, with every step visible.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter

import altair as alt
import pandas as pd
import streamlit as st

from engine import config, llm
from engine.pipeline import STAGES, fmt, mask, run_pipeline
from engine.store import Store
from ui import (decision_banner, inject_css, pill, preview_image, render_result_tabs, render_review_actions,
                render_stages_static, stage_html)

st.set_page_config(page_title="Invoice Autopilot", page_icon="🧾", layout="wide")
inject_css()

# Optional AI key from Streamlit secrets (never required).
try:
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LLM_MODEL"):
        if k in st.secrets and not os.getenv(k):
            os.environ[k] = st.secrets[k]
except Exception:
    pass

SAMPLE_INFO = [
    ("01", "Happy path · Acme Office Supplies", "Clean, itemised PDF that matches PO-4501 line by line."),
    ("05", "Edge 1 · Disguised duplicate · Acme", "The same invoice re-sent a week later as 'INV 871' with a new date and layout."),
    ("02", "Edge 2a · Split billing, part 1 · BrightPath", "First partial invoice ($24,000) against a $40,000 freight PO."),
    ("03", "Edge 2b · Split billing, part 2 · BrightPath", "Second partial ($19,500) — together they overbill the PO by 8.8%."),
    ("04", "Edge 3 · Scanned, no PO · Delta Facilities", "Image-only scan, no PO reference, and an ambiguous date (06/11/2026)."),
    ("06", "Edge 4 · Bank details changed · Cobalt Cloud", "Matches the PO perfectly — but asks to be paid into a new bank account."),
    ("07", "Bonus · Within tolerance, tax embedded · Fulcrum", "Adds $180 shipping that isn't on the PO; tax is folded into the total."),
    ("08", "Bonus · Blocked vendor · Evergreen", "Valid PO, but the vendor failed its compliance review."),
    ("09", "Bonus · Missing invoice no. & date · Delta", "Has to go back to the vendor for a reissue."),
]


@st.cache_resource
def get_store() -> Store:
    return Store()


store = get_store()


def sample_file(prefix: str):
    return next(config.SAMPLES_DIR.glob(f"{prefix}_*.pdf"))


# =========================================================================== pages
def page_process():
    st.markdown("<div class='app-title'>Process an invoice</div>"
                "<div class='app-sub'>Drop in a vendor invoice. The agent reads it, checks it against the vendor master "
                "and purchase orders, and makes a decision you can audit line by line.</div>", unsafe_allow_html=True)
    left, right = st.columns([1, 1.35], gap="large")

    with left:
        src = st.radio("Input", ["Sample invoices", "Upload your own"], horizontal=True, label_visibility="collapsed")
        data = name = None
        if src == "Sample invoices":
            labels = [s[1] for s in SAMPLE_INFO]
            choice = st.selectbox("Choose a test invoice (listed in demo order)", labels)
            prefix, _, blurb = SAMPLE_INFO[labels.index(choice)]
            st.markdown(f"<div class='hint'>{blurb}</div>", unsafe_allow_html=True)
            path = sample_file(prefix)
            data, name = path.read_bytes(), path.name
        else:
            up = st.file_uploader("Invoice PDF or image", type=["pdf", "png", "jpg", "jpeg"])
            if up:
                data, name = up.getvalue(), up.name
        pace = st.select_slider("Run speed", options=["Instant", "Demo", "Slow"], value="Demo",
                                help="Demo/Slow pause briefly between stages so each step is visible while presenting.")
        run = st.button("▶  Run process", type="primary", width="stretch", disabled=data is None)
        if data:
            with st.expander(f"Preview · {name}", expanded=True):
                try:
                    st.image(preview_image(data, name), width="stretch")
                except Exception:
                    st.caption("Preview unavailable for this file.")

    with right:
        st.markdown("#### Live run")
        if run and data:
            delay = {"Instant": 0, "Demo": 0.45, "Slow": 1.0}[pace]
            holders = {key: st.empty() for key, _ in STAGES}
            idx = {key: i for i, (key, _) in enumerate(STAGES, 1)}
            for key, label in STAGES:
                holders[key].markdown(stage_html(idx[key], label, "pending", []), unsafe_allow_html=True)
            checks: dict[str, list] = {}
            result = None
            for ev in run_pipeline(store, data, name):
                k = ev.get("stage")
                if ev["type"] == "stage_start":
                    checks[k] = []
                    holders[k].markdown(stage_html(idx[k], ev["label"], "running", []), unsafe_allow_html=True)
                    if delay:
                        time.sleep(delay * 0.5)
                elif ev["type"] == "check":
                    checks[k].append(ev["check"])
                    holders[k].markdown(stage_html(idx[k], STAGES[idx[k] - 1][1], "running", checks[k]), unsafe_allow_html=True)
                    if delay:
                        time.sleep(delay * 0.25)
                elif ev["type"] == "stage_end":
                    holders[k].markdown(stage_html(idx[k], ev["label"], ev["status"], checks[k], ev["ms"]), unsafe_allow_html=True)
                    if delay:
                        time.sleep(delay * 0.5)
                elif ev["type"] == "done":
                    result = ev["result"]
            st.session_state["last_run_id"] = result["run_id"]
            st.rerun()
        elif st.session_state.get("last_run_id"):
            run_row = store.run(st.session_state["last_run_id"])
            if run_row:
                st.caption(f"Run #{run_row['id']} · {run_row['filename']} · {run_row['created_at'].replace('T', ' ')}")
                render_stages_static(run_row["result"])
        else:
            for i, (key, label) in enumerate(STAGES, 1):
                st.markdown(stage_html(i, label, "pending", []), unsafe_allow_html=True)
            st.caption("Pick an invoice and press **Run process**.")

    run_row = store.run(st.session_state["last_run_id"]) if st.session_state.get("last_run_id") else None
    if run_row:
        st.divider()
        decision_banner(run_row["result"], run_row["status"])
        render_review_actions(store, run_row["id"], run_row["result"], run_row["status"], key=f"proc{run_row['id']}")
        if run_row.get("reviewer_note"):
            st.caption(f"Reviewer note ({run_row['reviewed_at']}): {run_row['reviewer_note']}")
        render_result_tabs(run_row["result"], key=f"proc{run_row['id']}")


def kpi(col, label, value, hint=""):
    col.markdown(f"<div class='kpi'><div class='l'>{label}</div><div class='v'>{value}</div><div class='h'>{hint}</div></div>",
                 unsafe_allow_html=True)


def page_dashboard():
    st.markdown("<div class='app-title'>Dashboard</div><div class='app-sub'>Every invoice processed, its outcome, and what is "
                "waiting on a human.</div>", unsafe_allow_html=True)
    runs = store.runs()
    if runs.empty:
        st.info("No invoices processed yet. Go to **Process invoice** and run a few samples.")
        return
    n = len(runs)
    auto = (runs.decision == "APPROVED").sum()
    pending = (runs.status == "PENDING_REVIEW").sum()
    rejected = runs.status.isin(["REJECTED", "REJECTED_BY_REVIEWER"]).sum()
    approved_value = runs[runs.status.isin(["APPROVED", "APPROVED_BY_REVIEWER"])].total.fillna(0).sum()
    held = runs[runs.status == "PENDING_REVIEW"].total.fillna(0).sum()
    cols = st.columns(6)
    kpi(cols[0], "Invoices processed", n)
    kpi(cols[1], "Touchless rate", f"{auto / n:.0%}", f"{auto} auto-approved")
    kpi(cols[2], "Waiting for review", pending, f"{fmt(held)} on hold")
    kpi(cols[3], "Rejected", rejected, "returned to vendor")
    kpi(cols[4], "Value approved", f"${approved_value:,.0f}")
    kpi(cols[5], "Avg. processing", f"{runs.duration_ms.mean() / 1000:.2f}s", "per invoice")

    st.write("")
    c1, c2 = st.columns(2)
    status_df = runs.status.value_counts().rename_axis("Status").reset_index(name="Invoices")
    status_df["Status"] = status_df.Status.str.replace("_", " ").str.title()
    colors = {"Approved": "#16a34a", "Approved By Reviewer": "#0d9488", "Pending Review": "#d97706",
              "Rejected": "#dc2626", "Rejected By Reviewer": "#991b1b"}
    c1.markdown("**Outcomes**")
    c1.altair_chart(alt.Chart(status_df).mark_bar(cornerRadiusEnd=4).encode(
        x=alt.X("Invoices:Q", axis=alt.Axis(tickMinStep=1)), y=alt.Y("Status:N", sort="-x", title=None),
        color=alt.Color("Status:N", scale=alt.Scale(domain=list(colors), range=list(colors.values())), legend=None),
        tooltip=["Status", "Invoices"]).properties(height=200), width="stretch")
    reasons = Counter()
    for rj in runs.result_json:
        for c in json.loads(rj)["checks"]:
            if c["status"] == "fail" and c["stage"] != "decide":
                reasons[c["label"] if not c["label"].startswith("Line:") else "Line price / qty"] += 1
    c2.markdown("**Why invoices were stopped**")
    if reasons:
        rdf = pd.DataFrame(reasons.most_common(), columns=["Reason", "Count"])
        c2.altair_chart(alt.Chart(rdf).mark_bar(color="#6366f1", cornerRadiusEnd=4).encode(
            x=alt.X("Count:Q", axis=alt.Axis(tickMinStep=1)), y=alt.Y("Reason:N", sort="-x", title=None),
            tooltip=["Reason", "Count"]).properties(height=200), width="stretch")
    else:
        c2.caption("No exceptions yet.")

    queue = runs[runs.status == "PENDING_REVIEW"]
    st.markdown(f"#### Review queue · {len(queue)}")
    if queue.empty:
        st.caption("Nothing waiting. 🎉")
    else:
        st.dataframe(queue[["id", "vendor_name", "invoice_number", "total", "route_to", "headline"]].rename(columns={
            "id": "Run", "vendor_name": "Vendor", "invoice_number": "Invoice", "total": "Total", "route_to": "Queue",
            "headline": "Reason"}), hide_index=True,
            column_config={"Total": st.column_config.NumberColumn(format="dollar"),
                           "Reason": st.column_config.TextColumn(width="large")})

    st.markdown("#### All runs")
    st.caption("Select a row to open the full audit trail.")
    view = runs[["id", "created_at", "vendor_name", "invoice_number", "po_number", "total", "status",
                 "route_to", "filename", "duration_ms"]].copy()
    view["created_at"] = view.created_at.str.replace("T", " ")
    view["status"] = view.status.str.replace("_", " ").str.title()
    event = st.dataframe(view.rename(columns={
        "id": "Run", "created_at": "Processed", "filename": "File", "vendor_name": "Vendor", "invoice_number": "Invoice",
        "po_number": "PO", "total": "Total", "status": "Status", "route_to": "Routed to", "duration_ms": "ms"}),
        hide_index=True, on_select="rerun", selection_mode="single-row", key="runs_table",
        column_config={"Total": st.column_config.NumberColumn(format="dollar")})
    sel = event.selection.rows if event and event.selection else []
    if sel:
        run_id = int(view.iloc[sel[0]].id)
        row = store.run(run_id)
        st.divider()
        st.markdown(f"### Run #{run_id} · {row['filename']} {pill(row['status'])}", unsafe_allow_html=True)
        decision_banner(row["result"], row["status"])
        render_review_actions(store, run_id, row["result"], row["status"], key=f"dash{run_id}")
        if row.get("reviewer_note"):
            st.caption(f"Reviewer note ({row['reviewed_at']}): {row['reviewer_note']}")
        a, b = st.columns([1, 1.4], gap="large")
        with a:
            st.markdown("**Execution trace**")
            render_stages_static(row["result"])
        with b:
            render_result_tabs(row["result"], key=f"dash{run_id}")


def page_reference():
    st.markdown("<div class='app-title'>Purchase orders & vendors</div><div class='app-sub'>The procurement data the agent "
                "checks against. Billed amounts update live as invoices are approved.</div>", unsafe_allow_html=True)
    st.markdown("#### Purchase orders")
    st.dataframe(store.po_table(), hide_index=True, column_config={
        "PO amount": st.column_config.NumberColumn(format="dollar"),
        "Billed": st.column_config.NumberColumn(format="dollar"),
        "Remaining": st.column_config.NumberColumn(format="dollar"),
        "Used": st.column_config.ProgressColumn(min_value=0, max_value=1.2, format="percent")})
    st.markdown("#### PO lines")
    st.dataframe(store.po_lines, hide_index=True, column_config={
        "unit_price": st.column_config.NumberColumn("unit price", format="dollar"),
        "line_total": st.column_config.NumberColumn("line total", format="dollar")})
    st.markdown("#### Vendor master")
    v = store.vendors.copy()
    v["bank_account"] = v.bank_account.map(mask)
    st.dataframe(v.drop(columns=["aliases"]), hide_index=True)


def page_how():
    st.markdown("<div class='app-title'>How it works</div><div class='app-sub'>The process, the rules it enforces, and the "
                "assumptions behind them.</div>", unsafe_allow_html=True)
    st.graphviz_chart("""
digraph G { rankdir=TB; nodesep=0.35; ranksep=0.45; bgcolor="transparent"; node [shape=box style="rounded,filled" fillcolor="#eef2ff" color="#c7d2fe" fontname="Helvetica" fontsize=12 margin="0.15,0.08"];
 edge [color="#94a3b8" fontname="Helvetica" fontsize=9];
 in [label="Invoice\\n(PDF / scan / image)" fillcolor="#f1f5f9"];
 a [label="1 Receive &\\nfingerprint"]; b [label="2 Read\\n(text layer → OCR)"]; c [label="3 Validate fields\\n& arithmetic"];
 d [label="4 Verify vendor\\n(identity, status, bank)"]; e [label="5 Duplicate\\ndetection"]; f [label="6 Match PO\\n(explicit or inferred)"];
 g [label="7 Decide" fillcolor="#e0e7ff"];
 ok [label="Approved →\\npayment run" fillcolor="#dcfce7" color="#86efac"];
 rv [label="Needs review →\\nnamed queue" fillcolor="#fef3c7" color="#fcd34d"];
 rj [label="Rejected →\\nmessage to vendor" fillcolor="#fee2e2" color="#fca5a5"];
 in -> a -> b -> c -> d -> e -> f -> g; g -> ok [label="all pass"]; g -> rv [label="review flag"]; g -> rj [label="blocking fail"];
 rv -> ok [label="reviewer approves" style=dashed]; rv -> rj [label="reviewer rejects" style=dashed];
 {rank=same; in; a; b; c;} {rank=same; d; e; f; g;} {rank=same; ok; rv; rj;}
}""", width="content")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Rules enforced")
        st.markdown(f"""
- **Mandatory fields:** invoice number, date and total — otherwise returned to the vendor.
- **Arithmetic:** lines must add to the subtotal and subtotal + tax to the total (±{fmt(config.ROUNDING_TOLERANCE)}).
- **Dates:** not more than {config.MAX_FUTURE_DAYS} days in the future, not older than {config.MAX_INVOICE_AGE_DAYS} days. Ambiguous DD/MM vs MM/DD dates are resolved and explained.
- **Vendor:** must be on the vendor master (tax ID first, then fuzzy name ≥ {config.VENDOR_NAME_MATCH_MIN}%), and active.
- **Bank details:** remit-to account must match the master. A mismatch is always a fraud check, never auto-paid.
- **Duplicates:** same file hash; same invoice number after normalising (INV-00871 = INV 871); same amount within {config.DUPLICATE_WINDOW_DAYS} days.
- **PO match:** PO must exist, belong to the vendor, be open and in the same currency. Billing is **cumulative**, so split invoices are tracked against the remaining balance.
- **Tolerance:** up to **{config.TOLERANCE_PCT:.0%} and no more than {fmt(config.TOLERANCE_ABS)}** over the PO is auto-approved (freight, rounding) and noted.
- **Line level:** unit price variance > {config.UNIT_PRICE_TOLERANCE_PCT:.0%} or quantity over PO is flagged.
- **Tax:** must equal the PO's tax rate on the subtotal; embedded (tax-inclusive) totals are split out.
- **No PO:** the agent scores the vendor's open POs and suggests the best match, but never auto-pays an inferred match.
""")
    with c2:
        st.markdown("#### Decision logic")
        st.markdown("""
Every check returns **pass / note / fail**, and each fail carries a severity:

| Outcome | When | Goes to |
|---|---|---|
| **Approved** | no failed checks (notes allowed) | next payment run |
| **Needs review** | any *review* fail | named queue: AP specialist, procurement, or AP manager for fraud |
| **Rejected** | any *blocking* fail | vendor, with a drafted message saying what to fix |

AI reads, rules decide: extraction can be AI-assisted, but every decision comes from explicit rules, so it can be explained and audited.
""")
        st.markdown("#### Assumptions")
        st.markdown(f"""
- Buyer is **{config.BUYER_NAME}**; all POs are in USD and compared **pre-tax**.
- Only approved invoices consume PO balance; pending ones don't, so a reviewer always sees the real remaining amount.
- A vendor-requested bank change is handled out of band (call-back to the number on file), never from the invoice.
- Reviewer decisions are final and recorded with a note.
""")
        mode = f"AI-assisted ({llm.provider()}) + rules" if llm.provider() else "Rules + OCR (no AI key set)"
        st.info(f"Extraction mode: **{mode}**")


# =========================================================================== shell
with st.sidebar:
    st.markdown("### 🧾 Invoice Autopilot")
    st.caption("PDF in → reasoned AP decision out")
pages = st.navigation([
    st.Page(page_process, title="Process invoice", icon="▶️", default=True),
    st.Page(page_dashboard, title="Dashboard", icon="📊"),
    st.Page(page_reference, title="POs & vendors", icon="📁"),
    st.Page(page_how, title="How it works", icon="🧭"),
])
with st.sidebar:
    runs = store.runs()
    st.divider()
    pending = int((runs.status == "PENDING_REVIEW").sum()) if not runs.empty else 0
    st.markdown(f"**{len(runs)}** processed · **{pending}** awaiting review")
    st.caption(f"Extraction: {'AI-assisted · ' + llm.provider() if llm.provider() else 'rules + OCR'}")
    if st.button("Reset demo data", width="stretch", help="Clears all runs so PO balances start fresh."):
        store.reset()
        st.session_state.pop("last_run_id", None)
        st.toast("Demo data reset")
        st.rerun()
pages.run()
