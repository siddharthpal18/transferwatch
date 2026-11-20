"""The Lambda entry point, driven with the same events a Lambda Function URL sends."""
import base64
import json

import pytest

pytest.importorskip("mangum")

from drt_mcp.lambda_app import handler  # noqa: E402

HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
    "host": "abc123.lambda-url.us-east-2.on.aws",
    "mcp-protocol-version": "2025-06-18",
}


def url_event(body: dict, method: str = "POST", path: str = "/mcp") -> dict:
    """A Lambda Function URL (payload format 2.0) request."""
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": dict(HEADERS),
        "requestContext": {
            "accountId": "anonymous", "apiId": "abc123", "domainName": HEADERS["host"],
            "domainPrefix": "abc123", "requestId": "req-1", "routeKey": "$default",
            "stage": "$default", "time": "20/Nov/2026:19:00:00 +0000", "timeEpoch": 1795200000000,
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1",
                     "sourceIp": "160.79.104.10", "userAgent": "Claude-User"},
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


class FakeContext:
    function_name = "drt-mcp"
    aws_request_id = "req-1"
    def get_remaining_time_in_millis(self):
        return 30_000


def call(body, method="POST"):
    resp = handler(url_event(body, method), FakeContext())
    raw = resp.get("body") or ""
    if resp.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    return resp["statusCode"], (json.loads(raw) if raw else None)


def rpc(id_, method, params=None):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def test_initialize():
    status, body = call(rpc(1, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "claude-ai", "version": "1.0"}}))
    assert status == 200
    assert body["result"]["serverInfo"]["name"] == "drt-transit"
    assert "Durham" in body["result"]["instructions"]


def test_list_tools_has_read_only_annotations():
    status, body = call(rpc(2, "tools/list"))
    assert status == 200
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert set(tools) == {"find_stop", "next_buses", "service_alerts"}
    assert all(t["annotations"]["readOnlyHint"] is True for t in tools.values())


def test_call_tool_end_to_end():
    status, body = call(rpc(3, "tools/call", {"name": "next_buses",
                                              "arguments": {"stop_id": "2595", "walk_minutes": 5}}))
    assert status == 200
    result = body["result"]
    assert not result.get("isError")
    data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert data["stop_name"] == "Oshawa Centre Terminal" and data["buses"]


def test_two_cold_calls_in_a_row():
    """Lambda reuses the process between requests: the second call must still work."""
    for i in range(2):
        status, body = call(rpc(10 + i, "tools/call", {"name": "find_stop", "arguments": {"query": "whitby go"}}))
        assert status == 200 and not body["result"].get("isError")
