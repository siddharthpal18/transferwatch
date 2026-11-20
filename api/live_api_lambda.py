"""
TransferWatch - drt-live-api

Returns the next buses at one DRT stop, merging the live feed with the
schedule, so a bus that isn't tracking still shows up (as "schedule only").

    GET <function-url>/?stop=<stop_id>

For each bus it returns:
  - expected time (live prediction, delay carried forward, or schedule)
  - tracking status: live GPS / last seen N min ago / not tracking
  - state: running, cancelled, or skipping this stop
  - history: how this exact trip did at this stop over the last 28 days
    (from the nightly drt-history job), and a likely arrival range
plus the service alerts that affect this stop or its routes.

No layers or packages needed: the GTFS-Realtime decoder below uses only the
Python standard library (tested against Google's official bindings).

Environment variables:
  BUCKET         your S3 bucket (e.g. transferwatch-drt)
  STATIC_PREFIX  optional, defaults to "static/"
"""

import json
import os
import re
import struct
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

BUCKET = os.environ.get("BUCKET", "")
STATIC_PREFIX = os.environ.get("STATIC_PREFIX", "static/")

FEEDS = {
    "trip_updates": "https://drtonline.durhamregiontransit.com/gtfsrealtime/TripUpdates",
    "vehicle_positions": "https://drtonline.durhamregiontransit.com/gtfsrealtime/VehiclePositions",
    "alerts": "https://maps.durham.ca/OpenDataGTFS/alerts.pb",
}
HEADERS = {"User-Agent": "TransferWatch/1.0 (personal data project)"}

FEED_TTL = 20            # seconds to reuse a downloaded feed (keeps load off DRT)
STATIC_TTL = 6 * 3600    # reload schedule files every 6 hours
LOOK_BACK = 60 * 60      # include scheduled trips up to 1 h ago (late buses)
LOOK_AHEAD = 90 * 60     # show buses up to 90 min ahead
GONE_AFTER = 60          # hide a bus 1 min after its expected time
LIVE_GPS = 120           # GPS update within 2 min = live
STALE_GPS = 600          # within 10 min = "last seen N min ago"
MAX_RESULTS = 15
MIN_RANGE_SAMPLES = 5    # need this many past arrivals before showing a range
LEAD_BUCKETS = [("0-2", 0, 120), ("2-5", 120, 300), ("5-10", 300, 600),
                ("10-20", 600, 1200), ("20-40", 1200, 2400)]
STAT_FIELDS = ["days", "tracked", "cancelled", "skipped", "observed",
               "delay_p10", "delay_med", "delay_p90", "early", "late5", "gps_lead",
               "on_time", "quantiles"]
MIN_PROB_DAYS = 5        # need this many days of a trip before showing its probabilities

STOP_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,40}$")
ROUTE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")
TRIP_CANCELLED = {3, 7}  # CANCELED, DELETED
STU_SKIPPED, STU_NO_DATA = 1, 2

CAUSES = ["", "UNKNOWN_CAUSE", "OTHER_CAUSE", "TECHNICAL_PROBLEM", "STRIKE", "DEMONSTRATION",
          "ACCIDENT", "HOLIDAY", "WEATHER", "MAINTENANCE", "CONSTRUCTION", "POLICE_ACTIVITY",
          "MEDICAL_EMERGENCY"]
EFFECTS = ["", "NO_SERVICE", "REDUCED_SERVICE", "SIGNIFICANT_DELAYS", "DETOUR",
           "ADDITIONAL_SERVICE", "MODIFIED_SERVICE", "OTHER_EFFECT", "UNKNOWN_EFFECT",
           "STOP_MOVED", "NO_EFFECT", "ACCESSIBILITY_ISSUE"]


# ---------------------------------------------------------------------------
# Minimal GTFS-Realtime (protobuf) decoder, standard library only
# ---------------------------------------------------------------------------

