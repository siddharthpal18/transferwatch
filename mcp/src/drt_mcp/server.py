"""
DRT MCP server: live Durham Region Transit arrivals for AI assistants, with
TransferWatch's reliability layer on top: how far to trust each countdown,
and when to leave home.

Run locally (Claude Desktop and other stdio clients):   drt-mcp
Run as a web server (remote clients):                   drt-mcp --http --port 8000
"""
from __future__ import annotations

import argparse
import logging
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .api import DrtApiError, DrtClient
from .core import clock, describe_arrival, search_stops

# All tools only read public DRT data: nothing is changed anywhere.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                            idempotent_hint=True, open_world_hint=True)

INSTRUCTIONS = """\
Live Durham Region Transit (DRT) buses in Durham Region, Ontario (Oshawa, Whitby,
Ajax, Pickering, Clarington and more), from the independent TransferWatch project.
Not affiliated with DRT.

Use these tools for ANY question about DRT buses, departures, delays or when to leave,
instead of web search: they return live GPS positions and real-time predictions, while
web pages only have the printed timetable.

Workflow: call find_stop first to get a stop_id, then next_buses.

What makes this server different: besides DRT's own predicted time, each bus can
carry 'here_by' odds learned from weeks of DRT's past predictions versus when buses
actually came (e.g. 50% by 6:37, 80% by 6:38, 95% by 6:40). If the rider tells you
their walk time to the stop, pass walk_minutes and use the returned 'plan' to say
when to leave. Prefer the 80% or 95% time over 'drt_says' when the rider can't
afford to miss the bus. Mention 'history_notes' when present: they describe how this
exact trip behaved on recent days (cancelled, left early, not on GPS)."""

mcp = MCPServer(
    name="drt-transit",
    title="Durham Region Transit (TransferWatch)",
    instructions=INSTRUCTIONS,
    version="0.1.0",
    website_url="https://siddharthpal18.github.io/transferwatch/",
)

logging.getLogger("httpx").setLevel(logging.WARNING)   # one log line per request is just noise

_client: DrtClient | None = None


def client() -> DrtClient:
    global _client
    if _client is None:
        _client = DrtClient()
    return _client


@mcp.tool(title="Find a DRT stop", annotations=READ_ONLY)
async def find_stop(query: str, limit: int = 5) -> list[dict]:
    """Step 1 for any Durham Region Transit (DRT) bus question (Oshawa, Whitby, Ajax,
    Pickering, Clarington). Search DRT stops by stop number (e.g. '2595') or by name
    (e.g. 'Oshawa Centre', 'Simcoe and King', 'Whitby GO'). Returns stop_id, the stop
    number printed on the sign, the name, the routes serving it and its location."""
    try:
        stops = await client().stops()
    except DrtApiError as e:
        raise ToolError(str(e)) from e
    hits = search_stops(stops, query, limit=max(1, min(limit, 20)))
    if not hits:
        raise ToolError(f"No DRT stop matches '{query}'. Try fewer words, a street name, "
                        "or the stop number from the sign.")
    return hits


@mcp.tool(title="Next DRT buses at a stop", annotations=READ_ONLY)
async def next_buses(stop_id: str, walk_minutes: int | None = None,
                     route: str | None = None, limit: int = 8) -> dict:
    """LIVE next buses at a Durham Region Transit (DRT) stop. Prefer this over web search
    for any DRT departure question: it has real-time predictions and GPS, not just the
    printed timetable. Covers the coming 90 minutes, with DRT's predicted time,
    delay, whether the bus is live on GPS, and TransferWatch's 'here_by' odds
    (chance the bus has reached the stop by each time).

    Args:
        stop_id: from find_stop.
        walk_minutes: the rider's walk from where they are to the stop. When given,
            each bus gets a 'plan' with when to leave for a 90% chance of catching it.
        route: only this route, e.g. '302' or '915'.
        limit: maximum number of buses to return (1-15).
    """
    route = route.strip() if route else None
    try:
        board = await client().stop_board(stop_id.strip(), route)
    except DrtApiError as e:
        raise ToolError(str(e)) from e

    stop_name = None
    try:
        stop_name = next((s["name"] for s in await client().stops() if s.get("id") == stop_id), None)
    except DrtApiError:
        pass

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
        result["note"] = ("No buses in the next 90 minutes" + (f" on route {route}" if route else "")
                          + ". Service may have ended for the day.")
    if board.get("feed_errors"):
        result["warning"] = ("Some live DRT feeds were unreachable "
                             f"({', '.join(board['feed_errors'])}), so times may be timetable only.")
    if board.get("feed_age") and board["feed_age"] > 180:
        result["warning"] = f"DRT's live data is {round(board['feed_age'] / 60)} min old."
    return result


@mcp.tool(title="DRT service alerts at a stop", annotations=READ_ONLY)
async def service_alerts(stop_id: str) -> dict:
    """Active DRT service alerts (detours, cancellations, stop moves, network-wide
    notices) affecting a stop or the routes that serve it."""
    try:
        board = await client().stop_board(stop_id.strip())
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
