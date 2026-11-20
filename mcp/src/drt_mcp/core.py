"""
Pure logic for the DRT MCP server: stop search, arrival odds and "leave at" times.

The odds math mirrors docs/app.js in TransferWatch exactly, so Claude and the
website always give a rider the same answer for the same bus.

Every arrival from the TransferWatch API may carry a `dist`:
    {"base": <unix secs>, "q": [19 offsets in secs for the 5%..95% quantiles],
     "n": <sample count>, "from": "countdowns" | "trip"}
meaning: the bus reaches the stop by base + q[i] with probability (i + 1) / 20.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Toronto")

# quantile index -> probability for the "here by" odds shown to riders (50%, 80%, 95%)
ODDS_MARKS = [(9, 0.5), (15, 0.8), (18, 0.95)]
LEAVE_TARGET = 0.9        # "leave at" aims for a 90% chance of catching the bus


# ---------------------------------------------------------------------------
# Formatting

def clock(ts: float | int | None) -> str | None:
    """Unix seconds -> '6:34 PM' in Toronto time."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, TZ).strftime("%I:%M %p").lstrip("0")


def pct(p: float) -> str:
    if p >= 0.95:
        return "95%+"
    if p < 0.05:
        return "under 5%"
    return f"{round(p * 100 / 5) * 5}%"


# ---------------------------------------------------------------------------
# Stop search

_WORD = re.compile(r"[a-z0-9]+")
# words people type that never appear in stop names ('Simcoe and Conlin', 'King @ Simcoe')
_STOPWORDS = {"and", "at", "the", "of", "stop", "near", "by", "on"}
# what riders say -> words DRT uses in stop names
_ALIASES = {
    "go": {"go", "station"},
    "stn": {"station"},
    "term": {"terminal"},
}


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _word_hits(word: str, tokens: list[str]) -> int:
    """2 = exact word match, 1 = prefix match ('cent' -> 'centre'), 0 = no match."""
    options = _ALIASES.get(word, {word})
    if any(t in options for t in tokens):
        return 2
    if len(word) >= 3 and any(t.startswith(word) for t in tokens):
        return 1
    return 0


def search_stops(stops: list[dict], query: str, limit: int = 5) -> list[dict]:
    """
    Find stops by stop number or name. An exact stop number match wins; otherwise
    every meaningful word of the query must match a whole word (or the start of a
    word, for 3+ letters) in the stop name, in any order: 'simcoe and conlin'
    matches 'Simcoe Northbound @ Conlin', 'whitby go' matches 'Whitby Station'.
    """
    q = query.strip().lower().lstrip("#")
    if not q:
        return []
    exact = [s for s in stops if q in (str(s.get("code", "")).lower(), str(s.get("id", "")).lower())]
    if exact:
        return [_stop_summary(s) for s in exact[:limit]]

    words = [w for w in _tokens(q) if w not in _STOPWORDS] or _tokens(q)
    if not words:
        return []
    scored = []
    for s in stops:
        name = s.get("name", "")
        tokens = _tokens(name)
        hits = [_word_hits(w, tokens) for w in words]
        if all(hits):
            # exact words first, then shorter names ('Oshawa Centre Terminal' before '... Platform 12')
            scored.append((-sum(hits), len(name), name, s))
    scored.sort(key=lambda x: x[:3])
    return [_stop_summary(x[3]) for x in scored[:limit]]


def _stop_summary(s: dict) -> dict:
    return {
        "stop_id": s.get("id"),
        "stop_number": s.get("code"),
        "name": s.get("name"),
        "routes": s.get("routes", []),
        "lat": s.get("lat"),
        "lon": s.get("lon"),
    }


# ---------------------------------------------------------------------------
# Probability helpers (ported from docs/app.js)

def cdf(dist: dict, t: float) -> float:
    """Chance the bus has already reached the stop by time t."""
    x = t - dist["base"]
    q = dist["q"]
    if x < q[0]:
        w = max(30, q[1] - q[0])
        return max(0.0, 0.05 * (1 - (q[0] - x) / w))
    if x >= q[18]:
        w = max(30, q[18] - q[17])
        return min(1.0, 0.95 + 0.05 * ((x - q[18]) / w))
    for i in range(18):
        if q[i] <= x < q[i + 1]:
            span = q[i + 1] - q[i]
            return (i + 1) / 20 + (((x - q[i]) / span) * 0.05 if span else 0.05)
    return 0.95


def cancel_rate(a: dict) -> float:
    return (a.get("prob") or {}).get("cancelled", 0) or 0


def catch_chance(a: dict, t: float) -> float:
    """Chance of catching the bus if you reach the stop at time t."""
    return (1 - cancel_rate(a)) * (1 - cdf(a["dist"], t))


def leave_by(a: dict, walk_sec: float, target: float = LEAVE_TARGET) -> float | None:
    """Latest time to leave for a `target` chance of catching it (None if out of reach)."""
    p_cancel = cancel_rate(a)
    if p_cancel >= 1:
        return None
    need = target / (1 - p_cancel)          # chance the bus hasn't come yet when you arrive
    if need > 0.95:
        return None
    p = 1 - need                            # ...so you must arrive by this quantile
    i = max(0, min(18, round(p * 20) - 1))
    return a["dist"]["base"] + a["dist"]["q"][i] - walk_sec


