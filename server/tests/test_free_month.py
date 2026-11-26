"""First month free (issued on a listed robot's first /pending) and the
refresh of unapplied codes that are getting too old for the robot."""
import os
import sqlite3
import threading
import time

import pytest

import ai_recharge
import recharge_api
from conftest import PUBKEY_HEX
from test_recharge_api import confirm


def payload(code):
    ok, p = ai_recharge.read_code(code, PUBKEY_HEX)
    assert ok
    return p


@pytest.fixture
def listed(tmp_path):
    robots = tmp_path / "robots.txt"
    robots.write_text("LIZA001 Liza\nGIYA001 Giya\nLUNA-01 Luna\n", encoding="utf-8")
    env = dict(os.environ, AI_PLAN_DB=str(tmp_path / "free.db"),
               AI_PLAN_ROBOTS=str(robots))
    app = recharge_api.create_app(env)
    app.db_path = env["AI_PLAN_DB"]
    return app


def rows(app, robot):
    con = sqlite3.connect(app.db_path)
    try:
        return con.execute("SELECT payment_id, source, applied FROM recharges "
                           "WHERE robot_id=?", (robot,)).fetchall()
    finally:
        con.close()


def test_first_check_gets_free_month_once(listed):
    c = listed.test_client()
    first = c.get("/ai-plan/pending?robot_id=GIYA001").get_json()
    assert len(first) == 1
    item = first[0]
    assert item["recharge_id"] == "free_GIYA001"
    assert (item["credit_inr"], item["days"]) == (300, 30)
    p = payload(item["code"])
    assert (p["robot_id"], p["recharge_id"], p["credit_inr"], p["days"]) == \
        ("GIYA001", "free_GIYA001", 300, 30)
    # asking again: the same code, not a second free month
    assert c.get("/ai-plan/pending?robot_id=GIYA001").get_json() == first
    assert rows(listed, "GIYA001") == [("free_GIYA001", "free", 0)]
    # applied -> gone, and never issued again
    r = c.post("/ai-plan/applied", json={"recharge_id": "free_GIYA001"})
    assert r.status_code == 200
    assert c.get("/ai-plan/pending?robot_id=GIYA001").get_json() == []
    st = c.get("/ai-plan/recharge-status?recharge_id=free_GIYA001").get_json()
    assert st["applied"] is True and st["robot_id"] == "GIYA001"
    assert len(rows(listed, "GIYA001")) == 1


def test_robot_ids_with_dash_work(listed):
    c = listed.test_client()
    item, = c.get("/ai-plan/pending?robot_id=LUNA-01").get_json()
    assert item["recharge_id"] == "free_LUNA-01"
    assert c.post("/ai-plan/applied", json={"recharge_id": "free_LUNA-01"}).status_code == 200


def test_no_free_month_after_a_paid_recharge(listed, razorpay):
    c = listed.test_client()
    order = c.post("/ai-plan/order", json={"robot_id": "LIZA001",
                                           "plan_id": "monthly"}).get_json()["order_id"]
    assert confirm(c, order, "pay_PAID1").status_code == 200
    c.post("/ai-plan/applied", json={"recharge_id": "pay_PAID1"})
    assert c.get("/ai-plan/pending?robot_id=LIZA001").get_json() == []
    assert [r[0] for r in rows(listed, "LIZA001")] == ["pay_PAID1"]


def test_paid_but_unapplied_robot_gets_no_extra_free_month(listed, razorpay):
    c = listed.test_client()
    order = c.post("/ai-plan/order", json={"robot_id": "LIZA001",
                                           "plan_id": "monthly"}).get_json()["order_id"]
    confirm(c, order, "pay_PAID2")
    got = c.get("/ai-plan/pending?robot_id=LIZA001").get_json()
    assert [g["recharge_id"] for g in got] == ["pay_PAID2"]


@pytest.mark.parametrize("robot", ["giya001", "NOPE001", "LIZA002"])
def test_no_free_month_for_unlisted_or_misspelled_ids(listed, robot):
    c = listed.test_client()
    assert c.get(f"/ai-plan/pending?robot_id={robot}").get_json() == []
    assert rows(listed, robot) == []


def test_no_free_month_without_a_factory_list(client):
    assert client.get("/ai-plan/pending?robot_id=GIYA001").get_json() == []


def test_free_month_can_be_switched_off(listed, monkeypatch):
    monkeypatch.setattr(recharge_api, "FREE_FIRST_MONTH", None)
    assert listed.test_client().get("/ai-plan/pending?robot_id=GIYA001").get_json() == []


