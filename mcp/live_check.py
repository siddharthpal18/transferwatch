import asyncio
from mcp import Client
from drt_mcp.server import mcp

async def main():
    async with Client(mcp) as c:
        stop = await c.call_tool("find_stop", {"query": "oshawa centre"})
        print(stop.content[0].text[:400])
        r = await c.call_tool("next_buses", {"stop_id": "2595", "walk_minutes": 8, "limit": 3})
        print(r.content[0].text)

asyncio.run(main())