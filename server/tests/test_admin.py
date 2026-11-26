"""Robot check-in (usage reports) and the internal admin dashboard."""
import csv
import io
import os
import sqlite3
import time

import pytest

import recharge_api
from test_recharge_api import confirm

ADMIN = {"X-Hutark-Admin": "arjun"}


@pytest.fixture
def app(tmp_path):
    robots = tmp_path / "robots.txt"
    robots.write_text("LIZA001 Liza\nGIYA001 Giya\nLUNA001 Luna\n", encoding="utf-8")
    env = dict(os.environ, AI_PLAN_DB=str(tmp_path / "admin.db"),
               AI_PLAN_ROBOTS=str(robots))
    a = recharge_api.create_app(env)
    a.db_path = env["AI_PLAN_DB"]
    return a


@pytest.fixture
def c(app):
    return app.test_client()


def plan(**kw):
    p = {"active": True, "reason": "ok", "credit_inr": 300.0, "used_inr": 12.5,
         "remaining_inr": 287.5, "remaining_pct": 95.8, "days_left": 27.5,
         "valid_until": time.time() + 27.5 * 86400, "low": False, "answers": 40,
         "robot_id": "LIZA001", "recharge_id": "pay_A"}
    p.update(kw)
    return p


def checkin(c, robot="LIZA001", version="v1.0.7", **kw):
    return c.post("/ai-plan/checkin", json={"robot_id": robot, "plan": plan(**kw),
                                            "version": version})


def db(app, sql, args=()):
    con = sqlite3.connect(app.db_path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# ── check-in ──────────────────────────────────────────────────────────────
def test_checkin_records_status_and_answers_like_pending(c, app):
    r = checkin(c, robot="GIYA001")
    assert r.status_code == 200
    item, = r.get_json()                              # its free month, like /pending
    assert item["recharge_id"] == "free_GIYA001"
    st, = db(app, "SELECT robot_id, version, active, reason, credit_inr, used_inr, "
                  "answers, recharge_id FROM robot_status")
    assert st == ("GIYA001", "v1.0.7", 1, "ok", 300.0, 12.5, 40, "pay_A")
    day, = db(app, "SELECT robot_id, recharge_id, used_inr, answers FROM usage_daily")
    assert day == ("GIYA001", "pay_A", 12.5, 40)


def test_checkin_keeps_one_row_per_day_and_plan(c, app):
    checkin(c, used_inr=1.0, answers=3)
    checkin(c, used_inr=2.5, answers=9)
    assert db(app, "SELECT used_inr, answers FROM usage_daily") == [(2.5, 9)]
    checkin(c, used_inr=0.4, answers=1, recharge_id="pay_B")     # new plan same day
    assert len(db(app, "SELECT * FROM usage_daily")) == 2


def test_checkin_ignores_unlisted_and_misspelled_robots(c, app):
    assert checkin(c, robot="NOPE001").status_code == 200
    assert checkin(c, robot="liza001").status_code == 200
    assert db(app, "SELECT * FROM robot_status") == []


@pytest.mark.parametrize("bad", [
    {"used_inr": "lots"}, {"used_inr": -5}, {"used_inr": 1e12}, {"answers": True},
    {"reason": "<script>"}, {"recharge_id": "x y"}, {"days_left": float("nan")},
])
def test_checkin_sanitises_robot_values(c, app, bad):
    r = c.post("/ai-plan/checkin", json={"robot_id": "LIZA001", "plan": plan(**bad),
                                         "version": "<b>v1</b>"})
    assert r.status_code == 200
    row, = db(app, "SELECT version, reason, used_inr, answers, recharge_id, days_left "
                   "FROM robot_status")
    assert row[0] is None                               # bad version dropped
    for k, v in bad.items():
        col = {"used_inr": 2, "answers": 3, "reason": 1, "recharge_id": 4, "days_left": 5}[k]
        assert row[col] is None


def test_checkin_with_garbage_body_still_answers(c):
    assert c.post("/ai-plan/checkin", json={"robot_id": "LIZA001", "plan": "x"}).status_code == 200
    assert c.post("/ai-plan/checkin", json={"plan": {}}).status_code == 400
    assert c.post("/ai-plan/checkin", data="nope").status_code == 400


def test_checkin_never_blocks_recharges_when_recording_fails(c, app, monkeypatch):
    con = sqlite3.connect(app.db_path)
    con.execute("DROP TABLE robot_status")             # simulate a broken table
    con.commit(); con.close()
    r = checkin(c, robot="GIYA001")
    assert r.status_code == 200 and r.get_json()[0]["recharge_id"] == "free_GIYA001"


# ── access ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["/admin", "/admin/admin.js", "/admin/api/summary",
                                  "/admin/api/robots", "/admin/api/robots/LIZA001",
                                  "/admin/api/payments", "/admin/api/payments.csv"])
