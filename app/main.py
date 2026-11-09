"""Praxion Deposit Desk: web app (FastAPI + server-rendered templates)."""
from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import policy as P
from .config import Settings, load_settings
from .db import DB
from .desk import SEATS, STAGES, Desk, DeskError
from .drafter import build_drafter
from .paypal import build_gateway
from .ratelimit import RateLimiter
from .seed import seed

HERE = Path(__file__).parent


def _money(v) -> str:
    try:
        return f"${Decimal(str(v)):,.2f}"
    except Exception:  # noqa: BLE001
        return str(v)


def create_app(settings: Settings | None = None, *, gateway=None, drafter=None) -> FastAPI:
    s = settings or load_settings()
    db = DB(s.database_path)
    seed(db)
    gw = gateway or build_gateway(s)
    desk = Desk(db, gw, drafter or build_drafter(s), s)
    limiter = RateLimiter(s.rate_limit_enabled)

    app = FastAPI(title="Praxion Deposit Desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.desk, app.state.db, app.state.gateway, app.state.settings = desk, db, gw, s
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters["money"] = _money
    tpl.env.filters["pct"] = P.pct_label
    tpl.env.filters["fromjson"] = json.loads
    tpl.env.globals.update(STAGES=STAGES, settings=s, gateway_label=gw.label, llm_on=s.llm_enabled)

    def seat_of(request: Request):
        return SEATS.get(request.cookies.get("seat", "rep"), SEATS["rep"])

    def render(request: Request, name: str, **ctx):
        flash = request.query_params.get("msg", "")
        err = request.query_params.get("err", "")
        return tpl.TemplateResponse(request, name, {"seat": seat_of(request), "flash": flash, "err": err, **ctx})

    def back(url: str, msg: str = "", err: str = "") -> RedirectResponse:
        from urllib.parse import urlencode
        qs = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        return RedirectResponse(url + (("?" + qs) if qs else ""), status_code=303)

    def client_key(request: Request) -> str:
        # Rightmost entry is the one added by the hosting proxy; the left side is client-controlled.
        fwd = request.headers.get("x-forwarded-for", "")
        return fwd.split(",")[-1].strip() or (request.client.host if request.client else "?")

    def draft_or_404(draft_id: int):
        try:
            return desk.draft_row(draft_id)
        except DeskError:
            raise HTTPException(404) from None

    def limit(request: Request, bucket: str):
        if not limiter.allow(bucket, client_key(request)):
            raise HTTPException(429, "Too many requests. Please wait a minute.")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def pipeline(request: Request):
        rows = []
        for q in db.quotes():
            m = P.compute(q, P.DEFAULT_POLICY.default_deposit_pct)
            rows.append({"q": q, "m": m, "inv": desk.invoice(q["id"]), "draft": desk.latest_draft(q["id"])})
        feed = db.q("SELECT * FROM activity ORDER BY id DESC LIMIT 8")
        paid = db.q("SELECT amount FROM invoices WHERE status IN ('PAID','MARKED_AS_PAID')")
        stats = {
            "awaiting": sum(1 for r in rows if r["q"]["stage"] == "awaiting_approval"),
            "sent": sum(1 for r in rows if r["q"]["stage"] == "sent"),
            "collected": sum(Decimal(p["amount"]) for p in paid),
        }
        return render(request, "pipeline.html", rows=rows, feed=feed, stats=stats)

    @app.get("/quotes/{quote_id}", response_class=HTMLResponse)
    def quote_page(request: Request, quote_id: str):
        q = db.quote(quote_id)
        if not q:
            raise HTTPException(404)
        draft = desk.latest_draft(quote_id)
        flags = [P.Flag(**f) for f in json.loads(draft["flags"])] if draft else []
        can, why = P.can_approve(flags, seat_of(request).role) if draft else (False, "")
        activity = db.q("SELECT * FROM activity WHERE quote_id = ? ORDER BY id DESC", (quote_id,))
        m = P.compute(q, P.DEFAULT_POLICY.default_deposit_pct)
        return render(request, "quote.html", q=q, m=m, draft=draft, flags=flags, can_approve=can, why=why,
                      inv=desk.invoice(quote_id), activity=activity, worst=P.worst(flags))

    @app.get("/drafts/{draft_id}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, draft_id: int):
        d = draft_or_404(draft_id)
        return render(request, "edit.html", d=d, q=db.quote(d["quote_id"]), money=json.loads(d["money"]))

    @app.get("/quotes/{quote_id}/collect", response_class=HTMLResponse)
    def collect_page(request: Request, quote_id: str):
        q = db.quote(quote_id)
        inv = desk.invoice(quote_id)
        if not q or not inv:
            raise HTTPException(404)
        tasks = db.q("SELECT * FROM tasks WHERE quote_id = ? ORDER BY id DESC", (quote_id,))
        activity = db.q("SELECT * FROM activity WHERE quote_id = ? ORDER BY id DESC LIMIT 6", (quote_id,))
        return render(request, "collect.html", q=q, inv=inv, tasks=tasks, activity=activity)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        return render(request, "audit.html",
                      approvals=db.q("SELECT a.*, d.version FROM approvals a JOIN drafts d ON d.id = a.draft_id"
                                     " ORDER BY a.id DESC"),
                      invoices=db.q("SELECT quote_id, paypal_invoice_id, status, amount, request_id, created_at,"
                                    " updated_at FROM invoices ORDER BY created_at DESC"),
                      events=db.q("SELECT * FROM webhook_events ORDER BY received_at DESC"),
                      tasks=db.q("SELECT * FROM tasks ORDER BY id DESC"))

    @app.get("/how-it-works", response_class=HTMLResponse)
    def how_page(request: Request):
        return render(request, "how.html", policy=P.DEFAULT_POLICY)

    # ---------------------------------------------------------------- actions
    @app.post("/seat")
    def set_seat(request: Request, seat: str = Form(...), next: str = Form("/")):
        resp = back(next if next.startswith("/") and not next.startswith("//") else "/")
        if seat in SEATS:
            resp.set_cookie("seat", seat, httponly=True, samesite="lax", max_age=86400)
        return resp

    @app.post("/quotes/{quote_id}/draft")
    def do_draft(request: Request, quote_id: str):
        limit(request, "draft")
        try:
            desk.draft(quote_id, seat_of(request).name)
        except DeskError as e:
            return back(f"/quotes/{quote_id}", err=str(e))
        return back(f"/quotes/{quote_id}", msg="Agent drafted the deposit request. Review it below.")

    @app.post("/drafts/{draft_id}/edit")
    def do_edit(request: Request, draft_id: int, deposit_pct: str = Form(""), message: str = Form(...),
                redraft: str = Form("")):
        d = draft_or_404(draft_id)
        try:
            desk.edit(draft_id, seat_of(request).name, deposit_pct=deposit_pct, message=message,
                      redraft_words=bool(redraft))
        except DeskError as e:
            return back(f"/drafts/{draft_id}/edit", err=str(e))
        return back(f"/quotes/{d['quote_id']}", msg="Saved as a new version. Policy re-checked; approve again.")

    @app.post("/drafts/{draft_id}/decide")
    def do_decide(request: Request, draft_id: int, decision: str = Form(...), draft_hash: str = Form(...),
                  reason: str = Form("")):
        d = draft_or_404(draft_id)
        try:
            desk.decide(draft_id, {"approve": "approved", "deny": "denied"}.get(decision, decision),
                        seat_of(request), draft_hash, reason)
        except DeskError as e:
            return back(f"/quotes/{d['quote_id']}", err=str(e))
        msg = "Approved. Ready to send through PayPal." if decision == "approve" else "Denied and logged."
        return back(f"/quotes/{d['quote_id']}", msg=msg)

    @app.post("/drafts/{draft_id}/send")
    def do_send(request: Request, draft_id: int):
        limit(request, "send")
        d = draft_or_404(draft_id)
        try:
            desk.send(draft_id, seat_of(request).name)
        except DeskError as e:
            return back(f"/quotes/{d['quote_id']}", err=str(e))
        return back(f"/quotes/{d['quote_id']}/collect", msg="Invoice sent through PayPal.")

    @app.post("/quotes/{quote_id}/refresh")
    def do_refresh(request: Request, quote_id: str):
        try:
            st = desk.refresh(quote_id)
        except DeskError as e:
            return back(f"/quotes/{quote_id}/collect", err=str(e))
        return back(f"/quotes/{quote_id}/collect", msg=f"PayPal says: {st}")

    @app.post("/demo/reset")
    def do_reset(request: Request):
        limit(request, "reset")
        db.reset()
        seed(db)
        return back("/", msg="Demo reset. Fabricated sample data reloaded.")

    # ---------------------------------------------------------------- PayPal
    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        limit(request, "webhook")
        raw = await request.body()
        code, outcome = desk.handle_webhook({k.lower(): v for k, v in request.headers.items()}, raw)
        return JSONResponse({"result": outcome}, status_code=code)

    @app.get("/invoices/{quote_id}/qr.png")
    def qr(quote_id: str):
        inv = desk.invoice(quote_id)
        if not inv or not inv["qr_png"]:
            raise HTTPException(404)
        return Response(inv["qr_png"], media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/quotes/{quote_id}/status")
    def status(quote_id: str):
        q = db.quote(quote_id)
        if not q:
            raise HTTPException(404)
        inv = desk.invoice(quote_id)
        last = db.one("SELECT text, created_at FROM activity WHERE quote_id = ? ORDER BY id DESC LIMIT 1", (quote_id,))
        return {"quote_id": quote_id, "stage": q["stage"], "stage_label": STAGES.get(q["stage"], q["stage"]),
                "invoice_status": inv["status"] if inv else None,
                "latest": dict(last) if last else None}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "paypal": "mock" if s.is_mock else "sandbox", "drafter": "llm" if s.llm_enabled else "offline"}

    # ---------------------------------------------------------------- mock-only payer simulation
    def _simulate(invoice_id: str, event_type: str, status: str) -> tuple[int, str]:
        gw.mark(invoice_id, status)
        event = {
            "id": f"WH-MOCK-{uuid.uuid4().hex[:12].upper()}",
            "event_version": "1.0",
            "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "resource_type": "invoices",
            "event_type": event_type,
            "summary": f"Simulated {event_type}",
            "resource": {"invoice": gw.get_invoice(invoice_id)},
            "links": [{"href": "https://api.sandbox.paypal.com/v1/notifications/webhooks-events/mock",
                       "rel": "self", "method": "GET"}],
        }
        raw = json.dumps(event).encode()
        return desk.handle_webhook(gw.sign(raw), raw)

    @app.get("/mock-pay/{invoice_id}", response_class=HTMLResponse)
    def mock_pay_page(request: Request, invoice_id: str):
        if not s.is_mock:
            raise HTTPException(404)
        row = db.one("SELECT * FROM invoices WHERE paypal_invoice_id = ?", (invoice_id,))
        if not row:
            raise HTTPException(404)
        return render(request, "mock_pay.html", inv=row, q=db.quote(row["quote_id"]))

    @app.post("/mock-pay/{invoice_id}")
    def mock_pay(request: Request, invoice_id: str, action: str = Form("pay")):
        if not s.is_mock:
            raise HTTPException(404)
        row = db.one("SELECT * FROM invoices WHERE paypal_invoice_id = ?", (invoice_id,))
        if not row:
            raise HTTPException(404)
        if action == "refund":
            _simulate(invoice_id, "INVOICING.INVOICE.REFUNDED", "REFUNDED")
            return back(f"/quotes/{row['quote_id']}/collect", msg="Simulated refund webhook delivered.")
        _simulate(invoice_id, "INVOICING.INVOICE.PAID", "PAID")
        dest = request.query_params.get("return") or f"/quotes/{row['quote_id']}/collect"
        return back(dest if dest.startswith("/") and not dest.startswith("//") else "/",
                    msg="Simulated PayPal payment. Signed webhook verified and applied.")

    return app


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader for local runs (real environment variables win)."""
    import os
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_app() -> FastAPI:  # used by: uvicorn app.main:get_app --factory
    _load_dotenv()
    return create_app()
