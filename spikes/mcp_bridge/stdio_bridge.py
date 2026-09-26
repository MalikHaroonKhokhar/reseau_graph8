"""stdio <-> Streamable HTTP MCP bridge, stdlib only (runs via `python3 -c` on Graph8's spawn host).

Env: UPSTREAM_URL (required), UPSTREAM_TOKEN (optional, sent as Bearer).
Every JSON-RPC line on stdin is POSTed upstream; replies go to stdout one per line.
"""
import json, os, sys, urllib.error, urllib.request

URL, TOKEN, SESSION = os.environ["UPSTREAM_URL"], os.environ.get("UPSTREAM_TOKEN"), None


def post(msg):
    global SESSION
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "User-Agent": "reseau-bridge/1"}
    if TOKEN:
        h["Authorization"] = "Bearer " + TOKEN
    if SESSION:
        h["Mcp-Session-Id"] = SESSION
    try:
        r = urllib.request.urlopen(urllib.request.Request(URL, json.dumps(msg).encode(), h), timeout=60)
    except urllib.error.HTTPError as e:
        r = e
    SESSION = r.headers.get("Mcp-Session-Id") or SESSION
    body = r.read().decode("utf-8", "replace").strip()
    if body.startswith("{") or body.startswith("["):
        out = json.loads(body)
        return out if isinstance(out, list) else [out]
    # SSE reply: forward every JSON-RPC message in data: lines
    out = [json.loads(l[5:]) for l in body.splitlines() if l.startswith("data:") and l[5:].strip().startswith("{")]
    if not out and "id" in msg:
        out = [{"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "upstream HTTP %s: %s" % (r.getcode(), body[:300])}}]
    return out


# ponytail: one request at a time, server->client requests mid-stream are dropped; fine for list/call tools
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    try:
        replies = post(msg)
    except Exception as e:
        replies = [{"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": "bridge: %r" % e}}] if "id" in msg else []
    for rep in replies:
        sys.stdout.write(json.dumps(rep) + "\n")
    sys.stdout.flush()
