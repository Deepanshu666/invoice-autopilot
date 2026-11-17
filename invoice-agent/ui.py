"""Shared UI pieces: styling, live stage cards, decision banner, result tabs, review actions."""
from __future__ import annotations

import html
import io

import pandas as pd
import streamlit as st

from engine.extract import FIELD_LABELS
from engine.pipeline import STAGES, fmt, mask

CSS = """
<style>
.block-container {padding-top: 3.2rem; max-width: 1380px;}
h1, h2, h3 {letter-spacing: -0.01em;}
.app-title {font-size: 1.9rem; font-weight: 750; margin: 0;}
.app-sub {color: #64748b; margin: 2px 0 18px 0; font-size: 0.95rem;}
.stage {border: 1px solid #e5e7eb; border-left: 4px solid #cbd5e1; border-radius: 10px;
        padding: 9px 14px; margin-bottom: 8px; background: #fff; transition: all .2s;}
.stage.pending {opacity: .5;}
.stage.running {border-left-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,.10); animation: pulse 1.1s ease-in-out infinite;}
.stage.pass {border-left-color: #16a34a;} .stage.warn {border-left-color: #d97706;} .stage.fail {border-left-color: #dc2626;}
@keyframes pulse {0%,100% {background:#fff;} 50% {background:#eef2ff;}}
.stage-head {display: flex; align-items: center; gap: 10px; font-weight: 620; font-size: 0.95rem; color: #0f172a;}
.stage-head .ms {margin-left: auto; color: #94a3b8; font-weight: 500; font-size: 12px;}
.dot {width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
      font-size: 12px; color: #fff; background: #cbd5e1; flex: none;}
.dot.running {background: #4f46e5;} .dot.pass {background: #16a34a;} .dot.warn {background: #d97706;} .dot.fail {background: #dc2626;}
.chk {font-size: 13px; color: #334155; margin: 5px 0 0 32px; line-height: 1.4;}
.chk .k {font-weight: 600;}
.chk .s-pass {color: #16a34a;} .chk .s-warn {color: #b45309;} .chk .s-fail {color: #dc2626;} .chk .s-info {color: #64748b;}
.decision {border-radius: 14px; padding: 18px 22px; margin: 6px 0 14px 0;}
.decision .label {font-size: 12px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;}
.decision .head {font-size: 1.15rem; font-weight: 650; color: #0f172a; margin: 4px 0 10px 0; line-height: 1.4;}
.decision .why {font-size: 13.5px; color: #475569; margin: -4px 0 12px 0; line-height: 1.45;}
.decision .meta {display: flex; gap: 28px; flex-wrap: wrap; font-size: 13px; color: #334155;}
.decision .meta b {display: block; font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: .05em;}
.decision.APPROVED {background: #f0fdf4; border: 1px solid #bbf7d0;} .decision.APPROVED .label {color: #15803d;}
.decision.NEEDS_REVIEW {background: #fffbeb; border: 1px solid #fde68a;} .decision.NEEDS_REVIEW .label {color: #b45309;}
.decision.REJECTED {background: #fef2f2; border: 1px solid #fecaca;} .decision.REJECTED .label {color: #b91c1c;}
.pill {display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 650;}
.kpi {min-height: 98px; background: #fff; border: 1px solid #e5e7eb; border-radius: 12px; padding: 14px 16px;}
.kpi .v {font-size: 1.6rem; font-weight: 720; color: #0f172a;} .kpi .l {font-size: 12px; color: #64748b;}
.kpi .h {font-size: 12px; color: #94a3b8;}
.hint {background: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 10px; padding: 10px 14px; color: #475569; font-size: 13px;}
.ledger {display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px;}
.ledger div {background: #f8fafc; border-radius: 8px; padding: 8px 10px; font-size: 12px; color: #64748b;}
.ledger div b {display: block; font-size: 15px; color: #0f172a;}
</style>
"""