def test_admin_is_hidden_without_the_nginx_login_header(c, path):
    assert c.get(path).status_code == 404
    assert c.get(path, headers={"X-Hutark-Admin": ""}).status_code == 404
    assert c.get(path, headers={"X-Hutark-Admin": "bad user<>"}).status_code == 404
    assert c.get(path, headers=ADMIN).status_code == 200


def test_admin_page_headers(c):
    r = c.get("/admin", headers=ADMIN)
    assert r.mimetype == "text/html"
    assert "script-src 'self';" in r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    assert html.count("<script") == html.count("<script src=") == 1


# ── data ──────────────────────────────────────────────────────────────────
def test_summary_robots_and_payments(c, app, razorpay):
    # LIZA001 pays (test keys), GIYA001 gets its free month and applies it
    order = c.post("/ai-plan/order", json={"robot_id": "LIZA001",
                                           "plan_id": "monthly"}).get_json()["order_id"]
    assert confirm(c, order, "pay_P1").status_code == 200
    c.post("/ai-plan/applied", json={"recharge_id": "pay_P1"})
    checkin(c, robot="LIZA001", used_inr=10.0, answers=50, recharge_id="pay_P1")
    checkin(c, robot="GIYA001", used_inr=0.0, answers=0, recharge_id="free_GIYA001",
            days_left=2.0)
    c.post("/ai-plan/applied", json={"recharge_id": "free_GIYA001"})
    # an abandoned checkout, 2 h ago
    c.post("/ai-plan/order", json={"robot_id": "LUNA001", "plan_id": "monthly"})
    con = sqlite3.connect(app.db_path)
    con.execute("UPDATE orders SET created=? WHERE robot_id='LUNA001'", (time.time() - 7200,))
    con.commit(); con.close()

    s = c.get("/admin/api/summary", headers=ADMIN).get_json()
    assert s["user"] == "arjun"
    assert s["test"]["all"] == {"inr": 499.0, "payments": 1}
    assert s["live"]["all"] == {"inr": 0.0, "payments": 0}
    assert s["robots"]["listed"] == 3 and s["robots"]["reporting"] == 2
    assert s["robots"]["online"] == 2 and s["robots"]["active_plan"] == 2
    assert s["robots"]["low_or_out"] == 1                      # GIYA001: 2 days left
    assert s["usage"] == {"answers": 50, "used_inr": 10.0}
    assert s["free_months"] == {"issued": 1, "applied": 1}
    assert s["abandoned_checkouts"] == 1

    robots = {r["robot_id"]: r for r in c.get("/admin/api/robots", headers=ADMIN).get_json()}
    assert list(robots) == ["LIZA001", "GIYA001", "LUNA001"]   # factory list order
    liza = robots["LIZA001"]
    assert (liza["model"], liza["online"], liza["version"], liza["used_inr"]) == \
        ("Liza", True, "v1.0.7", 10.0)
    assert (liza["payments"], liza["paid_test_inr"], liza["paid_live_inr"]) == (1, 499.0, 0.0)
    assert robots["GIYA001"]["free_month"] == "applied"
    assert robots["LUNA001"]["last_seen"] is None and robots["LUNA001"]["online"] is False

    pays = c.get("/admin/api/payments", headers=ADMIN).get_json()
    assert [(p["kind"], p["robot_id"]) for p in pays] == [("free", "GIYA001"), ("paid", "LIZA001")]
    paid = pays[1]
    assert (paid["amount_inr"], paid["mode"], paid["applied"], paid["source"]) == \
        (499.0, "test", True, "confirm")
    with_unpaid = c.get("/admin/api/payments?unpaid=1", headers=ADMIN).get_json()
    assert [p["kind"] for p in with_unpaid].count("unpaid") == 1

    d = c.get("/admin/api/robots/LIZA001", headers=ADMIN).get_json()
    assert d["robot"]["robot_id"] == "LIZA001"
    assert d["usage"] == [{"day": recharge_api.ist_day(time.time()),
                           "spent_inr": 10.0, "answers": 50}]
    assert [p["payment_id"] for p in d["payments"]] == ["pay_P1"]
    assert c.get("/admin/api/robots/NOPE", headers=ADMIN).status_code == 404


