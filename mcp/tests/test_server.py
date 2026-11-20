"""End-to-end: a real MCP client talking to the server in-process, with a fake TransferWatch API."""
import json

from mcp import Client

from drt_mcp.server import mcp


def payload(result):
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def test_lists_three_tools():
    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"find_stop", "next_buses", "service_alerts"}


async def test_find_stop_by_name_and_number():
    async with Client(mcp) as c:
        by_name = payload(await c.call_tool("find_stop", {"query": "oshawa centre"}))
        by_number = payload(await c.call_tool("find_stop", {"query": "#1002"}))
    assert by_name[0]["stop_id"] == "2595"
    assert by_number[0]["name"] == "Simcoe Northbound @ King"


async def test_find_stop_no_match_is_tool_error():
    async with Client(mcp) as c:
        r = await c.call_tool("find_stop", {"query": "union station"})
    assert r.is_error
    assert "No DRT stop matches" in r.content[0].text


async def test_next_buses_with_walk_time():
    async with Client(mcp) as c:
        r = payload(await c.call_tool("next_buses", {"stop_id": "2595", "walk_minutes": 5}))
    assert r["stop_name"] == "Oshawa Centre Terminal"
    first = r["buses"][0]
    assert first["route"] == "302" and first["tracking"] == "live GPS"
    assert [o["chance"] for o in first["here_by"]] == ["50%", "80%", "95%"]
    assert first["plan"]["kind"] == "leave_at"          # bus is ~10 min out, walk is 5
    notes = " | ".join(first["history_notes"])
    assert "Left early 1 of 4" in notes and "Usually 2 min late" in notes
    # untracked bus with no history: honest about missing odds
    by_route = {(b["route"], b["status"]): b for b in r["buses"]}
    assert by_route[("905", "running")]["here_by"] is None
    assert ("302", "cancelled") in by_route
    assert r["alert_count"] == 1


async def test_route_filter():
    async with Client(mcp) as c:
        r = payload(await c.call_tool("next_buses", {"stop_id": "2595", "route": "905"}))
    assert [b["route"] for b in r["buses"]] == ["905"]


async def test_empty_stop_and_feed_warning():
    async with Client(mcp) as c:
        r = payload(await c.call_tool("next_buses", {"stop_id": "1002"}))
    assert r["buses"] == [] and "No buses" in r["note"]
    assert "vehicle_positions" in r["warning"]


async def test_unknown_stop_is_tool_error():
    async with Client(mcp) as c:
        r = await c.call_tool("next_buses", {"stop_id": "99999999"})
    assert r.is_error and "find_stop" in r.content[0].text


async def test_service_alerts():
    async with Client(mcp) as c:
        r = payload(await c.call_tool("service_alerts", {"stop_id": "2595"}))
    assert r["alerts"][0]["effect"] == "DETOUR"


async def test_route_is_sent_to_api(fake_api):
    seen = []
    orig = fake_api.stop_board
    async def spy(stop_id, route=None):
        seen.append(route)
        return await orig(stop_id, route)
    fake_api.stop_board = spy
    async with Client(mcp) as c:
        await c.call_tool("next_buses", {"stop_id": "2595", "route": " 302 "})
    assert seen == ["302"]


async def test_buses_ending_at_this_stop_are_hidden():
    async with Client(mcp) as c:
        r = payload(await c.call_tool("next_buses", {"stop_id": "2595"}))
    assert all(b["headsign"] != "Oshawa Centre Terminal" for b in r["buses"])
