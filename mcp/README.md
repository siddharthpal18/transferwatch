# drt-mcp: Durham Region Transit for AI assistants

An [MCP](https://modelcontextprotocol.io) server that lets Claude, ChatGPT and other AI
assistants answer questions about live **Durham Region Transit (DRT)** buses in Oshawa,
Whitby, Ajax, Pickering and the rest of Durham Region, Ontario.

It's built on [TransferWatch](https://siddharthpal18.github.io/transferwatch/), and
adds something no other transit MCP server has: **how far to trust the countdown.**

> "I'm 8 minutes from Oshawa Centre. When do I leave for the 302?"
>
> "Leave at 6:34 PM. DRT says the bus comes at 6:36, but based on 1,234 past countdowns
> on route 302 it's here by 6:37 half the time, 6:38 80% of the time and 6:40 95% of the
> time. Heads-up: this trip left early 1 of the last 4 weekdays."

*Independent project. Not affiliated with or endorsed by Durham Region Transit.*

## What makes it different

Other transit servers repeat what the agency's feed says. This one also uses weeks of
TransferWatch history, comparing DRT's past predictions with when buses actually reached each stop:

- **Here-by odds:** chance the bus is at the stop by each time (50% / 80% / 95%)
- **Leave-at time:** when to leave for a 90% chance of catching the bus, using your walk time
- **Trip history notes:** "cancelled 1 of last 5 weekdays", "usually appears on GPS only
  3 min before this stop", "left early 2 of 5 weekdays"

The odds math is the same code path as the TransferWatch website (tested for parity).

## Tools

| Tool | What it answers |
|---|---|
| `find_stop(query)` | Stop ID, sign number and routes for a name or stop number |
| `next_buses(stop_id, walk_minutes?, route?)` | Next buses with DRT's time, delay, GPS status, here-by odds and a leave-at plan |
| `service_alerts(stop_id)` | Active detours, cancellations and notices for a stop and its routes |

## Install

Requires Python 3.10+.

```bash
pip install -e .            # from this folder
drt-mcp                     # runs over stdio
```

### Claude Desktop

Add to `claude_desktop_config.json`
(Windows: `%APPDATA%\Claude\claude_desktop_config.json`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "drt-transit": {
      "command": "drt-mcp"
    }
  }
}
```

If Claude can't find `drt-mcp`, use the full path from `where drt-mcp` (Windows) or
`which drt-mcp` (Mac/Linux) as the command.

### Test without Claude

```bash
npx -y @modelcontextprotocol/inspector drt-mcp
```

### As a web server (remote clients)

```bash
drt-mcp --http --port 8000          # MCP endpoint at http://127.0.0.1:8000/mcp
```

## Configuration

| Variable | Default |
|---|---|
| `DRT_API_URL` | the TransferWatch live API |
| `DRT_STOPS_URL` | `https://siddharthpal18.github.io/transferwatch/data/stops.json` |

## Development

```bash
pip install -e ".[dev]"
pytest
```

`test_matches_website_math` runs the website's own JavaScript in Node and checks the
Python odds match it on 300 random buses. It's skipped if Node or `docs/app.js` isn't present.

## Data

Live data: DRT's public GTFS and GTFS-Realtime feeds, via the TransferWatch API.
Odds are statistical estimates from recent history, not guarantees. Always allow extra time.
