"""The odds math must match the TransferWatch website (docs/app.js) exactly."""
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from drt_mcp.core import catch_chance, cdf, here_by, leave_by, pct, plan, search_stops

from .conftest import Q, STOPS

APP_JS = Path(os.environ.get("TRANSFERWATCH_APP_JS", "../docs/app.js"))


def bus(base=1_000_000, q=Q, cancelled=0.0):
    return {"state": "running", "route": "302", "dist": {"base": base, "q": q, "n": 50, "from": "trip"},
            "prob": {"cancelled": cancelled} if cancelled else None}


def test_cdf_is_monotonic_and_bounded():
    a = bus()
    vals = [cdf(a["dist"], a["dist"]["base"] + x) for x in range(-600, 900, 5)]
    assert all(0 <= v <= 1 for v in vals)
    assert all(b >= a_ for a_, b in zip(vals, vals[1:]))
    assert cdf(a["dist"], a["dist"]["base"] + Q[9]) == pytest.approx(0.5)


def test_leave_by_targets_90_percent():
    a = bus()
    t = leave_by(a, walk_sec=300)
    # arriving at t + walk should leave about a 90% chance the bus hasn't come yet
    assert catch_chance(a, t + 300) == pytest.approx(0.9, abs=0.01)


def test_cancellations_lower_the_odds():
    plain, risky = here_by(bus()), here_by(bus(cancelled=0.2))
    assert plain[0]["chance"] == "50%" and risky[0]["chance"] == "40%"
    assert leave_by(bus(cancelled=0.1), 300) is None      # 90% / 0.9 = 100% needed: impossible


def test_plan_kinds():
    a = bus()
    base = a["dist"]["base"]
    assert plan(a, 5, now=base - 20 * 60)["kind"] == "leave_at"
    assert plan(a, 5, now=base - 5 * 60)["kind"] == "leave_now"
    assert plan(a, 5, now=base)["kind"] in ("likely_miss", "too_late")
    assert plan(a, 30, now=base)["kind"] == "too_late"


def test_pct_labels():
    assert [pct(p) for p in (0.97, 0.02, 0.83)] == ["95%+", "under 5%", "85%"]


def test_search_prefers_short_names_and_exact_numbers():
    assert search_stops(STOPS, "2595")[0]["name"] == "Oshawa Centre Terminal"
    assert search_stops(STOPS, "whitby go")[0]["stop_id"] == "93001"
    assert search_stops(STOPS, "   ") == []


@pytest.mark.skipif(not (shutil.which("node") and APP_JS.exists()), reason="needs node and docs/app.js")
def test_matches_website_math():
    """Run the website's own cdf/catchChance/leaveBy in Node on random buses and compare."""
    src = APP_JS.read_text()
    start = src.index("function cdf(")
    end = src.index("const pctText")
    rng = random.Random(7)
    cases = []
    for _ in range(300):
        q = sorted(rng.randint(-300, 900) for _ in range(19))
        a = {"dist": {"base": 1_000_000, "q": q}, "prob": {"cancelled": rng.choice([0, 0, 0.05, 0.3])}}
        cases.append({"a": a, "t": 1_000_000 + rng.randint(-600, 1200), "w": rng.randint(60, 1500)})
    js = (src[start:end] + "\nconst cases = " + json.dumps(cases) + ";\n"
          "console.log(JSON.stringify(cases.map(c => [cdf(c.a.dist, c.t), catchChance(c.a, c.t), leaveBy(c.a, c.w, 0.9)])));")
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    for c, (j_cdf, j_catch, j_leave) in zip(cases, out):
        assert cdf(c["a"]["dist"], c["t"]) == pytest.approx(j_cdf)
        assert catch_chance(c["a"], c["t"]) == pytest.approx(j_catch)
        assert leave_by(c["a"], c["w"], 0.9) == j_leave


def test_search_ignores_and_and_maps_go_to_station():
    from drt_mcp.core import search_stops
    stops = [
        {"id": "2712", "code": "2712", "name": "Simcoe Northbound @ Conlin"},
        {"id": "2576", "code": "2576", "name": "Whitby Station"},
        {"id": "61", "code": "61", "name": "Gordon Northbound @ Whitby Shores Greenway"},
    ]
    assert [s["stop_id"] for s in search_stops(stops, "Simcoe and Conlin")] == ["2712"]
    assert [s["stop_id"] for s in search_stops(stops, "Simcoe & Conlin")] == ["2712"]
    assert [s["stop_id"] for s in search_stops(stops, "Whitby GO")] == ["2576"]
