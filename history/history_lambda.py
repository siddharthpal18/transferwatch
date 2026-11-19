"""
TransferWatch - drt-history (the reliability engine)

Runs once a night. For one service day it reads every snapshot the collector
saved (trip updates + vehicle positions, every 2 minutes) and works out, for
every scheduled bus at every stop:

  - when it actually arrived (its last prediction made just before arriving)
  - how late or early that was versus the timetable
  - whether it was ever on GPS, and how long before this stop it first appeared
  - whether it was cancelled, or skipped this stop

It also measures how wrong DRT's countdowns were ("said 6 min, came in 8").

Then it combines the last 28 days into small stats files that the live API reads:
  stats/stops/<stop_id>.json   per stop, per scheduled trip
  stats/routes.json            per route, countdown error by how far ahead

Event options (all optional):
  {"date": "2026-11-17"}       process this service day (default: yesterday, Toronto)
  {"skip_stats": true}         only process the day (handy when backfilling)
  {"stats_only": true}         only rebuild the 28-day stats

Environment variables:
  BUCKET   your S3 bucket (e.g. transferwatch-drt)

Lambda settings: memory 1024 MB, timeout 15 min.
"""

import gzip
import json
import os
import re
import struct
import time
import zlib
from array import array
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

BUCKET = os.environ.get("BUCKET", "")
RAW = "raw/durham/"
WINDOW_DAYS = 28
SHARDS = 8
ARRIVAL_SLACK = 180          # last prediction must be made at most 3 min before it
ERR_BIN = 30                 # countdown error histogram: 30-second bins
ERR_MIN, ERR_MAX = -615, 1215     # 30-s bins centred on whole half-minutes (0 sits mid-bin)
N_BINS = (ERR_MAX - ERR_MIN) // ERR_BIN + 1
LEAD_BUCKETS = [("0-2", 0, 120), ("2-5", 120, 300), ("5-10", 300, 600),
                ("10-20", 600, 1200), ("20-40", 1200, 2400)]
QUANTILES = [i / 20 for i in range(1, 20)]          # 5%, 10%, ... 95%
ON_TIME = (-60, 180)                                 # on time = up to 1 min early, 3 min late
STAMP_RE = re.compile(r"(\d{8}T\d{6}Z)\.pb$")
TRIP_CANCELLED = {3, 7}
STU_SKIPPED = 1

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
# S3 helpers (replaced in local tests)
# ---------------------------------------------------------------------------

_s3 = None


def s3():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def s3_list(prefix):
    keys, token = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3().list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        token = page["NextContinuationToken"]


def s3_get(key):
    return s3().get_object(Bucket=BUCKET, Key=key)["Body"].read()


def s3_put(key, data, gz=False):
    kw = {"Bucket": BUCKET, "Key": key, "Body": data, "ContentType": "application/json"}
    if gz:
        kw["ContentEncoding"] = "gzip"
    s3().put_object(**kw)


def get_json(key, gz=False):
    raw = s3_get(key)
    return json.loads(gzip.decompress(raw) if gz else raw)


def put_json(key, obj, gz=False):
    data = json.dumps(obj, separators=(",", ":")).encode()
    s3_put(key, gzip.compress(data) if gz else data, gz=gz)


def parallel(fn, items, workers=32):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def day_type(d):
    return "wk" if d.weekday() < 5 else ("sa" if d.weekday() == 5 else "su")


def trip_key(route_id, headsign, secs, d):
    """Identifies 'the same bus' across days, even if trip IDs change with a new timetable."""
    return f"{route_id}|{headsign}|{secs // 3600:02d}:{secs % 3600 // 60:02d}|{day_type(d)}"


def shard_of(stop_id):
    return zlib.crc32(stop_id.encode()) % SHARDS


def lead_bucket(seconds):
    for name, lo, hi in LEAD_BUCKETS:
        if lo <= seconds < hi:
            return name
    return None


