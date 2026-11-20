"""
DRT MCP server: live Durham Region Transit arrivals for AI assistants, with
TransferWatch's reliability layer on top: how far to trust each countdown,
and when to leave home.

Run locally (Claude Desktop and other stdio clients):   drt-mcp
Run as a web server (remote clients):                   drt-mcp --http --port 8000
"""
from __future__ import annotations

import argparse
import re
import logging
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .api import DrtApiError, DrtClient
from .core import clock, describe_arrival, search_stops

# All tools only read public DRT data: nothing is changed anywhere.
def read_only(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, destructive_hint=False,
                           idempotent_hint=True, open_world_hint=True)

INSTRUCTIONS = """\
Live Durham Region Transit (DRT) bus data for Durham Region, Ontario (Oshawa, Whitby,
Ajax, Pickering, Clarington and more), from the independent TransferWatch project.
Not affiliated with DRT.

Data comes from DRT's real-time feeds (GPS positions and live predictions), not the
printed timetable. Stops are identified by stop_id.

Field meanings: 'drt_says' is DRT's own predicted arrival time. 'here_by' gives the
chance the bus has reached the stop by each listed time (e.g. 50% by 6:37, 80% by
6:38, 95% by 6:40), learned from weeks of DRT's past predictions compared with actual
arrivals. 'plan' (present when walk_minutes is given) is the latest departure time
for a 90% chance of catching the bus. 'history_notes' describe how the same trip
behaved on recent days (cancelled, left early, not on GPS)."""

mcp = MCPServer(
    name="drt-transit",
    title="Durham Region Transit (TransferWatch)",
    instructions=INSTRUCTIONS,
    version="0.1.0",
    website_url="https://siddharthpal18.github.io/transferwatch/",
)

logging.getLogger("httpx").setLevel(logging.WARNING)   # one log line per request is just noise

_client: DrtClient | None = None


_STOP_ID = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")
_ROUTE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")


def clean_stop_id(stop_id: str) -> str:
    stop_id = (stop_id or "").strip().lstrip("#")
    if not stop_id:
        raise ToolError("stop_id is required: the DRT stop number shown on the stop sign, e.g. '2595'.")
    if not _STOP_ID.match(stop_id):
        raise ToolError(f"'{stop_id[:40]}' is not a DRT stop ID. Stop IDs are short numbers like '2595'.")
    return stop_id


def client() -> DrtClient:
    global _client
    if _client is None:
        _client = DrtClient()
    return _client


@mcp.tool(title="Find a DRT stop", annotations=read_only("Find a DRT stop"))
async def find_stop(query: str, limit: int = 5) -> list[dict]:
    """Search Durham Region Transit (DRT) stops in Oshawa, Whitby, Ajax, Pickering and
    Clarington by stop number (e.g. '2595') or by name (e.g. 'Oshawa Centre',
    'Simcoe and King', 'Whitby GO'). Returns each stop's stop_id, the stop number
    printed on the sign, its name, the routes serving it and its location."""
    try:
        stops = await client().stops()
    except DrtApiError as e:
        raise ToolError(str(e)) from e
    hits = search_stops(stops, query, limit=max(1, min(limit, 20)))
    if not hits:
        raise ToolError(f"No DRT stop matches '{query}'. Try fewer words, a street name, "
                        "or the stop number from the sign.")
    return hits


@mcp.tool(title="Next DRT buses at a stop", annotations=read_only("Next DRT buses at a stop"))
async def next_buses(stop_id: str, walk_minutes: int | None = None,
                     route: str | None = None, limit: int = 8) -> dict:
    """Live upcoming buses at a Durham Region Transit (DRT) stop for the next 90
    minutes, from DRT's real-time feeds. For each bus: DRT's predicted time, delay,
    whether it is live on GPS, and TransferWatch's 'here_by' odds (the chance the bus
    has reached the stop by each time).

    Args:
        stop_id: the DRT stop ID (the stop number shown on the stop sign).
        walk_minutes: the rider's walking time to the stop. When given, each bus
            includes a 'plan' with the latest departure time for a 90% chance of
            catching it.
        route: only this route, e.g. '302' or '915'.
        limit: maximum number of buses to return (1-15).
    """
    stop_id = clean_stop_id(stop_id)
    route = route.strip() if route and route.strip() else None
    if route and not _ROUTE.match(route):
        raise ToolError(f"'{route[:40]}' is not a DRT route. Routes look like '902', '302' or 'N2'.")
    if walk_minutes is not None and not 0 <= walk_minutes <= 120:
        raise ToolError("walk_minutes must be between 0 and 120.")
    try:
        board = await client().stop_board(stop_id, route)
    except DrtApiError as e:
        raise ToolError(str(e)) from e

    stop = None
    try:
        stop = next((s for s in await client().stops() if s.get("id") == stop_id), None)
    except DrtApiError:
        pass
    stop_name = stop.get("name") if stop else None
    stop_routes = [str(r) for r in (stop.get("routes") or [])] if stop else []

    now = time.time()
    arrivals = board.get("arrivals") or []
    if route:                       # also filter here, in case the API ignored the route
        want = route.lower()
        arrivals = [a for a in arrivals if str(a.get("route", "")).lower() == want
                    or str(a.get("route_id", "")).lower() == want]
    if stop_name:                   # a bus whose destination is this stop ends here: nobody can board it
        arrivals = [a for a in arrivals if (a.get("headsign") or "").strip().lower() != stop_name.lower()]
    buses = [describe_arrival(a, now, walk_minutes) for a in arrivals[:max(1, min(limit, 15))]]

    result = {
        "stop_id": stop_id,
        "stop_name": stop_name,
        "as_of": clock(board.get("generated_at") or now),
        "buses": buses,
        "alert_count": len(board.get("alerts") or []),
        "history_days": board.get("history_days", 0),
    }
    if not buses:
        if route and stop_routes and route.lower() not in (r.lower() for r in stop_routes):
            result["note"] = (f"Route {route} does not serve this stop. "
                              f"Routes here: {', '.join(stop_routes)}.")
        else:
            result["note"] = ("No buses in the next 90 minutes" + (f" on route {route}" if route else "")
                              + ". Service may have ended for the day.")
    if board.get("feed_errors"):
        result["warning"] = ("Some live DRT feeds were unreachable "
                             f"({', '.join(board['feed_errors'])}), so times may be timetable only.")
    if board.get("feed_age") and board["feed_age"] > 180:
        result["warning"] = f"DRT's live data is {round(board['feed_age'] / 60)} min old."
    return result


@mcp.tool(title="DRT service alerts at a stop", annotations=read_only("DRT service alerts at a stop"))
async def service_alerts(stop_id: str) -> dict:
    """Active DRT service alerts (detours, cancellations, stop moves, network-wide
    notices) affecting a stop or the routes that serve it."""
    try:
        stop_id = clean_stop_id(stop_id)
        board = await client().stop_board(stop_id)
    except DrtApiError as e:
        raise ToolError(str(e)) from e
    alerts = [{k: a.get(k) for k in ("header", "description", "effect", "cause", "url") if a.get(k)}
              for a in board.get("alerts") or []]
    return {"stop_id": stop_id, "alerts": alerts,
            "note": None if alerts else "No active alerts for this stop or its routes."}


def main() -> None:
    parser = argparse.ArgumentParser(description="DRT transit MCP server (TransferWatch)")
    parser.add_argument("--http", action="store_true", help="serve over Streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.http:
        mcp.run("streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
