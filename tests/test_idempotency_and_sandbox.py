import base64
import json
from dataclasses import replace

import httpx
import pytest

from app.db import DB
from app.desk import Desk, DeskError
from app.drafter import OfflineDrafter
from app.paypal import SandboxPayPal, decode_qr_response, local_qr_png
from app.seed import seed
from tests.conftest import approve_and_send, signed_event


def test_double_send_returns_same_invoice(desk, gw, rep):
    d, first = approve_and_send(desk, "Q-1042", rep)
    second = desk.send(d, rep.name)
    assert first["paypal_invoice_id"] == second["paypal_invoice_id"]
    assert [c[0] for c in gw.calls].count("create_invoice") == 1
    assert len(gw.invoices) == 1


def test_paypal_request_id_dedupes_at_gateway(gw):
    payload = {"detail": {"currency_code": "USD"}, "items": [{"unit_amount": {"value": "10.00"}}]}
    a = gw.create_invoice(payload, "req-1")
    b = gw.create_invoice(payload, "req-1")
    assert a["id"] == b["id"] and len(gw.invoices) == 1


def test_new_request_blocked_while_invoice_is_live(desk, rep):
    approve_and_send(desk, "Q-1042", rep)
    d = desk.draft("Q-1042", rep.name)
    flags = json.loads(desk.draft_row(d)["flags"])
    assert any(f["code"] == "invoice_exists" and f["severity"] == "block" for f in flags)
    with pytest.raises(DeskError):
        desk.decide(d, "approved", rep, desk.draft_row(d)["hash"])


def test_redrafting_allowed_after_refund(desk, gw, rep):
    _, inv = approve_and_send(desk, "Q-1042", rep)
    desk.handle_webhook(*signed_event(gw, inv["paypal_invoice_id"], "INVOICING.INVOICE.REFUNDED", "WH-R"))
    d = desk.draft("Q-1042", rep.name)
    assert not any(f["code"] == "invoice_exists" for f in json.loads(desk.draft_row(d)["flags"]))


# ---- Full flow through the real sandbox client, with PayPal faked at the HTTP layer ----

class FakePayPalSandbox:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.invoices: dict[str, dict] = {}
        self.qr = base64.b64encode(local_qr_png("https://www.sandbox.paypal.com/invoice/p/#X")).decode()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        p = req.url.path
        if p == "/v1/oauth2/token":
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "A21-test", "expires_in": 32400})
        assert req.headers["authorization"] == "Bearer A21-test"
        if p == "/v2/invoicing/invoices" and req.method == "POST":
            rid = req.headers["paypal-request-id"]
            inv_id = "INV2-SBX-" + str(abs(hash(rid)) % 10000)
            body = json.loads(req.content)
            self.invoices[inv_id] = {"id": inv_id, "status": "DRAFT", "detail": {**body["detail"], "metadata": {
                "recipient_view_url": f"https://www.sandbox.paypal.com/invoice/p/#{inv_id}"}}, "items": body["items"]}
            return httpx.Response(201, json=self.invoices[inv_id])
        if p.endswith("/send"):
            inv_id = p.split("/")[-2]
            self.invoices[inv_id]["status"] = "SENT"
            return httpx.Response(200, json={"href": self.invoices[inv_id]["detail"]["metadata"]["recipient_view_url"],
                                             "rel": "payer-view", "method": "GET"})
        if p.endswith("/generate-qr-code"):
            return httpx.Response(200, content=("--b\r\nContent-Type: text/plain\r\n\r\n" + self.qr + "\r\n--b--").encode(),
                                  headers={"content-type": "multipart/related; boundary=b"})
        if p.startswith("/v2/invoicing/invoices/") and req.method == "GET":
            return httpx.Response(200, json=self.invoices[p.split("/")[-1]])
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})


def test_sandbox_client_full_send_flow(settings, rep):
    fake = FakePayPalSandbox()
    s = replace(settings, paypal_env="sandbox", paypal_client_id="id", paypal_client_secret="sec",
                sandbox_buyer_email="buyer-sandbox@example.com")
    pp = SandboxPayPal(s, transport=httpx.MockTransport(fake))
    db = DB(s.database_path)
    seed(db)
    desk = Desk(db, pp, OfflineDrafter(), s)
    d, inv = approve_and_send(desk, "Q-1042", rep)

    create = next(r for r in fake.requests if r.url.path == "/v2/invoicing/invoices")
    assert create.url.host == "api-m.sandbox.paypal.com"
    assert create.headers["prefer"] == "return=representation"
    assert create.headers["paypal-request-id"].startswith("pdd-Q-1042-a")
    body = json.loads(create.content)
    assert body["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "5258.25"}
    # In sandbox mode the sample customer's address is never used.
    assert body["primary_recipients"][0]["billing_info"]["email_address"] == "buyer-sandbox@example.com"
    send = next(r for r in fake.requests if r.url.path.endswith("/send"))
    assert json.loads(send.content) == {"send_to_recipient": True, "send_to_invoicer": False}
    assert inv["status"] == "SENT"
    assert inv["payer_url"].startswith("https://www.sandbox.paypal.com/invoice/p/#")
    assert bytes(inv["qr_png"]).startswith(b"\x89PNG")
    # Token is cached: exactly one OAuth call for the whole flow.
    assert sum(r.url.path == "/v1/oauth2/token" for r in fake.requests) == 1
    # Status check fallback reads from PayPal.
    fake.invoices[inv["paypal_invoice_id"]]["status"] = "PAID"
    assert desk.refresh("Q-1042") == "PAID"
    assert db.quote("Q-1042")["stage"] == "paid"


def test_paypal_error_does_not_mark_sent(settings, rep):
    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "message": "bad"},
                              headers={"paypal-debug-id": "dbg1"})
    s = replace(settings, paypal_env="sandbox", paypal_client_id="i", paypal_client_secret="s")
    db = DB(s.database_path)
    seed(db)
    desk = Desk(db, SandboxPayPal(s, transport=httpx.MockTransport(handler)), OfflineDrafter(), s)
    d = desk.draft("Q-1042", rep.name)
    desk.decide(d, "approved", rep, desk.draft_row(d)["hash"])
    with pytest.raises(DeskError, match="PayPal returned an error"):
        desk.send(d, rep.name)
    assert desk.invoice("Q-1042") is None
    assert desk.draft_row(d)["status"] == "approved"


def test_qr_decoding_variants():
    png = local_qr_png("https://example.com")
    assert decode_qr_response(png, "image/png") == png
    b64 = base64.b64encode(png)
    assert decode_qr_response(b64, "text/plain") == png
    assert decode_qr_response(b"--x\r\n\r\n" + b64 + b"\r\n--x--", "multipart/related") == png
