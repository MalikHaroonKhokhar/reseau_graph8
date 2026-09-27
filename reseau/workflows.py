"""Graph8 workflows over the gateway: Start My Day (HAR-102). check() is the citation verifier every workflow's
output goes through (the daily report, HAR-103, and Ask Réseau, HAR-104, reuse it).

Mechanism (HAR-91, spikes/graph8_agent_run/FINDINGS.md): a workflow whose MCP `tool` node calls
get_my_day_context on the registered gateway, then an `agent` node writes the briefing. The tool output reaches
the agent as its user message through input_mappings (instructions are never interpolated, and only a bare
${node.field} resolves). The trigger executes the workflow over REST, polls the execution, and returns the
briefing only once check() has verified it against the tool output from that same run.

    python -m reseau.workflows setup           # create the voice agent and the workflow over "reseau-gateway"
    python -m reseau.workflows start-my-day    # run it (RESEAU_START_MY_DAY = the workflow's action_id)

Both need GRAPH8_API_KEY. The gateway must be registered and reachable (register_graph8 --keep, README).
Each run is billable: the agent node costs ~12 credits.
"""
import json
import os
import re
import sys
import time
from functools import partial

from mcp.shared.exceptions import MCPError

from reseau import evidence

WORKFLOW_ENV = "RESEAU_START_MY_DAY"
NAME = "reseau-start-my-day"
SECTIONS = {"focus": "Focus today", "needs_attention": "Needs attention", "yesterday": "Yesterday"}
NOTHING = "Nothing to report."
SENTENCE_BREAK = re.compile(r"[.!?]\s+(?=[A-Z0-9])")
TIMEOUT = 300  # seconds an execution may run; HAR-91's runs ended in under 30 s

INSTRUCTIONS = """You write a developer's Start My Day briefing. The user message is one JSON document: the output \
of Réseau's get_my_day_context tool. It is your only source of facts. Never add facts, guesses, advice, estimates \
or anything else it does not state.

Reply with only this JSON object, no code fences and no other text:
{"focus": [S, ...], "needs_attention": [S, ...], "yesterday": [S, ...]}
Each S is {"text": "<exactly one sentence>", "activity_ids": ["<activity_id>", ...]}.

- focus: from the JSON's "focus" array only. One sentence per issue: its title and priority, and the open pull \
requests it waits on (blocking_prs) or the issues blocking it (blocked_by), when there are any.
- needs_attention: from "needs_attention" only. One sentence per pull request: its title and how many unresolved \
review threads it has.
- yesterday: from "yesterday" only. One sentence with commit_count and the repositories in repos.
- activity_ids lists the activity_id of every item the sentence mentions, copied character for character from \
that same section of the JSON. The yesterday sentence cites every id in yesterday.activity_ids. Never cite an id \
from another section, and never write an id that is not in the JSON.
- A section whose part of the JSON is empty (an empty array, or commit_count 0) is exactly \
[{"text": "Nothing to report.", "activity_ids": []}].
- Plain sentences: no greetings, sign-offs, headings, markdown, or activity_ids inside text."""

# The agent node runs a voice agent; its persona leaks into replies, so this one is neutral (HAR-91 addendum).
AGENT = {"entity_type": "agent", "agent_status": "inactive", "role": "Assistant", "use_company_knowledge": False,
         "identity": {}, "persona": {
             "agent_name": NAME, "description": "Réseau Start My Day briefings (HAR-102)",
             "persona": "You turn tool output into terse, factual JSON. No greetings, no sign-offs.",
             "assertiveness_level": 0.5, "conciseness_level": 0.9, "formality_level": 0.5}}


class WorkflowError(Exception):
    def __init__(self, message, problems=()):
        super().__init__(message)
        self.problems = list(problems)


# ---- the citation verifier ----

def is_activity_id(value):
    try:
        evidence.parse(value)
        return True
    except MCPError:
        return False


