import json

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.db import DB
from app.desk import SEATS, Desk
from app.drafter import OfflineDrafter
from app.main import create_app
from app.paypal import MockPayPal
from app.seed import seed


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("PAYPAL_ENV", "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_WEBHOOK_ID",
              "LLM_API_URL", "LLM_API_KEY", "LLM_MODEL", "SANDBOX_BUYER_EMAIL"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def settings(tmp_path):
    return load_settings(database_path=str(tmp_path / "test.db"), rate_limit_enabled=False,
                         public_base_url="http://testserver")


@pytest.fixture
def gw(settings):
    return MockPayPal(settings)


@pytest.fixture
def desk(settings, gw):
    db = DB(settings.database_path)
    seed(db)
    return Desk(db, gw, OfflineDrafter(), settings)


@pytest.fixture
def client(settings, gw):
    app = create_app(settings, gateway=gw, drafter=OfflineDrafter())
    with TestClient(app) as c:
        yield c


@pytest.fixture
def rep():
    return SEATS["rep"]


@pytest.fixture
def manager():
    return SEATS["manager"]


def approve_and_send(desk, quote_id, seat):
    d = desk.draft(quote_id, seat.name)
    row = desk.draft_row(d)
    desk.decide(d, "approved", seat, row["hash"])
    return d, desk.send(d, seat.name)


def signed_event(gw, invoice_id, event_type="INVOICING.INVOICE.PAID", event_id="WH-TEST-1", **over):
    event = {
        "id": event_id,
        "event_type": event_type,
        "resource_type": "invoices",
        "resource": {"invoice": {"id": invoice_id, "status": event_type.split(".")[-1]}},
        "links": [{"href": "https://api.sandbox.paypal.com/v1/notifications/webhooks-events/x", "rel": "self"}],
    }
    event.update(over)
    raw = json.dumps(event).encode()
    return gw.sign(raw), raw
