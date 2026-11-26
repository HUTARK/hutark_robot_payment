// Hutark AI-plan payment page. Talks only to this server (same origin) and
// Razorpay Checkout. The price comes from the server's order, never from here.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var ROBOT_RE = /^[A-Za-z0-9_-]{1,64}$/;
  var state = { robot: null, plan: null, busy: false, pollTimer: null };

  function show(el, on) { el.hidden = !on; }
  function msg(el, text) { el.textContent = text || ""; show(el, !!text); }
  function rupees(n) { return "₹" + Number(n).toLocaleString("en-IN"); }

  function api(method, path, body) {
    var opts = { method: method, headers: {}, cache: "no-store" };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { status: r.status, data: data };
      });
    }, function () { return { status: 0, data: {} }; });
  }

  function niceError(res, fallback) {
    if (res.status === 0) return "No connection. Check your internet and try again.";
    if (res.status === 429) return "Too many tries. Please wait a minute and try again.";
    return (res.data && res.data.error) ? fallback + " (" + res.data.error + ")" : fallback;
  }

  // ── 1. robot ──────────────────────────────────────────────────────────
  function checkRobot(id) {
    msg($("robot-err"), ""); msg($("robot-ok"), "");
    if (!ROBOT_RE.test(id)) {
      msg($("robot-err"), "Enter the robot ID exactly as on the label (letters, numbers, - and _).");
      return;
    }
    $("robot-check").disabled = true;
    api("GET", "/ai-plan/robot?robot_id=" + encodeURIComponent(id)).then(function (res) {
      $("robot-check").disabled = false;
      if (res.status === 200) {
        state.robot = id;
        msg($("robot-ok"), "✓ Robot " + id + (res.data.model ? " · " + res.data.model : ""));
        loadPlan();
      } else if (res.status === 404) {
        state.robot = null; show($("plan-card"), false);
        msg($("robot-err"), "We can't find robot “" + id + "”. Check the ID on the label " +
          "(capital letters and zeros matter).");
      } else {
        msg($("robot-err"), niceError(res, "Could not check the robot."));
      }
    });
  }

  // ── 2. plan ───────────────────────────────────────────────────────────
  function loadPlan() {
    api("GET", "/ai-plan/plans").then(function (res) {
      var p = res.status === 200 && res.data && res.data[0];
      if (!p) { msg($("robot-err"), niceError(res, "Could not load the plan.")); return; }
      state.plan = p;
      var box = $("plan"); box.textContent = "";
      var price = document.createElement("div");
      price.className = "price";
      price.textContent = rupees(p.price_inr) + " ";
      var per = document.createElement("span");
      per.className = "per"; per.textContent = "for " + p.days + " days";
      price.appendChild(per);
      var ul = document.createElement("ul");
      [rupees(p.credit_inr) + " of ChatGPT usage for robot " + state.robot,
       "Saved answers, commands and movement always work, with or without a plan"]
        .forEach(function (t) { var li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
      box.appendChild(price); box.appendChild(ul);
      $("pay").textContent = "Pay " + rupees(p.price_inr);
      msg($("pay-err"), "");
      show($("plan-card"), true);
      $("plan-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }

  // ── payment ───────────────────────────────────────────────────────────
  function pay() {
    if (state.busy || !state.robot || !state.plan) return;
    if (typeof window.Razorpay !== "function") {
      msg($("pay-err"), "The payment window could not load. Check your internet, or turn off " +
        "any ad/script blocker, then reload the page.");
      return;
    }
    state.busy = true; $("pay").disabled = true; msg($("pay-err"), "");
    api("POST", "/ai-plan/order", { robot_id: state.robot, plan_id: state.plan.plan_id })
      .then(function (res) {
        if (res.status !== 200) {
          done(); msg($("pay-err"), niceError(res, "Could not start the payment."));
          return;
        }
        var o = res.data;
        show($("testmode"), /^rzp_test_/.test(o.key_id));
        var rzp = new window.Razorpay({
          key: o.key_id, order_id: o.order_id, amount: o.amount_paise, currency: o.currency,
          name: "Hutark", description: "AI plan – robot " + o.robot_id,
          notes: { robot_id: o.robot_id },
          theme: { color: "#1f5eff" },
          handler: function (r) { paid(r); },
          modal: { ondismiss: done }
        });
        rzp.on("payment.failed", function (r) {
          var d = (r && r.error && r.error.description) || "The payment did not go through.";
          msg($("pay-err"), d + " No money was taken for this attempt. You can try again.");
        });
        rzp.open();
      });
  }
  function done() { state.busy = false; $("pay").disabled = false; }

  function paid(r) {
    done();
    show($("robot-card"), false); show($("plan-card"), false); show($("done-card"), true);
    $("pay-ref").textContent = r.razorpay_payment_id;
    window.scrollTo({ top: 0, behavior: "smooth" });
    confirmPayment(r, 0);
  }

  // The webhook issues the code even if this never succeeds, so failures
  // here only change what the customer is told.
  function confirmPayment(r, attempt) {
    api("POST", "/ai-plan/confirm", r).then(function (res) {
      if (res.status === 200) { poll(r.razorpay_payment_id, 0); return; }
      if ((res.status === 0 || res.status >= 500 || res.status === 429) && attempt < 4) {
        setTimeout(function () { confirmPayment(r, attempt + 1); }, 2000 * (attempt + 1));
        return;
      }
      if (res.status === 0 || res.status >= 500 || res.status === 429) {
        poll(r.razorpay_payment_id, 0);     // the webhook will still issue it
        return;
      }
      $("done-title").textContent = "Payment needs a check";
      setState("waiting", "We could not confirm this payment automatically.");
      $("done-text").textContent = "If money was taken, your robot will still be recharged " +
        "once Razorpay confirms it. If it isn't within an hour, contact Hutark support with " +
        "the reference below.";
    });
  }

  function setState(kind, text) {
    $("robot-state").className = "state " + kind;
    $("robot-state-text").textContent = text;
  }

  // Ask every 5 s for 15 min, then every 30 s, until the robot reports it.
  function poll(paymentId, n) {
    clearTimeout(state.pollTimer);
    api("GET", "/ai-plan/recharge-status?recharge_id=" + encodeURIComponent(paymentId))
      .then(function (res) {
        if (res.status === 200 && res.data.applied) {
          $("done-title").textContent = "All done";
          setState("done", "✓ Robot " + res.data.robot_id + " is recharged.");
          $("done-text").textContent = "ChatGPT answers are on again. Thank you!";
          return;
        }
        state.pollTimer = setTimeout(function () { poll(paymentId, n + 1); },
                                     n < 180 ? 5000 : 30000);
      });
  }

  // ── wiring ────────────────────────────────────────────────────────────
  $("robot-form").addEventListener("submit", function (e) {
    e.preventDefault();
    checkRobot($("robot-id").value.trim());
  });
  $("robot-id").addEventListener("input", function () {
    state.robot = null; show($("plan-card"), false);
    msg($("robot-ok"), ""); msg($("robot-err"), "");
  });
  $("pay").addEventListener("click", pay);
  $("again").addEventListener("click", function () {
    clearTimeout(state.pollTimer);
    window.location.href = "/recharge";
  });

  var pre = new URLSearchParams(window.location.search).get("robot");
  if (pre) {
    $("robot-id").value = pre.trim();
    checkRobot(pre.trim());
  }
})();
