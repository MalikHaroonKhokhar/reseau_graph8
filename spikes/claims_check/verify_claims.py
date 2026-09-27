#!/usr/bin/env python3
"""Re-runnable check of the Réseau pitch claims against the live APIs.

  direct     no Graph8 writes: CoinGecko /sse vs /mcp served tools, GitHub/Linear auth + transport, Graph8
             protected-resource metadata, raw upstream tool counts (230) vs the gateway surface (14) and prefixing.
  transport  POST /voice/mcp-servers with five Streamable-HTTP spellings; expects 422 each (anything created is deleted).
  sse        register as `sse` + /test + delete: CoinGecko /sse and /mcp, Atlassian, Notion, Asana.
  fields     register with headers/auth/oauth_client_id/bearer_token canaries; checks they are not stored.
  stdio      command-existence oracle on the spawn host (Errno 2 = absent) plus an import oracle for mcp_proxy.
  reseau     /test on an existing registration (pass its uuid): the live gateway's tools_count.

Usage (repo root, tokens in env: set -a; . ./.env; set +a):
  uv run python spikes/claims_check/verify_claims.py direct|transport|sse|fields|stdio [...]
  uv run python spikes/claims_check/verify_claims.py reseau <mcp_server_uuid>
Registrations are named reseau-claims-<hex>-*, deleted right after use and proven gone from the list route.
Results go to claims_results.json (gitignored). Tokens never printed; list-route bodies pass bridge_probe.redact.
"""
import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "mcp_bridge"), os.path.join(HERE, "..", "..")]
import bridge_probe as bp  # noqa: E402  (g8(): serial, 3 s spacing, explicit UA)

RUN = "reseau-claims-%s-" % uuid.uuid4().hex[:6]
MS = "/api/v1/voice/mcp-servers"
LIST = "/api/v1/workflows/mcp-servers"
COINGECKO = "https://mcp.api.coingecko.com"
OAUTH_SSE = {"atlassian": "https://mcp.atlassian.com/v1/sse", "notion": "https://mcp.notion.com/sse",
             "asana": "https://mcp.asana.com/sse"}
SPELLINGS = ["streamable_http", "streamable-http", "streamablehttp", "http", "streamable"]
CANARIES = {"headers": {"X-Reseau-Canary": "canary-hdr-7f3a"}, "auth": "canary-auth-7f3a",
            "oauth_client_id": "canary-client-7f3a", "bearer_token": "canary-bearer-7f3a"}
RESULTS = {}


def g8(method, path, body=None):
    status, text, ms = bp.g8(method, path, body)
    try:
        return status, json.loads(text), ms
    except ValueError:
        return status, text, ms


def listed():
    _, d, _ = g8("GET", LIST)
    return d.get("servers", []), d.get("total")


def delete(sid):
    g8("DELETE", "%s/%s" % (MS, sid))
    servers, _ = listed()
    gone = all((s.get("mcp_server_id") or s.get("id")) != sid for s in servers)
    assert gone, "registration %s not proven gone" % sid
    return gone


def register(name, body):
    status, d, _ = g8("POST", MS, {"name": RUN + name, **body})
    data = d.get("data", d) if isinstance(d, dict) else {}
    return status, d, (data.get("mcp_server_id") if isinstance(data, dict) else None)


def test_once(name, body):
    """register -> /test -> delete. Returns the /test data and its wall time."""
    status, d, sid = register(name, body)
    if not sid:
        return {"create_status": status, "create": d}
    try:
        t0 = time.time()
        st, r, _ = g8("POST", "%s/%s/test" % (MS, sid))
        wall = round(time.time() - t0 - 3, 2)  # g8 sleeps 3 s before each call
        return {"create_status": status, "test_status": st, "test": r.get("data", r) if isinstance(r, dict) else r,
                "test_s": wall}
    finally:
        delete(sid)


def check_transport():
    out = {}
    for sp in SPELLINGS:
        status, d, sid = register("t-" + sp, {"transport_type": sp, "connection_url": COINGECKO + "/mcp"})
        msgs = [e.get("msg") for e in d.get("detail", [])] if isinstance(d, dict) and isinstance(d.get("detail"), list) else d
        out[sp] = {"status": status, "msg": msgs}
        if sid:
            delete(sid)
    RESULTS["transport"] = out


def check_sse():
    cases = {"coingecko_sse": COINGECKO + "/sse", "coingecko_mcp": COINGECKO + "/mcp", **OAUTH_SSE}
    RESULTS["sse"] = {k: test_once("s-" + k, {"transport_type": "sse", "connection_url": u}) for k, u in cases.items()}


