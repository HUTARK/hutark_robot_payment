"""Payment web page, factory robot list, and the status endpoint the page polls."""
import os
import time

import pytest

import recharge_api
from test_recharge_api import confirm, make_order


@pytest.fixture
def robots_file(tmp_path):
    p = tmp_path / "robots.txt"
    p.write_text("# factory list\nGIYA001  Giya\nLISA002 Lisa   # comment\nBARE003\n\n"
                 "bad id with spaces\n", encoding="utf-8")
    return p


@pytest.fixture
def reg_client(tmp_path, robots_file):
    env = dict(os.environ, AI_PLAN_DB=str(tmp_path / "reg.db"),
               AI_PLAN_ROBOTS=str(robots_file))
    a = recharge_api.create_app(env)
    a.config["TESTING"] = True
    return a.test_client()


# ── web page ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path,ctype", [("/recharge", "text/html"),
                                        ("/recharge.js", "text/javascript"),
                                        ("/recharge.css", "text/css")])
def test_web_files_served_with_security_headers(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200
    assert r.mimetype == ctype
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self' https://checkout.razorpay.com;" in csp
    assert "frame-ancestors 'none'" in csp and "'unsafe-eval'" not in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_page_has_no_inline_script(client):
    html = client.get("/recharge").get_data(as_text=True)
    # CSP forbids inline scripts, so every <script> must have a src.
    assert html.count("<script") == html.count("<script src=") == 2
    assert 'src="/recharge.js"' in html and "checkout.razorpay.com/v1/checkout.js" in html


def test_root_redirects_to_page(client):
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/recharge")


def test_no_path_traversal_via_web_routes(client):
    assert client.get("/recharge.js/../recharge_api.py").status_code == 404
    assert client.get("/../recharge_api.py").status_code == 404


# ── robot lookup ──────────────────────────────────────────────────────────
def test_robot_lookup_without_list_accepts_any_valid_id(client):
    r = client.get("/ai-plan/robot?robot_id=ANY_ID-1")
    assert r.status_code == 200 and r.get_json() == {"robot_id": "ANY_ID-1", "model": None}
    assert client.get("/ai-plan/robot?robot_id=bad id").status_code == 400
    assert client.get("/ai-plan/robot").status_code == 400


def test_robot_lookup_with_list(reg_client):
    assert reg_client.get("/ai-plan/robot?robot_id=GIYA001").get_json() == \
        {"robot_id": "GIYA001", "model": "Giya"}
    assert reg_client.get("/ai-plan/robot?robot_id=LISA002").get_json()["model"] == "Lisa"
    assert reg_client.get("/ai-plan/robot?robot_id=BARE003").get_json()["model"] is None
    r = reg_client.get("/ai-plan/robot?robot_id=GIYA01")
    assert r.status_code == 404 and r.get_json() == {"error": "unknown robot_id"}
    assert reg_client.get("/ai-plan/robot?robot_id=giya001").status_code == 404
    assert reg_client.get("/ai-plan/robot?robot_id=not").status_code == 404


def test_order_refused_for_robot_not_on_list(reg_client, razorpay):
    r = reg_client.post("/ai-plan/order", json={"robot_id": "NOPE999", "plan_id": "monthly"})
    assert r.status_code == 404 and r.get_json() == {"error": "unknown robot_id"}
    assert razorpay.calls == []                      # no Razorpay order created
    assert reg_client.post("/ai-plan/order", json={"robot_id": "GIYA001",
                                                   "plan_id": "monthly"}).status_code == 200


def test_robot_list_reloads_when_file_changes(reg_client, robots_file):
    assert reg_client.get("/ai-plan/robot?robot_id=NEW004").status_code == 404
    robots_file.write_text("GIYA001 Giya\nNEW004 Giya\n", encoding="utf-8")
    st = robots_file.stat()
    os.utime(robots_file, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert reg_client.get("/ai-plan/robot?robot_id=NEW004").status_code == 200
    assert reg_client.get("/ai-plan/robot?robot_id=LISA002").status_code == 404


def test_robot_list_missing_fails_closed(tmp_path, razorpay):
    env = dict(os.environ, AI_PLAN_DB=str(tmp_path / "x.db"),
               AI_PLAN_ROBOTS=str(tmp_path / "does-not-exist.txt"))
    c = recharge_api.create_app(env).test_client()
    assert c.get("/ai-plan/robot?robot_id=GIYA001").status_code == 404
    assert c.post("/ai-plan/order", json={"robot_id": "GIYA001",
                                          "plan_id": "monthly"}).status_code == 404
    assert razorpay.calls == []


def test_robot_list_deleted_later_keeps_last_good_list(reg_client, robots_file):
    assert reg_client.get("/ai-plan/robot?robot_id=GIYA001").status_code == 200
    robots_file.unlink()
    assert reg_client.get("/ai-plan/robot?robot_id=GIYA001").status_code == 200
    assert reg_client.get("/ai-plan/robot?robot_id=NOPE").status_code == 404


# ── recharge status (polled by the page) ──────────────────────────────────
def test_recharge_status_follows_applied(client, razorpay):
    order = make_order(client)
    assert confirm(client, order, "pay_WEB1").status_code == 200
    r = client.get("/ai-plan/recharge-status?recharge_id=pay_WEB1")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"recharge_id": "pay_WEB1", "robot_id": "GIYA001",
                    "applied": False, "applied_at": None}
    assert "code" not in body                          # never hands out the voucher
    t0 = time.time()
    assert client.post("/ai-plan/applied", json={"recharge_id": "pay_WEB1"}).status_code == 200
    body = client.get("/ai-plan/recharge-status?recharge_id=pay_WEB1").get_json()
    assert body["applied"] is True and body["applied_at"] >= t0 - 1


def test_recharge_status_bad_and_unknown(client):
    assert client.get("/ai-plan/recharge-status").status_code == 400
    assert client.get("/ai-plan/recharge-status?recharge_id=pay x").status_code == 400
    r = client.get("/ai-plan/recharge-status?recharge_id=pay_NOPE")
    assert r.status_code == 404 and r.get_json() == {"error": "unknown recharge_id"}


def test_new_lookups_are_rate_limited(client):
    limit = recharge_api.RATE_LIMITS["lookup"][0]
    codes = [client.get("/ai-plan/robot?robot_id=X").status_code for _ in range(limit)]
    assert codes == [200] * limit
    r = client.get("/ai-plan/recharge-status?recharge_id=pay_X")
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
