"""PayPal gateway: Invoicing v2 + Webhooks, sandbox only.

SandboxPayPal talks to https://api-m.sandbox.paypal.com with OAuth client credentials.
MockPayPal simulates the same calls locally (no network) for tests, CI and the
offline demo. Both expose the same methods so the rest of the app cannot tell
them apart, except for the label shown in the UI.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import re
import threading
import time
import uuid
import zlib
from urllib.parse import urlparse

import httpx
import segno

SANDBOX_CERT_HOSTS = {"api.sandbox.paypal.com", "api-m.sandbox.paypal.com"}
WEBHOOK_HEADERS = ("paypal-transmission-id", "paypal-transmission-time",
                   "paypal-transmission-sig", "paypal-cert-url", "paypal-auth-algo")
MOCK_ALGO = "HMACSHA256MOCK"


class PayPalError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None):
        super().__init__(message)
        self.status, self.debug_id = status, debug_id


def local_qr_png(url: str, scale: int = 8) -> bytes:
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=scale, border=2,
                                    dark="#141414", light="#ffffff")
    return buf.getvalue()


def payer_url_from(invoice: dict) -> str:
    url = invoice.get("detail", {}).get("metadata", {}).get("recipient_view_url", "")
    if url:
        return url
    for link in invoice.get("links", []) or []:
        if link.get("rel") in ("payer-view", "recipient-view"):
            return link.get("href", "")
    return ""


def decode_qr_response(content: bytes, content_type: str) -> bytes:
    """generate-qr-code returns a PNG; depending on the client it arrives as raw
    bytes, a base64 string, or a multipart body holding a base64 string."""
    if content.startswith(b"\x89PNG"):
        return content
    text = content.decode("ascii", errors="ignore")
    blocks, cur = [], []
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 8 and re.fullmatch(r"[A-Za-z0-9+/=]+", line):
            cur.append(line)
        elif cur:
            blocks.append("".join(cur))
            cur = []
    if cur:
        blocks.append("".join(cur))
    for cand in sorted(blocks, key=len, reverse=True):
        try:
            raw = base64.b64decode(cand, validate=True)
        except (binascii.Error, ValueError):
            continue
        if raw.startswith(b"\x89PNG"):
            return raw
    raise PayPalError(f"Could not decode QR image ({content_type or 'unknown type'})")


def webhook_headers(headers) -> dict:
    return {k: headers.get(k, "") for k in WEBHOOK_HEADERS}


def is_sandbox_event(event: dict, h: dict) -> bool:
    """Reject anything that does not come from the PayPal sandbox."""
    host = urlparse(h.get("paypal-cert-url", "")).hostname or ""
    if host not in SANDBOX_CERT_HOSTS:
        return False
    for link in event.get("links", []) or []:
        lh = urlparse(link.get("href", "")).hostname or ""
        if lh and "sandbox" not in lh:
            return False
    return True


class SandboxPayPal:
    label = "PayPal sandbox"
    is_mock = False

    def __init__(self, settings, transport: httpx.BaseTransport | None = None):
        self.s = settings
        self.base = settings.paypal_api_base
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()
        self.client = httpx.Client(base_url=self.base, timeout=20.0, transport=transport)

    # -- auth -------------------------------------------------------------
    def _access_token(self) -> str:
        with self._lock:
            if self._token and self._token[1] > time.time() + 60:
                return self._token[0]
            r = self.client.post("/v1/oauth2/token", data={"grant_type": "client_credentials"},
                                 auth=(self.s.paypal_client_id, self.s.paypal_client_secret))
            if r.status_code != 200:
                raise PayPalError("PayPal OAuth failed", r.status_code, r.headers.get("paypal-debug-id"))
            data = r.json()
            self._token = (data["access_token"], time.time() + int(data.get("expires_in", 300)))
            return self._token[0]

    def _req(self, method: str, path: str, *, json=None, request_id: str | None = None,
             prefer: bool = False, accept_json: bool = True) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if prefer:
            headers["Prefer"] = "return=representation"
        if accept_json:
            headers["Accept"] = "application/json"
        r = self.client.request(method, path, json=json, headers=headers)
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = body.get("message") or body.get("name") or ""
                if body.get("details"):
                    detail += ": " + "; ".join(d.get("description", d.get("issue", "")) for d in body["details"])
            except ValueError:
                detail = r.text[:200]
            raise PayPalError(f"PayPal {method} {path} -> {r.status_code} {detail}".strip(),
                              r.status_code, r.headers.get("paypal-debug-id"))
        return r

    # -- invoicing ---------------------------------------------------------
    def create_invoice(self, payload: dict, request_id: str) -> dict:
        r = self._req("POST", "/v2/invoicing/invoices", json=payload, request_id=request_id, prefer=True)
        data = r.json()
        if "id" not in data:  # older behaviour: 201 with only a link to the new invoice
            href = next((l["href"] for l in data.get("links", []) if l.get("rel") == "self"), data.get("href", ""))
            data = self.get_invoice(href.rstrip("/").split("/")[-1])
        return data

    def send_invoice(self, invoice_id: str, request_id: str) -> str:
        r = self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                      json={"send_to_recipient": True, "send_to_invoicer": False},
                      request_id=request_id)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return data.get("href", "") if isinstance(data, dict) else ""

    def get_invoice(self, invoice_id: str) -> dict:
        return self._req("GET", f"/v2/invoicing/invoices/{invoice_id}").json()

    def qr_png(self, invoice_id: str, payer_url: str = "") -> bytes:
        try:
            r = self._req("POST", f"/v2/invoicing/invoices/{invoice_id}/generate-qr-code",
                          json={"width": 400, "height": 400, "action": "pay"}, accept_json=False)
            return decode_qr_response(r.content, r.headers.get("content-type", ""))
        except PayPalError:
            if payer_url:  # same destination, rendered locally
                return local_qr_png(payer_url)
            raise

    # -- webhooks ------------------------------------------------------------
    def verify_webhook(self, headers: dict, event: dict, raw_body: bytes = b"") -> bool:
        """Ask PayPal to verify the signature (POST /v1/notifications/verify-webhook-signature)."""
        if not self.s.paypal_webhook_id:
            return False
        h = webhook_headers(headers)
        if not all(h.values()) or not is_sandbox_event(event, h):
            return False
        body = {
            "auth_algo": h["paypal-auth-algo"],
            "cert_url": h["paypal-cert-url"],
            "transmission_id": h["paypal-transmission-id"],
            "transmission_sig": h["paypal-transmission-sig"],
            "transmission_time": h["paypal-transmission-time"],
            "webhook_id": self.s.paypal_webhook_id,
            "webhook_event": event,
        }
        try:
            r = self._req("POST", "/v1/notifications/verify-webhook-signature", json=body)
        except PayPalError:
            return False
        return r.json().get("verification_status") == "SUCCESS"


class MockPayPal:
    """In-process stand-in for the PayPal sandbox. Makes no network calls.

    Webhooks are signed with HMAC-SHA256 over the same string PayPal signs
    (transmission_id|transmission_time|webhook_id|crc32(body)), so the
    verification path is exercised end to end."""

    label = "Mock mode (no PayPal calls)"
    is_mock = True
    webhook_id = "WH-MOCK-LOCAL"
    cert_url = "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-MOCK-LOCAL"

    def __init__(self, settings):
        self.s = settings
        self.invoices: dict[str, dict] = {}
        self.by_request_id: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def create_invoice(self, payload: dict, request_id: str) -> dict:
        self.calls.append(("create_invoice", request_id))
        if request_id in self.by_request_id:  # PayPal-Request-Id idempotency
            return self.invoices[self.by_request_id[request_id]]
        self._n += 1
        inv_id = f"INV2-MOCK-{uuid.uuid4().hex[:4].upper()}-{self._n:04d}"
        payer = f"{self.s.public_base_url}/mock-pay/{inv_id}"
        invoice = {
            "id": inv_id, "status": "DRAFT",
            "detail": {**payload["detail"], "invoice_number": f"PDD-{1000 + self._n}",
                       "metadata": {"recipient_view_url": payer}},
            "invoicer": payload.get("invoicer", {}),
            "primary_recipients": payload.get("primary_recipients", []),
            "items": payload["items"],
            "amount": {"currency_code": "USD",
                       "value": payload["items"][0]["unit_amount"]["value"]},
            "links": [{"href": payer, "rel": "payer-view", "method": "GET"}],
        }
        self.invoices[inv_id] = invoice
        self.by_request_id[request_id] = inv_id
        return invoice

    def send_invoice(self, invoice_id: str, request_id: str) -> str:
        self.calls.append(("send_invoice", request_id))
        inv = self.invoices[invoice_id]
        if inv["status"] == "DRAFT":
            inv["status"] = "SENT"
        return payer_url_from(inv)

    def get_invoice(self, invoice_id: str) -> dict:
        if invoice_id not in self.invoices:
            raise PayPalError("Mock invoice not found (mock state resets on restart)", 404)
        return self.invoices[invoice_id]

    def qr_png(self, invoice_id: str, payer_url: str = "") -> bytes:
        return local_qr_png(payer_url or payer_url_from(self.get_invoice(invoice_id)))

    # -- webhooks --------------------------------------------------------------
    def _signed_string(self, h: dict, raw_body: bytes) -> bytes:
        crc = zlib.crc32(raw_body) & 0xFFFFFFFF
        return f"{h['paypal-transmission-id']}|{h['paypal-transmission-time']}|{self.webhook_id}|{crc}".encode()

    def sign(self, raw_body: bytes) -> dict:
        h = {
            "paypal-transmission-id": uuid.uuid4().hex,
            "paypal-transmission-time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "paypal-cert-url": self.cert_url,
            "paypal-auth-algo": MOCK_ALGO,
        }
        sig = hmac.new(self.s.mock_webhook_secret.encode(), self._signed_string(h, raw_body), hashlib.sha256)
        h["paypal-transmission-sig"] = base64.b64encode(sig.digest()).decode()
        return h

    def verify_webhook(self, headers: dict, event: dict, raw_body: bytes = b"") -> bool:
        h = webhook_headers(headers)
        if not all(h.values()) or h["paypal-auth-algo"] != MOCK_ALGO or not is_sandbox_event(event, h):
            return False
        expected = hmac.new(self.s.mock_webhook_secret.encode(), self._signed_string(h, raw_body), hashlib.sha256)
        try:
            given = base64.b64decode(h["paypal-transmission-sig"])
        except (binascii.Error, ValueError):
            return False
        return hmac.compare_digest(expected.digest(), given)

    # -- helpers used by the simulated payer page --------------------------------
    def mark(self, invoice_id: str, status: str) -> dict:
        inv = self.get_invoice(invoice_id)
        inv["status"] = status
        return inv


def build_gateway(settings):
    return MockPayPal(settings) if settings.is_mock else SandboxPayPal(settings)