def check_fields():
    status, d, sid = register("fields", {"transport_type": "sse", "connection_url": COINGECKO + "/sse", **CANARIES})
    res = {"create_status": status, "create_keys": sorted((d.get("data") or {}).keys()) if isinstance(d, dict) else None}
    try:
        servers, _ = listed()
        rec = next(s for s in servers if (s.get("mcp_server_id") or s.get("id")) == sid)
        dump = json.dumps(rec) + json.dumps(d)
        res.update(list_keys=sorted(rec.keys()), extra_keys_stored=sorted(set(CANARIES) & set(rec)),
                   canary_values_echoed=[k for k, v in CANARIES.items() if json.dumps(v).strip('"{}') .split('"')[-1] in dump])
        st, r, _ = g8("POST", "%s/%s/test" % (MS, sid))
        res["test"] = r.get("data", r)
    finally:
        delete(sid)
    RESULTS["fields"] = res


def check_stdio():
    cases = {c: {"command": c} for c in ("npx", "uvx", "node", "bash", "mcp-proxy", "python3", "sh")}
    # Import oracle, calibrated in FINDINGS Run 3: a failed import exits fast (TaskGroup); a live process hangs -> 502.
    cases["import_mcp_proxy"] = {"command": "python3", "args": ["-c", "import mcp_proxy, time; time.sleep(100)"]}
    cases["ctl_import_json"] = {"command": "python3", "args": ["-c", "import json, time; time.sleep(100)"]}
    RESULTS["stdio"] = {k: test_once("x-" + k, {"transport_type": "stdio", **b}) for k, b in cases.items()}


def check_reseau(sid):
    st, r, _ = g8("POST", "%s/%s/test" % (MS, sid))
    RESULTS["reseau"] = {"status": st, "test": r.get("data", r) if isinstance(r, dict) else r}


async def _tools(transport):
    from mcp import Client
    names, cursor = [], None
    async with Client(transport, mode="legacy") as c:
        while True:
            page = await c.list_tools(cursor=cursor)
            names += [t.name for t in page.tools]
            cursor = page.next_cursor
            if not cursor:
                return sorted(names)


async def _authed_tools(url, token):
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from reseau.gateway import build_headers
    async with httpx2.AsyncClient(headers=build_headers(token), timeout=60) as http:
        return await _tools(streamable_http_client(url, http_client=http))


def _raw(method, url, **headers):
    from reseau import outbound
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "reseau-claims", "version": "1"}}}).encode()
    h = {"user-agent": outbound.USER_AGENT, "accept": "application/json, text/event-stream",
         "content-type": "application/json", **headers}
    try:
        st, rh, raw = outbound.http_transport(method, url, h, body if method == "POST" else None, 10, 15)
    except Exception as e:
        return {"error": type(e).__name__}
    return {"status": st, "ct": rh.get("content-type", "").split(";")[0], "www-authenticate": rh.get("www-authenticate")}


def check_direct():
    import anyio
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client
    from reseau import gateway
    out = {}
    cg_sse = anyio.run(_tools, sse_client(COINGECKO + "/sse"))
    cg_mcp = anyio.run(_tools, streamable_http_client(COINGECKO + "/mcp"))
    out["coingecko"] = {"sse_tools": cg_sse, "mcp_tools": cg_mcp, "same": cg_sse == cg_mcp}
    for name, url in (("github_unauth", "https://api.githubcopilot.com/mcp/"), ("linear_unauth", "https://mcp.linear.app/mcp"),
                      ("graph8_unauth", "https://be.graph8.com/mcp/")):
        out[name] = _raw("POST", url)
    out["linear_sse_get"] = _raw("GET", "https://mcp.linear.app/sse", accept="text/event-stream")
    env = os.environ
    full = {"github": anyio.run(_authed_tools, "https://api.githubcopilot.com/mcp/", env["GITHUB_MCP_TOKEN"]),
            "linear": anyio.run(_authed_tools, "https://mcp.linear.app/mcp", env["LINEAR_API_KEY"]),
            "graph8": anyio.run(_authed_tools, "https://be.graph8.com/mcp/", env["GRAPH8_API_KEY"])}
    out["upstream_full_counts"] = {k: len(v) for k, v in full.items()}
    out["upstream_full_total"] = sum(len(v) for v in full.values())
    out["raw_collisions_github_linear"] = sorted(set(full["github"]) & set(full["linear"]))

    async def surface():
        async with gateway.Gateway() as gw:
            return sorted(t.name for t in await gw.tools())
    exposed = anyio.run(surface)
    out["gateway_exposed"] = {"count": len(exposed), "names": exposed}
    RESULTS["direct"] = out


def main(argv):
    checks = {"direct": check_direct, "transport": check_transport, "sse": check_sse, "fields": check_fields,
              "stdio": check_stdio}
    if argv[:1] == ["reseau"]:
        check_reseau(argv[1])
    else:
        for a in argv or list(checks):
            checks[a]()
    servers, total = listed()
    RESULTS["_final_leftovers"] = [s.get("name") for s in servers if (s.get("name") or "").startswith(RUN)]
    RESULTS["_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = os.path.join(HERE, "claims_results.json")
    prev = json.load(open(path)) if os.path.exists(path) else {}
    json.dump({**prev, **RESULTS}, open(path, "w"), indent=1)
    print(json.dumps(RESULTS, indent=1)[:6000])


if __name__ == "__main__":
    main(sys.argv[1:])
