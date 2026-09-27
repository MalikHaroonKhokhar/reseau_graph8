"""Graph8 workflows over the gateway: Start My Day (HAR-102) and the team daily report (HAR-103). Neither returns
a sentence that reseau/verify.py hasn't checked against the tool output of the same run.

Mechanism (HAR-91, spikes/graph8_agent_run/FINDINGS.md): a workflow whose MCP `tool` node calls a gateway tool
(get_my_day_context; get_team_summary with the trigger's date), then an `agent` node writes sections of
sentences, each with the activity_ids it cites. The tool output reaches the agent as its user message through
input_mappings (instructions are never interpolated, and only a bare ${node.field} resolves). A trigger executes
the workflow over REST, polls the execution, and returns the sections only once they pass verification.

    python -m reseau.workflows setup                     # the voice agent and both workflows over "reseau-gateway"
    python -m reseau.workflows update                    # push this code's prompts onto the existing ones
    python -m reseau.workflows start-my-day              # RESEAU_START_MY_DAY = its action_id
    python -m reseau.workflows daily-report YYYY-MM-DD   # RESEAU_DAILY_REPORT = its action_id
    python -m reseau.workflows verify EXECUTION_ID       # check any run, e.g. one started from Graph8's dashboard

All need GRAPH8_API_KEY. The gateway must be registered and reachable (python -m reseau.tunnel, README).
Each run is billable: the agent node costs ~12 credits.
"""
import json
import os
import sys
import time
from datetime import date as Date
from functools import partial

from reseau import verify

START_MY_DAY_ENV = "RESEAU_START_MY_DAY"
REPORT_ENV = "RESEAU_DAILY_REPORT"
SECTIONS = {"summary": "Summary", "focus": "Focus today", "needs_attention": "Needs attention",
            "yesterday": "Yesterday"}
REPORT_SECTIONS = {"summary": "Summary", "completed": "Completed", "merged": "Merged", "commits": "Commits",
                   "blocked": "Blocked"}
COUNTED = ("completed", "merged", "commits")  # each is one sentence stating get_team_summary's total
TIMEOUT = 300  # seconds an execution may run; HAR-91's runs ended in under 30 s

INSTRUCTIONS = """You write a developer's Start My Day briefing: short, clear and readable, like a helpful \
teammate. The user message is one JSON document: the output of Réseau's get_my_day_context tool. It is your only \
source of facts. Never add facts, guesses, advice, estimates or anything else it does not state.

Reply with only this JSON object, no code fences and no other text:
{"summary": [S, S], "focus": [S, ...], "needs_attention": [S, ...], "yesterday": [S, ...]}
Each S is {"text": "<exactly one sentence>", "activity_ids": ["<activity_id>", ...]}.

- summary: one or two sentences that give the shape of the day, built only from what the JSON states: the top \
focus issue and what blocks it, how many pull requests have unresolved threads, and yesterday's commit count. \
Any number is a count from the JSON, in digits. No judgments (busy, strong, good), no causes, no advice, and \
nothing about what did not happen. activity_ids: the ids of the items it names, or at least the one it leads with.
- focus: from the JSON's "focus" array only. One sentence per issue: its title and priority, the identifier of \
every issue in its blocked_by (HAR-6 for linear:issue:HAR-6), and the open pull requests it waits on \
(blocking_prs) by number. activity_ids: the issue's activity_id, every id in its blocked_by, and the activity_id \
of every pull request it mentions.
- needs_attention: from "needs_attention" only. One sentence per pull request: its title and how many unresolved \
review threads it has.
- yesterday: from "yesterday" only. One sentence with commit_count and the repositories in repos. Its \
activity_ids is [] (Réseau attaches the commits itself).
- activity_ids lists the activity_id of every item the sentence mentions, copied character for character from \
that same section of the JSON. Never cite an id from another section, and never write an id that is not in the \
JSON.
- A section whose part of the JSON is empty (an empty array, or commit_count 0) is exactly \
[{"text": "Nothing to report.", "activity_ids": []}]. The summary is that too when the whole JSON is empty.
- Plain sentences: no greetings, sign-offs, headings, markdown, or activity_ids inside text."""

