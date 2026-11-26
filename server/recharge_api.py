#!/usr/bin/env python3
"""
recharge_api.py - Hutark AI-plan backend: Razorpay payment -> signed recharge code.

Endpoints (full reference in SERVER_SETUP.md)
  GET  /health                       {"ok": true}
  GET  /ai-plan/plans                [{plan_id, price_inr, credit_inr, days}]
  POST /ai-plan/order    {robot_id, plan_id}           -> Razorpay order for checkout
  POST /ai-plan/confirm  {razorpay_order_id, razorpay_payment_id, razorpay_signature}
                                                       -> {code, recharge_id, ...}
  POST /ai-plan/webhook  Razorpay webhook (payment.captured) - issues the code even
                         if the app never calls /confirm
  GET  /ai-plan/pending?robot_id=X   codes issued for X, not yet applied
                                     (the robot polls this itself)
  POST /ai-plan/applied  {recharge_id}                 -> mark a code as applied
  GET  /ai-plan/robot?robot_id=X     is X a Hutark robot (+ model), for the pay page
  GET  /ai-plan/recharge-status?recharge_id=pay_X      applied yet? (pay page)
  GET  /recharge[?robot=X]           customer payment web page (web/recharge.html)

Safety
  * The price comes from the ORDER stored here, never from the app.
  * /confirm checks the checkout signature HMAC-SHA256(order_id|payment_id,
    key secret); /webhook checks X-Razorpay-Signature = HMAC-SHA256(raw body,
    webhook secret).
  * One payment = one code (payment_id is the PRIMARY KEY, order_id is UNIQUE).
    If /confirm and the webhook race, the loser of the INSERT gets an
    IntegrityError and returns the winner's code. Even the losing code is
    harmless: it carries the same recharge_id, and a robot accepts each
    recharge_id only once.
  * Per-IP rate limits (in memory, per gunicorn worker) and a 64 KB body limit.

Config: environment variables only (see REQUIRED_ENV). Missing ones stop the
app at startup, so a broken /etc/hutark-ai.env shows up in `journalctl`.
Optional: AI_PLAN_ROBOTS = path of the factory robot list (one robot_id per
line, optionally followed by the model, `#` comments). When set, orders are
accepted only for robots on that list, so a typo on the payment page cannot
pay for a robot that does not exist. Re-read automatically when it changes.

Local run (test keys):  python recharge_api.py   (listens on 127.0.0.1:8080)
Production:             gunicorn -w 2 -b 127.0.0.1:8080 recharge_api:app
"""
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

import ai_recharge

# ── PLANS (business decision) ─────────────────────────────────────────────
# price_inr = what the customer pays; credit_inr = rupees of OpenAI usage the
# robot allows. The difference is Hutark's margin / service fee.
PLANS = {
    "monthly": {"price_inr": 499, "credit_inr": 300, "days": 30},
}

REQUIRED_ENV = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET",
                "RAZORPAY_WEBHOOK_SECRET", "HUTARK_AI_PRIVATE_KEY", "AI_PLAN_DB")

RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"
MAX_BODY_BYTES = 64 * 1024          # Razorpay webhooks are a few KB

# (requests, per seconds) per client IP, per gunicorn worker. Generous because
# mobile carriers put many customers behind one IP (CGNAT).
RATE_LIMITS = {
    "order":   (20, 60),
    "confirm": (40, 60),
    "lookup":  (120, 60),           # /pending and /applied
}

ROBOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RZP_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")     # order_XXXX / pay_XXXX

log = logging.getLogger("hutark-ai")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# Razorpay Checkout runs its own script and iframe; nothing else may.
WEB_CSP = ("default-src 'self'; script-src 'self' https://checkout.razorpay.com; "
           "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
           "connect-src 'self' https://*.razorpay.com; "
           "img-src 'self' data: https://*.razorpay.com; "
           "style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; "
           "form-action https://api.razorpay.com; frame-ancestors 'none'")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
    order_id TEXT PRIMARY KEY, robot_id TEXT NOT NULL, plan_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL, created REAL NOT NULL,
    credit_inr REAL, days INTEGER);