def activity_ids(value):
    """Every activity_id anywhere in a tool's JSON output: activity_id, activity_ids, blocked_by, via..."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return {i for v in value for i in activity_ids(v)}
    return {value} if is_activity_id(value) else set()


def check(briefing, sources):
    """The problems with a briefing; none means verified.

    briefing = {section: [{"text": one sentence, "activity_ids": [...]}]}; sources = {section: the tool output
    that section may cite}. Every sentence cites at least one activity_id, and only ones found in its section's
    source. The one uncited sentence is NOTHING, and only for a section whose source holds no activity_id; a
    section with activity can't be empty or NOTHING."""
    if not isinstance(briefing, dict):
        return ["not a JSON object of sections"]
    problems = ["%s: not a known section" % s for s in briefing if s not in sources]
    for section, source in sources.items():
        allowed, sentences = activity_ids(source), briefing.get(section)
        if not isinstance(sentences, list) or not sentences:
            problems.append("%s: no sentences" % section)
            continue
        for k, s in enumerate(sentences):
            where = "%s[%d]" % (section, k)
            text, cited = (s.get("text"), s.get("activity_ids")) if isinstance(s, dict) else (None, None)
            if not (isinstance(text, str) and text.strip() and isinstance(cited, list)
                    and all(isinstance(i, str) for i in cited)):
                problems.append("%s: not a {text, activity_ids} sentence" % where)
            elif text == NOTHING and not cited:
                if allowed:
                    problems.append("%s: says %r, but the tool returned %d activity_id(s)" % (where, NOTHING, len(allowed)))
            elif not cited:
                problems.append("%s: no citation: %r" % (where, text))
            else:
                problems += ["%s: cites %r, which the tool did not return for %s" % (where, i, section)
                             for i in cited if i not in allowed]
                if SENTENCE_BREAK.search(text.strip()):
                    problems.append("%s: more than one sentence: %r" % (where, text))
    return problems


def parse_briefing(reply):
    """The agent's reply -> {section: sentences}, or None if it holds no JSON object. Tolerates text around the
    object (a persona greeting, code fences). An empty section, or "nothing to report" in any casing, becomes
    [NOTHING], which check() then holds to the tool output."""
    text = reply if isinstance(reply, str) else ""
    try:
        briefing = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None
    if not isinstance(briefing, dict):
        return None
    for section, sentences in briefing.items():
        if sentences == [] or (isinstance(sentences, list) and len(sentences) == 1 and isinstance(sentences[0], dict)
                               and str(sentences[0].get("text")).strip().rstrip(".").casefold() == "nothing to report"
                               and not sentences[0].get("activity_ids")):
            briefing[section] = [{"text": NOTHING, "activity_ids": []}]
    return briefing


# ---- the workflow ----

def chain(nodes):
    """Wire nodes in order. The executor walks node.connections; the validator also wants mirrored edges with ids."""
    edges = []
    for k, (a, b) in enumerate(zip(nodes, nodes[1:])):
        a["connections"] = [b["node_id"]]
        edges.append({"id": "e%d" % k, "source": a["node_id"], "target": b["node_id"], "edge_type": "default"})
    return {"start_node_id": nodes[0]["node_id"], "nodes": nodes, "edges": edges}


def start_my_day_config(server_id, agent_id):
    """trigger -> get_my_day_context on the gateway -> agent. The tool takes no arguments, so no input_mappings."""
    return chain([
        {"node_id": "trigger_1", "name": "trigger_1", "node_type": "trigger",
         "config": {"trigger_type": "tool_call", "input_schema": {"type": "object", "properties": {}}}},
        {"node_id": "day_1", "name": "day_1", "node_type": "tool",
         "config": {"tool": "mcp", "mcp_server_id": server_id, "mcp_tool_name": "get_my_day_context"}},
        {"node_id": "agent_1", "name": "agent_1", "node_type": "agent",
         "config": {"agent_id": agent_id, "instructions": INSTRUCTIONS,
                    "input_mappings": [{"source_expression": "${day_1.content}", "target_field": "message"}]}}])


def setup(g8, server_id):
    """Create the voice agent and the Start My Day workflow over a registered gateway -> (agent_id, action_id).
    g8(method, path, body=None) -> (status, data): register_graph8.g8 bound to a client and key."""
    _, agent = g8("POST", "/api/v1/voice/agents", AGENT)
    agent_id = isinstance(agent, dict) and (agent.get("agent_id") or (agent.get("agent") or {}).get("agent_id"))
    if not agent_id:
        raise WorkflowError("voice agent create failed: %s" % agent)
    _, wf = g8("POST", "/api/v1/workflows", {"name": NAME, "description": "Réseau Start My Day (HAR-102)",
                                              "config": start_my_day_config(server_id, agent_id)})
    action_id = isinstance(wf, dict) and wf.get("action_id")
    if not action_id:
        raise WorkflowError("workflow create failed: %s (voice agent %s was created)" % (wf, agent_id))
    return agent_id, action_id