REPORT_INSTRUCTIONS = """You write a team's daily report: short, clear and readable, like a good standup note. The \
user message is one JSON document: the output of Réseau's get_team_summary tool. It is your only source of facts. \
Never add facts, guesses, advice, estimates or anything else it does not state.

Reply with only this JSON object, no code fences and no other text:
{"summary": [S, S], "completed": [S], "merged": [S], "commits": [S], "blocked": [S, ...]}
Each S is {"text": "<exactly one sentence>", "activity_ids": ["<activity_id>", ...]}.

- summary: one or two sentences that give the shape of the day for the team, built only from what the JSON \
states: the counts of issues completed, PRs merged and commits, and how many issues are blocked and by which \
issues. Any number is a count from the JSON, in digits. No judgments (busy, strong, good), no causes, no advice, \
and nothing about what did not happen. activity_ids: the ids of the items it names, or at least one behind each \
thing it mentions.
- completed, merged, commits: from total.completed, total.merged and total.commits only. Exactly one natural \
sentence each that states the count in digits and no other number, title or name, e.g. "The team completed 8 \
Linear issues.", "8 GitHub pull requests were merged.", "The team pushed 34 commits." (singular for 1). Its \
activity_ids is [] (Réseau attaches the count's activity_ids itself).
- blocked: from "blocked" only. One sentence per issue: its title, the identifier of every issue in its \
blocked_by (HAR-6 for linear:issue:HAR-6), and the pull requests it waits on (blocking_prs) by number, e.g. \
"UI Critic Phase 3 is blocked by HAR-6 until PR #9 lands." Its activity_ids are the issue's activity_id, every \
id in its blocked_by, and the activity_id of every pull request it mentions.
- Copy every activity_id character for character from the JSON; outside the summary, from that same part of it. \
Never write an id that is not in the JSON.
- A part with nothing in it (count 0, or an empty blocked array) is exactly \
[{"text": "Nothing to report.", "activity_ids": []}]. The summary is that too when there is no activity at all.
- Plain sentences: no greetings, sign-offs, headings, markdown, dates, or activity_ids inside text."""

# The agent node runs a voice agent; its persona leaks into replies, so this one is neutral (HAR-91 addendum).
AGENT = {"entity_type": "agent", "agent_status": "inactive", "role": "Assistant", "use_company_knowledge": False,
         "identity": {}, "persona": {
             "agent_name": "reseau-workflows", "description": "Réseau's Start My Day briefings and daily reports",
             "persona": "You turn tool output into clear, readable, factual JSON. No greetings, no sign-offs.",
             "assertiveness_level": 0.5, "conciseness_level": 0.7, "formality_level": 0.5}}


class WorkflowError(Exception):
    def __init__(self, message, problems=()):
        super().__init__(message)
        self.problems = list(problems)


# ---- the workflows ----

def chain(nodes):
    """Wire nodes in order. The executor walks node.connections; the validator also wants mirrored edges with ids."""
    edges = []
    for k, (a, b) in enumerate(zip(nodes, nodes[1:])):
        a["connections"] = [b["node_id"]]
        edges.append({"id": "e%d" % k, "source": a["node_id"], "target": b["node_id"], "edge_type": "default"})
    return {"start_node_id": nodes[0]["node_id"], "nodes": nodes, "edges": edges}


def tool_then_agent(server_id, agent_id, node, tool, instructions, inputs=()):
    """trigger -> tool on the gateway -> agent. The tool's arguments are the trigger inputs of the same names."""
    return chain([
        {"node_id": "trigger_1", "name": "trigger_1", "node_type": "trigger",
         "config": {"trigger_type": "tool_call",
                    "input_schema": {"type": "object", "properties": {k: {"type": "string"} for k in inputs}}}},
        {"node_id": node, "name": node, "node_type": "tool",
         "config": {"tool": "mcp", "mcp_server_id": server_id, "mcp_tool_name": tool,
                    "input_mappings": [{"source_expression": "${trigger.%s}" % k, "target_field": k} for k in inputs]}},
        {"node_id": "agent_1", "name": "agent_1", "node_type": "agent",
         "config": {"agent_id": agent_id, "instructions": instructions,
                    "input_mappings": [{"source_expression": "${%s.content}" % node, "target_field": "message"}]}}])


