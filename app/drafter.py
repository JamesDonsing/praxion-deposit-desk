"""Drafting adapter: turns an approved quote into customer-facing words.

Two implementations share one interface:
  * LLMDrafter     - calls any language-model provider that exposes a
                     chat-completions-style HTTP endpoint (LLM_API_URL / LLM_API_KEY).
  * OfflineDrafter - deterministic templates. Used when no LLM key is configured,
                     in tests, and as the automatic fallback if the model call fails
                     or returns output that does not match the schema.

The drafter only writes words. Amounts are computed by the rules engine and passed
in as facts; the rules engine then re-checks that the words match the money.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from .policy import Money, fmt, pct_label


class DraftText(BaseModel):
    customer_message: str = Field(min_length=20, max_length=900)
    invoice_note: str = Field(min_length=5, max_length=400)
    item_description: str = Field(min_length=3, max_length=300)
    risk_notes: list[str] = Field(default_factory=list, max_length=5)


@dataclass
class DraftResult:
    text: DraftText
    source: str  # "llm", "offline", or "offline-fallback"
    detail: str = ""


class Drafter(Protocol):
    def draft(self, quote: dict, m: Money, due_label: str) -> DraftResult: ...


def _facts(quote: dict, m: Money, due_label: str) -> dict:
    c = quote["customer"]
    return {
        "customer_first_name": c["first_name"],
        "project": quote["project"],
        "rep_name": quote["rep"]["name"],
        "company": quote.get("company", "our team"),
        "project_total": fmt(m.total),
        "deposit_amount": fmt(m.deposit),
        "deposit_percent": pct_label(m.deposit_pct),
        "balance_after_deposit": fmt(m.balance),
        "discount_percent": pct_label(m.discount_pct),
        "deposit_due": due_label,
        "next_step": quote.get("next_step", "We schedule your project and confirm the date with you."),
    }


class OfflineDrafter:
    """Deterministic drafter. Same input, same words, every time."""

    def draft(self, quote: dict, m: Money, due_label: str) -> DraftResult:
        f = _facts(quote, m, due_label)
        msg = (
            f"Hi {f['customer_first_name']}, thank you for choosing {f['company']} for your "
            f"{f['project'].lower()}. As we discussed, the deposit to reserve your project is "
            f"{f['deposit_amount']} ({f['deposit_percent']} of the {f['project_total']} project total). "
            f"You can pay securely through PayPal using the invoice link or the QR code. "
            f"Once it is received, {f['next_step'][0].lower() + f['next_step'][1:]} "
            f"The remaining {f['balance_after_deposit']} is due at completion. "
            f"Questions any time: just reply to this message. - {f['rep_name']}"
        )
        note = (f"Deposit for your {f['project'].lower()}. Due {f['deposit_due']}. "
                f"Applied in full to your project total of {f['project_total']}.")
        desc = f"{f['deposit_percent']} deposit toward {f['project']} (quote {quote['id']})"
        return DraftResult(DraftText(customer_message=msg, invoice_note=note[:400],
                                     item_description=desc[:300]), "offline")


SYSTEM_PROMPT = """You draft payment-request wording for a home-services company.
Return ONLY a JSON object with keys: customer_message, invoice_note, item_description, risk_notes.
Rules:
- Use only the facts given. Never invent amounts, dates, discounts, warranties or promises.
- If you mention money, use the exact formatted amounts given (e.g. deposit_amount).
- No pressure tactics, no urgency claims, no guarantees.
- customer_message: warm, professional, under 120 words, signed with the rep's first name.
- invoice_note: one or two sentences shown on the invoice.
- item_description: one short line for the invoice line item.
- risk_notes: up to 3 short notes for the approving manager if anything in the facts looks
  unusual (for example a large discount). Empty list if nothing stands out."""


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("no JSON object in model output")
    return json.loads(match.group(0))


class LLMDrafter:
    """Calls a chat-completions-style endpoint. Falls back to OfflineDrafter on any problem."""

    def __init__(self, url: str, api_key: str, model: str = "", timeout: float = 20.0,
                 transport: httpx.BaseTransport | None = None):
        self.url, self.api_key, self.model, self.timeout = url, api_key, model, timeout
        self.transport = transport
        self.fallback = OfflineDrafter()

    def draft(self, quote: dict, m: Money, due_label: str) -> DraftResult:
        facts = _facts(quote, m, due_label)
        body = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Facts:\n" + json.dumps(facts, indent=2)},
            ],
            "temperature": 0.2,
            "max_tokens": 600,
        }
        if self.model:
            body["model"] = self.model
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                r = client.post(self.url, json=body,
                                headers={"Authorization": f"Bearer {self.api_key}"})
                r.raise_for_status()
                data = r.json()
            content = data["choices"][0]["message"]["content"]
            text = DraftText(**_extract_json(content))
            return DraftResult(text, "llm")
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            result = self.fallback.draft(quote, m, due_label)
            result.source = "offline-fallback"
            result.detail = type(exc).__name__
            return result


def build_drafter(settings) -> Drafter:
    if settings.llm_enabled:
        return LLMDrafter(settings.llm_api_url, settings.llm_api_key, settings.llm_model)
    return OfflineDrafter()
