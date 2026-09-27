"""Graph8-facing side of the Réseau MCP Gateway: legacy HTTP+SSE (2024-11-05), the one remote transport
Graph8's MCP client speaks (spikes/mcp_bridge/FINDINGS.md, Run 3: path A).

Auth: Graph8 registrations carry only `connection_url`, no headers, so the credential is a capability secret
in the path: GET /g8/<token>/sse opens the stream, and the `endpoint` event points POSTs at
/g8/<token>/messages/?session_id=..., so every request carries the token. The token only unlocks this tool
surface; upstream credentials stay in the Gateway. Graph8's read route echoes connection_url in plaintext
(FINDINGS.md Run 3), so the token is rotatable: RESEAU_GATEWAY_TOKEN is a comma-separated list, all
accepted, so a new token can be registered before the old one is dropped. Compared in constant time,
added to the log-redaction set, and uvicorn's access log (which prints the path) is off.

Run: RESEAU_GATEWAY_TOKEN=... python -m reseau.front [--host 0.0.0.0] [--port 8080]
"""
import argparse
import hmac
import logging
import os

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from starlette.responses import Response

from reseau import gateway

log = logging.getLogger("reseau.front")

TOKEN_ENV = "RESEAU_GATEWAY_TOKEN"
MIN_TOKEN_LEN = 22  # secrets.token_urlsafe(16) = 128 bits


def load_tokens(env=os.environ):
    tokens = [t.strip() for t in (env.get(TOKEN_ENV) or "").split(",") if t.strip()]
    if not tokens:
        raise SystemExit("%s is not set; refusing to serve the gateway unauthenticated" % TOKEN_ENV)
    if any(len(t) < MIN_TOKEN_LEN or "/" in t for t in tokens):
        raise SystemExit("%s: every token must be >= %d URL-safe chars (python -c 'import secrets; "
                         "print(secrets.token_urlsafe(32))')" % (TOKEN_ENV, MIN_TOKEN_LEN))
    return tokens


def valid(candidate, tokens):
    # Check every token so timing doesn't reveal which one (or how many) matched.
    ok = False
    for t in tokens:
        ok |= hmac.compare_digest(candidate.encode(), t.encode())
    return ok


def mcp_server(gw):
    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=await gw.tools())

    async def call_tool(ctx, params):
        return await gw.call(params.name, params.arguments)

    return Server("reseau-gateway", on_list_tools=list_tools, on_call_tool=call_tool)


def app(gw, tokens):
    """ASGI app: /g8/<token>/sse and /g8/<token>/messages/. Anything else, a bad token included, is a 404,
    so a caller without the token learns nothing about which paths exist."""
    gateway.SECRETS.update(tokens)  # the SDK logs the endpoint event (path with token) at DEBUG
    gateway.install_log_redaction()
    server = mcp_server(gw)
    sse = SseServerTransport("/messages/")

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        parts = scope["path"].split("/", 3)  # "", "g8", token, rest
        if len(parts) != 4 or parts[1] != "g8" or not valid(parts[2], tokens):
            log.warning("rejected unauthenticated %s from %s", scope["method"], (scope.get("client") or ("?",))[0])
            return await Response("Not Found", status_code=404)(scope, receive, send)
        # root_path makes the SDK's endpoint event carry the token, so POSTs are authenticated too.
        scope = dict(scope, root_path=scope.get("root_path", "") + "/g8/" + parts[2], path="/" + parts[3])
        if scope["path"] == "/sse" and scope["method"] == "GET":
            async with sse.connect_sse(scope, receive, send) as (read, write):
                await server.run(read, write, server.create_initialization_options())
            return
        if scope["path"] == "/messages/":
            return await sse.handle_post_message(scope, receive, send)
        await Response("Not Found", status_code=404)(scope, receive, send)

    return asgi


async def serve(host, port, tokens):
    async with gateway.Gateway() as gw:
        log.info("gateway up: %s", {n: h["ok"] for n, h in gw.health().items()})
        cfg = uvicorn.Config(app(gw, tokens), host=host, port=port, access_log=False, lifespan="off")
        await uvicorn.Server(cfg).serve()


if __name__ == "__main__":
    import asyncio

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(a.host, a.port, load_tokens()))
