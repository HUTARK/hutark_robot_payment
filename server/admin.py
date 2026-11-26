"""
admin.py - internal admin dashboard: robot usage and payment history.

    GET /admin                         dashboard page (web/admin.html)
    GET /admin/api/summary             headline numbers
    GET /admin/api/robots              every robot: plan, usage, last seen, payments
    GET /admin/api/robots/<robot_id>   one robot: status, daily usage, payments
    GET /admin/api/payments            payment history (?unpaid=1 adds abandoned checkouts)
    GET /admin/api/payments.csv        the same as a spreadsheet file

Login is done by nginx (HTTP basic auth over HTTPS, one account per team
member). nginx then passes the signed-in name in the X-Hutark-Admin header
and removes that header from every other request, so the app only has to
check that it is there. Without it every /admin URL answers 404, so a
broken nginx config hides the dashboard instead of opening it.

Read-only: nothing here changes payments, codes or robots. Robot usage comes
from the robots' own check-ins (POST /ai-plan/checkin), so it is what the
robot reported - treat it as information, not as billing data.
"""
import csv
import io
import re
import time

from flask import Response, jsonify, request, send_from_directory

ADMIN_HEADER = "X-Hutark-Admin"
ADMIN_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
ONLINE_WITHIN_S = 15 * 60            # robots check in every 1-5 minutes
ABANDONED_AFTER_S = 30 * 60          # an unpaid order older than this
ADMIN_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
             "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
             "form-action 'none'; frame-ancestors 'none'")


