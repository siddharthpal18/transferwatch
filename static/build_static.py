"""
TransferWatch - build_static.py

Turns DRT's GTFS schedule zip into the small files the app needs.
Run it on your PC (Python 3.9+, no extra packages needed):

    python build_static.py GTFS_Durham_TXT.zip

It creates a "build" folder next to this script:

  build/s3/static/           -> upload to s3://transferwatch-drt/static/
      calendar.json          which services run on which dates
      routes.json            route names and colours
      trips.json             trip -> route + headsign (for live-only trips)
      stops/<stop_id>.json   every scheduled bus at that stop
  build/web/data/            -> copy into docs/data/ (the website)
      stops.json             stop list for the search box
      routes.json            route names and colours

Re-run it whenever DRT publishes a new schedule (a few times a year).
"""

import csv
import io
import json
import shutil
import sys
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent / "build"


def open_csv(zf, name, required=True):
    """Open a GTFS file from the zip, even if it sits inside a subfolder."""
    matches = [m for m in zf.namelist() if m.split("/")[-1] == name]
    if not matches:
        if required:
            sys.exit(f"ERROR: {name} not found in the zip")
        return []
    raw = zf.open(matches[0])
    return csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def to_seconds(hms):
    """'25:10:00' -> 90600. GTFS times can pass 24:00 for after-midnight trips."""
    hms = (hms or "").strip()
    if not hms:
        return None
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def clean(value):
    return (value or "").strip()


def main(zip_path):
    zf = zipfile.ZipFile(zip_path)

    # 1. Routes -------------------------------------------------------------
    routes = {}
    for r in open_csv(zf, "routes.txt"):
        routes[clean(r["route_id"])] = {
            "s": clean(r.get("route_short_name")) or clean(r["route_id"]),
            "l": clean(r.get("route_long_name")),
            "c": clean(r.get("route_color")) or "1F4E79",
            "t": clean(r.get("route_text_color")) or "FFFFFF",
        }
    print(f"routes:      {len(routes):>8,}")

    # 2. Trips --------------------------------------------------------------
    trips = {}
    for t in open_csv(zf, "trips.txt"):
        trips[clean(t["trip_id"])] = (
            clean(t["route_id"]),
            clean(t.get("trip_headsign")),
            clean(t["service_id"]),
        )
    print(f"trips:       {len(trips):>8,}")

    # 3. Calendar -----------------------------------------------------------
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    services, added, removed = {}, defaultdict(list), defaultdict(list)
    for c in open_csv(zf, "calendar.txt", required=False):
        services[clean(c["service_id"])] = {
            "days": [int(clean(c[d]) or 0) for d in days],
            "start": clean(c["start_date"]),
            "end": clean(c["end_date"]),
        }
    for c in open_csv(zf, "calendar_dates.txt", required=False):
        target = added if clean(c["exception_type"]) == "1" else removed
        target[clean(c["date"])].append(clean(c["service_id"]))
    all_dates = [s["end"] for s in services.values()] + list(added)
    calendar = {
        "services": services,
        "added": added,
        "removed": removed,
        "feed_end": max(all_dates) if all_dates else None,
        "built": date.today().isoformat(),
    }
    print(f"services:    {len(services):>8,}  (schedule valid until {calendar['feed_end']})")

    # 4. Stop times -> one list per stop ------------------------------------
    per_stop = defaultdict(list)
    skipped_no_time = skipped_no_pickup = 0
    for st in open_csv(zf, "stop_times.txt"):
        trip_id = clean(st["trip_id"])
        if trip_id not in trips:
            continue
        if clean(st.get("pickup_type")) == "1":   # riders can't board here (e.g. last stop)
            skipped_no_pickup += 1
            continue
        secs = to_seconds(st.get("arrival_time")) or to_seconds(st.get("departure_time"))
        if secs is None:
            skipped_no_time += 1
            continue
        route_id, headsign, service_id = trips[trip_id]
        seq = int(clean(st["stop_sequence"]) or 0)
        per_stop[clean(st["stop_id"])].append([trip_id, secs, service_id, seq, route_id, headsign])
    print(f"stops used:  {len(per_stop):>8,}")
    if skipped_no_pickup:
        print(f"  skipped {skipped_no_pickup:,} drop-off-only stop times")
    if skipped_no_time:
        print(f"  skipped {skipped_no_time:,} stop times with no time")

    # 5. Stops for search ---------------------------------------------------
    search = []
    for s in open_csv(zf, "stops.txt"):
        sid = clean(s["stop_id"])
        if sid not in per_stop:            # stations, entrances, unused stops
            continue
        stop_routes = sorted(
            {routes.get(row[4], {"s": row[4]})["s"] for row in per_stop[sid]},
            key=lambda x: (len(x), x),
        )
        search.append({
            "id": sid,
            "code": clean(s.get("stop_code")),
            "name": clean(s["stop_name"]),
            "lat": round(float(s["stop_lat"]), 5),
            "lon": round(float(s["stop_lon"]), 5),
            "routes": stop_routes,
        })
    search.sort(key=lambda x: x["name"])

    # 6. Write everything ---------------------------------------------------
    if OUT.exists():
        shutil.rmtree(OUT)
    s3_dir = OUT / "s3" / "static"
    stops_dir = s3_dir / "stops"
    web_dir = OUT / "web" / "data"
    stops_dir.mkdir(parents=True)
    web_dir.mkdir(parents=True)

    def dump(path, obj):
        path.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")

    dump(s3_dir / "calendar.json", calendar)
    dump(s3_dir / "routes.json", routes)
    dump(s3_dir / "trips.json", {k: [v[0], v[1]] for k, v in trips.items()})
    for sid, rows in per_stop.items():
        rows.sort(key=lambda r: r[1])
        safe = sid.replace("/", "_")
        dump(stops_dir / f"{safe}.json", {
            "stop_id": sid,
            "routes": sorted({r[4] for r in rows}),
            "trips": rows,
        })
    dump(web_dir / "stops.json", search)
    dump(web_dir / "routes.json", routes)

    print(f"\nDone. Wrote {len(per_stop):,} stop files.")
    print(f"  Upload: {s3_dir}  ->  s3://transferwatch-drt/static/")
    print(f"  Copy:   {web_dir}  ->  docs/data/")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python build_static.py GTFS_Durham_TXT.zip")
    main(sys.argv[1])
