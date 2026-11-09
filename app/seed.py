"""Fabricated sample data. Every person, address, email and amount below is made up.

Emails use the reserved example.com domain. In sandbox mode, invoices are addressed
to SANDBOX_BUYER_EMAIL instead, so no real inbox is ever contacted.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

COMPANY = "Sample Home Services Co."


def sample_quotes(today: date | None = None) -> list[dict]:
    t = today or date.today()

    def d(days: int) -> str:
        return (t + timedelta(days=days)).isoformat()

    rep = {"name": "Dana Reyes", "id": "rep-01"}
    rep2 = {"name": "Sam Ortega", "id": "rep-02"}
    return [
        {
            "id": "Q-1042", "company": COMPANY, "rep": rep, "region": "MIDWEST-SAMPLE",
            "project": "Roof replacement",
            "customer": {"first_name": "Avery", "last_name": "Collins",
                         "email": "avery.collins@example.com",
                         "address": "118 Sample Lane, Anytown, WI 00001"},
            "items": [
                {"name": "Architectural shingle roof, 28 squares", "qty": 1, "unit_price": "16450.00"},
                {"name": "Ice and water shield, full perimeter", "qty": 1, "unit_price": "1250.00"},
                {"name": "Ridge vent, 42 ft", "qty": 1, "unit_price": "750.00"},
            ],
            "discount_pct": "0.05", "currency": "USD",
            "signed_on": d(0), "valid_until": d(30),
            "next_step": "Our office schedules your install and confirms the date with you.",
            "note": "Clean quote: rep-level discount, signed today.",
        },
        {
            "id": "Q-1043", "company": COMPANY, "rep": rep, "region": "MIDWEST-SAMPLE",
            "project": "Window replacement",
            "customer": {"first_name": "Jordan", "last_name": "Patel",
                         "email": "jordan.patel@example.com",
                         "address": "42 Example Court, Anytown, WI 00002"},
            "items": [
                {"name": "Double-hung vinyl windows, installed", "qty": 12, "unit_price": "1050.00"},
                {"name": "Exterior trim capping", "qty": 1, "unit_price": "1600.00"},
            ],
            "discount_pct": "0.15", "currency": "USD",
            "signed_on": d(0), "valid_until": d(21),
            "next_step": "Our office orders your windows and books a final measure.",
            "note": "15% discount is above rep authority: needs the manager seat.",
        },
        {
            "id": "Q-1044", "company": COMPANY, "rep": rep2, "region": "WEST-SAMPLE",
            "project": "Bathroom remodel",
            "customer": {"first_name": "Morgan", "last_name": "Lee",
                         "email": "morgan.lee@example.com",
                         "address": "9 Placeholder Ave, Sampleville, OR 00003"},
            "items": [
                {"name": "Tub-to-shower conversion with wall system", "qty": 1, "unit_price": "10400.00"},
                {"name": "Grab bars and low-threshold base", "qty": 1, "unit_price": "1500.00"},
            ],
            "discount_pct": "0", "currency": "USD",
            "signed_on": d(-1), "valid_until": d(29),
            "next_step": "Our design team calls you to pick finishes.",
            "note": "Sample region cap: the agent sizes the deposit to fit.",
        },
        {
            "id": "Q-1045", "company": COMPANY, "rep": rep2, "region": "MIDWEST-SAMPLE",
            "project": "Soffit and fascia replacement",
            "customer": {"first_name": "Riley", "last_name": "Brooks",
                         "email": "riley.brooks@example.com",
                         "address": "301 Demo Road, Anytown, WI 00004"},
            "items": [
                {"name": "Vented aluminum soffit, 160 ft", "qty": 1, "unit_price": "2900.00"},
                {"name": "Wrapped aluminum fascia, 160 ft", "qty": 1, "unit_price": "1950.00"},
            ],
            "discount_pct": "0.25", "currency": "USD",
            "signed_on": d(0), "valid_until": d(14),
            "next_step": "Our office schedules your install.",
            "note": "25% discount is above the company maximum: blocked.",
        },
        {
            "id": "Q-1046", "company": COMPANY, "rep": rep, "region": "MIDWEST-SAMPLE",
            "project": "Siding replacement",
            "customer": {"first_name": "Casey", "last_name": "Nguyen",
                         "email": "casey.nguyen@example.com",
                         "address": "77 Fiction Street, Anytown, WI 00005"},
            "items": [
                {"name": "Insulated vinyl siding, 2,400 sq ft", "qty": 1, "unit_price": "19800.00"},
                {"name": "Soffit and fascia wrap", "qty": 1, "unit_price": "1500.00"},
            ],
            "discount_pct": "0.08", "currency": "USD",
            "signed_on": d(-45), "valid_until": d(-15),
            "next_step": "Our office schedules your install.",
            "note": "Quote expired: the agent refuses to request a deposit.",
        },
    ]


def seed(db, today: date | None = None) -> None:
    for q in sample_quotes(today):
        db.x("INSERT OR IGNORE INTO quotes (id, data, stage) VALUES (?, ?, 'quote_signed')",
             (q["id"], json.dumps(q)))
