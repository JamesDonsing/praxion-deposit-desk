import json
from datetime import date

import httpx
import pytest

from app import policy as P
from app.config import load_settings
from app.drafter import LLMDrafter, OfflineDrafter
from app.seed import sample_quotes

Q = sample_quotes(date(2026, 11, 2))[0]
M = P.agent_deposit(Q)


def _llm(content=None, status=200):
    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer k"
        sent = json.loads(req.content)
        assert "$5,258.25" in sent["messages"][1]["content"]  # amounts arrive as facts
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return LLMDrafter("https://llm.example.com/v1/chat", "k", "m", transport=httpx.MockTransport(handler))


GOOD = json.dumps({
    "customer_message": "Hi Avery, thanks for choosing us. Your deposit is $5,258.25; pay with the PayPal link. - Dana",
    "invoice_note": "Deposit for your roof replacement.",
    "item_description": "30% deposit toward roof replacement",
    "risk_notes": [],
})


def test_offline_drafter_is_deterministic_and_passes_policy():
    a = OfflineDrafter().draft(Q, M, "Nov 5, 2026")
    b = OfflineDrafter().draft(Q, M, "Nov 5, 2026")
    assert a.text == b.text and a.source == "offline"
    assert P.worst(P.evaluate(Q, M, a.text.customer_message, today=date(2026, 11, 2))) == "clear"


def test_llm_drafter_valid_output():
    r = _llm("Here you go:\n" + GOOD).draft(Q, M, "Nov 5, 2026")
    assert r.source == "llm" and r.text.customer_message.startswith("Hi Avery")


@pytest.mark.parametrize("content,status", [("not json", 200), (json.dumps({"customer_message": "x"}), 200), (None, 500)])
def test_llm_drafter_falls_back(content, status):
    r = _llm(content, status).draft(Q, M, "Nov 5, 2026")
    assert r.source == "offline-fallback"


def test_model_cannot_slip_a_wrong_amount_past_the_rules():
    bad = json.loads(GOOD)
    bad["customer_message"] = "Hi Avery, your deposit is only $500.00 today, a special price. - Dana"
    r = _llm(json.dumps(bad)).draft(Q, M, "Nov 5, 2026")
    flags = P.evaluate(Q, M, r.text.customer_message, today=date(2026, 11, 2))
    assert P.worst(flags) == P.BLOCK


def test_config_modes(monkeypatch):
    assert load_settings().paypal_env == "mock"
    monkeypatch.setenv("PAYPAL_ENV", "live")
    with pytest.raises(RuntimeError, match="sandbox-only"):
        load_settings()
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")
    with pytest.raises(RuntimeError):
        load_settings()
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "y")
    s = load_settings()
    assert s.paypal_env == "sandbox" and s.paypal_api_base == "https://api-m.sandbox.paypal.com"