def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf):
    """Yield (field_number, value) for every field in one protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        yield fn, v


def _int(v):        # int32/int64 may be negative (two's complement)
    return v - (1 << 64) if v >= (1 << 63) else v


def _float(v):
    return struct.unpack("<f", v)[0]


def _text(v):
    return bytes(v).decode("utf-8", "replace")


def _translated(buf):
    """Pick the English (or first) translation of a TranslatedString."""
    texts = []
    for fn, v in _fields(buf):
        if fn == 1:
            text, lang = "", ""
            for f2, v2 in _fields(v):
                if f2 == 1:
                    text = _text(v2)
                elif f2 == 2:
                    lang = _text(v2).lower()
            texts.append((lang, text))
    for lang, text in texts:
        if lang in ("", "en", "en-ca", "en-us"):
            return text
    return texts[0][1] if texts else ""


def _trip_descriptor(buf):
    d = {"trip_id": "", "route_id": "", "start_date": "", "rel": 0}
    for fn, v in _fields(buf):
        if fn == 1:
            d["trip_id"] = _text(v)
        elif fn == 3:
            d["start_date"] = _text(v)
        elif fn == 4:
            d["rel"] = v
        elif fn == 5:
            d["route_id"] = _text(v)
    return d


def _vehicle_descriptor(buf):
    d = {"id": "", "label": ""}
    for fn, v in _fields(buf):
        if fn == 1:
            d["id"] = _text(v)
        elif fn == 2:
            d["label"] = _text(v)
    return d


def _stop_time_event(buf):
    delay = t = None
    for fn, v in _fields(buf):
        if fn == 1:
            delay = _int(v)
        elif fn == 2:
            t = _int(v)
    return delay, t


def _stop_time_update(buf):
    u = {"seq": None, "stop_id": "", "arr_delay": None, "arr_time": None,
         "dep_delay": None, "dep_time": None, "rel": 0}
    for fn, v in _fields(buf):
        if fn == 1:
            u["seq"] = v
        elif fn == 2:
            u["arr_delay"], u["arr_time"] = _stop_time_event(v)
        elif fn == 3:
            u["dep_delay"], u["dep_time"] = _stop_time_event(v)
        elif fn == 4:
            u["stop_id"] = _text(v)
        elif fn == 5:
            u["rel"] = v
    return u


def _trip_update(buf):
    tu = {"trip": None, "stops": [], "vehicle_id": "", "timestamp": None, "delay": None}
    for fn, v in _fields(buf):
        if fn == 1:
            tu["trip"] = _trip_descriptor(v)
        elif fn == 2:
            tu["stops"].append(_stop_time_update(v))
        elif fn == 3:
            tu["vehicle_id"] = _vehicle_descriptor(v)["id"]
        elif fn == 4:
            tu["timestamp"] = v
        elif fn == 5:
            tu["delay"] = _int(v)
    return tu


def _vehicle_position(buf):
    vp = {"trip_id": "", "route_id": "", "vehicle_id": "", "label": "",
          "lat": None, "lon": None, "timestamp": None}
    for fn, v in _fields(buf):
        if fn == 1:
            t = _trip_descriptor(v)
            vp["trip_id"], vp["route_id"] = t["trip_id"], t["route_id"]
        elif fn == 2:
            for f2, v2 in _fields(v):
                if f2 == 1:
                    vp["lat"] = round(_float(v2), 5)
                elif f2 == 2:
                    vp["lon"] = round(_float(v2), 5)
        elif fn == 5:
            vp["timestamp"] = v
        elif fn == 8:
            d = _vehicle_descriptor(v)
            vp["vehicle_id"], vp["label"] = d["id"], d["label"]
    return vp


def _alert(buf):
    a = {"periods": [], "entities": [], "cause": 0, "effect": 0,
         "url": "", "header": "", "description": ""}
    for fn, v in _fields(buf):
        if fn == 1:
            start = end = 0
            for f2, v2 in _fields(v):
                if f2 == 1:
                    start = v2
                elif f2 == 2:
                    end = v2
            a["periods"].append((start, end))
        elif fn == 5:
            e = {"agency_id": "", "route_id": "", "stop_id": "", "trip_id": ""}
            for f2, v2 in _fields(v):
                if f2 == 1:
                    e["agency_id"] = _text(v2)
                elif f2 == 2:
                    e["route_id"] = _text(v2)
                elif f2 == 4:
                    e["trip_id"] = _trip_descriptor(v2)["trip_id"]
                elif f2 == 5:
                    e["stop_id"] = _text(v2)
            a["entities"].append(e)
        elif fn == 6:
            a["cause"] = v
        elif fn == 7:
            a["effect"] = v
        elif fn == 8:
            a["url"] = _translated(v)
        elif fn == 10:
            a["header"] = _translated(v)
        elif fn == 11:
            a["description"] = _translated(v)
    return a


def parse_feed(data):
    """Decode a GTFS-Realtime FeedMessage into plain dicts."""
    feed = {"timestamp": None, "trip_updates": [], "vehicles": [], "alerts": []}
    for fn, v in _fields(data):
        if fn == 1:
            for f2, v2 in _fields(v):
                if f2 == 3:
                    feed["timestamp"] = v2
        elif fn == 2:
            deleted = False
            parts = []
            for f2, v2 in _fields(v):
                if f2 == 2:
                    deleted = bool(v2)
                elif f2 in (3, 4, 5):
                    parts.append((f2, v2))
            if deleted:
                continue
            for f2, v2 in parts:
                if f2 == 3:
                    feed["trip_updates"].append(_trip_update(v2))
                elif f2 == 4:
                    feed["vehicles"].append(_vehicle_position(v2))
                else:
                    feed["alerts"].append(_alert(v2))
    return feed


# ---------------------------------------------------------------------------
# Toronto time (no tzdata needed on Lambda)
# ---------------------------------------------------------------------------

def toronto_offset(ts):
    """UTC offset in seconds for Toronto at a UTC timestamp (rules since 2007)."""
    y = datetime.fromtimestamp(ts, timezone.utc).year
    mar1 = datetime(y, 3, 1, tzinfo=timezone.utc)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7, hours=7)  # 2nd Sun Mar, 2am EST
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7, hours=6)        # 1st Sun Nov, 2am EDT
    return -4 * 3600 if dst_start.timestamp() <= ts < dst_end.timestamp() else -5 * 3600


def toronto_date(ts):
    return datetime.fromtimestamp(ts + toronto_offset(ts), timezone.utc).date()


def service_day_start(d):
    """GTFS times count from 'noon minus 12h' on the service day (handles DST days)."""
    noon_utc_guess = datetime(d.year, d.month, d.day, 17, tzinfo=timezone.utc).timestamp()
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp() - toronto_offset(noon_utc_guess)
    return noon - 12 * 3600


# ---------------------------------------------------------------------------
# Loading data (S3 static files, live feeds), with warm-container caching
# ---------------------------------------------------------------------------

_s3 = None
_static = {"loaded_at": 0}
_stop_cache = {}
_stats_cache = {}
_feed_cache = {}


class NotFound(Exception):
    pass


def read_static(key):
    """Read one schedule file from S3's static/ folder."""
    return read_json(STATIC_PREFIX + key)


