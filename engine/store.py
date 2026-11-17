"""Reference data (vendor master, POs) and the run history (SQLite)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config

APPROVED_STATES = ("APPROVED", "APPROVED_BY_REVIEWER")
OPEN_STATES = APPROVED_STATES + ("PENDING_REVIEW",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    filename TEXT,
    file_hash TEXT,
    vendor_id TEXT,
    vendor_name TEXT,
    invoice_number TEXT,
    invoice_number_norm TEXT,
    invoice_date TEXT,
    po_number TEXT,
    subtotal REAL,
    tax REAL,
    total REAL,
    currency TEXT,
    decision TEXT,
    status TEXT,
    route_to TEXT,
    headline TEXT,
    duration_ms INTEGER,
    result_json TEXT,
    reviewer_note TEXT,
    reviewed_at TEXT
);
"""


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


class Store:
    def __init__(self, db_path: Path | None = None, data_dir: Path | None = None):
        self.db_path = db_path or config.DB_PATH
        self.data_dir = data_dir or config.DATA_DIR
        self.con = _connect(self.db_path)
        self.reload_reference()

    # ---------------------------------------------------------- reference
    def reload_reference(self) -> None:
        d = self.data_dir
        self.vendors = pd.read_csv(d / "vendors.csv", dtype=str).fillna("")
        self.pos = pd.read_csv(d / "purchase_orders.csv", dtype=str).fillna("")
        self.pos["tax_rate"] = self.pos["tax_rate"].astype(float)
        self.po_lines = pd.read_csv(d / "po_lines.csv", dtype={"po_number": str, "description": str})
        self.po_lines["line_total"] = self.po_lines["quantity"] * self.po_lines["unit_price"]

    def vendor(self, vendor_id: str) -> dict | None:
        rows = self.vendors[self.vendors.vendor_id == vendor_id]
        return rows.iloc[0].to_dict() if len(rows) else None

    def po(self, po_number: str) -> dict | None:
        rows = self.pos[self.pos.po_number.str.upper() == (po_number or "").upper()]
        if not len(rows):
            return None
        po = rows.iloc[0].to_dict()
        lines = self.po_lines[self.po_lines.po_number == po["po_number"]]
        po["lines"] = lines.to_dict("records")
        po["amount"] = float(lines.line_total.sum())
        po["billed"] = self.billed_to_date(po["po_number"])
        po["remaining"] = round(po["amount"] - po["billed"], 2)
        return po

    def pos_for_vendor(self, vendor_id: str) -> list[dict]:
        return [self.po(p) for p in self.pos[self.pos.vendor_id == vendor_id].po_number]

    def po_table(self) -> pd.DataFrame:
        rows = []
        for p in self.pos.po_number:
            po = self.po(p)
            v = self.vendor(po["vendor_id"]) or {}
            rows.append({
                "PO": po["po_number"], "Vendor": v.get("name", po["vendor_id"]),
                "Description": po["description"], "Issued": po["issue_date"],
                "Status": po["status"], "Partial billing": po["allow_partial"],
                "PO amount": po["amount"], "Billed": po["billed"], "Remaining": po["remaining"],
                "Used": (po["billed"] / po["amount"]) if po["amount"] else 0,
                "Requester": po["requester"],
            })
        return pd.DataFrame(rows)

    # ---------------------------------------------------------- ledger
    def billed_to_date(self, po_number: str, exclude_run: int | None = None) -> float:
        q = f"SELECT COALESCE(SUM(subtotal),0) FROM runs WHERE po_number=? AND status IN ({','.join('?' * len(APPROVED_STATES))})"
        args: list = [po_number, *APPROVED_STATES]
        if exclude_run:
            q += " AND id != ?"
            args.append(exclude_run)
        return float(self.con.execute(q, args).fetchone()[0])

    def approved_invoices_for_po(self, po_number: str) -> list[dict]:
        q = f"SELECT id, invoice_number, subtotal, created_at FROM runs WHERE po_number=? AND status IN ({','.join('?' * len(APPROVED_STATES))}) ORDER BY id"
        return [dict(r) for r in self.con.execute(q, [po_number, *APPROVED_STATES])]

    def prior_invoices(self, vendor_id: str | None = None) -> list[dict]:
        """Invoices that are live (approved or awaiting review): candidates for duplicates."""
        q = f"SELECT * FROM runs WHERE status IN ({','.join('?' * len(OPEN_STATES))})"
        args: list = list(OPEN_STATES)
        if vendor_id:
            q += " AND vendor_id=?"
            args.append(vendor_id)
        return [dict(r) for r in self.con.execute(q, args)]

    def hash_seen(self, file_hash: str) -> dict | None:
        r = self.con.execute(
            f"SELECT * FROM runs WHERE file_hash=? AND status IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY id LIMIT 1",
            [file_hash, *OPEN_STATES]).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------- runs
    def save_run(self, result: dict) -> int:
        f = result["fields"]
        status = {"APPROVED": "APPROVED", "NEEDS_REVIEW": "PENDING_REVIEW", "REJECTED": "REJECTED"}[result["decision"]]
        cur = self.con.execute(
            """INSERT INTO runs (created_at, filename, file_hash, vendor_id, vendor_name, invoice_number,
               invoice_number_norm, invoice_date, po_number, subtotal, tax, total, currency, decision, status,
               route_to, headline, duration_ms, result_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(timespec="seconds"), result["filename"], result["file_hash"],
             result.get("vendor_id"), result.get("vendor_name") or f.get("vendor_name"), f.get("invoice_number"),
             result.get("invoice_number_norm"), f.get("invoice_date"), result.get("po_number"), f.get("subtotal"),
             f.get("tax"), f.get("total"), f.get("currency"), result["decision"], status, result.get("route_to"),
             result["headline"], result["duration_ms"], json.dumps(result, default=str)))
        self.con.commit()
        return cur.lastrowid

    def review(self, run_id: int, approve: bool, note: str, po_number: str | None = None) -> None:
        status = "APPROVED_BY_REVIEWER" if approve else "REJECTED_BY_REVIEWER"
        args = [status, note, datetime.now().isoformat(timespec="seconds")]
        q = "UPDATE runs SET status=?, reviewer_note=?, reviewed_at=?"
        if po_number:
            q += ", po_number=?"
            args.append(po_number)
        q += " WHERE id=?"
        args.append(run_id)
        self.con.execute(q, args)
        self.con.commit()

    def runs(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT * FROM runs ORDER BY id DESC", self.con)

    def run(self, run_id: int) -> dict | None:
        r = self.con.execute("SELECT * FROM runs WHERE id=?", [run_id]).fetchone()
        if not r:
            return None
        d = dict(r)
        d["result"] = json.loads(d.pop("result_json"))
        return d

    def reset(self) -> None:
        self.con.execute("DELETE FROM runs")
        self.con.execute("DELETE FROM sqlite_sequence WHERE name='runs'")
        self.con.commit()
