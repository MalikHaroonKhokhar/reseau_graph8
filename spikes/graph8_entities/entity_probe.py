#!/usr/bin/env python3
"""HAR-107 probe: one sample record per Réseau business entity from Graph8's MCP server.

Usage: python3 entity_probe.py
Reads GRAPH8_API_KEY / GRAPH8_API_URL from the repo-root .env. Read-only, serial, stdlib only.
Writes shapes.json: keys and value TYPES only, never values, so it is safe to commit.
Exit 0 only when every entity returned a sample; an empty org is reported as EMPTY, not as success.
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "mcp_bridge"))
import mcp_probe as m  # reuse rpc/load_env (explicit User-Agent avoids Cloudflare 1010)

# entity -> [(tool, args, list key in result)]; see FINDINGS.md for why these tools.
ENTITIES = {
    "customer": [("g8_search_companies", {"limit": 1}, "companies")],
    "opportunity": [("g8_get_deals", {"limit": 1}, "deals")],
    "conversation": [("g8_list_inbox", {"page_size": 1}, "threads"),
                     ("g8_list_meetings", {"page_size": 1, "scope": "all", "timeframe": "all"}, "meetings")],
    "commitment": [("g8_get_tasks", {"limit": 1}, "tasks")],
}
PAUSE = 3  # seconds between calls; bursts get Cloudflare 429


def shape(o):
    """Replace every value with its type name; lists keep one element's shape."""
    if isinstance(o, dict):
        return {k: shape(v) for k, v in o.items()}
    if isinstance(o, list):
        return [shape(o[0])] if o else []
    return type(o).__name__


def call(url, key, rid, name, args):
    time.sleep(PAUSE)
    status, _, msg = m.rpc(url, key, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
    res = msg.get("result") if isinstance(msg, dict) else None
    if status != 200 or not res or res.get("isError"):
        raise RuntimeError("%s -> HTTP %s %s" % (name, status, json.dumps(msg)[:300]))
    return res.get("structuredContent") or json.loads("".join(c.get("text", "") for c in res["content"]))


def main():
    assert shape({"a": "x@y.z", "b": [{"c": 1}], "d": None}) == {"a": "str", "b": [{"c": "int"}], "d": "NoneType"}
    m.load_env(os.path.join(HERE, "..", "..", ".env"))
    m.load_env(os.path.join(HERE, "..", "mcp_bridge", ".env"))
    url, key = os.environ.get("GRAPH8_API_URL", "https://be.graph8.com/mcp/"), os.environ.get("GRAPH8_API_KEY")
    if not key:
        sys.exit("GRAPH8_API_KEY not set")

    m.rpc(url, key, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": m.PROTOCOL, "capabilities": {}, "clientInfo": {"name": "reseau-entity-probe", "version": "1"}}})
    m.notify(url, key, None, "notifications/initialized")
    call(url, key, 2, "g8_current_org", {})  # org-context gate (-32003 otherwise)

    shapes, ok, rid = {}, True, 10
    for entity, tools in ENTITIES.items():
        got = False
        for name, args, list_key in tools:
            rid += 1
            try:
                res = call(url, key, rid, name, args)["result"]
            except RuntimeError as e:
                print("%-12s %-22s ERROR %s" % (entity, name, e))
                continue
            rows = res.get(list_key) or []
            state = "SAMPLE" if rows else "EMPTY"
            print("%-12s %-22s %s (total=%s)" % (entity, name, state, res.get("total")))
            shapes[name] = shape(rows[0]) if rows else None
            got = got or bool(rows)
        ok = ok and got

    # Linkage check: how many commitments point at a Linear issue or GitHub PR/commit.
    rid += 1
    tasks = call(url, key, rid, "g8_get_tasks", {"limit": 100})["result"].get("tasks") or []
    linked = [t for t in tasks if any(h in (t.get("source_url") or "") for h in ("linear.app/", "github.com/"))]
    print("linkage      g8_get_tasks           %d of %d scanned tasks carry a Linear/GitHub source_url" % (len(linked), len(tasks)))

    json.dump(shapes, open(os.path.join(HERE, "shapes.json"), "w"), indent=2, sort_keys=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
