#!/usr/bin/env python3
"""Does Graph8's outbound `sse` client send anything from undocumented credential fields on the wire?

Serves a no-auth legacy HTTP+SSE MCP server (1 tool) that records every request header it receives, then
registers it with Graph8 once per case and /tests it. /test success = Graph8 reached the server (positive
control); the recorded headers show whether any field's canary value was sent. Each field carries its own
canary, so a value that arrives names the field that produced it. Canaries are not secrets.

Usage (repo root, GRAPH8_API_KEY in env; start a tunnel to the port first):
  cloudflared tunnel --url http://localhost:8099        # prints https://<name>.trycloudflare.com
  uv run python spikes/claims_check/headers_probe.py https://<name>.trycloudflare.com [--port 8099]
Writes headers_results.json (gitignored). Every registration is deleted and proven gone.
"""
import json
import os
import secrets
import sys
import threading
import time

import anyio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import verify_claims as vc  # noqa: E402  (register/delete/g8 helpers, run-prefixed names)

NONCE = secrets.token_hex(8)  # path prefix, so scanner traffic on the tunnel is not mistaken for Graph8
FIELDS = {  # field -> value in the registration body; every canary is unique
    "headers": {"X-Canary-Headers": "cnry-headers", "Authorization": "Bearer cnry-headers-auth"},
    "http_headers": {"X-Canary-Http-Headers": "cnry-http-headers"},
    "extra_headers": {"X-Canary-Extra-Headers": "cnry-extra-headers"},
    "request_headers": {"X-Canary-Request-Headers": "cnry-request-headers"},
    "auth": "cnry-auth",
    "bearer_token": "cnry-bearer-token",
    "api_key": "cnry-api-key",
    "token": "cnry-token",
    "oauth_client_id": "cnry-oauth-client-id",
}
SEEN = []  # (case, method, path, {header: value}) for every request under /<NONCE>/
CASE = {"name": None}


def mcp_app():
    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name="ping", description="probe",
                                                       input_schema={"type": "object"})])

    async def call_tool(ctx, params):
        return types.CallToolResult(content=[types.TextContent(type="text", text="pong")])

    server = Server("reseau-headers-probe", on_list_tools=list_tools, on_call_tool=call_tool)
    sse = SseServerTransport("/messages/")

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        parts = scope["path"].split("/", 2)  # "", nonce, rest
        if len(parts) != 3 or parts[1] != NONCE:
            from starlette.responses import Response
            return await Response("Not Found", status_code=404)(scope, receive, send)
        SEEN.append((CASE["name"], scope["method"], "/" + parts[2],
                     {k.decode().lower(): v.decode("latin-1") for k, v in scope["headers"]}))
        scope = dict(scope, root_path="/" + NONCE, path="/" + parts[2])
        if scope["path"] == "/sse" and scope["method"] == "GET":
            async with sse.connect_sse(scope, receive, send) as (read, write):
                await server.run(read, write, server.create_initialization_options())
            return
        if scope["path"] == "/messages/":
            return await sse.handle_post_message(scope, receive, send)

    return asgi


def canaries_in(headers):
    blob = json.dumps(headers)
    vals = []
    for f, v in FIELDS.items():
        vals += list(v.values()) if isinstance(v, dict) else [v]
    return sorted({c for c in vals if c in blob} | {c.split()[-1] for c in vals if c.split()[-1] in blob})


def run_case(name, url, extra):
    CASE["name"] = name
    res = vc.test_once("h-" + name, {"transport_type": "sse", "connection_url": url, **extra})
    time.sleep(2)
    reqs = [s for s in SEEN if s[0] == name]
    return {"test": res.get("test"), "requests": len(reqs),
            "header_names": sorted({h for r in reqs for h in r[3]}),
            "canaries_received": sorted({c for r in reqs for c in canaries_in(r[3])})}


def main(argv):
    base = argv[0].rstrip("/")
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8099
    srv = uvicorn.Server(uvicorn.Config(mcp_app(), host="127.0.0.1", port=port, access_log=False,
                                        lifespan="off", log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(2)
    url = "%s/%s/sse" % (base, NONCE)
    results = {"control_no_fields": run_case("control", url, {}),
               "all_fields": run_case("fields", url, FIELDS)}
    results["_final_leftovers"] = [s.get("name") for s in vc.listed()[0] if (s.get("name") or "").startswith(vc.RUN)]
    srv.should_exit = True
    json.dump(results, open(os.path.join(HERE, "headers_results.json"), "w"), indent=1)
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