def read_json(key):
    """Read one JSON file from S3 (replaced in local tests)."""
    global _s3
    import boto3
    if _s3 is None:
        _s3 = boto3.client("s3")
    try:
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
    except _s3.exceptions.NoSuchKey:
        raise NotFound(key)
    except Exception as e:  # AccessDenied is returned for missing keys without ListBucket
        if "AccessDenied" in str(e) or "NoSuchKey" in str(e):
            raise NotFound(key)
        raise
    return json.loads(obj["Body"].read())


def load_static(now):
    if now - _static["loaded_at"] > STATIC_TTL:
        _static["calendar"] = read_static("calendar.json")
        _static["routes"] = read_static("routes.json")
        _static.pop("trips", None)       # loaded lazily, only if needed
        try:
            _static["route_stats"] = read_json("stats/routes.json")
        except NotFound:
            _static["route_stats"] = {"days": 0, "routes": {}}
        _stop_cache.clear()
        _stats_cache.clear()
        _static["loaded_at"] = now
    return _static


def load_stop_stats(stop_id):
    """28-day history for this stop's trips; empty until the nightly job has run."""
    if stop_id not in _stats_cache:
        if len(_stats_cache) > 300:
            _stats_cache.clear()
        try:
            _stats_cache[stop_id] = read_json(f"stats/stops/{stop_id.replace('/', '_')}.json")
        except NotFound:
            _stats_cache[stop_id] = {"days": 0, "trips": {}}
    return _stats_cache[stop_id]


def load_trips():
    if "trips" not in _static:
        _static["trips"] = read_static("trips.json")
    return _static["trips"]


def load_stop(stop_id):
    if stop_id not in _stop_cache:
        if len(_stop_cache) > 300:
            _stop_cache.clear()
        _stop_cache[stop_id] = read_static(f"stops/{stop_id.replace('/', '_')}.json")
    return _stop_cache[stop_id]