STATUS_STYLE = {
    "APPROVED": ("#dcfce7", "#166534", "Approved"),
    "APPROVED_BY_REVIEWER": ("#ccfbf1", "#115e59", "Approved by reviewer"),
    "PENDING_REVIEW": ("#fef3c7", "#92400e", "Pending review"),
    "REJECTED": ("#fee2e2", "#991b1b", "Rejected"),
    "REJECTED_BY_REVIEWER": ("#fecaca", "#7f1d1d", "Rejected by reviewer"),
    "NEEDS_REVIEW": ("#fef3c7", "#92400e", "Needs review"),
}
ICON = {"pending": "", "running": "…", "pass": "✓", "warn": "!", "fail": "✕", "info": "i"}


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def pill(status: str) -> str:
    bg, fg, label = STATUS_STYLE.get(status, ("#e2e8f0", "#334155", status))
    return f"<span class='pill' style='background:{bg};color:{fg}'>{label}</span>"


def stage_html(idx: int, label: str, state: str, checks: list[dict], ms: int | None = None) -> str:
    items = "".join(
        f"<div class='chk'><span class='s-{c['status']}'>{ICON.get(c['status'], '')}</span> "
        f"<span class='k'>{html.escape(c['label'])}</span> — {html.escape(str(c['detail']))}</div>"
        for c in checks)
    right = f"<span class='ms'>{ms} ms</span>" if ms is not None else (
        "<span class='ms'>running…</span>" if state == "running" else "")
    return (f"<div class='stage {state}'><div class='stage-head'><span class='dot {state}'>{ICON.get(state, '')}</span>"
            f"{idx}. {html.escape(label)}{right}</div>{items}</div>")


def render_stages_static(result: dict):
    by_stage: dict[str, list] = {}
    for c in result["checks"]:
        by_stage.setdefault(c["stage"], []).append(c)
    timing = {s["stage"]: s for s in result["stages"]}
    out = ""
    for i, (key, label) in enumerate(STAGES, 1):
        s = timing.get(key)
        out += stage_html(i, label, s["status"] if s else "pending", by_stage.get(key, []), s["ms"] if s else None)
    st.markdown(out, unsafe_allow_html=True)


def decision_banner(result: dict, status: str | None = None):
    d = result["decision"]
    label = {"APPROVED": "Approved", "NEEDS_REVIEW": "Needs review", "REJECTED": "Rejected"}[d]
    f = result["fields"]
    rd = result.get("reason_detail")
    detail = f"<div class='why'>{html.escape(rd)}</div>" if rd and rd != result["headline"] else ""
    extra = f"&nbsp;&nbsp;{pill(status)}" if status and status not in ("APPROVED", "PENDING_REVIEW", "REJECTED") else ""
    st.markdown(f"""
<div class='decision {d}'>
  <div class='label'>Decision · {label}{extra}</div>
  <div class='head'>{html.escape(result['headline'])}</div>
  {detail}
  <div class='meta'>
    <div><b>Routed to</b>{html.escape(result['route_to'] or '—')}</div>
    <div><b>Next action</b>{html.escape(result['next_action'])}</div>
    <div><b>Vendor</b>{html.escape(result.get('vendor_name') or '—')}</div>
    <div><b>Total</b>{fmt(f.get('total'))}</div>
    <div><b>Processed in</b>{result['duration_ms'] / 1000:.2f}s</div>
  </div>
</div>""", unsafe_allow_html=True)


def fields_table(result: dict) -> pd.DataFrame:
    f, src = result["fields"], result["fields"].get("sources", {})
    rows = []
    for k, label in FIELD_LABELS.items():
        v = f.get(k)
        if k in ("subtotal", "tax", "total"):
            v = fmt(v) if v is not None else None
        if k == "bank_account":
            v = mask(v) if v else None
        if k == "invoice_date" and f.get("invoice_date_raw") and f["invoice_date_raw"] != f.get("invoice_date"):
            v = f"{f['invoice_date']}  (printed '{f['invoice_date_raw']}')"
        rows.append({"Field": label, "Value": v if v not in (None, "") else "— not found",
                     "How obtained": src.get(k, "printed" if v not in (None, "") else "")})
    return pd.DataFrame(rows)


