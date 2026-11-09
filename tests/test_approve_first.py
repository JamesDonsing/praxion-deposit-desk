import json

import pytest

from app.desk import DeskError
from tests.conftest import approve_and_send


def test_drafting_never_touches_paypal(desk, gw, rep):
    desk.draft("Q-1042", rep.name)
    assert gw.calls == []


def test_send_without_approval_is_refused(desk, gw, rep):
    d = desk.draft("Q-1042", rep.name)
    with pytest.raises(DeskError, match="Not approved"):
        desk.send(d, rep.name)
    assert gw.calls == []


def test_denied_draft_cannot_be_sent(desk, gw, rep):
    d = desk.draft("Q-1042", rep.name)
    desk.decide(d, "denied", rep, desk.draft_row(d)["hash"], "Customer wants to wait")
    with pytest.raises(DeskError):
        desk.send(d, rep.name)
    assert gw.calls == []


def test_rep_cannot_approve_manager_level_discount(desk, rep, manager):
    d = desk.draft("Q-1043", rep.name)
    h = desk.draft_row(d)["hash"]
    with pytest.raises(DeskError, match="manager"):
        desk.decide(d, "approved", rep, h)
    desk.decide(d, "approved", manager, h)
    assert desk.draft_row(d)["status"] == "approved"


def test_blocked_draft_cannot_be_approved_even_by_manager(desk, manager):
    d = desk.draft("Q-1045", manager.name)
    with pytest.raises(DeskError, match="Blocked"):
        desk.decide(d, "approved", manager, desk.draft_row(d)["hash"])


def test_stale_screen_hash_is_rejected(desk, rep):
    d = desk.draft("Q-1042", rep.name)
    with pytest.raises(DeskError, match="changed"):
        desk.decide(d, "approved", rep, "0" * 64)


def test_edit_after_approval_voids_it(desk, gw, rep):
    d1 = desk.draft("Q-1042", rep.name)
    desk.decide(d1, "approved", rep, desk.draft_row(d1)["hash"])
    d2 = desk.edit(d1, rep.name, message="Hi Avery, thanks again. Use the PayPal link to pay your deposit.")
    assert desk.draft_row(d1)["status"] == "superseded"
    assert desk.draft_row(d2)["status"] == "pending"
    with pytest.raises(DeskError):
        desk.send(d1, rep.name)
    with pytest.raises(DeskError):
        desk.send(d2, rep.name)
    assert gw.calls == []


def test_tampered_draft_after_approval_is_refused(desk, gw, rep):
    d = desk.draft("Q-1042", rep.name)
    desk.decide(d, "approved", rep, desk.draft_row(d)["hash"])
    payload = json.loads(desk.draft_row(d)["payload"])
    payload["items"][0]["unit_amount"]["value"] = "1.00"
    desk.db.x("UPDATE drafts SET payload = ? WHERE id = ?", (json.dumps(payload), d))
    with pytest.raises(DeskError, match="Approval does not match"):
        desk.send(d, rep.name)
    assert gw.calls == []


def test_approved_draft_sends_exactly_what_was_approved(desk, gw, rep):
    d, inv = approve_and_send(desk, "Q-1042", rep)
    sent = gw.invoices[inv["paypal_invoice_id"]]
    approved = json.loads(desk.draft_row(d)["payload"])
    assert sent["items"] == approved["items"]
    assert inv["amount"] == "5258.25"
    assert sent["status"] == "SENT"
    assert [c[0] for c in gw.calls] == ["create_invoice", "send_invoice"]
    row = desk.db.one("SELECT * FROM approvals WHERE draft_id = ?", (d,))
    assert row["draft_hash"] == desk.draft_row(d)["hash"] and row["actor"] == rep.name


def test_http_flow_end_to_end(client, gw):
    r = client.post("/quotes/Q-1042/draft", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/quotes/Q-1042")
    assert "Policy check clear" in page.text and "Fabricated sample data" in page.text
    draft_id = client.app.state.desk.latest_draft("Q-1042")["id"]
    h = client.app.state.desk.latest_draft("Q-1042")["hash"]
    # Send before approval: refused, nothing reaches PayPal.
    r = client.post(f"/drafts/{draft_id}/send")
    assert "Not approved" in r.text and gw.calls == []
    client.post(f"/drafts/{draft_id}/decide", data={"decision": "approve", "draft_hash": h})
    r = client.post(f"/drafts/{draft_id}/send")
    assert r.status_code == 200 and "Waiting for PayPal webhook" in r.text
    qr = client.get("/invoices/Q-1042/qr.png")
    assert qr.status_code == 200 and qr.content.startswith(b"\x89PNG")
    inv_id = client.app.state.desk.invoice("Q-1042")["paypal_invoice_id"]
    r = client.post(f"/mock-pay/{inv_id}")
    assert "Deposit paid through PayPal" in r.text
    assert client.get("/api/quotes/Q-1042/status").json()["invoice_status"] == "PAID"
    assert "approved" in client.get("/audit").text


def test_rep_seat_sees_disabled_approve_for_manager_discount(client):
    client.post("/quotes/Q-1043/draft")
    page = client.get("/quotes/Q-1043").text
    assert 'class="why">Needs a manager' in page
    client.post("/seat", data={"seat": "manager", "next": "/quotes/Q-1043"})
    page = client.get("/quotes/Q-1043").text
    assert 'class="why"' not in page and "Alex Kim" in page


def test_edit_amount_rechecks_words_and_money(desk, rep):
    d1 = desk.draft("Q-1042", rep.name)
    # Change the deposit but keep the old wording: the rules catch the mismatch.
    d2 = desk.edit(d1, rep.name, deposit_pct="20")
    codes = {f["code"]: f["severity"] for f in json.loads(desk.draft_row(d2)["flags"])}
    assert codes.get("message_amount_mismatch") == "block"
    # Let the agent reword for the new amount: clear again, and the invoice line follows.
    d3 = desk.edit(d2, rep.name, deposit_pct="20", redraft_words=True)
    row = desk.draft_row(d3)
    assert json.loads(row["flags"]) == []
    item = json.loads(row["payload"])["items"][0]
    assert item["unit_amount"]["value"] == "3505.50" and "20%" in item["name"] and "20%" in item["description"]