def start_my_day_config(server_id, agent_id):
    return tool_then_agent(server_id, agent_id, "day_1", "get_my_day_context", INSTRUCTIONS)


def daily_report_config(server_id, agent_id):
    return tool_then_agent(server_id, agent_id, "team_1", "get_team_summary", REPORT_INSTRUCTIONS, ["date"])


WORKFLOWS = {START_MY_DAY_ENV: ("reseau-start-my-day", "Réseau Start My Day (HAR-102)", start_my_day_config),
             REPORT_ENV: ("reseau-daily-report", "Réseau team daily report (HAR-103)", daily_report_config)}


def setup(g8, server_id):
    """Create the voice agent and every workflow over a registered gateway -> (agent_id, {env var: action_id}).
    g8(method, path, body=None) -> (status, data): register_graph8.g8 bound to a client and key."""
    _, agent = g8("POST", "/api/v1/voice/agents", AGENT)
    agent_id = isinstance(agent, dict) and (agent.get("agent_id") or (agent.get("agent") or {}).get("agent_id"))
    if not agent_id:
        raise WorkflowError("voice agent create failed: %s" % agent)
    created = {}
    for env, (name, description, config) in WORKFLOWS.items():
        _, wf = g8("POST", "/api/v1/workflows", {"name": name, "description": description,
                                                  "config": config(server_id, agent_id)})
        if not (isinstance(wf, dict) and wf.get("action_id")):
            raise WorkflowError("workflow %s create failed: %s (created: voice agent %s, workflows %s)"
                                % (name, wf, agent_id, list(created.values())))
        created[env] = wf["action_id"]
    return agent_id, created


def execute(g8, action_id, input_data=None, timeout=TIMEOUT, clock=time.monotonic):
    """Run a workflow and poll it to the end -> (execution_id, node_results), as finished() checks them."""
    status, started = g8("POST", "/api/v1/workflows/%s/execute" % action_id, {"input_data": input_data or {}})
    execution = isinstance(started, dict) and started.get("execution_id")
    if not execution:
        raise WorkflowError("execute returned HTTP %s: %s" % (status, started))
    deadline = clock() + timeout
    while True:  # g8 spaces calls 3 s apart, which paces the polling
        _, run = g8("GET", "/api/v1/workflows/executions/" + execution)
        run = run if isinstance(run, dict) else {}
        if run.get("status") not in ("pending", "running"):
            return execution, finished(execution, run)
        if clock() > deadline:
            raise WorkflowError("execution %s still %s after %d s" % (execution, run["status"], timeout))


def finished(execution, run):
    """An execution's node_results. Raises WorkflowError unless the run and every node completed without an MCP
    error: a failed tool call must never reach the reader as a briefing or a report."""
    nodes = (run.get("output_data") or {}).get("node_results") or {}
    failed = [n for n, r in nodes.items() if r.get("status") != "completed" or (r.get("output") or {}).get("is_error")]
    if run.get("status") != "completed" or failed:
        detail = "; ".join("%s: %s" % (n, str(nodes[n].get("error") or (nodes[n].get("output") or {}).get("content"))[:300])
                           for n in failed)
        raise WorkflowError("execution %s %s: %s" % (execution, run.get("status"), detail or run.get("error_message") or run))
    return nodes


def outputs(execution, nodes, node):
    """-> (the tool node's JSON output, the agent's reply)."""
    try:
        output = json.loads(nodes[node]["output"]["content"])
    except (KeyError, TypeError, ValueError):
        raise WorkflowError("execution %s: %s returned no JSON" % (execution, node)) from None
    return output, (nodes.get("agent_1", {}).get("output") or {}).get("response")


def verified(once, attempts):
    """once() -> (execution_id, tool output, reply, sections, problems). The first attempt with no problems ->
    (execution_id, tool output, sections). Each attempt is billable; if all fail, WorkflowError carries the last
    one's problems."""
    for _ in range(attempts):
        execution, output, reply, sections, problems = once()
        if not problems:
            return execution, output, sections
    raise WorkflowError("execution %s: the reply failed verification: %r" % (execution, str(reply)[:500]), problems)


