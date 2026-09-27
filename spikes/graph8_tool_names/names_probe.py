#!/usr/bin/env python3
"""HAR-94: does Graph8 accept the gateway's provider-prefixed tool names, and where is its name limit?

Registers one throwaway stdio MCP server (stdlib, inline) whose tools carry the gateway's real exposed names
plus length/charset probes, reads back what Graph8 lists, then runs one workflow with an MCP `tool` node per
name (tool nodes only: free, no external effect) and checks each node reached the tool with that exact name.
Cleanup reuses agent_run_probe's: deletes only this run's ids and proves them gone.

Usage: python3 names_probe.py [selftest]      Reads GRAPH8_API_KEY from .env. Writes names_results.json (gitignored).
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "graph8_agent_run"))
sys.path.insert(0, os.path.join(HERE, "..", "mcp_bridge"))
import agent_run_probe as ar  # noqa: E402
from agent_run_probe import CREATED, chain, cleanup, create, data, g8, load_env, mcp_node  # noqa: E402

NAMES = [
    "github_list_issues", "linear_list_issues", "github_list_releases", "linear_list_releases",  # the HAR-94 collisions
    "github_add_reply_to_pull_request_comment",  # longest real exposed name today (40)
    "n64_" + "a" * 60, "n65_" + "a" * 61, "n128_" + "a" * 123, "n129_" + "a" * 124,
    "dot.name", "dash-name",
]

# Minimal stdio MCP server: one JSON-RPC message per line. Every tool replies "called:<its name>".
SERVER = r"""import json, sys
NAMES = %s
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    meth, r = m.get("method"), None
    if meth == "initialize":
        r = {"protocolVersion": m["params"].get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
             "serverInfo": {"name": "reseau-names-probe", "version": "1"}}
    elif meth == "tools/list":
        r = {"tools": [{"name": n, "description": "probe " + n, "inputSchema": {"type": "object"}} for n in NAMES]}
    elif meth == "tools/call":
        r = {"content": [{"type": "text", "text": "called:" + m["params"]["name"]}], "isError": False}
    else:
        r = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}) + "\n")
    sys.stdout.flush()
""" % json.dumps(NAMES)


def node_ids():
    return {n: "t%d" % i for i, n in enumerate(NAMES)}


def verdicts(results, listed):
    """Per name: listed by Graph8 as-is, and called by that exact name from a workflow node."""
    out = {}
    for n, nid in node_ids().items():
        r = results.get(nid, {})
        o = r.get("output") or {}
        out[n] = {"len": len(n), "listed": n in listed, "status": r.get("status"),
                  "called": r.get("status") == "completed" and not o.get("is_error") and o.get("content") == "called:" + n,
                  "error": r.get("error") or (o.get("content") if o.get("is_error") else None)}
    return out


def probe():
    out = {"run_prefix": ar.RUN}
    sid = create("server", "/api/v1/voice/mcp-servers", {"name": ar.RUN + "names", "transport_type": "stdio",
                                                          "command": "python3", "args": ["-c", SERVER]}, "mcp_server_id")
    if not sid:
        return dict(out, error="server registration failed")
    status, text, _ = g8("POST", "/api/v1/voice/mcp-servers/%s/test" % sid)
    out["test"] = data(text)
    status, text, _ = g8("GET", "/api/v1/voice/mcp-servers/%s/tools" % sid)
    listed = [t.get("name") for t in data(text).get("items", [])] if status == 200 else []
    out["listed"] = listed if status == 200 else status
    nodes = [{"node_id": "trigger_1", "name": "trigger_1", "node_type": "trigger",
              "config": {"trigger_type": "tool_call", "input_schema": {"type": "object", "properties": {}}}}]
    nodes += [mcp_node(nid, sid, n, {}) for n, nid in node_ids().items()]
    cfg = chain(nodes)
    _, text, _ = g8("POST", "/api/v1/workflows/validate", {"config": cfg})
    out["validate"] = data(text)
    action = create("workflow", "/api/v1/workflows", {"name": ar.RUN + "wf", "description": "HAR-94 names probe",
                                                      "config": cfg}, "action_id")
    if not action:
        return dict(out, error="workflow create failed")
    _, text, _ = g8("POST", "/api/v1/workflows/%s/execute" % action, {"input_data": {}})
    execution = data(text).get("execution_id")
    t0, ex = time.time(), {}
    while execution and time.time() - t0 < 300:
        ex = data(g8("GET", "/api/v1/workflows/executions/" + execution)[1])
        if ex.get("status") not in ("pending", "running"):
            break
    out["execution"] = {k: ex.get(k) for k in ("status", "duration_ms", "error_message")}
    out["names"] = verdicts((ex.get("output_data") or {}).get("node_results", {}), listed)
    return out


def selftest():
    ok = {"t0": {"status": "completed", "output": {"content": "called:" + NAMES[0], "is_error": False}},
          "t1": {"status": "completed", "output": {"content": "called:wrong", "is_error": False}},
          "t2": {"status": "failed", "error": "tool not found"}}
    v = verdicts(ok, [NAMES[0]])
    assert v[NAMES[0]]["called"] and v[NAMES[0]]["listed"]
    assert not v[NAMES[1]]["called"] and not v[NAMES[1]]["listed"]
    assert not v[NAMES[2]]["called"] and v[NAMES[2]]["error"] == "tool not found"
    ns = {}
    exec(SERVER.replace("for line in sys.stdin:", "for line in LINES:"), dict(ns, LINES=[]))  # compiles
    print("selftest ok")
    return 0


def main():
    if sys.argv[1:] == ["selftest"]:
        return selftest()
    for p in (os.path.join(HERE, ".env"), os.path.join(HERE, "..", "mcp_bridge", ".env"), os.path.join(HERE, "..", "..", ".env")):
        load_env(p)
    ar.RUN = ar.RUN.replace("har91", "har94")
    out, left = {}, None
    try:
        out = probe()
    finally:
        left = cleanup()
        out["leftovers"] = left
        json.dump(out, open(os.path.join(HERE, "names_results.json"), "w"), indent=2)
    print(json.dumps({k: v for k, v in out.items() if k != "listed"}, indent=2))
    return 0 if out.get("names") and not left else 1


if __name__ == "__main__":
    sys.exit(main())
