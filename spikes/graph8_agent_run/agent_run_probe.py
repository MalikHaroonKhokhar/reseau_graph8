#!/usr/bin/env python3
"""Define a Graph8 workflow that calls tools on two registered MCP servers, trigger it over REST, print its output.

Cases (default: green):
  red       try to run a workflow when nothing is defined -> fails, nothing to call
  green     register 2 MCP servers (CoinGecko legacy SSE + stdio bridge to Microsoft Learn), create a
            workflow with one MCP `tool` node per server, execute it, poll the execution, print node outputs
  agent     green + an `agent` node (throwaway voice agent) that summarises the MCP output.
            BILLABLE: ~12 credits per run (plan route: llm:g8_t1)
  selftest  offline check of the output validation
Cleanup touches only the ids this run created (names carry a per-run prefix), deletes each one directly and
re-reads it to prove it is gone; ids stay listed as leftovers until that proof arrives.
Reads GRAPH8_API_KEY (org-scoped key is enough) from .env here, in spikes/mcp_bridge, or at the repo root.
Writes agent_run_results.json (gitignored).
"""
import json, os, sys, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "mcp_bridge"))
from mcp_probe import load_env  # noqa: E402
from bridge_probe import BRIDGE, PREFIX, data, g8, redact  # noqa: E402

RUN = "%shar91-%s-" % (PREFIX, uuid.uuid4().hex[:8])  # unique per run, so concurrent probes never share names
SERVERS = {
    "cg": {"transport_type": "sse", "connection_url": "https://mcp.api.coingecko.com/sse"},
    "ms": {"transport_type": "stdio", "command": "python3", "args": ["-c", BRIDGE],
           "env_vars": {"UPSTREAM_URL": "https://learn.microsoft.com/api/mcp"}},
}
QUERY = "Azure Functions Python timer trigger"
CREATED = []  # (kind, id) in creation order; cleanup() deletes only these


def mcp_node(node_id, server, tool, mappings):
    # Arguments reach the MCP tool ONLY via input_mappings; any tool_config shape is silently dropped.
    return {"node_id": node_id, "name": node_id, "node_type": "tool",
            "config": {"tool": "mcp", "mcp_server_id": server, "mcp_tool_name": tool,
                       "input_mappings": [{"source_expression": s, "target_field": f} for f, s in mappings.items()]}}


def chain(nodes):
    """Wire nodes in order. The executor walks node.connections; the validator also demands mirrored edges with ids."""
    edges = []
    for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
        a["connections"] = [b["node_id"]]
        edges.append({"id": "e%d" % i, "source": a["node_id"], "target": b["node_id"], "edge_type": "default"})
    return {"start_node_id": nodes[0]["node_id"], "nodes": nodes, "edges": edges, "settings": {"stop_on_failure": False}}


def create(kind, path, body, key):
    status, text, _ = g8("POST", path, body)
    j = data(text)
    rid = (j.get(key) or (j.get("agent") or {}).get(key)) if status in (200, 201) else None
    if rid:
        CREATED.append((kind, rid))
    return rid


def content(results, node):
    out = results.get(node, {}).get("output") or {}
    try:
        return json.loads(out.get("content") or "null")
    except ValueError:
        return None


def outputs_ok(results, with_agent):
    """Every node completed without is_error, and BOTH servers returned real hits (an empty result is the silent
    failure mode of mis-mapped arguments, so status alone proves nothing)."""
    names = ["trigger_1", "ms_1", "cg_1"] + (["agent_1"] if with_agent else [])
    if any(results.get(n, {}).get("status") != "completed" or (results[n].get("output") or {}).get("is_error") for n in names):
        return False
    ms, cg = content(results, "ms_1"), content(results, "cg_1")
    ms_ok = isinstance(ms, dict) and any("timer trigger" in (r.get("title") or "").lower() for r in ms.get("results") or [])
    cg_ok = isinstance(cg, list) and any("price" in json.dumps(hit).lower() for hit in cg)
    agent_ok = not with_agent or "timer" in ((results["agent_1"].get("output") or {}).get("response") or "").lower()
    return ms_ok and cg_ok and agent_ok


def red():
    status, text, _ = g8("POST", "/api/v1/workflows/%s/execute" % uuid.uuid4(), {"input_data": {"q": QUERY}})
    return {"case": "red", "execute_status": status, "body": text[:400], "green": False}