def attach(reply, evidence):
    """evidence = {section: the tool's activity_ids behind a count}. A count's citation is the tool's whole list,
    which the agent doesn't copy: 34 commit ids overran its reply live. So they are attached here, to every
    sentence of the section except NOTHING, which citations() then holds to the tool output as usual."""
    for section, ids in evidence.items():
        found = reply.get(section) if isinstance(reply, dict) else None
        for s in found if isinstance(found, list) else []:
            if isinstance(s, dict) and s.get("text") != verify.NOTHING:
                s["activity_ids"] = list(ids)
    return reply


def summary_problems(reply, output):
    """The summary may cite anything in the tool output, and every number it states is a count found there."""
    return verify.numbers(reply, "summary", verify.counted(output))


def briefing_problems(briefing, context):
    """A briefing's problems against the get_my_day_context output it was written from: its citations, its
    summary's numbers, and a focus sentence naming and citing the blocker of every blocked focus issue."""
    sources = {s: context.get(s) for s in SECTIONS} | {"summary": context}
    return (verify.citations(briefing, sources) + summary_problems(briefing, context)
            + verify.blockers(briefing, "focus", [i for i in context.get("focus") or [] if i.get("blocked_by")]))


def report_problems(report, summary):
    """A report's problems against the get_team_summary output it was written from: its citations, its counts
    (each counted section states the total's count and cites all of it), its summary's numbers, and a sentence
    naming the blocker of every blocked issue."""
    tallies = {s: summary["total"][s] for s in COUNTED}
    return (verify.citations(report, tallies | {"blocked": summary["blocked"], "summary": summary})
            + verify.counts(report, tallies) + summary_problems(report, summary)
            + verify.blockers(report, "blocked", summary["blocked"]))


# tool node -> (the evidence attach() fills in from the tool output, the problems check)
CHECKS = {"day_1": (lambda context: {"yesterday": context["yesterday"]["activity_ids"]}, briefing_problems),
          "team_1": (lambda summary: {s: summary["total"][s]["activity_ids"] for s in COUNTED}, report_problems)}


def checked(node, output, reply):
    """An agent reply -> (sections, problems) against the tool output it was written from."""
    evidence, problems = CHECKS[node]
    sections = attach(verify.parse(reply), evidence(output))
    return sections, problems(sections, output)


def run_once(g8, action_id, node, input_data=None):
    """One execution -> (execution_id, tool output, agent reply, sections, problems)."""
    execution, nodes = execute(g8, action_id, input_data)
    output, reply = outputs(execution, nodes, node)
    return (execution, output, reply) + checked(node, output, reply)


def my_day_once(g8, action_id):
    return run_once(g8, action_id, "day_1")


def report_once(g8, action_id, day):
    return run_once(g8, action_id, "team_1", {"date": day})


def start_my_day(g8, action_id, attempts=2):
    """The dashboard's trigger: run Start My Day and return its verified briefing,
    {"execution_id", "date", "sections": {section: [{"text", "activity_ids"}]}, "incomplete"}. incomplete is the
    tool's own list of what may be missing, passed through untouched. A reply that fails verification is
    retried; if every attempt fails, WorkflowError carries the last one's problems."""
    execution, context, briefing = verified(partial(my_day_once, g8, action_id), attempts)
    return {"execution_id": execution, "date": context.get("date"), "sections": briefing,
            "incomplete": context.get("incomplete") or []}


def daily_report(g8, action_id, day, attempts=2):
    """The team daily report for day (YYYY-MM-DD in the gateway's timezone), verified:
    {"execution_id", "team", "date", "sections": {section: [{"text", "activity_ids"}]}, "unmapped", "incomplete"}.
    A day with no activity is a report whose every section is "Nothing to report.". unmapped (team members
    not counted) and incomplete are the tool's own, passed through untouched. Retried like start_my_day."""
    try:
        Date.fromisoformat(day)
    except (TypeError, ValueError):
        raise WorkflowError("date must be YYYY-MM-DD, got %r" % (day,)) from None
    execution, summary, report = verified(partial(report_once, g8, action_id, day), attempts)
    return {"execution_id": execution, "team": summary["team"], "date": summary["date"], "sections": report,
            "unmapped": summary["unmapped"], "incomplete": summary["incomplete"]}


