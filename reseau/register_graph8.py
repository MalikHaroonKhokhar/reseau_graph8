"""Register the running gateway with Graph8 as an `sse` MCP server, /test it, then delete it (live check).

    GRAPH8_API_KEY=... RESEAU_GATEWAY_TOKEN=... python -m reseau.register_graph8 https://<public gateway host> [--keep]

The gateway must already be serving (python -m reseau.front) behind that public URL. Steps: count the tools
the gateway exposes over SSE ourselves, create the registration, check whether Graph8's read route echoes
the token, POST /test and compare tools_count, then delete and verify the list no longer holds the record.
--keep leaves the registration in place for real use (the token is then readable org-wide: rotate it when done).

connection_url is a secret: nothing printed here contains the token (gateway.redact over every line).
Registration routes: spikes/mcp_bridge/FINDINGS.md, "Outbound registration surface".
"""
import argparse
import asyncio
import json
import os
import sys
import time

from mcp import Client
from mcp.client.sse import sse_client

from reseau import front, gateway, outbound

BASE = "https://be.graph8.com"
NAME = "reseau-gateway"


def say(*parts):
    print(gateway.redact(" ".join(str(p) for p in parts), gateway.SECRETS))


def g8(http, key, method, path, body=None):
    time.sleep(3)  # be.graph8.com 429-challenges bursts (FINDINGS.md)
    try:
        r = http.request(method, BASE + path, json.dumps(body).encode() if body is not None else None,
                         {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        status, data = r.status, r.data
    except outbound.HttpError as e:
        status, data = e.status, {"error": e.kind, "snippet": e.snippet}
    say(" ", method, path, "->", status)
    return status, (data.get("data", data) if isinstance(data, dict) else data)


def cleanup(http, key, uuid):
    """Delete the registration and prove it is gone. Only a 200 DELETE followed by a 200 list with a
    well-formed `servers` array that lacks the record counts; anything else is a failed cleanup."""
    status, _ = g8(http, key, "DELETE", "/api/v1/voice/mcp-servers/" + uuid)
    if status != 200:
        say("cleanup FAILED: DELETE returned HTTP", status, "- remove", uuid, "by hand")
        return False
    status, final = g8(http, key, "GET", "/api/v1/workflows/mcp-servers")
    servers = final.get("servers") if isinstance(final, dict) else None
    if status != 200 or not isinstance(servers, list) or not all(isinstance(s, dict) for s in servers):
        say("cleanup UNVERIFIED: list returned HTTP", status, "without a servers array")
        return False
    gone = not any(s.get("mcp_server_id") == uuid for s in servers)
    say("deleted:", gone, "final list:", {"servers": len(servers), "total": final.get("total")})
    return gone


async def gateway_tool_count(url):
    async with Client(sse_client(url), mode="legacy") as c:
        return len((await c.list_tools()).tools)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("public_url", help="public base URL the gateway is reachable at, e.g. https://gw.example.com")
    p.add_argument("--keep", action="store_true", help="leave the registration in place")
    a = p.parse_args()
    env = os.environ
    key = gateway.resolve_credential(gateway.Upstream("graph8", BASE, "GRAPH8_API_KEY"), env)
    token = front.load_tokens(env)[0]  # register with the newest token: put it first when rotating
    gateway.SECRETS.update({key, token})
    gateway.install_log_redaction()
    url = "%s/g8/%s/sse" % (a.public_url.rstrip("/"), token)

    expected = asyncio.run(gateway_tool_count(url))
    say("gateway exposes", expected, "tools at", url)

    http = outbound.Client()
    status, rec = g8(http, key, "POST", "/api/v1/voice/mcp-servers",
                     {"name": NAME, "transport_type": "sse", "connection_url": url})
    uuid = rec.get("mcp_server_id") if isinstance(rec, dict) else None
    if not uuid:
        say("create failed:", rec)
        return 1
    ok = False
    try:
        _, listing = g8(http, key, "GET", "/api/v1/workflows/mcp-servers")
        say("read route echoes the token:", token in json.dumps(listing), "(treat RESEAU_GATEWAY_TOKEN as exposed org-wide)")
        status, result = g8(http, key, "POST", "/api/v1/voice/mcp-servers/%s/test" % uuid)
        say("/test:", result)
        ok = status == 200 and isinstance(result, dict) and result.get("success") is True and result.get("tools_count") == expected
        say("PASS" if ok else "FAIL", "- tools_count %s, expected %s" % (
            result.get("tools_count") if isinstance(result, dict) else None, expected))
    finally:
        if a.keep:
            say("kept registration", uuid)
        else:
            ok = cleanup(http, key, uuid) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
