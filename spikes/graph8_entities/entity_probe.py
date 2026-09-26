#!/usr/bin/env python3
"""HAR-107 probe: one sample record per Réseau business entity from Graph8's MCP server.

Usage: python3 entity_probe.py              # sample each entity, write shapes.json
       python3 entity_probe.py --discover   # also write discovery.json (catalog, schemas, key scopes)
       python3 entity_probe.py --selftest   # offline checks only, no network
Reads GRAPH8_API_KEY / GRAPH8_API_URL from the repo-root .env. Read-only, serial, stdlib only.
Output files and stdout carry keys, types, tool names and error codes only, never record values.
Exit 0 only when every entity has a qualifying sample; an empty org is reported as EMPTY, not success.
"""
import json, os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "mcp_bridge"))
import mcp_probe as m  # reuse rpc/load_env (explicit User-Agent avoids Cloudflare 1010)

BIZ = {"deal", "company"}


def is_commitment(task):
    """A task counts as a commitment only when it is tied to a deal or company (FINDINGS.md)."""
    if task.get("entity_type") in BIZ and task.get("entity_id"):
        return True
    if task.get("company_id") is not None:
        return True
    # ponytail: link dict shape is unverified (no linked task seen yet); adjust once one is.
    return any((l.get("entity_type") or l.get("type")) in BIZ for l in task.get("links") or [])


# entity -> [(tool, args, list key in result, qualifies(row))]; see FINDINGS.md for why these tools.
ANY = lambda row: True
ENTITIES = {
    "customer": [("g8_search_companies", {"limit": 25}, "companies", ANY)],
    "opportunity": [("g8_get_deals", {"limit": 25}, "deals", ANY)],
    "conversation": [("g8_list_inbox", {"page_size": 25}, "threads", ANY),
                     ("g8_list_meetings", {"page_size": 25, "scope": "all", "timeframe": "all"}, "meetings", ANY)],
    "commitment": [("g8_get_tasks", {"limit": 100}, "tasks", is_commitment)],
}
PAUSE = 3  # seconds between calls; bursts get Cloudflare 429
# Terms whose absence FINDINGS.md reports; searched over names + descriptions of the full catalog.
TERMS = ["commit", "promis", "obligation", "action.item", "linear", "github", "jira", "issue.track",
         "pull.request", "source_url", "external_id"]


class ProbeError(Exception):
    pass


def shape(o):
    """Replace every value with its type name; lists keep one element's shape."""
    if isinstance(o, dict):
        return {k: shape(v) for k, v in o.items()}
    if isinstance(o, list):
        return [shape(o[0])] if o else []
    return type(o).__name__


def safe_error(name, status, msg):
    """Tool name, HTTP status and JSON-RPC code only; server text can echo record values."""
    code = None
    if isinstance(msg, dict):
        err = msg.get("error")
        code = err.get("code") if isinstance(err, dict) else ("isError" if (msg.get("result") or {}).get("isError") else None)
    return "%s -> HTTP %s code=%s" % (name, status, code)


