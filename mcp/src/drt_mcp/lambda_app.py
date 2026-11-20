"""
AWS Lambda entry point: serves the DRT MCP server over Streamable HTTP behind a
Lambda Function URL, so Claude (web, desktop, mobile) can add it as a custom connector.

Lambda handler setting:  drt_mcp.lambda_app.handler
MCP endpoint:            https://<function-url-id>.lambda-url.<region>.on.aws/mcp

Stateless + JSON responses: every request is self-contained, which suits Lambda
(no sessions to keep between invocations, no long-lived streams).
"""
from __future__ import annotations

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


def handler(event, context):
    # Mangum runs the app's startup/shutdown around every invocation, and the SDK's
    # session manager can only be started once, so each request gets a fresh app.
    # Building one is cheap: tools are registered once, at import time.
    return Mangum(build_app(), lifespan="auto")(event, context)