def download(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read()


def get_feed(name, now):
    cached = _feed_cache.get(name)
    if cached and now - cached[0] < FEED_TTL:
        return cached[1]
    feed = parse_feed(download(FEEDS[name]))
    _feed_cache[name] = (now, feed)
    return feed


# ---------------------------------------------------------------------------
# Building the answer
# ---------------------------------------------------------------------------

def active_services(calendar, d):
    key = d.strftime("%Y%m%d")
    wd = d.weekday()
    active = {sid for sid, s in calendar["services"].items()
              if s["start"] <= key <= s["end"] and s["days"][wd]}
    active |= set(calendar.get("added", {}).get(key, []))
    active -= set(calendar.get("removed", {}).get(key, []))
    return active


def scheduled_trips(stop, calendar, now):
    """Scheduled visits to this stop from LOOK_BACK ago to LOOK_AHEAD ahead."""
    today = toronto_date(now)
    found = {}
    for d in (today - timedelta(days=1), today):     # yesterday covers after-midnight trips
        base = service_day_start(d)
        services = active_services(calendar, d)
        for trip_id, secs, service_id, seq, route_id, headsign in stop["trips"]:
            ts = base + secs
            if service_id in services and now - LOOK_BACK <= ts <= now + LOOK_AHEAD:
                old = found.get(trip_id)
                if old is None or abs(ts - now) < abs(old["sched"] - now):
                    found[trip_id] = {"trip_id": trip_id, "sched": int(ts), "seq": seq,
                                      "route_id": route_id, "headsign": headsign,
                                      "key": trip_key(route_id, headsign, secs, d)}
    return list(found.values())


def trip_key(route_id, headsign, secs, d):
    """Same format as the nightly job: identifies 'the same bus' across days."""
    dt = "wk" if d.weekday() < 5 else ("sa" if d.weekday() == 5 else "su")
    return f"{route_id}|{headsign}|{secs // 3600:02d}:{secs % 3600 // 60:02d}|{dt}"


def history_for(key, stop_stats):
    row = stop_stats.get("trips", {}).get(key) if key else None
    return dict(zip(STAT_FIELDS, row)) if row else None


def arrival_distribution(entry, hist, route_stats, now):
    """
    How the arrival time is spread out, from past data: a base time plus 19 offsets
    (5%, 10% ... 95% quantiles, in seconds). The website turns this into
    "leave by 7:31 for a 90% chance" using the rider's walking time.
      - live prediction: base = predicted time, spread = how wrong this route's
        countdowns were at the same distance ahead
      - schedule only:   base = timetable time, spread = this trip's own past delays
    """
    if entry["source"] == "realtime" and entry["expected"]:
        lead = entry["expected"] - now
        bucket = next((b for b, lo, hi in LEAD_BUCKETS if lo <= lead < hi), None)
        routes = route_stats.get("routes", {})
        band = (routes.get(entry["route_id"], {}).get(bucket)
                or routes.get("_all", {}).get(bucket)) if bucket else None
        if band and len(band) > 4 and band[4]:
            return {"base": int(entry["expected"]), "q": band[4], "n": band[3], "from": "countdowns"}
    # far-off live buses (40+ min) and untracked buses use this trip's own history
    if entry["sched"] and hist and hist["observed"] >= MIN_RANGE_SAMPLES and hist.get("quantiles"):
        return {"base": int(entry["sched"]), "q": hist["quantiles"], "n": hist["observed"], "from": "trip"}
    return None


def likely_range(dist):
    """Middle 80% of the distribution: arrives in this window 8 times in 10."""
    if not dist:
        return None
    return [dist["base"] + dist["q"][1], dist["base"] + dist["q"][17]]


def probabilities(hist):
    """Simple rates from this trip's history, only once there's enough of it."""
    if not hist or hist["days"] < MIN_PROB_DAYS:
        return None
    out = {"days": hist["days"],
           "cancelled": round(hist["cancelled"] / hist["days"], 3)}
    if hist["observed"] >= MIN_PROB_DAYS and hist.get("on_time") is not None:
        out["on_time"] = round(hist["on_time"] / hist["observed"], 3)
        out["early"] = round(hist["early"] / hist["observed"], 3)
    return out


def apply_realtime(entry, tu, stop_id):
    """Fill expected time and state from this trip's TripUpdate. Returns False if the bus has passed."""
    if tu["trip"] and tu["trip"]["rel"] in TRIP_CANCELLED:
        entry["state"] = "cancelled"
        return True

    seq = entry.get("seq")
    mine = [u for u in tu["stops"]
            if u["stop_id"] == stop_id or (seq is not None and u["seq"] == seq and not u["stop_id"])]
    if mine:
        u = mine[0]
        if u["rel"] == STU_SKIPPED:
            entry["state"] = "skipped"
            return True
        if u["rel"] == STU_NO_DATA:
            return True
        t = u["arr_time"] or u["dep_time"]
        delay = u["arr_delay"] if u["arr_delay"] is not None else u["dep_delay"]
        if t:
            entry["expected"] = int(t)
        elif delay is not None and entry["sched"]:
            entry["expected"] = entry["sched"] + delay
        if entry["expected"]:
            entry["source"] = "realtime"
        return True

    with_seq = [u for u in tu["stops"] if u["seq"] is not None]
    if seq is not None and with_seq:
        if all(u["seq"] > seq for u in with_seq):
            return False                     # feed only lists later stops: bus already passed
        earlier = [u for u in with_seq if u["seq"] < seq]
        if earlier:
            last = max(earlier, key=lambda u: u["seq"])
            delay = last["arr_delay"] if last["arr_delay"] is not None else last["dep_delay"]
            if delay is not None:
                entry["expected"] = entry["sched"] + delay
                entry["source"] = "realtime"
                return True
    if tu.get("delay") is not None and entry["sched"]:
        entry["expected"] = entry["sched"] + tu["delay"]
        entry["source"] = "realtime"
    return True


def tracking_for(trip_id, vehicle_id, vehicles_by_trip, vehicles_by_id, feed_ts, now):
    v = vehicles_by_trip.get(trip_id) or (vehicles_by_id.get(vehicle_id) if vehicle_id else None)
    if not v:
        return {"status": "none", "age": None, "vehicle": None}
    seen = v["timestamp"] or feed_ts or now
    age = max(0, int(now - seen))
    status = "live" if age <= LIVE_GPS else "stale" if age <= STALE_GPS else "none"
    return {"status": status, "age": age, "vehicle": v["label"] or v["vehicle_id"] or None}


def route_matches(route_id, routes, want):
    """True if want is this route's short name (e.g. '902') or its route_id."""
    if not want:
        return True
    want = want.lower()
    return route_id.lower() == want or str(routes.get(route_id, {}).get("s", "")).lower() == want


def build_arrivals(stop_id, stop, static, feeds, now, stop_stats=None, route=None):
    tu_feed = feeds.get("trip_updates") or {"trip_updates": [], "timestamp": None}
    vp_feed = feeds.get("vehicle_positions") or {"vehicles": [], "timestamp": None}
    tu_by_trip = {t["trip"]["trip_id"]: t for t in tu_feed["trip_updates"] if t["trip"]}
    v_by_trip = {v["trip_id"]: v for v in vp_feed["vehicles"] if v["trip_id"]}
    v_by_id = {v["vehicle_id"]: v for v in vp_feed["vehicles"] if v["vehicle_id"]}
    routes = static["routes"]
    stop_stats = stop_stats or {"trips": {}}
    route_stats = static.get("route_stats") or {"routes": {}}

    entries = []
    for s in scheduled_trips(stop, static["calendar"], now):
        if not route_matches(s["route_id"], routes, route):
            continue
        e = dict(s, expected=None, source="schedule", state="running")
        tu = tu_by_trip.get(s["trip_id"])
        if tu and not apply_realtime(e, tu, stop_id):
            continue
        e["vehicle_id"] = tu["vehicle_id"] if tu else ""
        entries.append(e)

    # Live-only trips (added trips, or trip IDs missing from our schedule)
    known = {e["trip_id"] for e in entries}
    boards_here = {row[0] for row in stop["trips"]}     # trips riders can board at this stop
    for trip_id, tu in tu_by_trip.items():
        if trip_id in known or not any(u["stop_id"] == stop_id for u in tu["stops"]):
            continue
        route_id, headsign = tu["trip"]["route_id"], ""
        if trip_id in load_trips():
            # A timetabled trip that isn't in this stop's boarding list only drops off here
            # (e.g. it ends at this terminal), so riders can't catch it.
            if trip_id not in boards_here:
                continue
            route_id, headsign = load_trips()[trip_id]
        if not route_matches(route_id, routes, route):
            continue
        e = {"trip_id": trip_id, "sched": None, "seq": None, "route_id": route_id,
             "headsign": headsign, "expected": None, "source": "schedule",
             "state": "running", "vehicle_id": tu["vehicle_id"], "key": None}
        apply_realtime(e, tu, stop_id)
        if e["expected"] or e["state"] != "running":
            entries.append(e)

    out = []
    for e in entries:
        when = e["expected"] or e["sched"]
        if when is None:
            continue
        if e["state"] == "running" and when < now - GONE_AFTER:
            continue
        if e["state"] != "running" and (e["sched"] or when) < now - GONE_AFTER:
            continue
        if when > now + LOOK_AHEAD:
            continue
        r = routes.get(e["route_id"], {})
        delay = e["expected"] - e["sched"] if e["expected"] and e["sched"] else None
        hist = history_for(e["key"], stop_stats)
        out.append({
            "trip_id": e["trip_id"],
            "route_id": e["route_id"],
            "route": r.get("s", e["route_id"]),
            "color": r.get("c", "1F4E79"),
            "text_color": r.get("t", "FFFFFF"),
            "headsign": e["headsign"] or r.get("l", ""),
            "scheduled": e["sched"],
            "expected": e["expected"],
            "delay": delay,
            "source": e["source"],
            "state": e["state"],
            "tracking": tracking_for(e["trip_id"], e["vehicle_id"], v_by_trip, v_by_id,
                                     vp_feed.get("timestamp"), now),
            "history": hist,
        })
        if e["state"] == "running":
            dist = arrival_distribution(e, hist, route_stats, now)
            out[-1]["dist"] = dist
            out[-1]["range"] = likely_range(dist)
            out[-1]["prob"] = probabilities(hist)
    out.sort(key=lambda a: a["expected"] or a["scheduled"])
    return out[:MAX_RESULTS]


def build_alerts(stop_id, route_ids, trip_ids, alert_feed, now):
    if not alert_feed:
        return []
    found = []
    for a in alert_feed["alerts"]:
        active = not a["periods"] or any(
            (s == 0 or s <= now) and (e == 0 or now <= e) for s, e in a["periods"])
        if not active:
            continue
        relevant = False
        for ent in a["entities"]:
            if ent["stop_id"] == stop_id:
                relevant = True
            elif ent["route_id"] in route_ids and not ent["stop_id"]:
                relevant = True
            elif ent["trip_id"] and ent["trip_id"] in trip_ids:
                relevant = True
            elif ent["agency_id"] and not (ent["route_id"] or ent["stop_id"] or ent["trip_id"]):
                relevant = True                      # network-wide alert
        if relevant:
            found.append({
                "header": a["header"],
                "description": a["description"],
                "url": a["url"],
                "cause": CAUSES[a["cause"]] if a["cause"] < len(CAUSES) else "",
                "effect": EFFECTS[a["effect"]] if a["effect"] < len(EFFECTS) else "",
            })
    return found


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=15",
        },
        "body": json.dumps(body, separators=(",", ":")),
    }


