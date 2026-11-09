from datetime import date
from decimal import Decimal

from app import policy as P
from app.seed import sample_quotes

TODAY = date(2026, 11, 2)
Q = {q["id"]: q for q in sample_quotes(TODAY)}


def codes(flags):
    return {f.code: f.severity for f in flags}


def msg_for(m):
    return f"Your deposit is {P.fmt(m.deposit)} of the {P.fmt(m.total)} total."


def test_money_math_is_exact():
    m = P.compute(Q["Q-1042"], Decimal("0.30"))
    assert m.subtotal == Decimal("18450.00")
    assert m.discount == Decimal("922.50")
    assert m.total == Decimal("17527.50")
    assert m.deposit == Decimal("5258.25")
    assert m.balance == Decimal("12269.25")


def test_clean_quote_has_no_blocking_flags():
    m = P.agent_deposit(Q["Q-1042"])
    flags = P.evaluate(Q["Q-1042"], m, msg_for(m), today=TODAY)
    assert P.worst(flags) == "clear"
    assert P.can_approve(flags, "rep") == (True, "")


def test_discount_above_rep_authority_needs_manager():
    m = P.agent_deposit(Q["Q-1043"])
    flags = P.evaluate(Q["Q-1043"], m, msg_for(m), today=TODAY)
    assert codes(flags)["discount_needs_manager"] == P.MANAGER
    assert P.can_approve(flags, "rep")[0] is False
    assert P.can_approve(flags, "manager")[0] is True


def test_discount_above_company_max_is_blocked_for_everyone():
    m = P.agent_deposit(Q["Q-1045"])
    flags = P.evaluate(Q["Q-1045"], m, msg_for(m), today=TODAY)
    assert codes(flags)["discount_over_max"] == P.BLOCK
    assert P.can_approve(flags, "manager")[0] is False


def test_expired_quote_is_blocked():
    m = P.agent_deposit(Q["Q-1046"])
    assert codes(P.evaluate(Q["Q-1046"], m, msg_for(m), today=TODAY))["quote_expired"] == P.BLOCK


def test_region_cap_agent_fits_deposit_and_manual_overage_is_blocked():
    q = Q["Q-1044"]
    m = P.agent_deposit(q)
    assert m.deposit == Decimal("1000.00")
    assert codes(P.evaluate(q, m, msg_for(m), today=TODAY)) == {"region_cap_applied": P.INFO}
    over = P.compute(q, Decimal("0.30"))
    assert codes(P.evaluate(q, over, msg_for(over), today=TODAY))["deposit_over_region_cap"] == P.BLOCK


def test_deposit_percent_out_of_range_blocked():
    m = P.compute(Q["Q-1042"], Decimal("0.60"))
    assert codes(P.evaluate(Q["Q-1042"], m, msg_for(m), today=TODAY))["deposit_pct_out_of_range"] == P.BLOCK


def test_required_fields():
    q = {**Q["Q-1042"], "customer": {**Q["Q-1042"]["customer"], "email": "not-an-email", "address": ""},
         "signed_on": None}
    m = P.agent_deposit(q)
    c = codes(P.evaluate(q, m, msg_for(m), today=TODAY))
    assert c["missing_email"] == c["missing_address"] == c["quote_unsigned"] == P.BLOCK


def test_message_amount_must_match_the_money():
    m = P.agent_deposit(Q["Q-1042"])
    flags = P.evaluate(Q["Q-1042"], m, "Pay just $4,999.00 today.", today=TODAY)
    assert codes(flags)["message_amount_mismatch"] == P.BLOCK


def test_promise_language_warns():
    m = P.agent_deposit(Q["Q-1042"])
    flags = P.evaluate(Q["Q-1042"], m, msg_for(m) + " Guaranteed lowest price, today only!", today=TODAY)
    assert codes(flags)["promise_language"] == P.WARN
    assert P.can_approve(flags, "rep")[0] is True