def green(with_agent):
    out = {"case": "agent" if with_agent else "green", "run_prefix": RUN, "green": False}
    ids = {k: create("server", "/api/v1/voice/mcp-servers", dict(name=RUN + k, **v), "mcp_server_id") for k, v in SERVERS.items()}
    if not all(ids.values()):
        return dict(out, error="server registration failed")
    for k, u in ids.items():  # discovery; also warms cached_tools
        status, text, _ = g8("GET", "/api/v1/voice/mcp-servers/%s/tools" % u)
        out["tools_" + k] = [t["name"] for t in data(text).get("items", [])] if status == 200 else status
    nodes = [{"node_id": "trigger_1", "name": "trigger_1", "node_type": "trigger",
              "config": {"trigger_type": "tool_call", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}},
             mcp_node("ms_1", ids["ms"], "microsoft_docs_search", {"query": "${trigger.q}"}),
             mcp_node("cg_1", ids["cg"], "search_docs", {"query": "simple price", "language": "typescript"})]
    if with_agent:
        agent = create("agent", "/api/v1/voice/agents", {
            "entity_type": "agent", "agent_status": "inactive", "role": "Assistant", "use_company_knowledge": False, "identity": {},
            "persona": {"agent_name": RUN + "agent", "description": "HAR-91 probe", "persona": "You write terse summaries.",
                        "assertiveness_level": 0.5, "conciseness_level": 0.5, "formality_level": 0.5}}, "agent_id")
        if not agent:
            return dict(out, error="voice agent create failed")
        # `instructions` is sent literally (no ${} resolution); upstream data must arrive as the user turn via target "message"
        nodes.append({"node_id": "agent_1", "name": "agent_1", "node_type": "agent", "config": {
            "agent_id": agent, "instructions": "The user message is search results. Reply with only the title of the first result.",
            "input_mappings": [{"source_expression": "${ms_1.content}", "target_field": "message"}]}})
    cfg = chain(nodes)
    _, text, _ = g8("POST", "/api/v1/workflows/validate", {"config": cfg})
    out["validate"] = data(text)
    action = create("workflow", "/api/v1/workflows", {"name": RUN + "wf", "description": "HAR-91 probe", "config": cfg}, "action_id")
    if not action:
        return dict(out, error="workflow create failed")
    status, text, _ = g8("POST", "/api/v1/workflows/%s/execute" % action, {"input_data": {"q": QUERY}})
    execution = data(text).get("execution_id")
    out["execute"] = {"status": status, "body": data(text)}
    t0, ex = time.time(), {}
    while execution and time.time() - t0 < 300:
        ex = data(g8("GET", "/api/v1/workflows/executions/" + execution)[1])
        if ex.get("status") not in ("pending", "running"):
            break
    out["execution"] = {k: ex.get(k) for k in ("status", "trigger_type", "triggered_by", "duration_ms", "error_message")}
    out["node_results"] = (ex.get("output_data") or {}).get("node_results", {})
    out["green"] = ex.get("status") == "completed" and outputs_ok(out["node_results"], with_agent)
    return out


def server_listed(sid):
    """True/False from a complete server listing; None if the listing is unreadable or partial (can't prove absence)."""
    status, text, _ = g8("GET", "/api/v1/workflows/mcp-servers")
    j = data(text)
    servers = j.get("servers")
    if status != 200 or servers is None or len(servers) != j.get("total"):
        return None
    return any(s.get("mcp_server_id") == sid for s in servers)


GONE = {  # kind -> (delete path, proof that the id no longer exists)
    "workflow": ("/api/v1/workflows/%s", lambda i: g8("GET", "/api/v1/workflows/" + i)[0] == 404),
    "agent": ("/api/v1/voice/agents/%s", lambda i: g8("GET", "/api/v1/voice/agents/" + i)[0] == 404),
    "server": ("/api/v1/voice/mcp-servers/%s", lambda i: server_listed(i) is False),  # no GET-by-id route (405)
}


def cleanup():
    """Delete this run's ids, workflows first (they reference servers and agents). Returns the ids not proven gone."""
    order = {"workflow": 0, "agent": 1, "server": 2}
    left = []
    for kind, rid in sorted(CREATED, key=lambda c: order[c[0]]):
        path, gone = GONE[kind]
        g8("DELETE", path % rid)
        if not gone(rid):
            left.append((kind, rid))
    return left


def selftest():
    def res(ms, cg, is_error=False):
        node = lambda c: {"status": "completed", "output": {"content": json.dumps(c), "is_error": is_error}}
        return {"trigger_1": {"status": "completed", "output": {}}, "ms_1": node(ms), "cg_1": node(cg)}
    ms = {"results": [{"title": "Timer trigger for Azure Functions"}]}
    cg = [{"method": "client.simple.price.get", "summary": "Coin Price by IDs"}]
    assert outputs_ok(res(ms, cg), False)
    assert not outputs_ok(res(ms, []), False), "empty CoinGecko result must fail"
    assert not outputs_ok(res({"results": []}, cg), False), "empty Microsoft result must fail"
    assert not outputs_ok(res(ms, cg, is_error=True), False)
    with_agent = dict(res(ms, cg), agent_1={"status": "completed", "output": {"response": "Timer trigger for Azure Functions"}})
    assert outputs_ok(with_agent, True) and not outputs_ok(res(ms, cg), True)
    print("selftest ok")
    return 0


def main():
    case = (sys.argv[1:] or ["green"])[0]
    if case == "selftest":
        return selftest()
    load_env(os.path.join(HERE, ".env"))
    load_env()
    load_env(os.path.join(HERE, "..", "..", ".env"))
    result, left = {"case": case}, list(CREATED)
    try:
        result = red() if case == "red" else green(case == "agent")
    finally:
        left = cleanup()
        json.dump({"result": result, "created": CREATED, "leftovers": left},
                  open(os.path.join(HERE, "agent_run_results.json"), "w"), indent=2)
    print("\n== output")
    for node, r in sorted(result.get("node_results", {}).items()):
        print("  %-9s %-9s %s" % (node, r.get("status"), redact(json.dumps(r.get("output") or r.get("error")))[:300]))
    print("== created %d, leftovers %s" % (len(CREATED), left or "none"))
    print("== %s: %s   cleanup verified: %s" % (case, "GREEN" if result.get("green") else "RED", not left))
    return 0 if result.get("green") and not left else 1


if __name__ == "__main__":
    sys.exit(main())