def lambda_handler(event, context):
    params = (event or {}).get("queryStringParameters") or {}
    stop_id = (params.get("stop") or "").strip()
    if not STOP_RE.match(stop_id):
        return respond(400, {"error": "Add ?stop=<stop_id> to the URL"})
    route = (params.get("route") or "").strip() or None      # optional: only this route
    if route and not ROUTE_RE.match(route):
        return respond(400, {"error": "Invalid route"})

    now = time.time()
    static = load_static(now)
    try:
        stop = load_stop(stop_id)
    except NotFound:
        return respond(404, {"error": f"Unknown stop {stop_id}"})

    feeds, feed_errors = {}, []
    for name in FEEDS:
        try:
            feeds[name] = get_feed(name, now)
        except Exception as e:
            feed_errors.append(name)
            print(f"FEED ERROR {name}: {e}")

    stop_stats = load_stop_stats(stop_id)
    arrivals = build_arrivals(stop_id, stop, static, feeds, now, stop_stats, route)
    route_ids = set(stop["routes"])
    trip_ids = {a["trip_id"] for a in arrivals}
    alerts = build_alerts(stop_id, route_ids, trip_ids, feeds.get("alerts"), now)

    tu_ts = (feeds.get("trip_updates") or {}).get("timestamp")
    return respond(200, {
        "stop_id": stop_id,
        "route": route,
        "generated_at": int(now),
        "feed_age": int(now - tu_ts) if tu_ts else None,
        "feed_errors": feed_errors,
        "schedule_valid_until": static["calendar"].get("feed_end"),
        "history_days": stop_stats.get("days", 0),
        "arrivals": arrivals,
        "alerts": alerts,
    })
