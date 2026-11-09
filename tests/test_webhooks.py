import json

import httpx

from app.paypal import SandboxPayPal
from tests.conftest import approve_and_send, signed_event


def _sent(desk, rep):
    _, inv = approve_and_send(desk, "Q-1042", rep)
    return inv["paypal_invoice_id"]


def test_valid_signed_paid_event_updates_job_and_queues_next_step(desk, gw, rep):
    inv_id = _sent(desk, rep)
    h, raw = signed_event(gw, inv_id)
    assert desk.handle_webhook(h, raw) == (200, "invoice -> PAID")
    assert desk.invoice("Q-1042")["status"] == "PAID"
    assert desk.db.quote("Q-1042")["stage"] == "paid"
    assert len(desk.db.q("SELECT * FROM tasks WHERE quote_id = 'Q-1042'")) == 1
    last = desk.db.one("SELECT * FROM activity WHERE quote_id='Q-1042' ORDER BY id DESC LIMIT 1")
    assert last["kind"] == "agent" and "Deposit of $5,258.25 received" in last["text"]


def test_tampered_body_is_rejected(desk, gw, rep):
    inv_id = _sent(desk, rep)
    h, raw = signed_event(gw, inv_id)
    tampered = raw.replace(b"WH-TEST-1", b"WH-TEST-9")
    assert desk.handle_webhook(h, tampered)[0] == 401
    assert desk.invoice("Q-1042")["status"] == "SENT"


def test_wrong_secret_is_rejected(desk, gw, rep, settings):
    inv_id = _sent(desk, rep)
    from dataclasses import replace
    from app.paypal import MockPayPal
    attacker = MockPayPal(replace(settings, mock_webhook_secret="guess"))
    h, raw = signed_event(attacker, inv_id)
    assert desk.handle_webhook(h, raw)[0] == 401


def test_missing_headers_rejected(desk, gw, rep):
    inv_id = _sent(desk, rep)
    _, raw = signed_event(gw, inv_id)
    assert desk.handle_webhook({}, raw)[0] == 401


def test_non_sandbox_event_rejected(desk, gw, rep):
    inv_id = _sent(desk, rep)
    h, raw = signed_event(gw, inv_id, links=[{"href": "https://api.paypal.com/v1/notifications/webhooks-events/x"}])
    assert desk.handle_webhook(h, raw)[0] == 401
    h, raw = signed_event(gw, inv_id)
    h["paypal-cert-url"] = "https://api.paypal.com/v1/notifications/certs/CERT-1"
    assert desk.handle_webhook(h, raw)[0] == 401


def test_duplicate_event_processed_once(desk, gw, rep):
    inv_id = _sent(desk, rep)
    h, raw = signed_event(gw, inv_id)
    assert desk.handle_webhook(h, raw)[0] == 200
    assert desk.handle_webhook(h, raw) == (200, "duplicate ignored")
    assert len(desk.db.q("SELECT * FROM tasks")) == 1
    assert len(desk.db.q("SELECT * FROM webhook_events")) == 1


def test_unknown_invoice_is_acknowledged_but_ignored(desk, gw):
    h, raw = signed_event(gw, "INV2-NOT-OURS")
    assert desk.handle_webhook(h, raw) == (200, "unmatched invoice")


def test_refund_event(desk, gw, rep):
    inv_id = _sent(desk, rep)
    desk.handle_webhook(*signed_event(gw, inv_id))
    assert desk.handle_webhook(*signed_event(gw, inv_id, "INVOICING.INVOICE.REFUNDED", "WH-TEST-2"))[0] == 200
    assert desk.invoice("Q-1042")["status"] == "REFUNDED"
    assert desk.db.quote("Q-1042")["stage"] == "refunded"


def test_webhook_http_endpoint(client, gw):
    desk = client.app.state.desk
    from app.desk import SEATS
    _, inv = approve_and_send(desk, "Q-1042", SEATS["rep"])
    h, raw = signed_event(gw, inv["paypal_invoice_id"])
    r = client.post("/webhooks/paypal", content=raw, headers=h)
    assert r.status_code == 200
    assert client.post("/webhooks/paypal", content=raw, headers={}).status_code == 401
    assert client.post("/webhooks/paypal", content=b"not json").status_code == 400


# ---- Sandbox client: verification goes through PayPal's verify-webhook-signature API ----

SANDBOX_HEADERS = {
    "paypal-transmission-id": "a1b2c3",
    "paypal-transmission-time": "2026-11-02T18:00:00Z",
    "paypal-transmission-sig": "c2lnbmF0dXJl",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-abc",
    "paypal-auth-algo": "SHA256withRSA",
}


def _sandbox(settings, status="SUCCESS", seen=None):
    from dataclasses import replace

    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if request.url.path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": status})
        return httpx.Response(404)

    s = replace(settings, paypal_env="sandbox", paypal_client_id="id", paypal_client_secret="secret",
                paypal_webhook_id="WH-123")
    return SandboxPayPal(s, transport=httpx.MockTransport(handler))


def test_sandbox_verify_calls_paypal_with_exact_fields(settings):
    seen = []
    pp = _sandbox(settings, seen=seen)
    event = {"id": "WH-1", "event_type": "INVOICING.INVOICE.PAID"}
    assert pp.verify_webhook(SANDBOX_HEADERS, event) is True
    body = json.loads(seen[-1].content)
    assert seen[-1].url.host == "api-m.sandbox.paypal.com"
    assert body == {"auth_algo": "SHA256withRSA", "cert_url": SANDBOX_HEADERS["paypal-cert-url"],
                    "transmission_id": "a1b2c3", "transmission_sig": "c2lnbmF0dXJl",
                    "transmission_time": "2026-11-02T18:00:00Z", "webhook_id": "WH-123", "webhook_event": event}


def test_sandbox_verify_failure_and_non_sandbox_cert(settings):
    assert _sandbox(settings, status="FAILURE").verify_webhook(SANDBOX_HEADERS, {"id": "x"}) is False
    seen = []
    pp = _sandbox(settings, seen=seen)
    bad = {**SANDBOX_HEADERS, "paypal-cert-url": "https://evil.example.com/cert"}
    assert pp.verify_webhook(bad, {"id": "x"}) is False
    assert seen == []  # rejected before any network call
