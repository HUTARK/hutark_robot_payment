import sqlite3
import threading

import pytest

import ai_recharge
import recharge_api
from conftest import (PUBKEY_HEX, checkout_signature, webhook_body,
                      webhook_signature)

ROBOT = "GIYA001"


def make_order(client, robot=ROBOT, plan="monthly"):
    r = client.post("/ai-plan/order", json={"robot_id": robot, "plan_id": plan})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["order_id"]


def confirm(client, order_id, payment_id, signature=None):
    return client.post("/ai-plan/confirm", json={
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature or checkout_signature(order_id, payment_id),
    })


def send_webhook(client, raw, signature=None):
    return client.post("/ai-plan/webhook", data=raw, content_type="application/json",
                       headers={"X-Razorpay-Signature":
                                signature if signature is not None
                                else webhook_signature(raw)})


def db_rows(app, sql, args=()):
    con = sqlite3.connect(app.db_path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def assert_code_for(code, robot, payment_id, credit=300, days=30):
    ok, p = ai_recharge.read_code(code, PUBKEY_HEX)
    assert ok
    assert (p["robot_id"], p["recharge_id"], p["credit_inr"], p["days"]) == \
        (robot, payment_id, credit, days)


# ── health / plans ────────────────────────────────────────────────────────
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json() == {"ok": True}


def test_plans(client):
    r = client.get("/ai-plan/plans")
    assert r.status_code == 200
    assert r.get_json() == [{"plan_id": "monthly", "price_inr": 499,
                             "credit_inr": 300, "days": 30}]


# ── order ─────────────────────────────────────────────────────────────────
def test_order_creates_razorpay_order(client, app, razorpay):
    r = client.post("/ai-plan/order", json={"robot_id": ROBOT, "plan_id": "monthly"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["order_id"].startswith("order_")
    assert body["amount_paise"] == 49900 and body["currency"] == "INR"
    assert body["key_id"] == "rzp_test_dummykeyid"
    call, = razorpay.calls
    assert call["url"] == recharge_api.RAZORPAY_ORDERS_URL
    assert call["auth"] == ("rzp_test_dummykeyid", "test-key-secret-not-real")
    assert call["json"]["amount"] == 49900 and call["json"]["currency"] == "INR"
    assert call["json"]["notes"] == {"robot_id": ROBOT, "plan_id": "monthly"}
    assert len(call["json"]["receipt"]) <= 40
    assert db_rows(app, "SELECT robot_id, plan_id, amount_paise, credit_inr, days "
                        "FROM orders") == [(ROBOT, "monthly", 49900, 300.0, 30)]


@pytest.mark.parametrize("body", [
    {"robot_id": ROBOT, "plan_id": "yearly"},          # unknown plan
    {"plan_id": "monthly"},                            # no robot
    {"robot_id": "GIYA 001; drop", "plan_id": "monthly"},  # bad characters
    {"robot_id": "x" * 65, "plan_id": "monthly"},      # too long
    {"robot_id": ROBOT, "plan_id": ["monthly"]},       # wrong type
])
def test_order_rejects_bad_input(client, razorpay, body):
    assert client.post("/ai-plan/order", json=body).status_code == 400
    assert razorpay.calls == []


def test_order_non_json_body(client, razorpay):
    r = client.post("/ai-plan/order", data="[1,2]", content_type="application/json")
    assert r.status_code == 400 and razorpay.calls == []


def test_order_razorpay_error_is_502(client, app, razorpay):
    razorpay.status = 401
    assert client.post("/ai-plan/order", json={"robot_id": ROBOT,
                                                "plan_id": "monthly"}).status_code == 502
    assert db_rows(app, "SELECT * FROM orders") == []


def test_order_razorpay_unreachable_is_502(client, razorpay):
    razorpay.raise_exc = recharge_api.requests.ConnectionError("down")
    assert client.post("/ai-plan/order", json={"robot_id": ROBOT,
                                                "plan_id": "monthly"}).status_code == 502


# ── confirm ───────────────────────────────────────────────────────────────
def test_confirm_good_signature_issues_valid_code(client, app, razorpay):
    order_id = make_order(client)
    r = confirm(client, order_id, "pay_GOOD000000001")
    assert r.status_code == 200
    body = r.get_json()
    assert body["recharge_id"] == "pay_GOOD000000001"
    assert (body["robot_id"], body["credit_inr"], body["days"]) == (ROBOT, 300, 30)
    assert_code_for(body["code"], ROBOT, "pay_GOOD000000001")
    assert db_rows(app, "SELECT applied, source FROM recharges") == [(0, "confirm")]


@pytest.mark.parametrize("bad", ["0" * 64, "", "not-hex",
                                 checkout_signature("x", "y")])
def test_confirm_bad_signature_400(client, app, razorpay, bad):
    order_id = make_order(client)
    r = client.post("/ai-plan/confirm", json={
        "razorpay_order_id": order_id, "razorpay_payment_id": "pay_BAD00000001",
        "razorpay_signature": bad})
    assert r.status_code == 400
    assert db_rows(app, "SELECT * FROM recharges") == []


def test_confirm_signed_with_wrong_secret_400(client, app, razorpay):
    order_id = make_order(client)
    sig = checkout_signature(order_id, "pay_X000000001", secret="someone-elses")
    assert confirm(client, order_id, "pay_X000000001", sig).status_code == 400
    assert db_rows(app, "SELECT * FROM recharges") == []


def test_confirm_unknown_order_400(client, razorpay):
    r = confirm(client, "order_NOTOURS0001", "pay_Q000000001")
    assert r.status_code == 400 and r.get_json()["error"] == "unknown order"


def test_confirm_twice_returns_same_code(client, app, razorpay, issued_codes):
    order_id = make_order(client)
    first = confirm(client, order_id, "pay_TWICE0000001").get_json()
    second = confirm(client, order_id, "pay_TWICE0000001").get_json()
    assert first == second
    assert len(issued_codes) == 1                # signed only once
    assert len(db_rows(app, "SELECT * FROM recharges")) == 1


def test_second_payment_on_same_order_gets_no_code(client, app, razorpay):
    order_id = make_order(client)
    assert confirm(client, order_id, "pay_FIRST000001").status_code == 200
    assert confirm(client, order_id, "pay_SECOND00001").status_code == 409
    assert len(db_rows(app, "SELECT * FROM recharges")) == 1


# ── webhook ───────────────────────────────────────────────────────────────
def test_webhook_good_signature_issues_code(client, app, razorpay):
    order_id = make_order(client)
    r = send_webhook(client, webhook_body("pay_HOOK00000001", order_id))
    assert r.status_code == 200 and r.get_json()["status"] == "issued"
    (code, source), = db_rows(app, "SELECT code, source FROM recharges")
    assert source == "webhook"
    assert_code_for(code, ROBOT, "pay_HOOK00000001")
    # the app's later /confirm gets the very same code
    assert confirm(client, order_id, "pay_HOOK00000001").get_json()["code"] == code


@pytest.mark.parametrize("which", ["wrong", "missing", "tampered", "other_secret"])
def test_webhook_bad_signature_400(client, app, razorpay, which):
    order_id = make_order(client)
    raw = webhook_body("pay_EVIL00000001", order_id)
    if which == "wrong":
        r = send_webhook(client, raw, "ab" * 32)
    elif which == "missing":
        r = send_webhook(client, raw, "")
    elif which == "tampered":
        sig = webhook_signature(raw)
        r = send_webhook(client, raw.replace(b"49900", b"49901"), sig)
    else:
        r = send_webhook(client, raw, webhook_signature(raw, "other"))
    assert r.status_code == 400
    assert db_rows(app, "SELECT * FROM recharges") == []


def test_webhook_is_idempotent(client, app, razorpay, issued_codes):
    order_id = make_order(client)
    raw = webhook_body("pay_DUP000000001", order_id)
    assert send_webhook(client, raw).get_json()["status"] == "issued"
    assert send_webhook(client, raw).get_json()["status"] == "existing"
    assert send_webhook(client, raw).status_code == 200
    assert len(issued_codes) == 1
    assert len(db_rows(app, "SELECT * FROM recharges")) == 1


def test_webhook_after_confirm_returns_existing(client, app, razorpay):
    order_id = make_order(client)
    code = confirm(client, order_id, "pay_CFIRST00001").get_json()["code"]
    r = send_webhook(client, webhook_body("pay_CFIRST00001", order_id))
    assert r.status_code == 200 and r.get_json()["status"] == "existing"
    assert db_rows(app, "SELECT code FROM recharges") == [(code,)]


@pytest.mark.parametrize("raw", [
    webhook_body("pay_OTHER0000001", "order_TEST0000000001", event="payment.failed"),
    webhook_body("pay_OTHER0000001", "order_NOTOURS0001"),           # not our order
    webhook_body("pay_OTHER0000001", None),                         # no order id
    b'{"event": "payment.captured"}',                                # no payload
    b"not json at all",
])
def test_webhook_ignores_irrelevant_but_answers_200(client, app, razorpay, raw):
    make_order(client)
    r = send_webhook(client, raw)
    assert r.status_code == 200
    assert db_rows(app, "SELECT * FROM recharges") == []


@pytest.mark.parametrize("amount,currency", [(100, "INR"), (49900, "USD")])
def test_webhook_amount_mismatch_issues_nothing(client, app, razorpay, amount, currency):
    order_id = make_order(client)
    r = send_webhook(client, webhook_body("pay_CHEAP0000001", order_id, amount,
                                          currency=currency))
    assert r.status_code == 200 and r.get_json()["status"] == "amount_mismatch"
    assert db_rows(app, "SELECT * FROM recharges") == []


# ── race: webhook and /confirm at the same moment ─────────────────────────
def test_webhook_and_confirm_race(app, razorpay, monkeypatch, issued_codes):
    order_id = make_order(app.test_client())
    payment_id = "pay_RACE00000001"
    # Hold both requests right after signing, i.e. after each has seen
    # "no code yet", so both try to INSERT.
    barrier = threading.Barrier(2, timeout=10)
    signing = ai_recharge.make_code

    def slow_make_code(*a, **k):
        code = signing(*a, **k)
        barrier.wait()
        return code

    monkeypatch.setattr(ai_recharge, "make_code", slow_make_code)
    results = {}

    def do_confirm():
        results["confirm"] = confirm(app.test_client(), order_id, payment_id)

    def do_webhook():
        results["webhook"] = send_webhook(app.test_client(),
                                          webhook_body(payment_id, order_id))

    threads = [threading.Thread(target=do_confirm), threading.Thread(target=do_webhook)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not any(t.is_alive() for t in threads)

    assert len(issued_codes) == 2                    # both really raced
    assert results["confirm"].status_code == 200
    assert results["webhook"].status_code == 200
    assert {results["webhook"].get_json()["status"]} <= {"issued", "existing"}
    (stored,), = db_rows(app, "SELECT code FROM recharges")
    assert results["confirm"].get_json()["code"] == stored    # one payment, one code
    assert_code_for(stored, ROBOT, payment_id)
    # and from now on everyone sees that one code
    assert confirm(app.test_client(), order_id, payment_id).get_json()["code"] == stored


# ── pending / applied ─────────────────────────────────────────────────────
def test_pending_and_applied(client, razorpay):
    o1, o2 = make_order(client), make_order(client)
    o3 = make_order(client, robot="LIZA001")
    c1 = confirm(client, o1, "pay_P1000000001").get_json()["code"]
    send_webhook(client, webhook_body("pay_P2000000001", o2))
    confirm(client, o3, "pay_P3000000001")

    pend = client.get(f"/ai-plan/pending?robot_id={ROBOT}").get_json()
    assert [p["recharge_id"] for p in pend] == ["pay_P1000000001", "pay_P2000000001"]
    assert set(pend[0]) == {"recharge_id", "code", "credit_inr", "days", "created"}
    assert pend[0]["code"] == c1 and pend[0]["credit_inr"] == 300 and pend[0]["days"] == 30
    for p in pend:
        assert_code_for(p["code"], ROBOT, p["recharge_id"])

    for _ in range(2):                                       # idempotent
        r = client.post("/ai-plan/applied", json={"recharge_id": "pay_P1000000001"})
        assert r.status_code == 200 and r.get_json()["applied"] is True
    pend = client.get(f"/ai-plan/pending?robot_id={ROBOT}").get_json()
    assert [p["recharge_id"] for p in pend] == ["pay_P2000000001"]

    client.post("/ai-plan/applied", json={"recharge_id": "pay_P2000000001"})
    assert client.get(f"/ai-plan/pending?robot_id={ROBOT}").get_json() == []
    # other robots are unaffected
    assert len(client.get("/ai-plan/pending?robot_id=LIZA001").get_json()) == 1


def test_applied_keeps_first_timestamp(client, app, razorpay):
    order_id = make_order(client)
    confirm(client, order_id, "pay_TS000000001")
    client.post("/ai-plan/applied", json={"recharge_id": "pay_TS000000001"})
    first = db_rows(app, "SELECT applied_at FROM recharges")
    client.post("/ai-plan/applied", json={"recharge_id": "pay_TS000000001"})
    assert db_rows(app, "SELECT applied_at FROM recharges") == first


def test_pending_and_applied_bad_input(client):
    assert client.get("/ai-plan/pending").status_code == 400
    assert client.get("/ai-plan/pending?robot_id=a%20b").status_code == 400
    assert client.post("/ai-plan/applied", json={}).status_code == 400
    assert client.post("/ai-plan/applied",
                       json={"recharge_id": "pay_UNKNOWN0001"}).status_code == 404


# ── limits ────────────────────────────────────────────────────────────────
def test_rate_limit_order_per_ip(client, razorpay):
    limit, _ = recharge_api.RATE_LIMITS["order"]
    body = {"robot_id": ROBOT, "plan_id": "monthly"}
    for _ in range(limit):
        assert client.post("/ai-plan/order", json=body).status_code == 200
    r = client.post("/ai-plan/order", json=body)
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    # a different client IP (as nginx reports it) is not blocked
    r = client.post("/ai-plan/order", json=body,
                    headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 200


def test_rate_limit_confirm_per_ip(client, razorpay):
    limit, _ = recharge_api.RATE_LIMITS["confirm"]
    for i in range(limit):
        assert confirm(client, "order_NOPE000001", "pay_N%010d" % i).status_code == 400
    assert confirm(client, "order_NOPE000001", "pay_LAST000001").status_code == 429


def test_rate_limiter_window_slides():
    now = [0.0]
    rl = recharge_api.RateLimiter(2, 60, clock=lambda: now[0])
    assert rl.allow("ip") and rl.allow("ip") and not rl.allow("ip")
    assert rl.allow("other-ip")
    now[0] = 61
    assert rl.allow("ip")


def test_request_size_limit(client, razorpay):
    big = b'{"robot_id": "' + b"A" * (recharge_api.MAX_BODY_BYTES + 10) + b'"}'
    for path in ("/ai-plan/order", "/ai-plan/confirm", "/ai-plan/webhook",
                 "/ai-plan/applied"):
        r = client.post(path, data=big, content_type="application/json")
        assert r.status_code == 413, path
    assert razorpay.calls == []


def test_unknown_route_and_method_are_json(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.is_json
    r = client.get("/ai-plan/confirm")
    assert r.status_code == 405 and r.is_json


# ── config / database ─────────────────────────────────────────────────────
def test_missing_env_fails_fast_without_leaking_values(tmp_path):
    with pytest.raises(RuntimeError) as e:
        recharge_api.create_app({"RAZORPAY_KEY_ID": "rzp_test_x",
                                 "RAZORPAY_KEY_SECRET": "super-secret-value"})
    msg = str(e.value)
    assert "RAZORPAY_WEBHOOK_SECRET" in msg and "HUTARK_AI_PRIVATE_KEY" in msg
    assert "super-secret-value" not in msg


def test_unreadable_private_key_fails_fast(tmp_path):
    import os
    env = dict(os.environ, HUTARK_AI_PRIVATE_KEY=str(tmp_path / "missing.key"),
               AI_PLAN_DB=str(tmp_path / "x.db"))
    try:
        with pytest.raises(RuntimeError, match="cannot load the private key"):
            recharge_api.create_app(env)
    finally:
        ai_recharge.PRIVATE_KEY_FILE = os.environ["HUTARK_AI_PRIVATE_KEY"]


def test_db_uses_wal(app):
    assert db_rows(app, "PRAGMA journal_mode") == [("wal",)]


def test_migrates_database_from_example_version(tmp_path, razorpay):
    """A database made by recharge_api_example.py keeps working."""
    import os
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders(order_id TEXT PRIMARY KEY, robot_id TEXT, "
                "plan_id TEXT, amount_paise INTEGER, created REAL)")
    con.execute("CREATE TABLE recharges(payment_id TEXT PRIMARY KEY, order_id TEXT "
                "UNIQUE, robot_id TEXT, credit_inr REAL, days INTEGER, code TEXT, "
                "created REAL)")
    con.execute("INSERT INTO orders VALUES ('order_OLD0000001','OLD001','monthly',"
                "49900, 1)")
    con.commit()
    con.close()
    app = recharge_api.create_app(dict(os.environ, AI_PLAN_DB=str(path)))
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(recharges)")}
    assert {"applied", "applied_at", "source"} <= cols
    c = app.test_client()
    r = confirm(c, "order_OLD0000001", "pay_OLD0000001")      # old order, plan lookup
    assert r.status_code == 200
    assert_code_for(r.get_json()["code"], "OLD001", "pay_OLD0000001")
    assert len(c.get("/ai-plan/pending?robot_id=OLD001").get_json()) == 1


def test_codes_do_not_verify_with_another_key(client, razorpay):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    order_id = make_order(client)
    code = confirm(client, order_id, "pay_KEY00000001").get_json()["code"]
    assert ai_recharge.read_code(code, PUBKEY_HEX)[0] is True
    assert ai_recharge.read_code(code, other)[0] is False