CREATE TABLE IF NOT EXISTS recharges(
    payment_id TEXT PRIMARY KEY, order_id TEXT UNIQUE, robot_id TEXT,
    credit_inr REAL, days INTEGER, code TEXT, created REAL,
    applied INTEGER NOT NULL DEFAULT 0, applied_at REAL, source TEXT);
"""
# columns added after the first version (older databases get them via ALTER)
MIGRATIONS = {
    "orders": [("credit_inr", "REAL"), ("days", "INTEGER")],
    "recharges": [("applied", "INTEGER NOT NULL DEFAULT 0"),
                  ("applied_at", "REAL"), ("source", "TEXT")],
}
RECHARGE_COLS = "payment_id, order_id, robot_id, credit_inr, days, code, created"


class RobotRegistry:
    """Factory list of robot ids (optional). Re-read when the file changes."""

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._robots = {}
        self._lock = threading.Lock()

    def _load(self):
        mtime = os.stat(self.path).st_mtime_ns
        if mtime == self._mtime:
            return
        robots = {}
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                parts = line.split("#", 1)[0].split(None, 1)
                if parts and ROBOT_ID_RE.match(parts[0]):
                    robots[parts[0]] = parts[1].strip() if len(parts) > 1 else None
        self._robots, self._mtime = robots, mtime
        log.info("robot list loaded: %d robots", len(robots))

    def lookup(self, robot_id):
        """-> (known, model). With no list configured every valid id is known."""
        if not self.path:
            return True, None
        with self._lock:
            try:
                self._load()
            except OSError as e:
                # Keep the last good list; with none at all, refuse (fail closed).
                log.error("robot list %s unreadable: %s", self.path, type(e).__name__)
                if self._mtime is None:
                    return False, None
            if robot_id in self._robots:
                return True, self._robots[robot_id]
            return False, None


class RateLimiter:
    """Sliding-window limiter keyed by client IP. Thread-safe, in memory."""

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        cutoff = now - self.window
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 10000:             # forget idle IPs
                for k in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
                    del self._hits[k]
            return True


def _hmac_ok(secret, message, given):
    if not isinstance(given, str) or not given:
        return False
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), given.encode("utf-8", "replace"))


def _json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _str(v):
    return v.strip() if isinstance(v, str) else ""


def create_app(env=None):
    env = os.environ if env is None else env
    missing = [k for k in REQUIRED_ENV if not env.get(k)]
    if missing:
        raise RuntimeError("missing environment variables: " + ", ".join(missing))
    cfg = {k: env[k] for k in REQUIRED_ENV}

    # The signing key: make_code() reads ai_recharge.PRIVATE_KEY_FILE.
    ai_recharge.PRIVATE_KEY_FILE = cfg["HUTARK_AI_PRIVATE_KEY"]
    try:
        ai_recharge._private_key()                  # fail fast, never print it
    except Exception as e:
        raise RuntimeError(f"cannot load the private key from "
                           f"{cfg['HUTARK_AI_PRIVATE_KEY']} ({type(e).__name__})") from None

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    app.json.sort_keys = False
    # gunicorn listens on 127.0.0.1 only, so every request comes through nginx,
    # which appends the real client IP to X-Forwarded-For.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    limiters = {name: RateLimiter(*lim) for name, lim in RATE_LIMITS.items()}
    registry = RobotRegistry(env.get("AI_PLAN_ROBOTS") or None)
    db_path = cfg["AI_PLAN_DB"]

    if not log.handlers and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)

    # ── database ──────────────────────────────────────────────────────────
    @contextmanager
    def db():
        con = sqlite3.connect(db_path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=15000")
            con.execute("PRAGMA synchronous=NORMAL")
            with con:                               # commit / rollback
                yield con
        finally:
            con.close()

    def init_db():
        con = sqlite3.connect(db_path, timeout=15)
        try:
            con.execute("PRAGMA journal_mode=WAL")  # persistent, set once
            con.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                for name, decl in cols:
                    if name not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            con.execute("CREATE INDEX IF NOT EXISTS recharges_robot_pending "
                        "ON recharges(robot_id, applied)")
            con.commit()
        finally:
            con.close()

    init_db()

    # ── helpers ───────────────────────────────────────────────────────────
    def client_ip():
        return request.remote_addr or "?"

    def rate_limited(name):
        if limiters[name].allow(client_ip()):
            return None
        resp = jsonify(error="too many requests, try again in a minute")
        resp.status_code = 429
        resp.headers["Retry-After"] = str(RATE_LIMITS[name][1])
        return resp

    def recharge_json(row):
        return {"code": row["code"], "recharge_id": row["payment_id"],
                "robot_id": row["robot_id"], "credit_inr": row["credit_inr"],
                "days": row["days"]}

    def issue_for_payment(payment_id, order_id, source, amount_paise=None,
                          currency=None):
        """Issue (or return the already issued) code for one payment.
        -> (status, row). status: "issued", "existing", "unknown_order",
        "order_used" (order already paid by another payment), "amount_mismatch"."""
        with db() as con:
            row = con.execute(f"SELECT {RECHARGE_COLS} FROM recharges "
                              "WHERE payment_id=?", (payment_id,)).fetchone()
            if row:
                if row["order_id"] != order_id:
                    return "order_used", None
                return "existing", row
            order = con.execute("SELECT robot_id, plan_id, amount_paise, credit_inr, "
                                "days FROM orders WHERE order_id=?",
                                (order_id,)).fetchone()
            if not order:
                return "unknown_order", None
            if con.execute("SELECT 1 FROM recharges WHERE order_id=?",
                           (order_id,)).fetchone():
                return "order_used", None
        if amount_paise is not None and (amount_paise != order["amount_paise"]
                                         or currency != "INR"):
            return "amount_mismatch", None

        if order["credit_inr"] is not None and order["days"] is not None:
            credit, days = order["credit_inr"], order["days"]
        else:                                       # orders from the old schema
            plan = PLANS[order["plan_id"]]
            credit, days = plan["credit_inr"], plan["days"]
        code = ai_recharge.make_code(order["robot_id"], credit, days,
                                     recharge_id=payment_id)
        now = time.time()
        try:
            with db() as con:
                con.execute("INSERT INTO recharges(payment_id, order_id, robot_id, "
                            "credit_inr, days, code, created, applied, source) "
                            "VALUES (?,?,?,?,?,?,?,0,?)",
                            (payment_id, order_id, order["robot_id"], credit, days,
                             code, now, source))
        except sqlite3.IntegrityError:
            # /confirm and the webhook arrived together and the other one won.
            with db() as con:
                row = con.execute(f"SELECT {RECHARGE_COLS} FROM recharges "
                                  "WHERE payment_id=?", (payment_id,)).fetchone()
            if row and row["order_id"] == order_id:
                return "existing", row
            return "order_used", None
        log.info("issued code: payment=%s order=%s robot=%s credit=%s days=%s via=%s",
                 payment_id, order_id, order["robot_id"], credit, days, source)
        return "issued", {"payment_id": payment_id, "order_id": order_id,
                          "robot_id": order["robot_id"], "credit_inr": credit,
                          "days": days, "code": code, "created": now}

    # ── errors: always JSON ───────────────────────────────────────────────
    @app.before_request
    def limit_body():
        if (request.content_length or 0) > MAX_BODY_BYTES:
            raise RequestEntityTooLarge()

    @app.errorhandler(HTTPException)
    def http_error(e):
        return jsonify(error=e.name.lower()), e.code

    @app.errorhandler(Exception)
    def server_error(e):
        log.exception("unhandled error on %s %s", request.method, request.path)
        return jsonify(error="internal error"), 500

    # ── payment web page ──────────────────────────────────────────────────
    def web_file(name, mimetype):
        resp = send_from_directory(WEB_DIR, name, mimetype=mimetype, max_age=300)
        resp.headers["Content-Security-Policy"] = WEB_CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"      # robot id is in the URL
        return resp

    @app.get("/")
    def root():
        return redirect("/recharge", code=302)

    @app.get("/recharge")
    def recharge_page():
        return web_file("recharge.html", "text/html")

    @app.get("/recharge.js")
    def recharge_js():
        return web_file("recharge.js", "text/javascript")

    @app.get("/recharge.css")
    def recharge_css():
        return web_file("recharge.css", "text/css")

    # ── endpoints ─────────────────────────────────────────────────────────
    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.get("/ai-plan/plans")
    def plans():
        return jsonify([{"plan_id": pid, "price_inr": p["price_inr"],
                         "credit_inr": p["credit_inr"], "days": p["days"]}
                        for pid, p in PLANS.items()])

    @app.post("/ai-plan/order")
    def create_order():
        limited = rate_limited("order")
        if limited:
            return limited
        body = _json_body()
        robot_id = _str(body.get("robot_id"))
        plan_id = body.get("plan_id")
        plan = PLANS.get(plan_id) if isinstance(plan_id, str) else None
        if not ROBOT_ID_RE.match(robot_id) or not plan:
            return jsonify(error="robot_id and a valid plan_id are required"), 400
        if not registry.lookup(robot_id)[0]:
            log.warning("order refused: unknown robot %s from %s", robot_id, client_ip())
            return jsonify(error="unknown robot_id"), 404
        amount = int(round(plan["price_inr"] * 100))
        try:
            r = requests.post(RAZORPAY_ORDERS_URL,
                              auth=(cfg["RAZORPAY_KEY_ID"], cfg["RAZORPAY_KEY_SECRET"]),
                              timeout=15,
                              json={"amount": amount, "currency": "INR",
                                    "receipt": f"ai-{robot_id}-{int(time.time())}"[:40],
                                    "notes": {"robot_id": robot_id, "plan_id": plan_id}})
            order_id = r.json().get("id") if r.status_code == 200 else None
        except (requests.RequestException, ValueError) as e:
            log.warning("razorpay order call failed: %s", type(e).__name__)
            order_id = None
        else:
            if r.status_code != 200:
                log.warning("razorpay order call returned HTTP %s", r.status_code)
        if not isinstance(order_id, str) or not RZP_ID_RE.match(order_id):
            return jsonify(error="could not create the payment order"), 502
        with db() as con:
            con.execute("INSERT INTO orders(order_id, robot_id, plan_id, amount_paise, "
                        "created, credit_inr, days) VALUES (?,?,?,?,?,?,?)",
                        (order_id, robot_id, plan_id, amount, time.time(),
                         plan["credit_inr"], plan["days"]))
        log.info("order created: order=%s robot=%s plan=%s amount_paise=%s",
                 order_id, robot_id, plan_id, amount)
        return jsonify(order_id=order_id, amount_paise=amount, currency="INR",
                       key_id=cfg["RAZORPAY_KEY_ID"], robot_id=robot_id,
                       plan_id=plan_id)

    @app.post("/ai-plan/confirm")
    def confirm():
        limited = rate_limited("confirm")
        if limited:
            return limited
        body = _json_body()
        order_id = _str(body.get("razorpay_order_id"))
        payment_id = _str(body.get("razorpay_payment_id"))
        signature = body.get("razorpay_signature")
        if not (RZP_ID_RE.match(order_id) and RZP_ID_RE.match(payment_id)
                and _hmac_ok(cfg["RAZORPAY_KEY_SECRET"],
                             f"{order_id}|{payment_id}".encode(), signature)):
            log.warning("confirm: bad signature or ids from %s", client_ip())
            return jsonify(error="payment could not be verified"), 400
        status, row = issue_for_payment(payment_id, order_id, "confirm")
        if status in ("issued", "existing"):
            return jsonify(recharge_json(row))
        if status == "unknown_order":
            return jsonify(error="unknown order"), 400
        return jsonify(error="this order was already paid by another payment"), 409

    @app.post("/ai-plan/webhook")
    def webhook():
        raw = request.get_data()
        if not _hmac_ok(cfg["RAZORPAY_WEBHOOK_SECRET"], raw,
                        request.headers.get("X-Razorpay-Signature")):
            log.warning("webhook: bad signature from %s", client_ip())
            return jsonify(error="invalid signature"), 400
        # Signature is valid: from here on Razorpay always gets 200 for
        # anything we decide (ignored / duplicate / issued). Only a genuine
        # server fault (e.g. disk full) answers 500, so Razorpay retries.
        try:
            event = json.loads(raw)
        except ValueError:
            return jsonify(ok=True, status="ignored", reason="unparseable")
        if not isinstance(event, dict) or event.get("event") != "payment.captured":
            return jsonify(ok=True, status="ignored", reason="event")
        entity = (((event.get("payload") or {}).get("payment") or {})
                  .get("entity") or {})
        payment_id, order_id = _str(entity.get("id")), _str(entity.get("order_id"))
        if not (RZP_ID_RE.match(payment_id) and RZP_ID_RE.match(order_id)):
            return jsonify(ok=True, status="ignored", reason="no order")
        status, _ = issue_for_payment(payment_id, order_id, "webhook",
                                      amount_paise=entity.get("amount"),
                                      currency=entity.get("currency"))
        if status == "amount_mismatch":
            log.error("webhook: amount/currency mismatch for payment=%s order=%s "
                      "- no code issued", payment_id, order_id)
        elif status == "order_used":
            log.error("webhook: order=%s already fulfilled by another payment; "
                      "payment=%s got no code", order_id, payment_id)
        return jsonify(ok=True, status=status)

    @app.get("/ai-plan/pending")
    def pending():
        limited = rate_limited("lookup")
        if limited:
            return limited
        robot_id = _str(request.args.get("robot_id"))
        if not ROBOT_ID_RE.match(robot_id):
            return jsonify(error="robot_id is required"), 400
        with db() as con:
            rows = con.execute("SELECT payment_id, code, credit_inr, days, created "
                               "FROM recharges WHERE robot_id=? AND applied=0 "
                               "ORDER BY created", (robot_id,)).fetchall()
        return jsonify([{"recharge_id": r["payment_id"], "code": r["code"],
                         "credit_inr": r["credit_inr"], "days": r["days"],
                         "created": r["created"]} for r in rows])

    @app.post("/ai-plan/applied")
    def applied():
        limited = rate_limited("lookup")
        if limited:
            return limited
        recharge_id = _str(_json_body().get("recharge_id"))
        if not RZP_ID_RE.match(recharge_id):
            return jsonify(error="recharge_id is required"), 400
        with db() as con:
            cur = con.execute("UPDATE recharges SET applied=1, "
                              "applied_at=COALESCE(applied_at, ?) WHERE payment_id=?",
                              (time.time(), recharge_id))
        if cur.rowcount == 0:
            return jsonify(error="unknown recharge_id"), 404
        return jsonify(ok=True, recharge_id=recharge_id, applied=True)

    @app.get("/ai-plan/robot")
    def robot_info():
        limited = rate_limited("lookup")
        if limited:
            return limited
        robot_id = _str(request.args.get("robot_id"))
        if not ROBOT_ID_RE.match(robot_id):
            return jsonify(error="robot_id is required"), 400
        known, model = registry.lookup(robot_id)
        if not known:
            return jsonify(error="unknown robot_id"), 404
        return jsonify(robot_id=robot_id, model=model)

    @app.get("/ai-plan/recharge-status")
    def recharge_status():
        # Never returns the code itself: a status page has no need for the
        # voucher, so a leaked payment id cannot leak it either.
        limited = rate_limited("lookup")
        if limited:
            return limited
        recharge_id = _str(request.args.get("recharge_id"))
        if not RZP_ID_RE.match(recharge_id):
            return jsonify(error="recharge_id is required"), 400
        with db() as con:
            row = con.execute("SELECT robot_id, applied, applied_at FROM recharges "
                              "WHERE payment_id=?", (recharge_id,)).fetchone()
        if not row:
            return jsonify(error="unknown recharge_id"), 404
        return jsonify(recharge_id=recharge_id, robot_id=row["robot_id"],
                       applied=bool(row["applied"]), applied_at=row["applied_at"])

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8080)
else:
    app = create_app()          # gunicorn recharge_api:app
