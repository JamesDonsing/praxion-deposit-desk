"""Deterministic rules engine.

The rules own the money math and the go/no-go. The language model never sets an
amount: it writes words, and these rules check the words, too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

# Severity ladder, lowest to highest.
INFO, WARN, MANAGER, BLOCK = "info", "warn", "manager", "block"
SEVERITY_ORDER = {INFO: 0, WARN: 1, MANAGER: 2, BLOCK: 3}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MONEY_RE = re.compile(r"\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)")
PROMISE_WORDS = ("guarantee", "guaranteed", "lifetime", "free ", "no risk", "risk-free",
                 "best price", "lowest price", "price will go up", "today only")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    return f"${value:,.2f}"


def pct_label(value) -> str:
    """0.3 -> '30%', 0.084 -> '8.4%'."""
    v = (Decimal(str(value)) * 100).quantize(Decimal("0.1"))
    return f"{v:.0f}%" if v == v.to_integral_value() else f"{v}%"


@dataclass(frozen=True)
class RegionCap:
    """Sample deposit cap: deposit may not exceed the lesser of pct of total or amount."""
    max_pct: Decimal
    max_amount: Decimal
    label: str


@dataclass(frozen=True)
class Policy:
    rep_discount_max: Decimal = Decimal("0.10")
    manager_discount_max: Decimal = Decimal("0.20")
    deposit_min_pct: Decimal = Decimal("0.10")
    deposit_max_pct: Decimal = Decimal("0.50")
    default_deposit_pct: Decimal = Decimal("0.30")
    min_deposit_amount: Decimal = Decimal("250.00")
    currency: str = "USD"
    # Sample only. Configure to the rules that apply where you operate.
    region_caps: dict = field(default_factory=lambda: {
        "WEST-SAMPLE": RegionCap(Decimal("0.10"), Decimal("1000.00"),
                                 "Sample region rule: deposit is the lesser of 10% or $1,000"),
    })


DEFAULT_POLICY = Policy()


@dataclass(frozen=True)
class Flag:
    code: str
    severity: str
    text: str

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "text": self.text}


@dataclass(frozen=True)
class Money:
    subtotal: Decimal
    discount_pct: Decimal
    discount: Decimal
    total: Decimal
    deposit_pct: Decimal
    deposit: Decimal
    balance: Decimal

    def as_dict(self) -> dict:
        return {k: str(v) for k, v in self.__dict__.items()}


def subtotal(quote: dict) -> Decimal:
    return money(sum(Decimal(str(i["qty"])) * Decimal(str(i["unit_price"])) for i in quote.get("items", [])))


def max_deposit_for(quote: dict, total: Decimal, policy: Policy = DEFAULT_POLICY) -> Decimal | None:
    cap = policy.region_caps.get(quote.get("region", ""))
    if not cap:
        return None
    return min(money(total * cap.max_pct), cap.max_amount)


def agent_deposit(quote: dict, policy: Policy = DEFAULT_POLICY) -> Money:
    """What the agent proposes: the company default, lowered to fit any region cap."""
    m = compute(quote, policy.default_deposit_pct, policy=policy)
    cap = max_deposit_for(quote, m.total, policy)
    if cap is not None and m.deposit > cap:
        m = compute(quote, deposit_amount=cap, policy=policy)
    return m


def compute(quote: dict, deposit_pct=None, *, deposit_amount=None,
            policy: Policy = DEFAULT_POLICY) -> Money:
    """Money math for one quote. Pass either a deposit percentage or a fixed amount."""
    sub = subtotal(quote)
    disc_pct = Decimal(str(quote.get("discount_pct", 0)))
    disc = money(sub * disc_pct)
    total = money(sub - disc)
    if deposit_amount is not None:
        deposit = money(deposit_amount)
        dep_pct = (deposit / total).quantize(Decimal("0.0001")) if total else Decimal("0")
    else:
        dep_pct = Decimal(str(deposit_pct if deposit_pct is not None else policy.default_deposit_pct))
        deposit = money(total * dep_pct)
    return Money(sub, disc_pct, disc, total, dep_pct, deposit, money(total - deposit))


def amounts_in(text: str) -> list[Decimal]:
    out = []
    for m in MONEY_RE.finditer(text or ""):
        out.append(money(m.group(1).replace(",", "")))
    return out


def evaluate(quote: dict, m: Money, message: str, *, today: date | None = None,
             policy: Policy = DEFAULT_POLICY) -> list[Flag]:
    """Return every policy flag for this draft. BLOCK flags make approval impossible."""
    today = today or date.today()
    flags: list[Flag] = []
    cust = quote.get("customer", {})

    # Required fields
    for key, label in (("first_name", "customer first name"), ("last_name", "customer last name"),
                       ("address", "service address")):
        if not str(cust.get(key, "")).strip():
            flags.append(Flag(f"missing_{key}", BLOCK, f"Missing {label}."))
    if not EMAIL_RE.match(str(cust.get("email", ""))):
        flags.append(Flag("missing_email", BLOCK, "Customer email is missing or invalid."))
    if not quote.get("items"):
        flags.append(Flag("missing_items", BLOCK, "Quote has no line items."))
    if quote.get("currency", "USD") != policy.currency:
        flags.append(Flag("currency", BLOCK, f"Only {policy.currency} quotes are supported."))

    # Quote state
    if not quote.get("signed_on"):
        flags.append(Flag("quote_unsigned", BLOCK, "Quote is not signed by the homeowner yet."))
    valid_until = quote.get("valid_until")
    if valid_until and date.fromisoformat(valid_until) < today:
        flags.append(Flag("quote_expired", BLOCK,
                          f"Quote expired on {valid_until}. Re-quote before asking for a deposit."))

    # Discount authority
    if m.discount_pct > policy.manager_discount_max:
        flags.append(Flag("discount_over_max", BLOCK,
                          f"Discount {m.discount_pct:.0%} is above the company maximum "
                          f"of {policy.manager_discount_max:.0%}."))
    elif m.discount_pct > policy.rep_discount_max:
        flags.append(Flag("discount_needs_manager", MANAGER,
                          f"Discount {m.discount_pct:.0%} is above rep authority "
                          f"({policy.rep_discount_max:.0%}). A manager must approve."))

    # Deposit rules
    cap = policy.region_caps.get(quote.get("region", ""))
    cap_amount = max_deposit_for(quote, m.total, policy)
    if cap_amount is not None and m.deposit > cap_amount:
        flags.append(Flag("deposit_over_region_cap", BLOCK,
                          f"Deposit {fmt(m.deposit)} exceeds the region cap of {fmt(cap_amount)}. {cap.label}."))
    elif cap is not None:
        flags.append(Flag("region_cap_applied", INFO, f"{cap.label}. Deposit set to fit."))
    if cap_amount is None and not (policy.deposit_min_pct <= m.deposit_pct <= policy.deposit_max_pct):
        flags.append(Flag("deposit_pct_out_of_range", BLOCK,
                          f"Deposit {m.deposit_pct:.0%} is outside the allowed "
                          f"{policy.deposit_min_pct:.0%}–{policy.deposit_max_pct:.0%}."))
    if m.deposit < policy.min_deposit_amount and m.total >= policy.min_deposit_amount:
        flags.append(Flag("deposit_small", WARN,
                          f"Deposit {fmt(m.deposit)} is below the usual minimum of {fmt(policy.min_deposit_amount)}."))

    # The words must match the money.
    allowed = {m.deposit, m.total, m.balance, m.subtotal, m.discount}
    stray = [a for a in amounts_in(message) if a not in allowed]
    if stray:
        flags.append(Flag("message_amount_mismatch", BLOCK,
                          "Customer message mentions " + ", ".join(fmt(a) for a in stray)
                          + ", which is not on the approved quote."))
    lowered = f" {(message or '').lower()} "
    hits = [w.strip() for w in PROMISE_WORDS if w in lowered]
    if hits:
        flags.append(Flag("promise_language", WARN,
                          "Message uses promise or pressure language: " + ", ".join(sorted(set(hits))) + "."))
    if len(message or "") > 900:
        flags.append(Flag("message_too_long", WARN, "Customer message is over 900 characters."))
    return flags


def worst(flags: list[Flag]) -> str:
    if not flags:
        return "clear"
    return max(flags, key=lambda f: SEVERITY_ORDER[f.severity]).severity


def can_approve(flags: list[Flag], role: str) -> tuple[bool, str]:
    sev = {f.severity for f in flags}
    if BLOCK in sev:
        return False, "Blocked by policy. Fix the flagged items first."
    if MANAGER in sev and role != "manager":
        return False, "Needs a manager. Switch to the manager seat to approve."
    return True, ""