def register(app, db, registry, web_dir, log, ist_day):
    def admin_user():
        u = request.headers.get(ADMIN_HEADER, "")
        return u if ADMIN_USER_RE.match(u) else None

    @app.before_request
    def admin_gate():
        p = request.path
        if p == "/admin" or p.startswith("/admin/"):
            if not admin_user():
                return jsonify(error="not found"), 404
        return None

    def no_store(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = ADMIN_CSP
        return resp

    def web(name, mimetype):
        return no_store(send_from_directory(web_dir, name, mimetype=mimetype, max_age=0))

    @app.get("/admin")
    def admin_page():
        return web("admin.html", "text/html")

    @app.get("/admin/admin.js")
    def admin_js():
        return web("admin.js", "text/javascript")

    @app.get("/admin/admin.css")
    def admin_css():
        return web("admin.css", "text/css")

    # ── data ──────────────────────────────────────────────────────────────
    def payments(con, robot_id=None, unpaid=False, limit=2000):
        """Paid and free recharges (+ abandoned checkouts), newest first."""
        where, args = "", []
        if robot_id:
            where, args = "WHERE r.robot_id=?", [robot_id]
        rows = [dict(r) for r in con.execute(
            "SELECT r.created AS time, r.robot_id, r.payment_id, r.order_id, "
            "COALESCE(o.amount_paise, 0) AS amount_paise, "
            "CASE WHEN r.source='free' THEN 'free' ELSE COALESCE(o.mode, 'test') END AS mode, "
            "r.credit_inr, r.days, r.source, r.applied, r.applied_at "
            f"FROM recharges r LEFT JOIN orders o ON o.order_id=r.order_id {where} "
            "ORDER BY r.created DESC LIMIT ?", args + [limit])]
        for r in rows:
            r["kind"] = "free" if r["source"] == "free" else "paid"
            r["amount_inr"] = r.pop("amount_paise") / 100
            r["applied"] = bool(r["applied"])
        if unpaid:
            where2, args2 = "", [time.time() - ABANDONED_AFTER_S]
            if robot_id:
                where2, args2 = "AND o.robot_id=?", args2 + [robot_id]
            for o in con.execute(
                    "SELECT o.created AS time, o.robot_id, o.order_id, o.amount_paise, "
                    "COALESCE(o.mode, 'test') AS mode, o.credit_inr, o.days "
                    "FROM orders o LEFT JOIN recharges r ON r.order_id=o.order_id "
                    f"WHERE r.order_id IS NULL AND o.created < ? {where2} "
                    "ORDER BY o.created DESC LIMIT ?", args2 + [limit]):
                rows.append({"time": o["time"], "robot_id": o["robot_id"],
                             "payment_id": None, "order_id": o["order_id"],
                             "amount_inr": o["amount_paise"] / 100, "mode": o["mode"],
                             "credit_inr": o["credit_inr"], "days": o["days"],
                             "source": None, "applied": False, "applied_at": None,
                             "kind": "unpaid"})
            rows.sort(key=lambda r: r["time"], reverse=True)
            rows = rows[:limit]
        return rows

    def robot_rows(con):
        now = time.time()
        listed = registry.all()
        models = {rid: model for rid, model in listed}
        status = {r["robot_id"]: dict(r) for r in con.execute("SELECT * FROM robot_status")}
        pay = {r["robot_id"]: dict(r) for r in con.execute(
            "SELECT r.robot_id, "
            "SUM(CASE WHEN COALESCE(r.source,'')!='free' AND o.mode='live' THEN o.amount_paise ELSE 0 END) AS live_paise, "
            "SUM(CASE WHEN COALESCE(r.source,'')!='free' AND COALESCE(o.mode,'test')='test' THEN o.amount_paise ELSE 0 END) AS test_paise, "
            "SUM(CASE WHEN COALESCE(r.source,'')!='free' THEN 1 ELSE 0 END) AS payments, "
            "MAX(CASE WHEN COALESCE(r.source,'')!='free' THEN r.created END) AS last_payment, "
            "MAX(CASE WHEN r.source='free' THEN 1 + r.applied END) AS free_state, "
            "SUM(CASE WHEN r.applied=0 THEN 1 ELSE 0 END) AS waiting "
            "FROM recharges r LEFT JOIN orders o ON o.order_id=r.order_id "
            "GROUP BY r.robot_id")}
        ids = [rid for rid, _ in listed]
        ids += sorted((set(status) | set(pay)) - set(ids))
        out = []
        for rid in ids:
            s, p = status.get(rid, {}), pay.get(rid, {})
            last_seen = s.get("last_seen")
            free_state = {None: "none", 1: "waiting", 2: "applied"}.get(p.get("free_state"))
            out.append({
                "robot_id": rid,
                "model": models.get(rid),
                "listed": rid in models,
                "last_seen": last_seen,
                "online": bool(last_seen and now - last_seen < ONLINE_WITHIN_S),
                "version": s.get("version"),
                "active": bool(s.get("active")) if s else None,
                "reason": s.get("reason"),
                "credit_inr": s.get("credit_inr"),
                "used_inr": s.get("used_inr"),
                "remaining_inr": s.get("remaining_inr"),
                "days_left": s.get("days_left"),
                "valid_until": s.get("valid_until"),
                "answers": s.get("answers"),
                "payments": p.get("payments") or 0,
                "paid_live_inr": (p.get("live_paise") or 0) / 100,
                "paid_test_inr": (p.get("test_paise") or 0) / 100,
                "last_payment": p.get("last_payment"),
                "free_month": free_state,
                "waiting_codes": p.get("waiting") or 0,
            })
        return out

    def daily_usage(con, robot_id, days=90):
        """Spend per day, from the cumulative 'used' the robot reports per
        plan: each day's spend is the growth since the day before."""
        since = ist_day(time.time() - days * 86400)
        rows = con.execute("SELECT day, recharge_id, used_inr, answers, credit_inr "
                           "FROM usage_daily WHERE robot_id=? AND day>=? "
                           "ORDER BY recharge_id, day", (robot_id, since)).fetchall()
        prev = {}
        per_day = {}
        for r in rows:
            last_used, last_answers = prev.get(r["recharge_id"], (0.0, 0))
            used = r["used_inr"] or 0.0
            answers = r["answers"] or 0
            d = per_day.setdefault(r["day"], {"day": r["day"], "spent_inr": 0.0,
                                              "answers": 0})
            d["spent_inr"] += max(0.0, used - last_used)
            d["answers"] += max(0, answers - last_answers)
            prev[r["recharge_id"]] = (used, answers)
        out = sorted(per_day.values(), key=lambda d: d["day"])
        for d in out:
            d["spent_inr"] = round(d["spent_inr"], 4)
        return out

    @app.get("/admin/api/summary")
    def admin_summary():
        now = time.time()
        month = time.strftime("%Y-%m", time.gmtime(now + 5.5 * 3600))
        with db() as con:
            robots = robot_rows(con)
            totals = {f"{r['mode']}_{r['period']}": r for r in (dict(x) for x in con.execute(
                "SELECT COALESCE(o.mode,'test') AS mode, "
                "CASE WHEN strftime('%Y-%m', r.created + 19800, 'unixepoch')=? "
                "THEN 'month' ELSE 'older' END AS period, "
                "COUNT(*) AS n, SUM(o.amount_paise) AS paise "
                "FROM recharges r JOIN orders o ON o.order_id=r.order_id "
                "WHERE COALESCE(r.source,'')!='free' GROUP BY 1, 2", (month,)))}
            free = con.execute("SELECT COUNT(*) AS n, SUM(applied) AS applied "
                               "FROM recharges WHERE source='free'").fetchone()
            abandoned = con.execute(
                "SELECT COUNT(*) FROM orders o LEFT JOIN recharges r "
                "ON r.order_id=o.order_id WHERE r.order_id IS NULL AND o.created<?",
                (now - ABANDONED_AFTER_S,)).fetchone()[0]

        def money(mode, period=None):
            keys = [f"{mode}_{period}"] if period else [f"{mode}_month", f"{mode}_older"]
            return {"inr": sum((totals.get(k) or {}).get("paise") or 0 for k in keys) / 100,
                    "payments": sum((totals.get(k) or {}).get("n") or 0 for k in keys)}

        reported = [r for r in robots if r["last_seen"]]
        return jsonify(
            user=admin_user(),
            month=month,
            live={"month": money("live", "month"), "all": money("live")},
            test={"month": money("test", "month"), "all": money("test")},
            robots={"listed": sum(r["listed"] for r in robots),
                    "reporting": len(reported),
                    "online": sum(r["online"] for r in robots),
                    "active_plan": sum(1 for r in reported if r["active"]),
                    "low_or_out": sum(1 for r in reported if r["reason"] in
                                      ("expired", "credit_used_up")
                                      or (r["active"] and r["days_left"] is not None
                                          and r["days_left"] <= 3))},
            usage={"answers": sum(r["answers"] or 0 for r in reported),
                   "used_inr": round(sum(r["used_inr"] or 0 for r in reported), 2)},
            free_months={"issued": free["n"] or 0, "applied": free["applied"] or 0},
            abandoned_checkouts=abandoned,
            now=now)

    @app.get("/admin/api/robots")
    def admin_robots():
        with db() as con:
            return jsonify(robot_rows(con))

    @app.get("/admin/api/robots/<robot_id>")
    def admin_robot(robot_id):
        with db() as con:
            row = next((r for r in robot_rows(con) if r["robot_id"] == robot_id), None)
            if not row:
                return jsonify(error="unknown robot"), 404
            return jsonify(robot=row, usage=daily_usage(con, robot_id),
                           payments=payments(con, robot_id, unpaid=True, limit=200))

    @app.get("/admin/api/payments")
    def admin_payments():
        unpaid = request.args.get("unpaid") == "1"
        with db() as con:
            return jsonify(payments(con, unpaid=unpaid))

    @app.get("/admin/api/payments.csv")
    def admin_payments_csv():
        unpaid = request.args.get("unpaid") == "1"
        with db() as con:
            rows = payments(con, unpaid=unpaid, limit=100000)
        log.info("admin %s exported %d payment rows", admin_user(), len(rows))
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time_ist", "robot_id", "kind", "mode", "amount_inr", "credit_inr",
                    "days", "payment_id", "order_id", "delivered_via", "applied",
                    "applied_time_ist"])

        def ist(ts):
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.gmtime(ts + 5.5 * 3600)) if ts else ""

        def cell(v):
            # spreadsheet formula injection: never start a cell with = + - @
            s = "" if v is None else str(v)
            return "'" + s if s[:1] in ("=", "+", "-", "@") else s

        for r in rows:
            w.writerow([cell(x) for x in (
                ist(r["time"]), r["robot_id"], r["kind"], r["mode"], r["amount_inr"],
                r["credit_inr"], r["days"], r["payment_id"], r["order_id"],
                r["source"], "yes" if r["applied"] else "no", ist(r["applied_at"]))])
        day = time.strftime("%Y%m%d", time.gmtime(time.time() + 5.5 * 3600))
        return no_store(Response(
            buf.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="hutark-payments-{day}.csv"'}))
