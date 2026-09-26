#!/usr/bin/env python3
"""Probe a remote MCP server over streamable HTTP: initialize -> tools/list -> one read-only tools/call.

Usage: python3 mcp_probe.py [github|linear|all]
Reads .env next to this file. stdlib only, no deps.
"""
import json, os, ssl, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL = "2025-06-18"

SERVERS = {
    # name: (url env, default url, token envs, read-only probe tool, args)
    "github": ("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/",
               ("GITHUB_MCP_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT"), "get_me", {}),
    "linear": ("LINEAR_MCP_URL", "https://mcp.linear.app/mcp",
               ("LINEAR_MCP_TOKEN", "LINEAR_ACCESS_TOKEN", "LINEAR_API_KEY"), "list_teams", {}),
    "graph8": ("GRAPH8_API_URL", "https://be.graph8.com/mcp/",
               ("GRAPH8_API_KEY",), "whoami", {}),
}


def load_env(path=None):
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def rpc(url, token, payload, session=None, timeout=45):
    """One JSON-RPC POST. Returns (status, headers, parsed_message_or_raw_text)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL,
        "User-Agent": "graph8-mcp-probe/1",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, dict(r.headers), parse_body(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")[:800]


def parse_body(text):
    """Streamable HTTP replies with either a JSON object or an SSE stream."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk.startswith("{"):
                msg = json.loads(chunk)
                if "result" in msg or "error" in msg:
                    return msg
    return text[:800] or None


def notify(url, token, session, method):
    try:
        rpc(url, token, {"jsonrpc": "2.0", "method": method}, session, timeout=15)
    except Exception:
        pass


def probe(name):
    url_env, default_url, token_envs, probe_tool, probe_args = SERVERS[name]
    url = os.environ.get(url_env, default_url)
    probe_tool = os.environ.get(name.upper() + "_PROBE_TOOL", probe_tool)
    token = next((os.environ[e] for e in token_envs if os.environ.get(e)), None)
    out = {"server": name, "url": url, "auth": "bearer" if token else "none", "errors": []}
    print("\n== %s  %s  (token: %s)" % (name, url, "yes" if token else "NO"))

    t0 = time.time()
    status, headers, msg = rpc(url, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                   "clientInfo": {"name": "graph8-mcp-probe", "version": "1"}}})
    out["initialize_ms"] = int((time.time() - t0) * 1000)
    out["initialize_status"] = status
    out["content_type"] = headers.get("Content-Type")
    session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    out["session_id"] = bool(session)
    print("   initialize: HTTP %s in %sms  ct=%s  session=%s" % (status, out["initialize_ms"], out["content_type"], bool(session)))

    if status != 200 or not isinstance(msg, dict) or "result" not in msg:
        out["errors"].append("initialize failed: HTTP %s %s" % (status, headers.get("WWW-Authenticate") or msg))
        print("   !! %s" % out["errors"][-1])
        return out
    out["server_info"] = msg["result"].get("serverInfo")
    out["negotiated_protocol"] = msg["result"].get("protocolVersion")
    print("   serverInfo: %s  protocol=%s" % (out["server_info"], out["negotiated_protocol"]))

    notify(url, token, session, "notifications/initialized")

    t0 = time.time()
    status, _, msg = rpc(url, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session)
    out["tools_list_ms"] = int((time.time() - t0) * 1000)
    tools = (msg.get("result", {}).get("tools") if isinstance(msg, dict) else None) or []
    out["tool_count"] = len(tools)
    out["tools"] = sorted(t["name"] for t in tools)
    print("   tools/list: HTTP %s in %sms -> %d tools" % (status, out["tools_list_ms"], len(tools)))
    if not tools:
        out["errors"].append("tools/list returned nothing: HTTP %s %s" % (status, msg))
        print("   !! %s" % out["errors"][-1])
        return out

    tool = probe_tool if probe_tool in out["tools"] else None
    if tool is None:
        out["errors"].append("expected read-only tool %r missing (have: %s...)" % (probe_tool, out["tools"][:8]))
        print("   !! %s" % out["errors"][-1])
        return out
    t0 = time.time()
    status, _, msg = rpc(url, token, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": tool, "arguments": probe_args}}, session)
    out["call_ms"] = int((time.time() - t0) * 1000)
    out["called_tool"] = tool
    result = msg.get("result") if isinstance(msg, dict) else None
    is_err = bool(result and result.get("isError")) or (isinstance(msg, dict) and "error" in msg)
    out["call_ok"] = status == 200 and result is not None and not is_err
    preview = json.dumps(result or msg)[:300]
    print("   tools/call %s: HTTP %s in %sms ok=%s\n     %s" % (tool, status, out["call_ms"], out["call_ok"], preview))
    if not out["call_ok"]:
        out["errors"].append("tools/call %s failed: %s" % (tool, preview))
    return out


def main():
    load_env()
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    names = list(SERVERS) if which == "all" else [which]
    results = [probe(n) for n in names]

    if len(results) > 1:
        print("\n== both servers in one session")
        ok = [r for r in results if r.get("tool_count")]
        names_seen = {}
        for r in ok:
            for t in r["tools"]:
                names_seen.setdefault(t, []).append(r["server"])
        clash = {t: s for t, s in names_seen.items() if len(s) > 1}
        total = sum(r["tool_count"] for r in ok)
        print("   combined toolset: %d tools from %d servers; name collisions: %s" % (total, len(ok), clash or "none"))
        print("   both read-only calls succeeded: %s" % all(r.get("call_ok") for r in results))

    print("\n== summary")
    for r in results:
        print("   %-7s init=%s tools=%s call=%s %s" % (r["server"], r.get("initialize_status"),
              r.get("tool_count", 0), r.get("call_ok"), ("ERRORS: " + " | ".join(r["errors"])) if r["errors"] else ""))
    json.dump(results, open(os.path.join(HERE, "probe_results.json"), "w"), indent=2)
    return 0 if all(r.get("call_ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