def pct(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def hist_pct(hist, q):
    total = sum(hist)
    if not total:
        return None
    target, run = q * total, 0
    for i, c in enumerate(hist):
        run += c
        if run >= target:
            return ERR_MIN + i * ERR_BIN + ERR_BIN // 2
    return ERR_MAX


def active_services(calendar, d):
    key = d.strftime("%Y%m%d")
    wd = d.weekday()
    active = {sid for sid, s in calendar["services"].items()
              if s["start"] <= key <= s["end"] and s["days"][wd]}
    active |= set(calendar.get("added", {}).get(key, []))
    active -= set(calendar.get("removed", {}).get(key, []))
    return active


# ---------------------------------------------------------------------------
# Step 1: what was scheduled on day D
# ---------------------------------------------------------------------------

def load_schedule(d):
    """trip_id -> {route, headsign, stops: {stop_id: (secs, seq)}} for trips running on day d."""
    calendar = get_json("static/calendar.json")
    services = active_services(calendar, d)
    stop_keys = [k for k in s3_list("static/stops/") if k.endswith(".json")]
    files = parallel(lambda k: get_json(k), stop_keys)
    trips = {}
    for f in files:
        sid = f["stop_id"]
        for trip_id, secs, service_id, seq, route_id, headsign in f["trips"]:
            if service_id not in services:
                continue
            t = trips.setdefault(trip_id, {"route": route_id, "headsign": headsign, "stops": {}})
            t["stops"][sid] = (secs, seq)
    return trips


# ---------------------------------------------------------------------------
# Step 2: replay the day's snapshots
# ---------------------------------------------------------------------------

def snapshot_keys(feed, d, start, end):
    keys = []
    for day in (d, d + timedelta(days=1)):
        for k in s3_list(f"{RAW}{feed}/date={day.isoformat()}/"):
            m = STAMP_RE.search(k)
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
            if start <= ts < end:
                keys.append((ts, k))
    keys.sort()
    return keys


def replay(d, trips):
    base = service_day_start(d)
    start, end = base + 3 * 3600, base + 28 * 3600     # 3 am to 4 am next day, Toronto time
    ymd = d.strftime("%Y%m%d")
    seq_to_stop = {tid: {seq: sid for sid, (secs, seq) in t["stops"].items()} for tid, t in trips.items()}

    last = {}                    # (trip, stop) -> (snapshot time, predicted arrival)
    preds = {}                   # (trip, stop) -> array of [snapshot time, predicted arrival, ...]
    cancelled, skipped, seen_in_feed = set(), set(), set()
    first_gps = {}               # trip -> first time the bus reported a position on this trip

    def fetch(item):
        ts, key = item
        try:
            return ts, parse_feed(s3_get(key))
        except Exception as e:
            print(f"skip {key}: {e}")
            return ts, None

    tu_keys = snapshot_keys("trip_updates", d, start, end)
    vp_keys = snapshot_keys("vehicle_positions", d, start, end)

    for i in range(0, len(tu_keys), 64):                 # batches keep memory small
        for file_ts, feed in parallel(fetch, tu_keys[i:i + 64], workers=16):
            if not feed:
                continue
            now = feed["timestamp"] or file_ts
            for tu in feed["trip_updates"]:
                trip = tu["trip"]
                if not trip or trip["trip_id"] not in trips:
                    continue
                if trip["start_date"] and trip["start_date"] != ymd:
                    continue
                tid = trip["trip_id"]
                seen_in_feed.add(tid)
                if trip["rel"] in TRIP_CANCELLED:
                    cancelled.add(tid)
                    continue
                t = trips[tid]
                for u in tu["stops"]:
                    sid = u["stop_id"] or seq_to_stop[tid].get(u["seq"])
                    if sid not in t["stops"]:
                        continue
                    if u["rel"] == STU_SKIPPED:
                        skipped.add((tid, sid))
                        continue
                    p = u["arr_time"] or u["dep_time"]
                    if not p:
                        delay = u["arr_delay"] if u["arr_delay"] is not None else u["dep_delay"]
                        if delay is None:
                            continue
                        p = base + t["stops"][sid][0] + delay
                    last[(tid, sid)] = (now, p)
                    if 0 <= p - now < LEAD_BUCKETS[-1][2]:
                        # keep every countdown shown (not just the first) so accuracy isn't biased
                        arr = preds.get((tid, sid))
                        if arr is None:
                            arr = preds[(tid, sid)] = array("q")
                        arr.append(int(now))
                        arr.append(int(p))

    for i in range(0, len(vp_keys), 128):
        for file_ts, feed in parallel(fetch, vp_keys[i:i + 128], workers=16):
            if not feed:
                continue
            for v in feed["vehicles"]:
                tid = v["trip_id"]
                if tid in trips:
                    seen = v["timestamp"] or feed["timestamp"] or file_ts
                    if tid not in first_gps or seen < first_gps[tid]:
                        first_gps[tid] = seen

    return {"last": last, "preds": preds, "cancelled": cancelled, "skipped": skipped,
            "seen": seen_in_feed, "first_gps": first_gps,
            "n_tu": len(tu_keys), "n_vp": len(vp_keys)}


# ---------------------------------------------------------------------------
# Step 3: turn the replay into one row per bus per stop
# ---------------------------------------------------------------------------

def process_day(d):
    t0 = time.time()
    trips = load_schedule(d)
    r = replay(d, trips)
    base = service_day_start(d)

    shards = [[] for _ in range(SHARDS)]
    route_hist = defaultdict(lambda: {b[0]: [0] * N_BINS for b in LEAD_BUCKETS})
    with_delay = 0

    for tid, t in trips.items():
        tracked = int(tid in r["first_gps"])
        cx = int(tid in r["cancelled"])
        for sid, (secs, seq) in t["stops"].items():
            sched = base + secs
            actual = None
            got = r["last"].get((tid, sid))
            if got and not cx:
                snap_ts, p = got
                if snap_ts >= p - ARRIVAL_SLACK and abs(p - sched) < 3 * 3600:
                    actual = int(p)
            delay = actual - int(sched) if actual is not None else None
            lead = None
            if actual is not None and tracked:
                lead = actual - int(r["first_gps"][tid])
                if lead < 0:
                    lead = None
            if delay is not None:
                with_delay += 1
                arr = r["preds"].get((tid, sid), ())
                hist = route_hist[t["route"]]
                for j in range(0, len(arr), 2):
                    b = lead_bucket(arr[j + 1] - arr[j])        # what the rider saw: "N min"
                    err = actual - arr[j + 1]                   # how wrong it turned out
                    i = (min(max(err, ERR_MIN), ERR_MAX) - ERR_MIN) // ERR_BIN
                    hist[b][i] += 1
            shards[shard_of(sid)].append([
                sid, trip_key(t["route"], t["headsign"], secs, d), delay, tracked, cx,
                int((tid, sid) in r["skipped"]), lead,
            ])

    prefix = f"history/daily/date={d.isoformat()}/"
    parallel(lambda k: put_json(f"{prefix}part-{k}.json.gz", shards[k], gz=True), range(SHARDS), workers=8)
    put_json(f"{prefix}routes.json", route_hist)
    summary = {
        "date": d.isoformat(),
        "trip_update_snapshots": r["n_tu"],
        "vehicle_snapshots": r["n_vp"],
        "trips_scheduled": len(trips),
        "trips_in_live_feed": len(r["seen"]),
        "trips_on_gps": len(r["first_gps"]),
        "trips_cancelled": len(r["cancelled"]),
        "stop_visits": sum(len(s) for s in shards),
        "stop_visits_with_arrival": with_delay,
        "seconds": round(time.time() - t0, 1),
    }
    put_json(f"{prefix}summary.json", summary)
    print(json.dumps(summary))
    return summary


# ---------------------------------------------------------------------------
# Step 4: combine the last 28 days into stats the live API reads
# ---------------------------------------------------------------------------

def build_stats(upto):
    days = [upto - timedelta(days=i) for i in range(WINDOW_DAYS)]
    have = {k.split("date=")[1].split("/")[0] for k in s3_list("history/daily/") if k.endswith("summary.json")}
    days = [d for d in days if d.isoformat() in have]
    if not days:
        print("no processed days yet")
        return {"days": 0}

    # Countdown error per route and lead bucket
    merged = defaultdict(lambda: {b[0]: [0] * N_BINS for b in LEAD_BUCKETS})
    for d in days:
        for route, buckets in get_json(f"history/daily/date={d.isoformat()}/routes.json").items():
            for b, hist in buckets.items():
                for target in (merged[route][b], merged["_all"][b]):
                    for i, c in enumerate(hist):
                        target[i] += c
    routes = {}
    for route, buckets in merged.items():
        out = {}
        for b, hist in buckets.items():
            n = sum(hist)
            if n >= 20:
                out[b] = [hist_pct(hist, 0.1), hist_pct(hist, 0.5), hist_pct(hist, 0.9), n,
                          [hist_pct(hist, q) for q in QUANTILES]]
        if out:
            routes[route] = out
    put_json("stats/routes.json", {"built": upto.isoformat(), "days": len(days), "routes": routes})

    # Per stop, per scheduled trip
    stop_files = 0
    for k in range(SHARDS):
        agg = {}
        for d in days:
            try:
                rows = get_json(f"history/daily/date={d.isoformat()}/part-{k}.json.gz", gz=True)
            except Exception:
                continue
            for sid, key, delay, tracked, cx, sk, lead in rows:
                a = agg.setdefault((sid, key), [0, 0, 0, 0, [], []])
                a[0] += 1
                a[1] += tracked
                a[2] += cx
                a[3] += sk
                if delay is not None:
                    a[4].append(delay)
                if lead is not None:
                    a[5].append(lead)
        per_stop = defaultdict(dict)
        for (sid, key), (n, tr, cx, sk, delays, leads) in agg.items():
            delays.sort()
            leads.sort()
            per_stop[sid][key] = [
                n, tr, cx, sk, len(delays),
                pct(delays, 0.1), pct(delays, 0.5), pct(delays, 0.9),
                sum(1 for x in delays if x < -60),       # left more than 1 min early
                sum(1 for x in delays if x > 300),       # more than 5 min late
                pct(leads, 0.5),                         # typical seconds on GPS before this stop
                sum(1 for x in delays if ON_TIME[0] <= x <= ON_TIME[1]),
                [pct(delays, q) for q in QUANTILES] if len(delays) >= 3 else None,
            ]
        parallel(lambda item: put_json(f"stats/stops/{item[0].replace('/', '_')}.json", {
            "built": upto.isoformat(), "days": len(days),
            "fields": ["days", "tracked", "cancelled", "skipped", "observed",
                       "delay_p10", "delay_med", "delay_p90", "early", "late5", "gps_lead",
                       "on_time", "quantiles"],
            "trips": item[1]}), list(per_stop.items()))
        stop_files += len(per_stop)
    print(f"stats built from {len(days)} days, {stop_files} stop files")
    return {"days": len(days), "stop_files": stop_files}


def lambda_handler(event, context):
    event = event or {}
    if event.get("date"):
        d = date.fromisoformat(event["date"])
    else:
        d = toronto_date(time.time()) - timedelta(days=1)
    result = {}
    if not event.get("stats_only"):
        result["day"] = process_day(d)
    if not event.get("skip_stats"):
        result["stats"] = build_stats(d)
    return result