def call(url, key, rid, name, args):
    time.sleep(PAUSE)
    status, _, msg = m.rpc(url, key, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
    res = msg.get("result") if isinstance(msg, dict) else None
    if status != 200 or not res or res.get("isError"):
        raise ProbeError(safe_error(name, status, msg))
    try:
        return res.get("structuredContent") or json.loads("".join(c.get("text", "") for c in res["content"]))
    except ValueError:
        raise ProbeError("%s -> unparseable result" % name)


def discover(url, key, rid):
    """Evidence behind FINDINGS.md: tool catalog, entity output fields, term hits, key scopes. No record values."""
    _, _, msg = m.rpc(url, key, {"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {}})
    visible = msg["result"]["tools"]
    catalog = call(url, key, rid + 1, "g8_tool_search", {"query": "g8", "limit": 1000, "activate": False})["result"]["matches"]
    entity_tools = {t for tools in ENTITIES.values() for t, *_ in tools} | {
        "g8_get_deal", "g8_get_task", "g8_get_reply", "g8_get_meeting", "g8_list_notes", "g8_get_company_deals"}
    fields = {t["name"]: {d: sorted(s.get("properties", {})) for d, s in (t.get("outputSchema") or {}).get("$defs", {}).items()}
              for t in visible if t["name"] in entity_tools}
    hits = {term: sorted(c["name"] for c in catalog if re.search(term, c["name"] + " " + (c.get("description") or ""), re.I))
            for term in TERMS}
    k = call(url, key, rid + 2, "g8_execute", {"tool_name": "g8_connection_describe_current_key", "arguments": {}})
    k = json.loads(k["result"])["data"] if isinstance(k.get("result"), str) else k["result"]["data"]
    return {
        "visible_tools": sorted([t["name"], (t.get("annotations") or {}).get("readOnlyHint")] for t in visible),
        "catalog": sorted([c["name"], c.get("family")] for c in catalog),
        "catalog_total": len(catalog),
        "entity_output_fields": fields,
        "term_hits_in_catalog_names_and_descriptions": hits,
        "key": {"key_mode": k.get("key_mode"), "unrestricted": k.get("unrestricted"), "scopes": sorted(k.get("scopes") or []),
                "reachable_operations": k["capabilities"]["reachable_operations"],
                "total_operations": k["capabilities"]["total_operations"],
                "scope_vocabulary": sorted(k["capabilities"]["by_scope"])},
    }


def selftest():
    assert shape({"a": "x@y.z", "b": [{"c": 1}], "d": None}) == {"a": "str", "b": [{"c": "int"}], "d": "NoneType"}
    assert not is_commitment({"entity_type": "team_member", "entity_id": "u1", "company_id": None, "links": []})
    assert not is_commitment({"entity_type": "contact", "entity_id": "7", "links": [{"entity_type": "contact"}]})
    assert is_commitment({"entity_type": "deal", "entity_id": "d1"})
    assert is_commitment({"company_id": 42})
    assert is_commitment({"links": [{"entity_type": "company", "entity_id": "42"}]})
    leak = {"error": {"code": -32000, "message": "Acme Corp jane@acme.com"}}
    assert safe_error("t", 500, leak) == "t -> HTTP 500 code=-32000"
    assert "acme" not in safe_error("t", 200, {"result": {"isError": True, "content": [{"text": "jane@acme.com"}]}}).lower()
    assert "acme" not in safe_error("t", 403, "<html>Acme</html>").lower()
    print("selftest ok")


def main():
    selftest()
    if "--selftest" in sys.argv:
        return 0
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
        for name, args, list_key, qualifies in tools:
            rid += 1
            try:
                res = call(url, key, rid, name, args)["result"]
            except ProbeError as e:
                print("%-12s %-22s ERROR %s" % (entity, name, e))
                continue
            rows = res.get(list_key) or []
            good = [r for r in rows if qualifies(r)]
            state = "SAMPLE" if good else ("UNQUALIFIED" if rows else "EMPTY")
            print("%-12s %-22s %s (total=%s, scanned=%d, qualifying=%d)" % (entity, name, state, res.get("total"), len(rows), len(good)))
            shapes[name] = shape(good[0]) if good else None
            got = got or bool(good)
        ok = ok and got

    # Linkage check: commitments whose source_url points at a Linear issue or GitHub PR/commit.
    rid += 1
    tasks = call(url, key, rid, "g8_get_tasks", {"limit": 100})["result"].get("tasks") or []
    linked = [t for t in tasks if is_commitment(t) and re.search(r"linear\.app/|github\.com/", t.get("source_url") or "")]
    print("linkage      g8_get_tasks           %d of %d scanned tasks are commitments with a Linear/GitHub source_url" % (len(linked), len(tasks)))

    json.dump(shapes, open(os.path.join(HERE, "shapes.json"), "w"), indent=2, sort_keys=True)
    if "--discover" in sys.argv:
        json.dump(discover(url, key, rid + 1), open(os.path.join(HERE, "discovery.json"), "w"), indent=1, sort_keys=True)
        print("wrote discovery.json")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
