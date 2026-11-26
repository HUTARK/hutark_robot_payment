// Hutark AI-plan admin dashboard. Read-only; data from /admin/api/*.
// Every value goes into the page as text (never as HTML).
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var TZ = "Asia/Kolkata";
  var REFRESH_MS = 60000;
  var state = { robots: [], payments: [], summary: null, view: "overview",
                selected: null, detail: null, range: 30 };

  // ── formatting ──────────────────────────────────────────────────────────
  function rupees(n, dp) {
    if (n === null || n === undefined) return "–";
    return "₹" + Number(n).toLocaleString("en-IN",
      { minimumFractionDigits: dp || 0, maximumFractionDigits: dp === undefined ? 2 : dp });
  }
  function when(ts) {
    if (!ts) return "–";
    return new Date(ts * 1000).toLocaleString("en-IN", { timeZone: TZ, day: "2-digit",
      month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function ago(ts) {
    if (!ts) return "never";
    var s = Date.now() / 1000 - ts;
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }
  function el(tag, props, kids) {
    var e = document.createElement(tag);
    if (props) Object.keys(props).forEach(function (k) {
      if (k === "text") e.textContent = props[k];
      else if (k === "cls") e.className = props[k];
      else e.setAttribute(k, props[k]);
    });
    (kids || []).forEach(function (c) {
      if (c === null || c === undefined) return;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return e;
  }
  function status(kind, label) { return el("span", { cls: "st " + kind, text: label }); }

  // Plan state for a robot, as (status kind, label). Never color alone.
  function planState(r) {
    if (!r.last_seen) return ["none", "Not reporting"];
    if (r.reason === "no_plan") return ["serious", "No plan"];
    if (r.reason === "expired") return ["critical", "Expired"];
    if (r.reason === "credit_used_up") return ["critical", "Credit used up"];
    var low = (r.days_left !== null && r.days_left <= 3) ||
              (r.credit_inr && r.remaining_inr !== null && r.remaining_inr < r.credit_inr * 0.1);
    return low ? ["warning", "Running low"] : ["good", "Active"];
  }
  function needsAttention(r) {
    var k = planState(r)[0];
    return k === "serious" || k === "critical" || k === "warning" || r.waiting_codes > 0;
  }
  function meter(r) {
    if (!r.credit_inr) return el("span", { cls: "muted", text: "–" });
    var pct = Math.max(0, Math.min(100, 100 * (r.used_inr || 0) / r.credit_inr));
    var fill = el("span", { cls: "fill" }); fill.style.width = pct.toFixed(1) + "%";
    return el("span", { cls: "meter", title: pct.toFixed(1) + "% used" }, [
      el("span", { cls: "track" }, [fill]),
      el("span", { text: rupees(r.used_inr, 2) + " of " + rupees(r.credit_inr) })]);
  }
  function online(r) {
    if (!r.last_seen) return status("none", "Never");
    return r.online ? status("good", "Online") : el("span", { cls: "muted", text: ago(r.last_seen) });
  }
  function kindBadge(p) {
    var label = { paid: "Paid", free: "Free month", unpaid: "Not paid" }[p.kind] || p.kind;
    return el("span", { cls: "badge", text: label });
  }
  function modeBadge(m) { return el("span", { cls: "badge " + (m === "live" ? "live" : ""), text: m }); }
  function delivered(p) {
    if (p.kind === "unpaid") return el("span", { cls: "muted", text: "–" });
    return p.applied ? status("good", when(p.applied_at))
                     : status("warning", "Waiting for robot");
  }
  function via(p) {
    return { confirm: "Payment page / app", webhook: "Razorpay webhook", free: "Free month" }[p.source] ||
           (p.kind === "unpaid" ? "Checkout closed" : "–");
  }

  // ── tables ──────────────────────────────────────────────────────────────
  function table(t, cols, rows, onClick, selectedKey) {
    t.textContent = "";
    var head = el("tr", null, cols.map(function (c) {
      return el("th", { cls: c.num ? "num" : "", text: c.title });
    }));
    t.appendChild(el("thead", null, [head]));
    var body = el("tbody");
    rows.forEach(function (r) {
      var tr = el("tr", null, cols.map(function (c) {
        var v = c.render(r);
        return el("td", { cls: c.num ? "num" : "" }, [typeof v === "string" ? v : v]);
      }));
      if (onClick) {
        tr.className = "click" + (selectedKey && selectedKey(r) ? " selected" : "");
        tr.tabIndex = 0;
        tr.addEventListener("click", function () { onClick(r); });
        tr.addEventListener("keydown", function (e) { if (e.key === "Enter") onClick(r); });
      }
      body.appendChild(tr);
    });
    t.appendChild(body);
  }

  var robotCols = [
    { title: "Robot", render: function (r) {
        return el("span", null, [el("b", { text: r.robot_id }),
          r.model ? el("span", { cls: "muted", text: " · " + r.model }) : null,
          r.listed ? null : el("span", { cls: "muted", text: " (not on list)" })]); } },
    { title: "Plan", render: function (r) { var s = planState(r); return status(s[0], s[1]); } },
    { title: "Credit used", render: meter },
    { title: "Days left", num: true, render: function (r) {
        return r.days_left === null || r.days_left === undefined ? "–" : String(r.days_left); } },
    { title: "Answers", num: true, render: function (r) {
        return r.answers === null || r.answers === undefined ? "–" : r.answers.toLocaleString("en-IN"); } },
    { title: "Seen", render: online },
    { title: "Paid (live)", num: true, render: function (r) { return rupees(r.paid_live_inr); } },
    { title: "Payments", num: true, render: function (r) { return String(r.payments); } },
    { title: "Free month", render: function (r) {
        return { none: "–", waiting: "Waiting", applied: "Used" }[r.free_month] || "–"; } },
    { title: "Version", render: function (r) { return r.version || "–"; } }
  ];
  var payCols = [
    { title: "Time (IST)", render: function (p) { return when(p.time); } },
    { title: "Robot", render: function (p) { return el("b", { text: p.robot_id }); } },
    { title: "Type", render: kindBadge },
    { title: "Mode", render: function (p) { return p.kind === "free" ? "–" : modeBadge(p.mode); } },
    { title: "Amount", num: true, render: function (p) { return rupees(p.amount_inr); } },
    { title: "Credit", num: true, render: function (p) {
        return rupees(p.credit_inr) + " / " + p.days + " d"; } },
    { title: "Via", render: via },
    { title: "Delivered to robot", render: delivered },
    { title: "Payment ID", render: function (p) {
        return el("code", { text: p.payment_id || p.order_id || "" }); } }
  ];

  // ── overview ────────────────────────────────────────────────────────────
  function tile(k, v, s) {
    return el("div", { cls: "tile" }, [el("div", { cls: "k", text: k }),
      el("div", { cls: "v", text: v }), s ? el("div", { cls: "s", text: s }) : null]);
  }
  function renderOverview() {
    var s = state.summary; if (!s) return;
    $("hero-revenue").textContent = rupees(s.live.month.inr);
    $("hero-sub").textContent = s.live.month.payments + " live payments this month · " +
      rupees(s.live.all.inr) + " all time" +
      (s.test.all.payments ? " · test mode: " + s.test.all.payments + " payments (" +
        rupees(s.test.all.inr) + "), not real money" : "");
    var t = $("tiles"); t.textContent = "";
    [tile("Robots online", s.robots.online + " of " + s.robots.listed,
          s.robots.reporting + " have reported at least once"),
     tile("Active plans", String(s.robots.active_plan), "of robots reporting"),
     tile("Expired or ≤ 3 days", String(s.robots.low_or_out), "may need a reminder"),
     tile("ChatGPT answers", s.usage.answers.toLocaleString("en-IN"), "on current plans"),
     tile("Credit used", rupees(s.usage.used_inr, 2), "on current plans (OpenAI cost)"),
     tile("Free months", s.free_months.applied + " used", s.free_months.issued + " issued"),
     tile("Checkouts not paid", String(s.abandoned_checkouts), "opened, then closed")
    ].forEach(function (x) { t.appendChild(x); });
    var att = state.robots.filter(needsAttention);
    $("attention-empty").hidden = att.length > 0;
    $("attention").hidden = att.length === 0;
    table($("attention"), robotCols.slice(0, 6), att, openRobot);
    table($("latest"), payCols.slice(0, 8), state.payments.filter(function (p) {
      return p.kind !== "unpaid"; }).slice(0, 8));
  }

  // ── robots ──────────────────────────────────────────────────────────────
  function renderRobots() {
    var q = $("robot-search").value.trim().toLowerCase();
    var only = $("robot-attention").checked;
    var rows = state.robots.filter(function (r) {
      if (only && !needsAttention(r)) return false;
      return !q || r.robot_id.toLowerCase().indexOf(q) >= 0 ||
             (r.model || "").toLowerCase().indexOf(q) >= 0;
    });
    table($("robots"), robotCols, rows, openRobot,
          function (r) { return r.robot_id === state.selected; });
  }

  function openRobot(r) {
    state.selected = r.robot_id;
    if (state.view !== "robots") show("robots");
    renderRobots();
    loadRobot(r.robot_id, true);
  }

  function loadRobot(id, scroll) {
    get("/admin/api/robots/" + encodeURIComponent(id)).then(function (d) {
      if (!d || state.selected !== id) return;
      state.detail = d;
      renderDetail();
      if (scroll) $("robot-detail").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function renderDetail() {
    var d = state.detail, box = $("robot-detail");
    if (!d) { box.hidden = true; return; }
    var r = d.robot, s = planState(r);
    box.textContent = ""; box.hidden = false;
    box.appendChild(el("div", { cls: "detail-head" }, [
      el("h2", { text: r.robot_id + (r.model ? " · " + r.model : "") }),
      status(s[0], s[1]), online(r),
      el("span", { cls: "muted", text: "software " + (r.version || "unknown") }),
      el("button", { cls: "link", text: "Close" })]));
    box.querySelector(".detail-head button").addEventListener("click", function () {
      state.selected = null; state.detail = null; box.hidden = true; renderRobots(); });

    var tiles = el("div", { cls: "tiles" });
    [tile("Credit used", r.credit_inr ? rupees(r.used_inr, 2) + " of " + rupees(r.credit_inr) : "–",
          r.credit_inr ? Math.round(100 * (r.used_inr || 0) / r.credit_inr) + "% of this plan" : null),
     tile("Days left", r.days_left === null || r.days_left === undefined ? "–" : String(r.days_left),
          r.valid_until ? "until " + when(r.valid_until) : null),
     tile("Answers this plan", r.answers === null || r.answers === undefined ? "–" : String(r.answers),
          r.answers ? "avg " + rupees((r.used_inr || 0) / r.answers, 4) + " each" : null),
     tile("Paid in total", rupees(r.paid_live_inr), r.payments + " payments" +
          (r.paid_test_inr ? " · " + rupees(r.paid_test_inr) + " test" : ""))
    ].forEach(function (x) { tiles.appendChild(x); });
    box.appendChild(tiles);

    // daily spend chart (single series: no legend, the title names it)
    var head = el("div", { cls: "card-head" }, [
      el("h3", { text: "ChatGPT cost per day (₹, IST days)" }),
      el("span", null, [30, 90].map(function (n) {
        var b = el("button", { cls: "link", text: (n === state.range ? "● " : "") + "Last " + n + " days" });
        b.style.marginLeft = "12px";
        b.addEventListener("click", function () { state.range = n; renderDetail(); });
        return b;
      }))]);
    box.appendChild(head);
    var days = fillDays(d.usage, state.range);
    box.appendChild(chart(days));
    var total = days.reduce(function (a, x) { return a + x.spent_inr; }, 0);
    var answers = days.reduce(function (a, x) { return a + x.answers; }, 0);
    box.appendChild(el("p", { cls: "muted small", text: "Last " + state.range + " days: " +
      rupees(total, 2) + " for " + answers.toLocaleString("en-IN") + " answers." +
      (d.usage.length ? "" : " No usage reported yet – the robot needs software v1.0.7 or newer.") }));

    var det = el("details", null, [el("summary", { cls: "muted small", text: "Show as table" })]);
    var tt = el("table");
    table(tt, [{ title: "Day", render: function (x) { return x.day; } },
               { title: "Cost", num: true, render: function (x) { return rupees(x.spent_inr, 4); } },
               { title: "Answers", num: true, render: function (x) { return String(x.answers); } }],
          days.filter(function (x) { return x.spent_inr || x.answers; }).reverse());
    det.appendChild(el("div", { cls: "table-wrap" }, [tt]));
    box.appendChild(det);

    box.appendChild(el("h3", { text: "Payments" }));
    var pt = el("table");
    if (d.payments.length) table(pt, payCols.filter(function (c) { return c.title !== "Robot"; }), d.payments);
    box.appendChild(d.payments.length ? el("div", { cls: "table-wrap" }, [pt])
                                      : el("p", { cls: "muted", text: "No payments yet." }));
  }

  function istDay(date) {
    return new Date(date.getTime()).toLocaleDateString("en-CA", { timeZone: TZ });
  }
  function fillDays(usage, n) {
    var by = {}; usage.forEach(function (u) { by[u.day] = u; });
    var out = [], now = new Date();
    for (var i = n - 1; i >= 0; i--) {
      var day = istDay(new Date(now.getTime() - i * 86400000));
      var u = by[day];
      out.push({ day: day, spent_inr: u ? u.spent_inr : 0, answers: u ? u.answers : 0 });
    }
    return out;
  }

  // Bars: <= 24px wide, 2px gap, 4px rounded top, square at the baseline.
  function chart(days) {
    var NS = "http://www.w3.org/2000/svg";
    var W = 900, H = 220, L = 52, R = 8, T = 10, B = 26;
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("class", "chart");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "ChatGPT cost per day");
    function add(tag, attrs, text) {
      var e = document.createElementNS(NS, tag);
      Object.keys(attrs).forEach(function (k) { e.setAttribute(k, attrs[k]); });
      if (text !== undefined) e.textContent = text;
      svg.appendChild(e); return e;
    }
    var max = Math.max.apply(null, days.map(function (d) { return d.spent_inr; }));
    var step = niceStep(max);
    var top = Math.max(step * 3, step * Math.ceil(max / step || 1));
    var ph = H - T - B, pw = W - L - R;
    for (var g = 0; g <= top + 1e-9; g += step) {
      var y = T + ph - ph * g / top;
      add("line", { x1: L, x2: W - R, y1: y, y2: y, "class": g === 0 ? "base" : "grid" });
      add("text", { x: L - 6, y: y + 4, "text-anchor": "end" }, "₹" + fmtTick(g, step));
    }
    var slot = pw / days.length, bw = Math.min(24, slot - 2);
    days.forEach(function (d, i) {
      var x = L + i * slot + (slot - bw) / 2;
      var h = top ? ph * d.spent_inr / top : 0;
      var yb = T + ph;
      if (h > 0) {
        var rr = Math.min(4, h, bw / 2);
        add("path", { "class": "bar", d: "M" + x + "," + yb + "V" + (yb - h + rr) +
          "Q" + x + "," + (yb - h) + " " + (x + rr) + "," + (yb - h) +
          "H" + (x + bw - rr) + "Q" + (x + bw) + "," + (yb - h) + " " + (x + bw) + "," + (yb - h + rr) +
          "V" + yb + "Z" });
      }
      var hit = add("rect", { "class": "hit", x: L + i * slot, y: T, width: slot, height: ph });
      hit.addEventListener("mousemove", function (e) { tip(e, d); });
      hit.addEventListener("mouseleave", hideTip);
      var every = days.length > 45 ? 14 : 7;
      if ((days.length - 1 - i) % every === 0) {
        add("text", { x: L + i * slot + slot / 2, y: H - 8, "text-anchor": "middle" },
            new Date(d.day + "T00:00:00").toLocaleDateString("en-IN", { day: "numeric", month: "short" }));
      }
    });
    return svg;
  }
  function niceStep(max) {
    if (!max) return 0.01;
    var raw = max / 3, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  function fmtTick(v, step) {
    var dp = step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step)));
    return v.toFixed(dp);
  }
  function tip(e, d) {
    var t = $("tip"); t.textContent = "";
    t.appendChild(el("b", { text: new Date(d.day + "T00:00:00").toLocaleDateString("en-IN",
      { weekday: "short", day: "numeric", month: "short" }) }));
    t.appendChild(document.createTextNode(rupees(d.spent_inr, 4) + " · " + d.answers + " answers"));
    t.hidden = false;
    var x = Math.min(e.clientX + 12, window.innerWidth - t.offsetWidth - 8);
    t.style.left = x + "px"; t.style.top = (e.clientY - t.offsetHeight - 10) + "px";
  }
  function hideTip() { $("tip").hidden = true; }

  // ── payments ────────────────────────────────────────────────────────────
  function renderPayments() {
    var kind = $("pay-kind").value, mode = $("pay-mode").value, robot = $("pay-robot").value;
    var rows = state.payments.filter(function (p) {
      return (!kind || p.kind === kind) && (!robot || p.robot_id === robot) &&
             (!mode || (p.kind !== "free" && p.mode === mode));
    });
    table($("payments"), payCols, rows);
    var paid = rows.filter(function (p) { return p.kind === "paid"; });
    var live = paid.filter(function (p) { return p.mode === "live"; })
                   .reduce(function (a, p) { return a + p.amount_inr; }, 0);
    $("pay-total").textContent = rows.length + " rows · " + paid.length + " paid · live total " +
      rupees(live) + (rows.length >= 2000 ? " · showing the latest 2000 (CSV has all)" : "");
    $("csv").href = "/admin/api/payments.csv" + ($("pay-unpaid").checked ? "?unpaid=1" : "");
  }
  function robotOptions() {
    var sel = $("pay-robot"), cur = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    state.robots.forEach(function (r) {
      sel.appendChild(el("option", { value: r.robot_id,
        text: r.robot_id + (r.model ? " · " + r.model : "") }));
    });
    sel.value = cur;
  }

  // ── data ────────────────────────────────────────────────────────────────
  function get(path) {
    return fetch(path, { cache: "no-store", credentials: "same-origin" }).then(function (r) {
      if (r.status === 401 || r.status === 404) throw new Error("Not signed in – reload the page to log in.");
      if (!r.ok) throw new Error("Server error " + r.status);
      return r.json();
    }).catch(function (e) {
      var b = $("error"); b.textContent = e.message || "Cannot reach the server."; b.hidden = false;
      return null;
    });
  }
  function load() {
    var unpaid = $("pay-unpaid").checked || $("pay-kind").value === "unpaid";
    return Promise.all([get("/admin/api/summary"), get("/admin/api/robots"),
                        get("/admin/api/payments?unpaid=1")]).then(function (res) {
      if (!res[0] || !res[1] || !res[2]) return;
      $("error").hidden = true;
      state.summary = res[0]; state.robots = res[1];
      state.payments = unpaid ? res[2] : res[2].filter(function (p) { return p.kind !== "unpaid"; });
      state.allPayments = res[2];
      $("user").textContent = res[0].user ? "Signed in as " + res[0].user : "";
      $("updated").textContent = "Updated " + new Date().toLocaleTimeString("en-IN",
        { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
      robotOptions(); renderOverview(); renderRobots(); renderPayments();
      if (state.selected) loadRobot(state.selected, false);
    });
  }
  function applyUnpaidFilter() {
    var unpaid = $("pay-unpaid").checked || $("pay-kind").value === "unpaid";
    state.payments = (state.allPayments || []).filter(function (p) {
      return unpaid || p.kind !== "unpaid"; });
    renderPayments();
  }

  function show(view) {
    state.view = view;
    hideTip();
    ["overview", "robots", "payments"].forEach(function (v) {
      $("view-" + v).hidden = v !== view;
    });
    document.querySelectorAll(".tabs button").forEach(function (b) {
      b.setAttribute("aria-selected", String(b.getAttribute("data-view") === view));
    });
    try { history.replaceState(null, "", "#" + view); } catch (e) {}
  }

  document.querySelectorAll(".tabs button").forEach(function (b) {
    b.addEventListener("click", function () { show(b.getAttribute("data-view")); });
  });
  document.querySelectorAll("[data-goto]").forEach(function (b) {
    b.addEventListener("click", function () { show(b.getAttribute("data-goto")); });
  });
  $("refresh").addEventListener("click", load);
  $("robot-search").addEventListener("input", renderRobots);
  $("robot-attention").addEventListener("change", renderRobots);
  ["pay-mode", "pay-robot"].forEach(function (id) { $(id).addEventListener("change", renderPayments); });
  $("pay-kind").addEventListener("change", applyUnpaidFilter);
  $("pay-unpaid").addEventListener("change", applyUnpaidFilter);

  var start = (location.hash || "").slice(1);
  show(["overview", "robots", "payments"].indexOf(start) >= 0 ? start : "overview");
  load();
  setInterval(function () { if (!document.hidden) load(); }, REFRESH_MS);
})();
