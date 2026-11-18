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
    navigator.geolocation.getCurrentPosition((pos) => {
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
      msg.textContent = "Closest stops:";
      near.forEach(([d, s]) => {
        const walk = Math.max(1, Math.round(d / 80));        // ~80 m per minute walking
        results.append(stopButton(s, d < 1000 ? `${Math.round(d)} m · ${walk} min walk` : `${(d / 1000).toFixed(1)} km`));
      });
    }, () => { msg.textContent = "Couldn't get your location. Try searching instead."; },
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
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
      el("div", { class: "track" }, trackingLine(a)));
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