def render_result_tabs(result: dict, key: str):
    tabs = st.tabs(["Extracted data", "PO match", "All checks", "Vendor message", "Raw text"])
    f = result["fields"]
    with tabs[0]:
        c1, c2 = st.columns([1.15, 1])
        c1.dataframe(fields_table(result), hide_index=True, width="stretch")
        items = f.get("line_items") or []
        if items:
            df = pd.DataFrame(items).rename(columns={"description": "Description", "quantity": "Qty",
                                                     "unit_price": "Unit price", "amount": "Amount"})
            c2.dataframe(df, hide_index=True, width="stretch",
                         column_config={"Unit price": st.column_config.NumberColumn(format="dollar"),
                                        "Amount": st.column_config.NumberColumn(format="dollar")})
        else:
            c2.info("No line items detected.")
        meta = f"Read via **{result['method']}**"
        if result.get("ocr_confidence") is not None:
            meta += f" · OCR confidence **{result['ocr_confidence']:.0f}%**"
        c2.caption(meta)
    with tabs[1]:
        snap = result.get("po_snapshot")
        po = result.get("po_number") or result.get("suggested_po")
        if po and snap:
            st.markdown(f"**{po}**" + (" — *suggested match, awaiting confirmation*" if result.get("suggested_po") else ""))
            st.markdown(
                "<div class='ledger'>" + "".join(f"<div>{lbl}<b>{fmt(v)}</b></div>" for lbl, v in [
                    ("PO amount", snap["amount"]), ("Billed before", snap["billed_before"]),
                    ("This invoice (pre-tax)", snap["this_invoice"]), ("Billed after", snap["billed_after"]),
                    ("Remaining after", snap["remaining_after"])]) + "</div>", unsafe_allow_html=True)
        if result.get("po_candidates"):
            st.markdown("**PO candidates scored** (invoice had no PO reference)")
            st.dataframe(pd.DataFrame([{"PO": c["po_number"], "Confidence": c["score"], "Why": ", ".join(c["why"]),
                                        "Description": c["description"]} for c in result["po_candidates"]]),
                         hide_index=True, width="stretch",
                         column_config={"Confidence": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%d")})
        if not po and not result.get("po_candidates"):
            st.info("No PO matching performed.")
    with tabs[2]:
        df = pd.DataFrame(result["checks"])[["stage", "label", "status", "severity", "detail"]]
        df["stage"] = df["stage"].map(dict(STAGES))
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"detail": st.column_config.TextColumn(width="large")})
    with tabs[3]:
        msg = result.get("vendor_message")
        if msg:
            st.markdown(f"**To:** {msg['to']}  \n**Subject:** {msg['subject']}")
            st.code(msg["body"], language=None)
            st.caption("Drafted automatically. Copy with the icon at the top right of the box.")
        elif result["decision"] == "NEEDS_REVIEW":
            st.info("Internal exception — no message goes to the vendor until a reviewer decides. "
                    "For bank-detail changes, contact the vendor only via the phone number on file.")
        else:
            st.success("Nothing to send — invoice approved.")
    with tabs[4]:
        st.code(result.get("text") or "(no text)", language=None)


def render_review_actions(store, run_id: int, result: dict, status: str, key: str):
    if status != "PENDING_REVIEW":
        return
    with st.container(border=True):
        st.markdown(f"**Reviewer action** · queue: {result['route_to']}")
        c1, c2 = st.columns([1, 2])
        po_choice = None
        vendor_id = result.get("vendor_id")
        if result.get("suggested_po") and vendor_id:
            options = [p["po_number"] for p in store.pos_for_vendor(vendor_id)]
            po_choice = c1.selectbox("Confirm PO", options, index=options.index(result["suggested_po"]), key=f"po_{key}")
        default_note = {
            "AP Manager - fraud check": "Called vendor on number in master — ",
            "Procurement - PO owner": "PO owner contacted — ",
        }.get(result["route_to"], "")
        note = c2.text_input("Note for the audit trail", value=default_note, key=f"note_{key}")
        b1, b2, _ = st.columns([1, 1, 3])
        if b1.button("Approve", type="primary", key=f"ap_{key}", width="stretch"):
            store.review(run_id, True, note or "Approved", po_choice)
            st.toast(f"Run #{run_id} approved" + (f" against {po_choice}" if po_choice else ""))
            st.rerun()
        if b2.button("Reject", key=f"rj_{key}", width="stretch"):
            store.review(run_id, False, note or "Rejected")
            st.toast(f"Run #{run_id} rejected")
            st.rerun()


@st.cache_data(show_spinner=False, max_entries=64)
def preview_image(data: bytes, filename: str):
    if filename.lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff")):
        return data
    from pdf2image import convert_from_bytes
    img = convert_from_bytes(data, dpi=90, first_page=1, last_page=1)[0]
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