def test_daily_usage_is_growth_per_day_across_plans(c, app):
    con = sqlite3.connect(app.db_path)
    today = recharge_api.ist_day(time.time())
    d1 = recharge_api.ist_day(time.time() - 2 * 86400)
    d2 = recharge_api.ist_day(time.time() - 86400)
    con.executemany("INSERT INTO usage_daily VALUES (?,?,?,?,?,?,?)", [
        ("LIZA001", d1, "pay_A", 5.0, 10, 300, 0),
        ("LIZA001", d2, "pay_A", 8.0, 16, 300, 0),     # +3 / +6
        ("LIZA001", today, "pay_A", 9.0, 18, 300, 0),  # +1 / +2 (then a new plan)
        ("LIZA001", today, "pay_B", 0.5, 1, 300, 0),   # +0.5 / +1
    ])
    con.commit(); con.close()
    usage = c.get("/admin/api/robots/LIZA001", headers=ADMIN).get_json()["usage"]
    assert usage == [{"day": d1, "spent_inr": 5.0, "answers": 10},
                     {"day": d2, "spent_inr": 3.0, "answers": 6},
                     {"day": today, "spent_inr": 1.5, "answers": 3}]


def test_csv_export(c, app, razorpay):
    order = c.post("/ai-plan/order", json={"robot_id": "LIZA001",
                                           "plan_id": "monthly"}).get_json()["order_id"]
    confirm(c, order, "pay_CSV1")
    r = c.get("/admin/api/payments.csv", headers=ADMIN)
    assert r.mimetype == "text/csv"
    assert "attachment" in r.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(r.get_data(as_text=True))))
    assert rows[0][:5] == ["time_ist", "robot_id", "kind", "mode", "amount_inr"]
    assert rows[1][1:5] == ["LIZA001", "paid", "test", "499.0"]
    assert rows[1][7] == "pay_CSV1" and rows[1][10] == "no"


def test_csv_neutralises_formula_cells(c, app):
    con = sqlite3.connect(app.db_path)
    con.execute("INSERT INTO recharges(payment_id, order_id, robot_id, credit_inr, days, "
                "code, created, applied, source) VALUES "
                "('pay_F', NULL, '=HYPERLINK(1)', 300, 30, 'x', ?, 0, 'webhook')",
                (time.time(),))
    con.commit(); con.close()
    text = c.get("/admin/api/payments.csv", headers=ADMIN).get_data(as_text=True)
    assert "'=HYPERLINK(1)" in text and ",=HYPERLINK" not in text


def test_orders_record_key_mode_and_old_orders_become_test(tmp_path, razorpay):
    db_path = tmp_path / "old.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        "CREATE TABLE orders(order_id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, "
        "plan_id TEXT NOT NULL, amount_paise INTEGER NOT NULL, created REAL NOT NULL, "
        "credit_inr REAL, days INTEGER);"
        "INSERT INTO orders VALUES ('order_OLD', 'LIZA001', 'monthly', 49900, 1, 300, 30);")
    con.commit(); con.close()
    env = dict(os.environ, AI_PLAN_DB=str(db_path), RAZORPAY_KEY_ID="rzp_live_dummy")
    c = recharge_api.create_app(env).test_client()
    c.post("/ai-plan/order", json={"robot_id": "LIZA001", "plan_id": "monthly"})
    con = sqlite3.connect(db_path)
    modes = dict(con.execute("SELECT order_id, mode FROM orders").fetchall())
    con.close()
    assert modes.pop("order_OLD") == "test"
    assert list(modes.values()) == ["live"]