def verify_execution(g8, execution):
    """Any past run, e.g. one started from Graph8's dashboard, which shows the agent's raw reply: -> (sections,
    problems) through the same checks as the triggers. Free: it only reads the execution."""
    _, run = g8("GET", "/api/v1/workflows/executions/" + execution)
    run = run if isinstance(run, dict) else {}
    if run.get("status") in ("pending", "running"):
        raise WorkflowError("execution %s is still %s" % (execution, run["status"]))
    nodes = finished(execution, run)
    node = next((n for n in CHECKS if n in nodes), None)
    if not node:
        raise WorkflowError("execution %s ran none of Réseau's workflows (no node %s)" % (execution, " or ".join(CHECKS)))
    return checked(node, *outputs(execution, nodes, node))


def update(g8, server_id, action_ids):
    """Push this code's definitions and prompts, and the voice agent's persona, onto workflows setup() created,
    {env var: action_id}, keeping their action_ids and voice agent -> agent_id. Graph8 stores the prompt in the
    workflow, so a prompt change reaches runs only through this."""
    agent_id = None
    for env, action_id in action_ids.items():
        _, got = g8("GET", "/api/v1/workflows/" + action_id)
        nodes = ((((got or {}).get("action") or {}).get("skill_config") or {}).get("nodes")) or []
        agent_id = next((n["config"].get("agent_id") for n in nodes if n.get("node_type") == "agent"), None)
        if not agent_id:
            raise WorkflowError("%s %s: no agent node found (HTTP body: %s)" % (env, action_id, str(got)[:300]))
        name, description, config = WORKFLOWS[env]
        status, out = g8("PUT", "/api/v1/workflows/" + action_id, {"name": name, "description": description,
                                                                   "config": config(server_id, agent_id)})
        if status != 200:
            raise WorkflowError("%s %s: update returned HTTP %s: %s" % (env, action_id, status, out))
    status, out = g8("PUT", "/api/v1/voice/agents/" + agent_id, AGENT)
    if status != 200:
        raise WorkflowError("voice agent %s: update returned HTTP %s: %s" % (agent_id, status, out))
    return agent_id


def gateway_server_id(g8, name):
    """The registered gateway's mcp_server_id, looked up by its registration name."""
    _, listing = g8("GET", "/api/v1/workflows/mcp-servers")
    found = [s["mcp_server_id"] for s in (listing or {}).get("servers") or [] if s.get("name") == name]
    if len(found) != 1:
        raise WorkflowError("expected one MCP server named %r, found %d (python -m reseau.tunnel)" % (name, len(found)))
    return found[0]


def main(argv=sys.argv[1:]):
    from reseau import gateway, outbound, register_graph8

    if argv not in (["setup"], ["update"], ["start-my-day"]) and not (
            len(argv) == 2 and argv[0] in ("daily-report", "verify")):
        raise SystemExit(__doc__)
    key = gateway.resolve_credential(gateway.Upstream("graph8", register_graph8.BASE, "GRAPH8_API_KEY"))
    gateway.SECRETS.add(key)
    g8 = partial(register_graph8.g8, outbound.Client(), key)
    if argv == ["setup"]:
        agent_id, created = setup(g8, gateway_server_id(g8, register_graph8.NAME))
        print("voice agent %s" % agent_id)
        print("\n".join("%s=%s" % kv for kv in created.items()))
        return 0
    if argv == ["update"]:
        action_ids = {env: os.environ[env] for env in WORKFLOWS if os.environ.get(env)}
        if not action_ids:
            sys.exit("none of %s is set: run `setup` first" % ", ".join(WORKFLOWS))
        agent_id = update(g8, gateway_server_id(g8, register_graph8.NAME), action_ids)
        print("updated %s and voice agent %s" % (", ".join(action_ids), agent_id))
        return 0
    if argv[0] == "verify":
        sections, problems = verify_execution(g8, argv[1])
        print(json.dumps({"sections": sections, "problems": problems}, indent=2, ensure_ascii=False))
        print("VERIFIED" if not problems else "FAILED: %d problem(s)" % len(problems))
        return 1 if problems else 0
    env = START_MY_DAY_ENV if argv[0] == "start-my-day" else REPORT_ENV
    action_id = os.environ.get(env) or sys.exit("%s is not set: run `setup` first" % env)
    out = start_my_day(g8, action_id) if argv[0] == "start-my-day" else daily_report(g8, action_id, argv[1])
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
