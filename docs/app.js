// TransferWatch v1: stop search + live stop board
(function () {
  "use strict";

  const CFG = window.TW_CONFIG || {};
  const API = CFG.API_URL || "";
  const REFRESH_MS = (CFG.REFRESH_SECONDS || 30) * 1000;
  const TZ = "America/Toronto";

  let stops = [];
  let byId = {};
  let current = null;      // stop shown on the board
  let lastData = null;     // last API answer
  let lastOk = 0;          // when it arrived (ms)
  let timer = null;
  let ticker = null;

  const $ = (sel) => document.querySelector(sel);

  // Build DOM safely: strings become text nodes, never HTML.
  function el(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "style") Object.assign(node.style, v);
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }

  // Saved stops (browser storage can be unavailable, so always guarded)
  function getSaved() {
    try { return JSON.parse(localStorage.getItem("tw_saved") || "[]"); } catch (e) { return []; }
  }
  function setSaved(list) {
    try { localStorage.setItem("tw_saved", JSON.stringify(list)); } catch (e) { /* ignore */ }
  }

  function getWalk(id) {
    try { const v = parseInt(localStorage.getItem(`tw_walk_${id}`), 10); return v > 0 ? v : null; } catch (e) { return null; }
  }
  function setWalk(id, mins) {
    try { if (mins) localStorage.setItem(`tw_walk_${id}`, String(mins)); else localStorage.removeItem(`tw_walk_${id}`); } catch (e) { /* ignore */ }
  }

  // ---- Probability helpers. dist = {base, q: 19 offsets for 5%..95%} ----
  // P(bus has arrived by time t)
  function cdf(dist, t) {
    const x = t - dist.base, q = dist.q;
    if (x < q[0]) {
      const w = Math.max(30, q[1] - q[0]);
      return Math.max(0, 0.05 * (1 - (q[0] - x) / w));
    }
    if (x >= q[18]) {
      const w = Math.max(30, q[18] - q[17]);
      return Math.min(1, 0.95 + 0.05 * ((x - q[18]) / w));
    }
    for (let i = 0; i < 18; i++) {
      if (x >= q[i] && x < q[i + 1]) {
        const span = q[i + 1] - q[i];
        return (i + 1) / 20 + (span ? ((x - q[i]) / span) * 0.05 : 0.05);
      }
    }
    return 0.95;
  }
  // Chance of catching the bus if you reach the stop at time t
  function catchChance(a, t) {
    const pCancel = a.prob ? a.prob.cancelled : 0;
    return (1 - pCancel) * (1 - cdf(a.dist, t));
  }
  // Latest time to leave home for a given chance of catching it (null if that chance is out of reach)
  function leaveBy(a, walkSec, target) {
    const pCancel = a.prob ? a.prob.cancelled : 0;
    const need = target / (1 - pCancel);            // chance the bus hasn't come yet when you arrive
    if (need > 0.95) return null;
    const p = 1 - need;                              // ...so you must arrive by this quantile
    const i = Math.max(0, Math.min(18, Math.round(p * 20) - 1));
    return a.dist.base + a.dist.q[i] - walkSec;
  }
  const pctText = (p) => (p >= 0.95 ? "95%+" : p < 0.05 ? "under 5%" : `${Math.round(p * 100 / 5) * 5}%`);

  const clock = (sec) =>
    new Date(sec * 1000).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: TZ });

  function stopLabel(s) {
    return s.code ? `Stop #${s.code}` : `Stop ${s.id}`;
  }

  // ---------------- Home: search, near me, saved ----------------

  function stopButton(s, extra) {
    return el("li", {},
      el("button", { type: "button", onclick: () => openStop(s.id) },
        el("span", {},
          el("div", { class: "name" }, s.name),
          el("div", { class: "sub" }, `${stopLabel(s)} · Routes ${s.routes.join(", ")}`)),
        extra ? el("span", { class: "dist" }, extra) : null));
  }

  function search(text) {
    const q = text.trim().toLowerCase();
    const results = $("#results");
    results.replaceChildren();
    $("#nearMsg").hidden = true;
    if (!q) return;
    const tokens = q.split(/\s+/);
    const hits = stops.filter((s) => {
      const hay = `${s.name} ${s.code} ${s.id}`.toLowerCase();
      return tokens.every((t) => hay.includes(t));
    });
    hits.sort((a, b) => (b.code === q) - (a.code === q) || a.name.length - b.name.length);
    hits.slice(0, 8).forEach((s) => results.append(stopButton(s)));
    if (!hits.length) results.append(el("li", { class: "hint" }, "No stops match that search."));
  }

  function nearMe() {
    const msg = $("#nearMsg");
    msg.hidden = false;
    if (!navigator.geolocation) { msg.textContent = "Location isn't available in this browser."; return; }
    msg.textContent = "Finding stops near you…";

    const fail = (err, triedLowAccuracy) => {
      if (err.code === 1) {
        msg.textContent = "Location is blocked for this site. In Safari tap aA → Website Settings → Location → Allow, then reload.";
      } else if (!triedLowAccuracy) {
        // GPS failed or was slow: retry with Wi-Fi/cell location, which works indoors
        msg.textContent = "Still looking (using Wi-Fi location)…";
        navigator.geolocation.getCurrentPosition(found, (e) => fail(e, true),
          { enableHighAccuracy: false, timeout: 15000, maximumAge: 300000 });
      } else if (err.code === 3) {
        msg.textContent = "Finding your location took too long. Try again, or search instead. (code 3)";
      } else {
        msg.textContent = "Your device couldn't work out your location. Check Location Services is on, or search instead. (code 2)";
      }
    };

    const found = (pos) => {
      const { latitude: la, longitude: lo } = pos.coords;
      const dist = (s) => {
        const dy = (s.lat - la) * 111320;
        const dx = (s.lon - lo) * 111320 * Math.cos((la * Math.PI) / 180);
        return Math.hypot(dx, dy);
      };
      const near = stops.map((s) => [dist(s), s]).sort((a, b) => a[0] - b[0]).slice(0, 8);
      const results = $("#results");
      results.replaceChildren();
      $("#q").value = "";
      msg.textContent = near.length && near[0][0] > 30000
        ? "You seem to be outside Durham Region. Closest DRT stops:"
        : "Closest stops:";
      near.forEach(([d, s]) => {
        const walk = Math.max(1, Math.round(d / 80));        // ~80 m per minute walking
        results.append(stopButton(s, d < 1000 ? `${Math.round(d)} m · ${walk} min walk` : `${(d / 1000).toFixed(1)} km`));
      });
    };

    navigator.geolocation.getCurrentPosition(found, (e) => fail(e, false),
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
  }

  function renderSaved() {
    const list = getSaved().map((id) => byId[id]).filter(Boolean);
    $("#savedTitle").hidden = !list.length;
    $("#saved").replaceChildren(...list.map((s) => stopButton(s)));
  }

  function showHome() {
    stopRefresh();
    current = null;
    $("#stop").hidden = true;
    $("#home").hidden = false;
    renderSaved();
    history.replaceState(null, "", location.pathname);
    document.title = "TransferWatch · Live DRT arrivals";
  }

  // ---------------- Stop board ----------------

  function openStop(id) {
    const s = byId[id];
    if (!s) { showHome(); return; }
    current = s;
    lastData = null;
    $("#home").hidden = true;
    $("#stop").hidden = false;
    $("#stopName").textContent = s.name;
    $("#stopMeta").textContent = `${stopLabel(s)} · Routes ${s.routes.join(", ")}`;
    updateStar();
    $("#walk").value = String(getWalk(s.id) || "");
    $("#alerts").replaceChildren();
    $("#arrivals").replaceChildren(el("li", { class: "empty" }, "Loading live arrivals…"));
    $("#status").textContent = "";
    history.replaceState(null, "", `?stop=${encodeURIComponent(id)}`);
    document.title = `${s.name} · TransferWatch`;
    window.scrollTo(0, 0);
    refresh();
    startRefresh();
  }

  function updateStar() {
    const saved = current && getSaved().includes(current.id);
    const star = $("#star");
    star.setAttribute("aria-pressed", saved ? "true" : "false");
    star.setAttribute("aria-label", saved ? "Remove saved stop" : "Save stop");
    star.innerHTML = saved ? "&#9733;" : "&#9734;";
  }

  function toggleStar() {
    if (!current) return;
    const list = getSaved();
    const i = list.indexOf(current.id);
    if (i >= 0) list.splice(i, 1); else list.unshift(current.id);
    setSaved(list.slice(0, 12));
    updateStar();
  }

  async function refresh() {
    if (!current) return;
    if (!API || API.includes("PASTE-YOUR")) {
      $("#arrivals").replaceChildren(el("li", { class: "empty error" },
        "The live API isn't set up yet. Add your Function URL to config.js."));
      return;
    }
    const id = current.id;
    try {
      const res = await fetch(`${API}?stop=${encodeURIComponent(id)}`, { cache: "no-store" });
      const data = await res.json();
      if (!current || current.id !== id) return;        // user moved on
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      lastData = data;
      lastOk = Date.now();
      render();
    } catch (err) {
      if (!lastData) {
        $("#arrivals").replaceChildren(el("li", { class: "empty error" },
          "Couldn't load live arrivals. Retrying in a moment."));
      }
      $("#status").textContent = `Connection problem: ${err.message}. Showing last update.`;
    }
  }

  function delayText(a) {
    if (a.delay === null || a.delay === undefined || a.source !== "realtime") return null;
    const m = Math.round(Math.abs(a.delay) / 60);
    if (m === 0) return el("span", { class: "ontime" }, "On time");
    return a.delay > 0 ? el("span", { class: "late" }, `${m} min late`)
                       : el("span", { class: "early" }, `${m} min early`);
  }

  function trackingLine(a) {
    if (a.state === "cancelled") {
      return [el("span", { class: "late" }, "Cancelled"),
              a.scheduled ? `Was due ${clock(a.scheduled)}` : null];
    }
    if (a.state === "skipped") {
      return [el("span", { class: "late" }, "Won't stop here"), "Detour or stop closed"];
    }
    const t = a.tracking || {};
    let dot, text;
    if (t.status === "live") {
      dot = "live"; text = "Live GPS";
    } else if (t.status === "stale") {
      dot = "stale"; text = `Last seen ${Math.max(1, Math.round(t.age / 60))} min ago`;
    } else {
      dot = "none";
      text = a.source === "realtime" ? "No GPS · predicted time" : "Not tracking · scheduled time";
    }
    const d = delayText(a);
    return [el("span", { class: "tgroup" }, el("span", { class: `dot ${dot}` }), text),
            d ? el("span", {}, "·") : null, d];
  }

  // "weekdays" / "Saturdays" / "Sundays": history is kept separately for each
  function dayLabel(sec) {
    const wd = new Date(sec * 1000).toLocaleDateString("en-US", { weekday: "short", timeZone: TZ });
    return wd === "Sat" ? "Saturdays" : wd === "Sun" ? "Sundays" : "weekdays";
  }

  // What this exact trip did at this stop over recent days
  function insights(a) {
    const h = a.history;
    if (!h || a.state !== "running" || h.days < 3) return null;
    const n = h.days;
    const days = dayLabel(a.scheduled || a.expected);
    const chips = [];
    const live = (a.tracking || {}).status === "live";
    if (h.gps_lead !== null && h.gps_lead < 600 && !live) {
      chips.push(["warn", `Usually shows on GPS only ~${Math.max(1, Math.round(h.gps_lead / 60))} min before this stop, so leave by the clock`]);
    }
    if (h.cancelled > 0) chips.push(["bad", `Cancelled ${h.cancelled} of last ${n} ${days}`]);
    const notOnGps = n - h.tracked - h.cancelled;
    if (notOnGps > 0 && notOnGps / n >= 0.2) chips.push(["warn", `Not on GPS ${notOnGps} of last ${n} ${days}`]);
    if (h.observed >= 3) {
      if (h.early > 0) chips.push(["warn", `Left early ${h.early} of ${h.observed} ${days}`]);
      const m = Math.round(Math.abs(h.delay_med) / 60);
      if (a.prob && a.prob.on_time !== undefined) {
        chips.push(["info", `On time ${h.on_time} of ${h.observed} ${days} (${Math.round(a.prob.on_time * 100)}%)`]);
      }
      if (Math.abs(h.delay_med) >= 60) {
        chips.push(["info", h.delay_med > 0 ? `Usually ${m} min late` : `Usually ${m} min early`]);
      } else if (!(a.prob && a.prob.on_time !== undefined)) {
        chips.push(["info", "Usually on time"]);
      }
      if (h.late5 > 0) chips.push(["warn", `5+ min late ${h.late5} of ${h.observed} ${days}`]);
    }
    return chips.length
      ? el("div", { class: "insights" }, chips.map(([kind, text]) => el("span", { class: `chip ${kind}` }, text)))
      : null;
  }

  function planLine(a, nowSec) {
    if (a.state !== "running" || !a.dist || !current) return null;
    const walk = getWalk(current.id);
    if (!walk) return null;
    const w = walk * 60;
    const nowChance = catchChance(a, nowSec + w);
    const by90 = leaveBy(a, w, 0.9);
    const basis = a.dist.from === "trip"
      ? `Based on this bus's last ${a.dist.n} trips here`
      : `Based on ${a.dist.n.toLocaleString()} past countdowns on this route`;
    let kind, text;
    if (by90 && by90 > nowSec + 60) {
      kind = "good";
      text = `Leave at ${clock(by90)}`;
    } else if (nowChance >= 0.5) {
      kind = "hurry";
      text = "Leave now";
    } else if (nowChance >= 0.05) {
      kind = "miss";
      text = "You'll probably miss this one";
    } else {
      kind = "miss";
      text = "Too late for this one";
    }
    return el("div", { class: `plan ${kind}`, title: basis }, text);
  }

  // When the bus reaches the stop: "Here by 7:40 · 50%  7:42 · 80%  7:44 · 95%"
  function arrivalOdds(a) {
    if (a.state !== "running" || !a.dist) return null;
    const pCancel = a.prob ? a.prob.cancelled : 0;
    const marks = [[9, 0.5], [15, 0.8], [18, 0.95]];
    const byTime = new Map();                     // same minute twice: keep the higher chance
    for (const [i, p] of marks) byTime.set(clock(a.dist.base + a.dist.q[i]), p);
    const parts = [...byTime].map(([t, p]) =>
      el("span", { class: "odd" }, el("b", {}, t), ` ${Math.round(p * (1 - pCancel) * 100)}%`));
    return el("div", { class: "odds" }, el("span", { class: "oddlabel" }, "Here by"), parts);
  }

  function rangeText(r) {
    const a = clock(r[0]), b = clock(r[1]);
    if (a === b) return a;
    const sa = a.slice(-2), sb = b.slice(-2);
    return sa === sb ? `${a.slice(0, -3)}–${b}` : `${a}–${b}`;
  }

  function arrivalRow(a, nowSec) {
    const when = a.expected || a.scheduled;
    const mins = Math.round((when - nowSec) / 60);
    const off = a.state !== "running";
    let big;
    if (a.state === "cancelled") big = "Cancelled";
    else if (a.state === "skipped") big = "Skipped";
    else if (mins <= 0) big = "Due";
    else big = [String(mins), el("small", {}, "min")];

    const kind = off ? a.state : (a.source === "schedule" && (a.tracking || {}).status !== "live" ? "sched" : "rt");
    const clockText = clock(when);

    return el("li", { class: `arr ${kind}` },
      el("div", { class: "route", style: { background: `#${a.color}`, color: `#${a.text_color}` } }, a.route),
      el("div", { class: "head", title: a.headsign }, a.headsign || `Route ${a.route}`),
      el("div", { class: "when" },
        el("div", { class: "mins" }, big),
        el("div", { class: "clock" }, clockText)),
      el("div", { class: "track" }, trackingLine(a)),
      planLine(a, nowSec),
      arrivalOdds(a),
      insights(a));
  }

  function render() {
    if (!lastData) return;
    const nowSec = Date.now() / 1000;
    const data = lastData;

    const alerts = (data.alerts || []).map((al) =>
      el("details", { class: "alert" },
        el("summary", {}, el("span", { class: "tag" }, (al.effect || "Alert").replace(/_/g, " ").toLowerCase()), al.header || "Service alert"),
        al.description ? el("p", {}, al.description) : null,
        al.url && /^https?:\/\//.test(al.url) ? el("p", {}, el("a", { href: al.url, target: "_blank", rel: "noopener" }, "More info")) : null));
    $("#alerts").replaceChildren(...alerts);

    const rows = (data.arrivals || []).filter((a) => (a.expected || a.scheduled) > nowSec - 60 || a.state !== "running");
    $("#arrivals").replaceChildren(...(rows.length
      ? rows.map((a) => arrivalRow(a, nowSec))
      : [el("li", { class: "empty" }, "No buses in the next 90 minutes.")]));

    const age = Math.round((Date.now() - lastOk) / 1000);
    const parts = [`Updated ${age < 5 ? "just now" : `${age}s ago`}`, `refreshes every ${REFRESH_MS / 1000}s`];
    if ((data.feed_errors || []).includes("trip_updates")) parts.push("DRT live feed unavailable, showing schedule");
    const hd = data.history_days || 0;
    parts.push(hd >= 3 ? `reliability from the last ${hd} days`
                       : `reliability insights start after 3 days of history (${hd} so far)`);
    $("#status").textContent = parts.join(" · ");
  }

  function startRefresh() {
    stopRefresh();
    timer = setInterval(refresh, REFRESH_MS);
    ticker = setInterval(render, 10000);       // keep countdowns current between refreshes
  }
  function stopRefresh() {
    clearInterval(timer); clearInterval(ticker);
    timer = ticker = null;
  }

  document.addEventListener("visibilitychange", () => {
    if (!current) return;
    if (document.hidden) stopRefresh();
    else { refresh(); startRefresh(); }
  });

  // ---------------- Start ----------------

  async function init() {
    $("#q").addEventListener("input", (e) => search(e.target.value));
    $("#near").addEventListener("click", nearMe);
    $("#back").addEventListener("click", showHome);
    $("#star").addEventListener("click", toggleStar);
    const walkSel = $("#walk");
    walkSel.append(el("option", { value: "" }, "Set walk time"));
    for (const m of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30]) walkSel.append(el("option", { value: String(m) }, `${m} min walk`));
    walkSel.addEventListener("change", () => {
      if (!current) return;
      setWalk(current.id, parseInt(walkSel.value, 10) || null);
      render();
    });
    $("#homeLink").addEventListener("click", (e) => { e.preventDefault(); showHome(); });

    try {
      const res = await fetch("data/stops.json");
      stops = await res.json();
    } catch (e) {
      $("#results").replaceChildren(el("li", { class: "error" }, "Couldn't load the stop list."));
      return;
    }
    byId = Object.fromEntries(stops.map((s) => [s.id, s]));

    const wanted = new URLSearchParams(location.search).get("stop");
    if (wanted && byId[wanted]) openStop(wanted);
    else renderSaved();
  }

  init();
})();
