"""Test setup: throwaway key pair + dummy config, Razorpay mocked, no network."""
import atexit
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile

import pytest

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER)

# Must happen before recharge_api is imported (it builds `app` at import).
_TMP = tempfile.mkdtemp(prefix="hutark-ai-test-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ.update(
    RAZORPAY_KEY_ID="rzp_test_dummykeyid",
    RAZORPAY_KEY_SECRET="test-key-secret-not-real",
    RAZORPAY_WEBHOOK_SECRET="test-webhook-secret-not-real",
    HUTARK_AI_PRIVATE_KEY=os.path.join(_TMP, "hutark_ai_private.key"),
    AI_PLAN_DB=os.path.join(_TMP, "import.db"),
)

import ai_recharge  # noqa: E402

ai_recharge.PRIVATE_KEY_FILE = os.environ["HUTARK_AI_PRIVATE_KEY"]
ai_recharge.PUBLIC_KEY_FILE = os.path.join(_TMP, "hutark_ai_pubkey.txt")
ai_recharge.keygen()

import requests  # noqa: E402

import recharge_api  # noqa: E402

KEY_SECRET = os.environ["RAZORPAY_KEY_SECRET"]
WEBHOOK_SECRET = os.environ["RAZORPAY_WEBHOOK_SECRET"]
PUBKEY_HEX = open(ai_recharge.PUBLIC_KEY_FILE).read().strip()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("test tried to reach the network")
    monkeypatch.setattr(requests.sessions.Session, "request", boom)


@pytest.fixture(autouse=True)
def issued_codes(monkeypatch):
    """Records every code the server signs; after each test, every one of them
    must verify with the public key (what the robot does)."""
    codes = []
    real = ai_recharge.make_code

    def recording(*a, **k):
        code = real(*a, **k)
        codes.append(code)
        return code

    monkeypatch.setattr(ai_recharge, "make_code", recording)
    yield codes
    for code in codes:
        ok, payload = ai_recharge.read_code(code, PUBKEY_HEX)
        assert ok, f"issued code does not verify: {payload}"


@pytest.fixture
def app(tmp_path):
    env = dict(os.environ, AI_PLAN_DB=str(tmp_path / "ai_plan.db"))
    a = recharge_api.create_app(env)
    a.config["TESTING"] = True
    a.config["PROPAGATE_EXCEPTIONS"] = False
    a.db_path = env["AI_PLAN_DB"]
    return a


@pytest.fixture
def client(app):
    return app.test_client()


class FakeRazorpay:
    """Stands in for requests.post to https://api.razorpay.com/v1/orders."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.raise_exc = None

    def __call__(self, url, auth=None, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "auth": auth, "json": json, "timeout": timeout})
        if self.raise_exc:
            raise self.raise_exc
        n = len(self.calls)
        body = {"id": f"order_TEST{n:010d}", "entity": "order",
                "amount": json["amount"], "currency": json["currency"],
                "receipt": json["receipt"], "status": "created"}
        return FakeResponse(self.status, body if self.status == 200 else
                            {"error": {"code": "BAD_REQUEST_ERROR"}})


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def razorpay(monkeypatch):
    fake = FakeRazorpay()
    monkeypatch.setattr(recharge_api.requests, "post", fake)
    return fake


# ── helpers used by the tests ─────────────────────────────────────────────
def checkout_signature(order_id, payment_id, secret=KEY_SECRET):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(),
                    hashlib.sha256).hexdigest()


def webhook_body(payment_id, order_id, amount=49900, event="payment.captured",
                 currency="INR"):
    return json.dumps({
        "entity": "event", "event": event, "contains": ["payment"],
        "payload": {"payment": {"entity": {
            "id": payment_id, "entity": "payment", "amount": amount,
            "currency": currency, "status": "captured", "order_id": order_id,
            "captured": True}}},
        "created_at": 1700000000,
    }).encode()


def webhook_signature(raw, secret=WEBHOOK_SECRET):
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
