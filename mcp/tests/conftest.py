import json
import time
from pathlib import Path

import httpx
import pytest

from drt_mcp import server
from drt_mcp.api import DrtClient

STOPS = [
    {"id": "2595", "code": "2595", "name": "Oshawa Centre Terminal", "lat": 43.889, "lon": -78.881,
     "routes": ["302", "905"]},
    {"id": "1002", "code": "1002", "name": "Simcoe Northbound @ King", "lat": 43.897, "lon": -78.863,
     "routes": ["403"]},
    {"id": "93001", "code": "93001", "name": "Whitby GO Station Platform 1", "lat": 43.864, "lon": -78.938,
     "routes": ["915"]},
]

# 19 quantile offsets (seconds) for 5%..95%: bus is usually 0-4 min later than DRT says
Q = [-30, 0, 10, 20, 30, 40, 50, 55, 60, 65, 75, 90, 100, 110, 120, 140, 180, 220, 240]


def make_board(now: float) -> dict:
    live = int(now + 10 * 60)
    return {
        "stop_id": "2595",
        "generated_at": int(now),
        "feed_age": 12,
        "feed_errors": [],
        "history_days": 4,
        "arrivals": [
            {"trip_id": "t1", "route_id": "302", "route": "302", "headsign": "North Campus",
             "scheduled": live - 60, "expected": live, "delay": 60, "source": "realtime",
             "state": "running", "tracking": {"status": "live", "age": 20, "vehicle": "8470"},
             "history": {"days": 4, "tracked": 4, "cancelled": 0, "skipped": 0, "observed": 4,
                         "delay_med": 90, "early": 1, "late5": 0, "gps_lead": 1800, "on_time": 3},
             "dist": {"base": live, "q": Q, "n": 1234, "from": "countdowns"},
             "prob": None},
            {"trip_id": "t2", "route_id": "905", "route": "905", "headsign": "Pickering",
             "scheduled": int(now + 40 * 60), "expected": None, "delay": None, "source": "schedule",
             "state": "running", "tracking": {"status": "none", "age": None, "vehicle": None},
             "history": None, "dist": None, "prob": None},
            {"trip_id": "t3", "route_id": "302", "route": "302", "headsign": "North Campus",
             "scheduled": int(now + 25 * 60), "expected": None, "delay": None, "source": "schedule",
             "state": "cancelled", "tracking": {"status": "none", "age": None, "vehicle": None},
             "history": None},
        ],
        "alerts": [{"header": "Detour on Simcoe", "description": "Route 302 detoured",
                    "url": "", "cause": "CONSTRUCTION", "effect": "DETOUR"}],
    }


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("stops.json"):
        return httpx.Response(200, json=STOPS)
    stop = request.url.params.get("stop")
    if stop == "2595":
        board = make_board(time.time())
        board["arrivals"].append({**board["arrivals"][0], "trip_id": "t9", "headsign": "Oshawa Centre Terminal"})
        board["arrivals"].sort(key=lambda a: a["expected"] or a["scheduled"])
        route = request.url.params.get("route")
        if route:  # the real API filters before its 15-bus cap
            board["arrivals"] = [a for a in board["arrivals"] if a["route"] == route]
            board["route"] = route
        return httpx.Response(200, json=board)
    if stop == "1002":
        board = make_board(time.time())
        board.update(stop_id="1002", arrivals=[], alerts=[], feed_errors=["vehicle_positions"])
        return httpx.Response(200, json=board)
    return httpx.Response(404, json={"error": f"Unknown stop {stop}"})


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    c = DrtClient(api_url="https://api.test/", stops_url="https://pages.test/data/stops.json",
                  transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_client", c)
    yield c