def here_by(a: dict) -> list[dict]:
    """'Here by 6:37 (50%), 6:38 (80%), 6:40 (95%)'. Same minute twice keeps the higher chance."""
    if a.get("state") != "running" or not a.get("dist"):
        return []
    keep = 1 - cancel_rate(a)
    by_time: dict[str, float] = {}
    for i, p in ODDS_MARKS:
        by_time[clock(a["dist"]["base"] + a["dist"]["q"][i])] = p
    return [{"time": t, "chance": f"{round(p * keep * 100)}%"} for t, p in by_time.items()]


def plan(a: dict, walk_minutes: int, now: float) -> dict | None:
    """When to leave home, given the rider's walk to the stop. Mirrors planLine() in app.js."""
    if a.get("state") != "running" or not a.get("dist") or not walk_minutes:
        return None
    w = walk_minutes * 60
    now_chance = catch_chance(a, now + w)
    by90 = leave_by(a, w)
    if by90 and by90 > now + 60:
        advice, kind = f"Leave at {clock(by90)}", "leave_at"
    elif now_chance >= 0.5:
        advice, kind = "Leave now", "leave_now"
    elif now_chance >= 0.05:
        advice, kind = "You'll probably miss this one", "likely_miss"
    else:
        advice, kind = "Too late for this one", "too_late"
    return {
        "advice": advice,
        "kind": kind,
        "leave_at": clock(by90) if kind == "leave_at" else None,
        "chance_if_you_leave_now": pct(now_chance),
    }


def basis(a: dict) -> str | None:
    d = a.get("dist")
    if not d:
        return None
    if d.get("from") == "trip":
        return f"this bus's last {d['n']} trips at this stop"
    return f"{d['n']:,} past countdowns on route {a.get('route', '')}"


# ---------------------------------------------------------------------------
# History notes (subset of insights() in app.js)

def _day_label(ts: float) -> str:
    wd = datetime.fromtimestamp(ts, TZ).weekday()
    return "Saturdays" if wd == 5 else "Sundays" if wd == 6 else "weekdays"


def history_notes(a: dict) -> list[str]:
    """What this exact trip did at this stop on recent days (needs 3+ days)."""
    h = a.get("history")
    if not h or a.get("state") != "running" or (h.get("days") or 0) < 3:
        return []
    n = h["days"]
    days = _day_label(a.get("scheduled") or a.get("expected") or 0)
    live = (a.get("tracking") or {}).get("status") == "live"
    notes = []
    gps_lead = h.get("gps_lead")
    if gps_lead is not None and gps_lead < 600 and not live:
        notes.append(f"Usually appears on GPS only ~{max(1, round(gps_lead / 60))} min before "
                     "this stop, so leave by the clock, not the map")
    if h.get("cancelled"):
        notes.append(f"Cancelled {h['cancelled']} of last {n} {days}")
    not_on_gps = n - (h.get("tracked") or 0) - (h.get("cancelled") or 0)
    if not_on_gps > 0 and not_on_gps / n >= 0.2:
        notes.append(f"Not on GPS {not_on_gps} of last {n} {days}")
    observed = h.get("observed") or 0
    if observed >= 3:
        if h.get("early"):
            notes.append(f"Left early {h['early']} of {observed} {days}")
        med = h.get("delay_med") or 0
        if abs(med) >= 60:
            m = round(abs(med) / 60)
            notes.append(f"Usually {m} min late" if med > 0 else f"Usually {m} min early")
        if h.get("on_time") is not None:
            notes.append(f"On time {h['on_time']} of {observed} {days}")
        if h.get("late5"):
            notes.append(f"5+ min late {h['late5']} of {observed} {days}")
    return notes


# ---------------------------------------------------------------------------
# Shaping one arrival for an AI client

def tracking_text(t: dict | None) -> str:
    t = t or {}
    status, age = t.get("status"), t.get("age")
    if status == "live":
        return "live GPS"
    if status == "stale" and age is not None:
        return f"GPS last seen {max(1, round(age / 60))} min ago"
    return "not on GPS (timetable or prediction only)"


def describe_arrival(a: dict, now: float, walk_minutes: int | None = None) -> dict:
    when = a.get("expected") or a.get("scheduled")
    delay = a.get("delay")
    out = {
        "route": a.get("route"),
        "headsign": a.get("headsign"),
        "status": a.get("state"),                 # running | cancelled | skipped
        "drt_says": clock(when),
        "minutes_away": max(0, round((when - now) / 60)) if when else None,
        "scheduled": clock(a.get("scheduled")),
        "delay_minutes": round(delay / 60, 1) if delay is not None else None,
        "prediction_source": a.get("source"),    # realtime | schedule
        "tracking": tracking_text(a.get("tracking")),
    }
    odds = here_by(a)
    if odds:
        out["here_by"] = odds
        out["odds_based_on"] = basis(a)
    else:
        out["here_by"] = None
        out["odds_note"] = "Not enough history for odds on this bus yet"
    if walk_minutes:
        out["plan"] = plan(a, walk_minutes, now)
    notes = history_notes(a)
    if notes:
        out["history_notes"] = notes
    return out