def test_simultaneous_first_checks_issue_one_free_month(listed):
    barrier = threading.Barrier(6)
    results = []

    def ask():
        c = listed.test_client()
        barrier.wait()
        results.append(c.get("/ai-plan/pending?robot_id=GIYA001").get_json())

    threads = [threading.Thread(target=ask) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 6
    assert all(len(r) == 1 and r[0]["recharge_id"] == "free_GIYA001" for r in results)
    assert rows(listed, "GIYA001") == [("free_GIYA001", "free", 0)]


# ── refresh of old unapplied codes ────────────────────────────────────────
def age_code(app, recharge_id, days, column="code_issued"):
    con = sqlite3.connect(app.db_path)
    try:
        with con:
            con.execute(f"UPDATE recharges SET {column}=? WHERE payment_id=?",
                        (time.time() - days * 86400, recharge_id))
    finally:
        con.close()


def signed_in_the_past(monkeypatch, days):
    """Make ai_recharge sign codes as if it were `days` ago."""
    import types
    past = time.time() - days * 86400
    monkeypatch.setattr(ai_recharge, "time", types.SimpleNamespace(time=lambda: past))
    return lambda: monkeypatch.setattr(ai_recharge, "time", time)


def test_old_unapplied_code_is_resigned_with_same_recharge(client, razorpay, monkeypatch):
    r = client.post("/ai-plan/order", json={"robot_id": "GIYA001", "plan_id": "monthly"})
    order = r.get_json()["order_id"]
    back_to_now = signed_in_the_past(monkeypatch, 6.5)
    old_code = confirm(client, order, "pay_OLD1").get_json()["code"]
    back_to_now()
    old = payload(old_code)
    age_code(client.application, "pay_OLD1", 6.5)

    item, = client.get("/ai-plan/pending?robot_id=GIYA001").get_json()
    new = payload(item["code"])
    assert item["code"] != old_code
    for k in ("robot_id", "recharge_id", "credit_inr", "days"):
        assert new[k] == old[k]                    # same recharge: still once only
    assert new["issued_at"] >= int(time.time()) - 5
    assert old["issued_at"] < new["issued_at"] - 6 * 86400
    # stored, so the next check hands out the same refreshed code
    again, = client.get("/ai-plan/pending?robot_id=GIYA001").get_json()
    assert again["code"] == item["code"]
    # /confirm for that payment now also returns the fresh code
    assert confirm(client, order, "pay_OLD1").get_json()["code"] == item["code"]


def test_recent_code_is_not_resigned(client, razorpay):
    r = client.post("/ai-plan/order", json={"robot_id": "GIYA001", "plan_id": "monthly"})
    code = confirm(client, r.get_json()["order_id"], "pay_NEW1").get_json()["code"]
    age_code(client.application, "pay_NEW1", 5)
    assert client.get("/ai-plan/pending?robot_id=GIYA001").get_json()[0]["code"] == code


def test_code_from_before_this_version_is_refreshed_by_created(client, razorpay,
                                                             monkeypatch):
    # rows written by the previous version have no code_issued
    r = client.post("/ai-plan/order", json={"robot_id": "GIYA001", "plan_id": "monthly"})
    back_to_now = signed_in_the_past(monkeypatch, 10)
    code = confirm(client, r.get_json()["order_id"], "pay_LEGACY").get_json()["code"]
    back_to_now()
    con = sqlite3.connect(client.application.db_path)
    with con:
        con.execute("UPDATE recharges SET code_issued=NULL, created=? WHERE payment_id=?",
                    (time.time() - 10 * 86400, "pay_LEGACY"))
    con.close()
    item, = client.get("/ai-plan/pending?robot_id=GIYA001").get_json()
    assert item["code"] != code and payload(item["code"])["recharge_id"] == "pay_LEGACY"


def test_applied_codes_are_never_refreshed(client, razorpay):
    r = client.post("/ai-plan/order", json={"robot_id": "GIYA001", "plan_id": "monthly"})
    code = confirm(client, r.get_json()["order_id"], "pay_DONE").get_json()["code"]
    client.post("/ai-plan/applied", json={"recharge_id": "pay_DONE"})
    age_code(client.application, "pay_DONE", 30)
    assert client.get("/ai-plan/pending?robot_id=GIYA001").get_json() == []
    con = sqlite3.connect(client.application.db_path)
    assert con.execute("SELECT code FROM recharges WHERE payment_id='pay_DONE'"
                       ).fetchone()[0] == code
    con.close()
