"""HTTP access to the TransferWatch live API and the public stop list."""
from __future__ import annotations

import os
import time

import httpx

DEFAULT_API_URL = "https://dciiu4vmtkjdzlevlz27de433q0kbwno.lambda-url.us-east-2.on.aws/"
DEFAULT_STOPS_URL = "https://siddharthpal18.github.io/transferwatch/data/stops.json"
USER_AGENT = "drt-mcp/0.1 (TransferWatch MCP server)"
STOPS_TTL = 6 * 3600
BOARD_TTL = 15            # seconds: identical questions in a burst share one API call
BOARD_CACHE_MAX = 200


class DrtApiError(Exception):
    """A problem talking to the TransferWatch API, worded for the AI client."""


class DrtClient:
    def __init__(self, api_url: str | None = None, stops_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_url = api_url or os.environ.get("DRT_API_URL", DEFAULT_API_URL)
        self.stops_url = stops_url or os.environ.get("DRT_STOPS_URL", DEFAULT_STOPS_URL)
        self._http = httpx.AsyncClient(timeout=20, transport=transport,
                                       headers={"User-Agent": USER_AGENT})
        self._stops: list[dict] | None = None
        self._stops_at = 0.0
        self._boards: dict[tuple[str, str | None], tuple[float, dict]] = {}

    async def stops(self) -> list[dict]:
        if self._stops is None or time.time() - self._stops_at > STOPS_TTL:
            try:
                r = await self._http.get(self.stops_url)
                r.raise_for_status()
                self._stops = r.json()
                self._stops_at = time.time()
            except (httpx.HTTPError, ValueError) as e:
                if self._stops is None:
                    raise DrtApiError(f"Couldn't load the DRT stop list: {e}") from e
        return self._stops

    async def stop_board(self, stop_id: str, route: str | None = None) -> dict:
        key = (stop_id, route)
        hit = self._boards.get(key)
        if hit and time.time() - hit[0] < BOARD_TTL:
            return hit[1]
        board = await self._fetch_board(stop_id, route)
        if len(self._boards) >= BOARD_CACHE_MAX:
            self._boards.clear()
        self._boards[key] = (time.time(), board)
        return board

    async def _fetch_board(self, stop_id: str, route: str | None) -> dict:
        params = {"stop": stop_id}
        if route:
            params["route"] = route      # filtered before the API's 15-bus cap, so later buses aren't cut
        try:
            r = await self._http.get(self.api_url, params=params)
        except httpx.HTTPError as e:
            raise DrtApiError(f"The TransferWatch API didn't respond: {e}") from e
        if r.status_code == 404:
            raise DrtApiError(f"Unknown DRT stop '{stop_id}'. Use find_stop to look up the stop ID.")
        if r.status_code >= 400:
            raise DrtApiError(f"The TransferWatch API returned HTTP {r.status_code}.")
        try:
            return r.json()
        except ValueError as e:
            raise DrtApiError("The TransferWatch API returned something that isn't JSON.") from e

    async def aclose(self) -> None:
        await self._http.aclose()
