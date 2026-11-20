"""
AWS Lambda entry point: serves the DRT MCP server over Streamable HTTP behind a
Lambda Function URL, so Claude (web, desktop, mobile) can add it as a custom connector.

Lambda handler setting:  drt_mcp.lambda_app.handler
MCP endpoint:            https://<function-url-id>.lambda-url.<region>.on.aws/mcp

Stateless + JSON responses: every request is self-contained, which suits Lambda
(no sessions to keep between invocations, no long-lived streams).
"""
from __future__ import annotations

import hmac
import os

from mangum import Mangum
from mcp.server.transport_security import TransportSecuritySettings

from .server import mcp

def build_app():
    return mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # DNS rebinding protection guards servers on localhost. This one is a public,
        # read-only endpoint on AWS's domain, so Host-header pinning isn't needed.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


MCP_PATH = "/mcp"
KEY_HEADER = "x-api-key"


def _expected_key() -> str:
    """Optional shared secret. Set the Lambda environment variable MCP_API_KEY to make the
    server private (callers must send it in the x-api-key header); remove it to go public."""
    return os.environ.get("MCP_API_KEY", "").strip()


def _reply(status: int, message: str, extra_headers: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "text/plain", **(extra_headers or {})},
        "body": message,
        "isBase64Encoded": False,
    }


def handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or "").upper()
    path = (event.get("rawPath") or http.get("path") or "/").rstrip("/") or "/"

    # Answer everything except "POST /mcp" right here, in about a millisecond.
    # A GET on /mcp asks for a long-lived event stream, which a stateless Lambda
    # can't serve: it would hang until the timeout and bill 30 s per request.
    # The MCP spec lets servers answer such GETs with 405, and clients then use POST only.
    if path != MCP_PATH:
        return _reply(404, "Not found. The MCP endpoint is /mcp (POST only).\n")
    if method != "POST":
        return _reply(405, "Method not allowed. Send MCP requests as POST to /mcp.\n",
                      {"Allow": "POST"})

    expected = _expected_key()
    if expected:
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        given = (headers.get(KEY_HEADER) or "").strip()
        if not hmac.compare_digest(given.encode(), expected.encode()):   # constant-time compare
            return _reply(401, "Missing or wrong API key.\n")

    # "/mcp/" (pasted with a trailing slash) would get a redirect: serve it as "/mcp"
    event = {**event, "rawPath": MCP_PATH}
    if http:
        event["requestContext"] = {**event["requestContext"], "http": {**http, "path": MCP_PATH}}

    # Mangum runs the app's startup/shutdown around every invocation, and the SDK's
    # session manager can only be started once, so each request gets a fresh app.
    # Building one is cheap: tools are registered once, at import time.
    return Mangum(build_app(), lifespan="auto")(event, context)
