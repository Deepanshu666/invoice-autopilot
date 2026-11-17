"""Optional AI-assisted extraction.

The process runs fully without an API key (rules + OCR). If ANTHROPIC_API_KEY or
OPENAI_API_KEY is set, an LLM also reads the invoice text and its reading is
cross-checked against the rule-based one: it can fill gaps, and any disagreement
is surfaced for review rather than silently trusted. Decisions always stay rule-based.
"""
from __future__ import annotations

import json
import os
import re

PROMPT = """You extract fields from vendor invoices. Return ONLY a JSON object with these keys
(use null when a value is not present on the invoice; do not guess):
invoice_number, invoice_date (YYYY-MM-DD), due_date (YYYY-MM-DD), po_number, vendor_name, tax_id,
currency (ISO code), subtotal (number), tax (number), total (number), bank_account (digits only).

Invoice text:
<<<
{text}
>>>"""


def provider() -> str | None:
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return None


def extract_with_llm(text: str, timeout: int = 30) -> dict:
    import requests

    p = provider()
    prompt = PROMPT.format(text=text[:12000])
    if p == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.getenv("LLM_MODEL", "claude-haiku-4-5"), "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout)
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json().get("content", []))
    elif p == "openai":
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            json={"model": os.getenv("LLM_MODEL", "gpt-4o-mini"), "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout)
        r.raise_for_status()
        out = r.json()["choices"][0]["message"]["content"]
    else:
        raise RuntimeError("No LLM API key configured")
    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {}