def execute(g8, action_id, timeout=TIMEOUT, clock=time.monotonic):
    """Run a workflow and poll it to the end -> (execution_id, node_results). Raises WorkflowError unless the
    run and every node completed without an MCP error: a failed tool call must never reach the reader as a
    briefing."""
    status, started = g8("POST", "/api/v1/workflows/%s/execute" % action_id, {"input_data": {}})
    execution = isinstance(started, dict) and started.get("execution_id")
    if not execution:
        raise WorkflowError("execute returned HTTP %s: %s" % (status, started))
    deadline = clock() + timeout
    while True:  # g8 spaces calls 3 s apart, which paces the polling
        _, run = g8("GET", "/api/v1/workflows/executions/" + execution)
        run = run if isinstance(run, dict) else {}
        if run.get("status") not in ("pending", "running"):
            break
        if clock() > deadline:
            raise WorkflowError("execution %s still %s after %d s" % (execution, run["status"], timeout))
    nodes = (run.get("output_data") or {}).get("node_results") or {}
    failed = [n for n, r in nodes.items() if r.get("status") != "completed" or (r.get("output") or {}).get("is_error")]
    if run.get("status") != "completed" or failed:
        detail = "; ".join("%s: %s" % (n, str(nodes[n].get("error") or (nodes[n].get("output") or {}).get("content"))[:300])
                           for n in failed)
        raise WorkflowError("execution %s %s: %s" % (execution, run.get("status"), detail or run.get("error_message") or run))
    return execution, nodes


def run_once(g8, action_id):
    """One Start My Day execution -> (execution_id, tool output, agent reply, briefing, check() problems)."""
    execution, nodes = execute(g8, action_id)
    try:
        context = json.loads(nodes["day_1"]["output"]["content"])
    except (KeyError, TypeError, ValueError):
        raise WorkflowError("execution %s: get_my_day_context returned no JSON" % execution) from None
    reply = (nodes.get("agent_1", {}).get("output") or {}).get("response")
    briefing = parse_briefing(reply)
    return execution, context, reply, briefing, check(briefing, {s: context.get(s) for s in SECTIONS})


def start_my_day(g8, action_id, attempts=2):
    """The dashboard's trigger: run Start My Day and return its verified briefing,
    {"execution_id", "date", "sections": {section: [{"text", "activity_ids"}]}, "incomplete"}. incomplete is the
    tool's own list of what may be missing, passed through untouched. A reply that fails check() is retried
    (each attempt is billable); if every attempt fails, WorkflowError carries the last one's problems."""
    for _ in range(attempts):
        execution, context, reply, briefing, problems = run_once(g8, action_id)
        if not problems:
            return {"execution_id": execution, "date": context.get("date"), "sections": briefing,
                    "incomplete": context.get("incomplete") or []}
    raise WorkflowError("execution %s: the briefing failed verification: %r" % (execution, str(reply)[:500]), problems)


def gateway_server_id(g8, name):
    """The registered gateway's mcp_server_id, looked up by its registration name."""
    _, listing = g8("GET", "/api/v1/workflows/mcp-servers")
    found = [s["mcp_server_id"] for s in (listing or {}).get("servers") or [] if s.get("name") == name]
    if len(found) != 1:
        raise WorkflowError("expected one MCP server named %r, found %d (register_graph8 --keep)" % (name, len(found)))
    return found[0]


def main(argv=sys.argv[1:]):
    from reseau import gateway, outbound, register_graph8

    if argv not in (["setup"], ["start-my-day"]):
        raise SystemExit(__doc__)
    key = gateway.resolve_credential(gateway.Upstream("graph8", register_graph8.BASE, "GRAPH8_API_KEY"))
    gateway.SECRETS.add(key)
    g8 = partial(register_graph8.g8, outbound.Client(), key)
    if argv == ["setup"]:
        agent_id, action_id = setup(g8, gateway_server_id(g8, register_graph8.NAME))
        print("voice agent %s\n%s=%s" % (agent_id, WORKFLOW_ENV, action_id))
        return 0
    action_id = os.environ.get(WORKFLOW_ENV) or sys.exit("%s is not set: run `setup` first" % WORKFLOW_ENV)
    print(json.dumps(start_my_day(g8, action_id), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
