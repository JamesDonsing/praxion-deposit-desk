"""The Deposit Desk agent: draft -> policy check -> human decision -> PayPal -> status.

Approve-first invariants (all enforced here, all covered by tests):
  1. Nothing reaches PayPal unless the exact draft (by SHA-256 hash) has an
     "approved" decision from a person with the right seat.
  2. Any edit creates a new draft version and voids earlier approvals.
  3. One live deposit invoice per quote; repeated sends return the same invoice
     (local check + PayPal-Request-Id).
  4. Each webhook event is processed at most once, and only after its signature
     verifies and it comes from the sandbox.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from . import policy as P
from .db import DB, now_iso
from .paypal import PayPalError, payer_url_from

ACTIVE_INVOICE = ("DRAFT", "SENT", "SCHEDULED", "UNPAID", "PAYMENT_PENDING", "PARTIALLY_PAID", "PAID",
                  "MARKED_AS_PAID")

STAGES = {
    "quote_signed": "Quote signed",
    "awaiting_approval": "Awaiting approval",
    "approved": "Approved, ready to send",
    "denied": "Denied",
    "sent": "Invoice sent",
    "paid": "Deposit paid",
    "refunded": "Refunded",
    "cancelled": "Invoice cancelled",
}

EVENT_STATUS = {
    "INVOICING.INVOICE.PAID": ("PAID", "paid"),
    "INVOICING.INVOICE.REFUNDED": ("REFUNDED", "refunded"),
    "INVOICING.INVOICE.CANCELLED": ("CANCELLED", "cancelled"),
}


class DeskError(Exception):
    """A refused action, with a message safe to show on screen."""


def canonical_hash(payload: dict, message: str) -> str:
    blob = json.dumps({"invoice": payload, "message": message}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class Seat:
    name: str
    role: str  # rep | manager


SEATS = {
    "rep": Seat("Dana Reyes (rep)", "rep"),
    "manager": Seat("Alex Kim (sales manager)", "manager"),
}


class Desk:
    def __init__(self, db: DB, gateway, drafter, settings, pol: P.Policy = P.DEFAULT_POLICY):
        self.db, self.pp, self.drafter, self.s, self.policy = db, gateway, drafter, settings, pol

    # ------------------------------------------------------------------ helpers
    def _quote(self, quote_id: str) -> dict:
        q = self.db.quote(quote_id)
        if not q:
            raise DeskError("Unknown quote.")
        return q

    def draft_row(self, draft_id: int):
        row = self.db.one("SELECT * FROM drafts WHERE id = ?", (draft_id,))
        if not row:
            raise DeskError("Unknown draft.")
        return row

    def latest_draft(self, quote_id: str):
        return self.db.one("SELECT * FROM drafts WHERE quote_id = ? ORDER BY version DESC LIMIT 1", (quote_id,))

    def invoice(self, quote_id: str):
        return self.db.one("SELECT * FROM invoices WHERE quote_id = ?", (quote_id,))

    def _recipient_email(self, quote: dict) -> str:
        if not self.s.is_mock and self.s.sandbox_buyer_email:
            return self.s.sandbox_buyer_email
        return quote["customer"]["email"]

    def build_invoice(self, quote: dict, m: P.Money, note: str, item_desc: str, due: date) -> dict:
        """Exact body for POST /v2/invoicing/invoices."""
        c = quote["customer"]
        invoicer = {"business_name": self.s.business_name}
        if self.s.paypal_invoicer_email:
            invoicer["email_address"] = self.s.paypal_invoicer_email
        return {
            "detail": {
                "currency_code": "USD",
                "reference": quote["id"],
                "invoice_date": date.today().isoformat(),
                "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due.isoformat()},
                "note": note,
                "terms_and_conditions": "Deposit is applied in full to the project total. "
                                        "Balance is due at completion per the signed agreement.",
                "memo": f"Deposit Desk - quote {quote['id']} - rep {quote['rep']['name']}",
            },
            "invoicer": invoicer,
            "primary_recipients": [{"billing_info": {
                "name": {"given_name": c["first_name"], "surname": c["last_name"]},
                "email_address": self._recipient_email(quote),
            }}],
            "items": [{
                "name": f"Project deposit ({P.pct_label(m.deposit_pct)})",
                "description": item_desc,
                "quantity": "1",
                "unit_amount": {"currency_code": "USD", "value": f"{m.deposit:.2f}"},
                "unit_of_measure": "AMOUNT",
            }],
            "configuration": {"allow_tip": False, "partial_payment": {"allow_partial_payment": False}},
        }

    def _store_draft(self, quote: dict, m: P.Money, message: str, note: str, item_desc: str,
                     source: str, agent_notes: list[str], actor: str, extra_flags=()) -> int:
        due = date.today() + timedelta(days=3)
        payload = self.build_invoice(quote, m, note, item_desc, due)
        flags = P.evaluate(quote, m, message, policy=self.policy) + list(extra_flags)
        open_inv = self.invoice(quote["id"])
        if open_inv and open_inv["status"] in ACTIVE_INVOICE:
            flags.append(P.Flag("invoice_exists", P.BLOCK,
                                f"A deposit invoice already exists for this quote ({open_inv['paypal_invoice_id']})."))
        h = canonical_hash(payload, message)
        with self.db.tx():
            prev = self.latest_draft(quote["id"])
            version = (prev["version"] + 1) if prev else 1
            voided = self.db.q("SELECT id FROM drafts WHERE quote_id = ? AND status IN ('pending','approved')",
                               (quote["id"],))
            self.db.x("UPDATE drafts SET status = 'superseded' WHERE quote_id = ? AND status IN ('pending','approved')",
                      (quote["id"],))
            draft_id = self.db.x(
                "INSERT INTO drafts (quote_id, version, payload, message, money, flags, agent_notes, source, hash,"
                " status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (quote["id"], version, json.dumps(payload), message, json.dumps(m.as_dict()),
                 json.dumps([f.as_dict() for f in flags]), json.dumps(agent_notes), source, h,
                 "pending", actor, now_iso()))
            self.db.set_stage(quote["id"], "awaiting_approval")
        if voided:
            self.db.log(quote["id"], "system", "Deposit Desk",
                        f"Draft v{version} replaces the earlier draft. Any earlier approval is void.")
        return draft_id

    # ------------------------------------------------------------------ actions
    def draft(self, quote_id: str, actor: str) -> int:
        """Agent step: compute the money, write the words, check the policy."""
        quote = self._quote(quote_id)
        m = P.agent_deposit(quote, self.policy)
        due_label = (date.today() + timedelta(days=3)).strftime("%b %d, %Y").replace(" 0", " ")
        result = self.drafter.draft(quote, m, due_label)
        extra = []
        if result.source == "offline-fallback":
            extra.append(P.Flag("drafter_fallback", P.INFO,
                                "Language model unavailable; deterministic template used."))
        draft_id = self._store_draft(quote, m, result.text.customer_message, result.text.invoice_note,
                                     result.text.item_description, result.source,
                                     result.text.risk_notes, actor, extra)
        row = self.draft_row(draft_id)
        flags = json.loads(row["flags"])
        blocking = [f for f in flags if f["severity"] == P.BLOCK]
        mgr = [f for f in flags if f["severity"] == P.MANAGER]
        if blocking:
            verdict = f"Blocked: {blocking[0]['text']}"
        elif mgr:
            verdict = "Needs a manager's approval: " + mgr[0]["text"]
        else:
            verdict = "Policy check clear. Waiting for your approval."
        self.db.log(quote_id, "agent", "Deposit Desk agent",
                    f"Drafted deposit request v{row['version']}: {P.fmt(m.deposit)} of {P.fmt(m.total)}. {verdict}")
        return draft_id

    def edit(self, draft_id: int, actor: str, *, deposit_pct: str | None = None, message: str | None = None,
             redraft_words: bool = False) -> int:
        """A person changes the draft. Always a new version; the rules re-run; approval resets."""
        row = self.draft_row(draft_id)
        if row["status"] not in ("pending", "approved", "denied"):
            raise DeskError("This draft can no longer be edited.")
        latest = self.latest_draft(row["quote_id"])
        if latest["id"] != row["id"]:
            raise DeskError("A newer draft exists. Edit the latest version.")
        quote = self._quote(row["quote_id"])
        payload = json.loads(row["payload"])
        old = json.loads(row["money"])
        if deposit_pct not in (None, ""):
            try:
                pct = (Decimal(str(deposit_pct).strip().rstrip("%")) / 100).quantize(Decimal("0.0001"))
            except Exception as exc:  # noqa: BLE001 - user input
                raise DeskError("Deposit percent must be a number.") from exc
            m = P.compute(quote, pct, policy=self.policy)
        else:
            m = P.compute(quote, deposit_amount=Decimal(old["deposit"]), policy=self.policy)
        changed_money = m.deposit != Decimal(old["deposit"])
        note, item_desc = payload["detail"]["note"], payload["items"][0]["description"]
        new_msg = (message if message is not None else row["message"]).strip()
        source = "edited"
        allowed = {m.deposit, m.total, m.balance, m.subtotal, m.discount}
        words_stale = any(a not in allowed for a in P.amounts_in(new_msg))
        if changed_money:
            # Keep the invoice line honest about the new percentage.
            item_desc = f"{P.pct_label(m.deposit_pct)} deposit toward {quote['project']} (quote {quote['id']})"
        user_rewrote = new_msg != row["message"].strip()
        if redraft_words and not user_rewrote and (changed_money or words_stale):
            due_label = (date.today() + timedelta(days=3)).strftime("%b %d, %Y").replace(" 0", " ")
            result = self.drafter.draft(quote, m, due_label)
            new_msg, note = result.text.customer_message, result.text.invoice_note
            source = "edited+agent"
        new_id = self._store_draft(quote, m, new_msg, note, item_desc, source,
                                   json.loads(row["agent_notes"]), actor)
        v = self.draft_row(new_id)["version"]
        self.db.log(row["quote_id"], "human", actor,
                    f"Edited the draft (now v{v}, deposit {P.fmt(m.deposit)}). Policy re-checked; approval reset.")
        return new_id

    def decide(self, draft_id: int, decision: str, seat: Seat, expected_hash: str, reason: str = "") -> int:
        if decision not in ("approved", "denied"):
            raise DeskError("Decision must be approve or deny.")
        with self.db.tx():
            row = self.draft_row(draft_id)
            if row["status"] != "pending":
                raise DeskError(f"This draft is {row['status']}; only a pending draft can be decided.")
            if expected_hash != row["hash"]:
                raise DeskError("The draft changed since you opened it. Review the latest version.")
            if canonical_hash(json.loads(row["payload"]), row["message"]) != row["hash"]:
                raise DeskError("Draft integrity check failed.")
            flags = [P.Flag(**f) for f in json.loads(row["flags"])]
            if decision == "approved":
                ok, why = P.can_approve(flags, seat.role)
                if not ok:
                    raise DeskError(why)
            approval_id = self.db.x(
                "INSERT INTO approvals (draft_id, quote_id, decision, actor, role, reason, draft_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (draft_id, row["quote_id"], decision, seat.name, seat.role, reason.strip()[:300], row["hash"], now_iso()))
            self.db.x("UPDATE drafts SET status = ? WHERE id = ?", (decision, draft_id))
            self.db.set_stage(row["quote_id"], "approved" if decision == "approved" else "denied")
        verb = "Approved" if decision == "approved" else "Denied"
        tail = f" Reason: {reason.strip()}" if reason.strip() else ""
        self.db.log(row["quote_id"], "human", seat.name,
                    f"{verb} draft v{row['version']} (hash {row['hash'][:12]}).{tail}")
        return approval_id

    def send(self, draft_id: int, actor: str) -> dict:
        """Create + send the PayPal invoice. Only for an approved, unmodified draft."""
        with self.db.tx():
            row = self.draft_row(draft_id)
            existing = self.invoice(row["quote_id"])
            if existing and existing["draft_id"] == draft_id:
                return dict(existing)  # idempotent: same click twice, same invoice
            if existing and existing["status"] in ACTIVE_INVOICE:
                raise DeskError("A deposit invoice already exists for this quote.")
            if row["status"] != "approved":
                raise DeskError("Not approved. Nothing is sent without a person's approval.")
            approval = self.db.one(
                "SELECT * FROM approvals WHERE draft_id = ? AND decision = 'approved' ORDER BY id DESC LIMIT 1",
                (draft_id,))
            payload = json.loads(row["payload"])
            if (not approval or approval["draft_hash"] != row["hash"]
                    or canonical_hash(payload, row["message"]) != row["hash"]):
                raise DeskError("Approval does not match this exact draft. Re-approve.")
            request_id = f"pdd-{row['quote_id']}-a{approval['id']}-{row['hash'][:16]}"
            try:
                inv = self.pp.create_invoice(payload, request_id + "-create")
                self.pp.send_invoice(inv["id"], request_id + "-send")
                inv = self.pp.get_invoice(inv["id"])
                payer = payer_url_from(inv)
                try:
                    qr = self.pp.qr_png(inv["id"], payer)
                except PayPalError:
                    qr = None
            except PayPalError as exc:
                self.db.log(row["quote_id"], "system", "PayPal", f"PayPal call failed: {exc}")
                raise DeskError(f"PayPal returned an error. Nothing was marked sent. ({exc})") from exc
            status = inv.get("status", "SENT")
            amount = payload["items"][0]["unit_amount"]["value"]
            ts = now_iso()
            self.db.x("DELETE FROM invoices WHERE quote_id = ?", (row["quote_id"],))
            self.db.x("INSERT INTO invoices (quote_id, draft_id, approval_id, paypal_invoice_id, request_id, status,"
                      " amount, payer_url, qr_png, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (row["quote_id"], draft_id, approval["id"], inv["id"], request_id, status, amount,
                       payer, qr, ts, ts))
            self.db.x("UPDATE drafts SET status = 'sent' WHERE id = ?", (draft_id,))
            self.db.set_stage(row["quote_id"], "sent")
        self.db.log(row["quote_id"], "paypal", "PayPal Invoicing",
                    f"Invoice {inv['id']} created and sent for ${Decimal(amount):,.2f}. Status: {status}.")
        self.db.log(row["quote_id"], "agent", "Deposit Desk agent",
                    "Invoice is live. Show the homeowner the QR code to pay on the spot; "
                    "I will post here the moment PayPal confirms payment.")
        return dict(self.invoice(row["quote_id"]))

    def refresh(self, quote_id: str) -> str:
        """Fallback if a webhook is missed: read the invoice status from PayPal."""
        inv = self.invoice(quote_id)
        if not inv:
            raise DeskError("No invoice yet.")
        try:
            remote = self.pp.get_invoice(inv["paypal_invoice_id"])
        except PayPalError as exc:
            raise DeskError(str(exc)) from exc
        status = remote.get("status", inv["status"])
        if status != inv["status"]:
            self._apply_status(quote_id, inv, status, source="status check")
        return status

    # ------------------------------------------------------------------ webhooks
    def handle_webhook(self, headers: dict, raw_body: bytes) -> tuple[int, str]:
        try:
            event = json.loads(raw_body or b"{}")
        except ValueError:
            return 400, "invalid json"
        if not isinstance(event, dict) or not event.get("id") or not event.get("event_type"):
            return 400, "not a webhook event"
        if not self.pp.verify_webhook(headers, event, raw_body):
            return 401, "signature verification failed"
        with self.db.tx():
            if self.db.one("SELECT 1 FROM webhook_events WHERE event_id = ?", (event["id"],)):
                return 200, "duplicate ignored"
            resource = event.get("resource") or {}
            inv_id = (resource.get("invoice") or {}).get("id") or resource.get("id") or ""
            inv = self.db.one("SELECT * FROM invoices WHERE paypal_invoice_id = ?", (inv_id,))
            mapped = EVENT_STATUS.get(event["event_type"])
            if not inv:
                outcome = "unmatched invoice"
            elif not mapped:
                outcome = "event type ignored"
            else:
                self._apply_status(inv["quote_id"], inv, mapped[0], source="webhook")
                outcome = f"invoice -> {mapped[0]}"
            self.db.x("INSERT INTO webhook_events (event_id, event_type, resource_id, outcome, received_at)"
                      " VALUES (?,?,?,?,?)", (event["id"], event["event_type"], inv_id, outcome, now_iso()))
        return 200, outcome

    def _apply_status(self, quote_id: str, inv, status: str, source: str) -> None:
        self.db.x("UPDATE invoices SET status = ?, updated_at = ? WHERE quote_id = ?", (status, now_iso(), quote_id))
        quote = self._quote(quote_id)
        amt = P.fmt(Decimal(inv["amount"]))
        self.db.log(quote_id, "paypal", "PayPal Webhooks" if source == "webhook" else "PayPal",
                    f"Invoice {inv['paypal_invoice_id']} is now {status} ({source}).")
        if status in ("PAID", "MARKED_AS_PAID"):
            self.db.set_stage(quote_id, "paid")
            self._task(quote_id, f"Schedule {quote['project'].lower()} for {quote['customer']['first_name']} "
                                 f"{quote['customer']['last_name'][0]}.", "Office")
            self.db.log(quote_id, "agent", "Deposit Desk agent",
                        f"Deposit of {amt} received through PayPal. Job marked paid, and the office has a new "
                        f"task: schedule the {quote['project'].lower()}.")
        elif status in ("REFUNDED", "PARTIALLY_REFUNDED"):
            self.db.set_stage(quote_id, "refunded")
            self._task(quote_id, "Call the homeowner about the refund and confirm next steps.", quote["rep"]["name"])
            self.db.log(quote_id, "agent", "Deposit Desk agent",
                        f"Deposit of {amt} was refunded. Job paused; {quote['rep']['name']} has a follow-up task.")
        elif status in ("CANCELLED", "AUTO_CANCELLED"):
            self.db.set_stage(quote_id, "cancelled")
            self.db.log(quote_id, "agent", "Deposit Desk agent",
                        "Invoice cancelled. I can draft a new request when you're ready; it will need approval again.")

    def _task(self, quote_id: str, title: str, owner: str) -> None:
        self.db.x("INSERT INTO tasks (quote_id, title, owner, created_at) VALUES (?,?,?,?)",
                  (quote_id, title, owner, now_iso()))
