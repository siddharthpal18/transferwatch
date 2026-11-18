# TransferWatch

**Never run for the bus again.** A live board for Durham Region Transit (DRT) riders that shows
whether each bus is really tracking, cancelled, skipping your stop, or just on the schedule.

Built because the bus I take sometimes doesn't appear in transit apps until it's 4 minutes away,
which is less than my 8-minute walk to the stop.

## What v1 does
- Search stops by name or number, or find stops near you
- Live arrivals that merge DRT's realtime feed with the schedule, so a bus that isn't tracking still shows up
- A tracking badge on every bus: live GPS, last seen N minutes ago, or not tracking (scheduled time)
- Cancelled trips and skipped stops, plus the service alerts that affect your stop or routes
- Falls back to the schedule if DRT's live feed is down

## How it works
```
DRT live feeds ──> drt-collector (Lambda, every 2 min) ──> S3 raw files (history for v2 accuracy scores)
DRT live feeds ──> drt-live-api (Lambda Function URL) <── S3 static/ (schedule from build_static.py)
                          │
                          v
                  Website (GitHub Pages)
```

| Folder | What it is |
| --- | --- |
| `collector/` | Lambda that saves DRT's raw GTFS-Realtime feeds to S3 every 2 minutes |
| `static/` | `build_static.py`: turns DRT's GTFS schedule into per-stop files |
| `api/` | `live_api_lambda.py`: live arrivals API with a standard-library GTFS-Realtime decoder (no layers) |
| `docs/` | Static website (HTML, CSS, JS), served by GitHub Pages |

## Roadmap
- v2: saved trips, cancellation push alerts for affected riders only, Leave Now alerts, confidence scores
- v3: early departures, ghost buses, bus bunching, GO train connection check
- v4: search by destination

Independent project, not affiliated with Durham Region Transit. Data: DRT open data.
