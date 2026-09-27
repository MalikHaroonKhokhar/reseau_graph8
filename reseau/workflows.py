"""Graph8 workflows over the gateway: Start My Day (HAR-102), the team daily report (HAR-103) and Ask Réseau
(HAR-104). None returns a sentence that reseau/verify.py hasn't checked against the tool output of the same run.

Mechanism (HAR-91, spikes/graph8_agent_run/FINDINGS.md): a workflow whose MCP `tool` node calls a gateway tool
(get_my_day_context; get_team_summary with the trigger's date), then an `agent` node writes sections of
sentences, each with the activity_ids it cites. The tool output reaches the agent as its user message through
input_mappings (instructions are never interpolated, and only a bare ${node.field} resolves). A trigger executes
the workflow over REST, polls the execution, and returns the sections only once they pass verification.

Ask Réseau: a tool node's mcp_tool_name is not interpolated (live 2026-09-27: "${trigger.tool}" failed where the
same call with the name written out returned), and Graph8's agent node can't call MCP tools itself (HAR-91). So a
Graph8 agent chooses the tool in a route workflow, and each semantic tool has its own answer workflow: the
tool, a run_javascript node that joins the question to its output, and an agent that answers from it. The
route agent is offered the semantic tools only; a route to anything else is refused before any tool runs.

    python -m reseau.workflows setup                     # the voice agent and every workflow not set up yet
    python -m reseau.workflows update                    # push this code's prompts onto the existing ones
    python -m reseau.workflows start-my-day              # RESEAU_START_MY_DAY = its action_id
    python -m reseau.workflows daily-report YYYY-MM-DD   # RESEAU_DAILY_REPORT = its action_id
    python -m reseau.workflows ask "QUESTION"            # RESEAU_ASK = route=<action_id>,<tool>=<action_id>,...
    python -m reseau.workflows verify EXECUTION_ID       # check any run, e.g. one started from Graph8's dashboard

All need GRAPH8_API_KEY. The gateway must be registered and reachable (python -m reseau.tunnel, README).
Each run is billable: an agent node costs ~12 credits, and a question runs two.
"""
import json
import os
import sys
import time
from datetime import date as Date
from functools import partial

from reseau import evidence, semantic, verify

START_MY_DAY_ENV = "RESEAU_START_MY_DAY"
REPORT_ENV = "RESEAU_DAILY_REPORT"
ASK_ENV = "RESEAU_ASK"
ENVS = (START_MY_DAY_ENV, REPORT_ENV, ASK_ENV)
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

DECLINE = "No evidence found."
# The voice agent's canned reply when Graph8 can't run its model (live 2026-09-27, in place of any JSON): an outage on
# Graph8's side, not a reply that got the facts wrong.
GRAPH8_DOWN = "respond right now due to a temporary issue"
# The tools Ask Réseau can reach, each through its own answer workflow: the semantic tools and get_evidence. The
# gateway's raw github_*, linear_* and g8_* tools are left out (deck slide 7).
ASK_TOOLS = {t.name: t for t in [*semantic.TOOLS, evidence.TOOL]}

ROUTE_INSTRUCTIONS = """You route one question about a software team's work to the one Réseau tool that answers \
it. The user message is one JSON document: {"question", "today" (YYYY-MM-DD), "timezone", "people", "projects"}.

Reply with only this JSON object, no code fences and no other text:
{"tool": "<tool name>", "arguments": {"<argument>": "<string>", ...}}
or, when none of the tools can answer the question, {"tool": null, "arguments": {}}.

The tools:
%s

- person is one of "people", exactly as written there. A question about anyone not listed there gets \
{"tool": null, "arguments": {}}.
- project is one of "projects", and only when the question names it.
- date is YYYY-MM-DD, worked out from "today": yesterday is the day before it. With no day in the question, use \
today.
- activity_id is linear:issue:ENG-142 for a Linear issue key like ENG-142, and github:pr:owner/repo#9 for a pull \
request.
- What blocks an issue, named by key or by title: get_project_context when the question names a project in \
"projects", otherwise get_team_summary with today's date (its blocked list holds every blocked issue of the team).
- Why a work item matters, or which customer, deal or commitment waits on it: get_business_context.
- Anything these tools don't cover (money, the weather, opinions, predictions, general knowledge): \
{"tool": null, "arguments": {}}. Never guess.

Examples, with today 2026-09-27 and "ana" in people:
- "What is blocking the checkout page?" -> {"tool": "get_team_summary", "arguments": {"date": "2026-09-27"}}
- "What did ana do yesterday?" -> {"tool": "get_person_activity", "arguments": {"person": "ana", "date": "2026-09-26"}}
- "Why does ENG-142 matter?" -> {"tool": "get_business_context", "arguments": {"activity_id": "linear:issue:ENG-142"}}
- "What is our revenue?" -> {"tool": null, "arguments": {}}""" % "\n".join(
    "- %s(%s): %s" % (t.name, ", ".join(t.input_schema["properties"]), t.description) for t in ASK_TOOLS.values())

ANSWER_INSTRUCTIONS = """You answer one question about a software team's work. The user message is the question, \
then the JSON output of the Réseau tool that was called for it. That JSON is your only source of facts. Never add \
facts, guesses, advice, estimates or anything else it does not state, and never answer from general knowledge.

Reply with only this JSON object, no code fences and no other text:
{"answer": [S, ...]}
Each S is {"text": "<exactly one sentence>", "activity_ids": ["<activity_id>", ...]}.

- One to five sentences that answer the question directly. Each one mentions at most 6 items, and its \
activity_ids lists the activity_id of every item it mentions, copied character for character from the JSON. \
Never write an id that is not in the JSON. With more items than that, name the ones that answer the question best.
- Blocked work: name every issue in its blocked_by by identifier (HAR-6 for linear:issue:HAR-6) and the pull \
requests it waits on (blocking_prs) by number, and cite them all.
- Business context: name the customers, opportunities and commitments in links, citing their graph8: \
activity_ids. If links is empty (reason no_link_found), say in one sentence that no Graph8 customer, opportunity \
or commitment is linked to the work item, citing the work item's activity_id.
- State a number only when the JSON states it (a count field, or the length of a list), in digits. Never count \
items yourself: name them instead, e.g. "haroon completed HAR-86 and HAR-87 and merged PR #20."
- Inside text, quote a title with single quotes ('like this'), never double quotes.
- If nothing in the JSON answers the question, reply exactly \
{"answer": [{"text": "%s", "activity_ids": []}]}.
- Plain sentences: no greetings, sign-offs, headings, markdown, or activity_ids inside text.""" % DECLINE

# The agent node runs a voice agent; its persona leaks into replies, so this one is neutral (HAR-91 addendum).
AGENT = {"entity_type": "agent", "agent_status": "inactive", "role": "Assistant", "use_company_knowledge": False,
         "identity": {}, "persona": {
             "agent_name": "reseau-workflows",
             "description": "Réseau's Start My Day briefings, daily reports and Ask Réseau answers",
             "persona": "You turn tool output into clear, readable, factual JSON. No greetings, no sign-offs.",
             "assertiveness_level": 0.5, "conciseness_level": 0.7, "formality_level": 0.5}}


class WorkflowError(Exception):
    """problems: why the reply failed verification. upstream: the provider that was down, when that's the cause."""

    def __init__(self, message, problems=(), upstream=None):
        super().__init__(message)
        self.problems, self.upstream = list(problems), upstream


# ---- the workflows ----

def chain(nodes):
    """Wire nodes in order. The executor walks node.connections; the validator also wants mirrored edges with ids."""
    edges = []
    for k, (a, b) in enumerate(zip(nodes, nodes[1:])):
        a["connections"] = [b["node_id"]]
        edges.append({"id": "e%d" % k, "source": a["node_id"], "target": b["node_id"], "edge_type": "default"})
    return {"start_node_id": nodes[0]["node_id"], "nodes": nodes, "edges": edges}


def trigger_node(inputs):
    return {"node_id": "trigger_1", "name": "trigger_1", "node_type": "trigger",
            "config": {"trigger_type": "tool_call",
                       "input_schema": {"type": "object", "properties": {k: {"type": "string"} for k in inputs}}}}


def tool_node(server_id, node, tool, inputs):
    """An MCP call on the gateway whose arguments are the trigger inputs of the same names."""
    return {"node_id": node, "name": node, "node_type": "tool",
            "config": {"tool": "mcp", "mcp_server_id": server_id, "mcp_tool_name": tool,
                       "input_mappings": [{"source_expression": "${trigger.%s}" % k, "target_field": k} for k in inputs]}}


def agent_node(agent_id, instructions, message):
    return {"node_id": "agent_1", "name": "agent_1", "node_type": "agent",
            "config": {"agent_id": agent_id, "instructions": instructions,
                       "input_mappings": [{"source_expression": message, "target_field": "message"}]}}


def tool_then_agent(server_id, agent_id, node, tool, instructions, inputs=()):
    """trigger -> tool on the gateway -> agent."""
    return chain([trigger_node(inputs), tool_node(server_id, node, tool, inputs),
                  agent_node(agent_id, instructions, "${%s.content}" % node)])


def start_my_day_config(server_id, agent_id):
    return tool_then_agent(server_id, agent_id, "day_1", "get_my_day_context", INSTRUCTIONS)


def daily_report_config(server_id, agent_id):
    return tool_then_agent(server_id, agent_id, "team_1", "get_team_summary", REPORT_INSTRUCTIONS, ["date"])


def route_config(server_id, agent_id):
    """trigger -> agent: the question and what arguments need (directory()) in, a tool and arguments out."""
    return chain([trigger_node(["message"]), agent_node(agent_id, ROUTE_INSTRUCTIONS, "${trigger.message}")])


def answer_config(tool, server_id, agent_id):
    """trigger -> the one semantic tool -> the question joined to its output -> agent. The agent's message is a
    single field and only a bare ${node.field} resolves, so a run_javascript node joins them (HAR-91 addendum)."""
    params = list(ASK_TOOLS[tool].input_schema["properties"])
    merge = {"node_id": "merge_1", "name": "merge_1", "node_type": "run_javascript",
             "config": {"timeout_ms": 5000,
                        "code": "return 'Question: ' + vars.question + '\\n\\nOutput of the %s tool:\\n' + vars.output;" % tool,
                        "input_mappings": [{"source_expression": "${trigger.question}", "target_field": "question"},
                                           {"source_expression": "${ask_1.content}", "target_field": "output"}]}}
    return chain([trigger_node(["question", *params]), tool_node(server_id, "ask_1", tool, params), merge,
                  agent_node(agent_id, ANSWER_INSTRUCTIONS, "${merge_1.result}")])


WORKFLOWS = {START_MY_DAY_ENV: ("reseau-start-my-day", "Réseau Start My Day (HAR-102)", start_my_day_config),
             REPORT_ENV: ("reseau-daily-report", "Réseau team daily report (HAR-103)", daily_report_config)}
# RESEAU_ASK names all of these (ask_ids)
ASK = {"route": ("reseau-ask-route", "Ask Réseau: pick the semantic tool (HAR-104)", route_config)} | {
    t: ("reseau-ask-" + t.replace("_", "-"), "Ask Réseau: answer from %s (HAR-104)" % t, partial(answer_config, t))
    for t in ASK_TOOLS}


def ask_ids(value):
    """RESEAU_ASK as setup prints it, "route=<action_id>,get_evidence=<action_id>,..." -> {key: action_id}."""
    ids = dict(p.strip().split("=", 1) for p in (value or "").split(",") if "=" in p)
    missing = [k for k in ASK if not ids.get(k)]
    if missing:
        raise WorkflowError("%s lacks %s: run `setup`" % (ASK_ENV, ", ".join(missing)))
    return {k: ids[k] for k in ASK}


def definitions(values):
    """{env var: its value, as setup returns it} -> [(action_id, (name, description, config))], every workflow."""
    found = [(a, WORKFLOWS[env]) for env, a in values.items() if env in WORKFLOWS]
    if values.get(ASK_ENV):
        ids = ask_ids(values[ASK_ENV])
        found += [(ids[k], ASK[k]) for k in ASK]
    return found


def setup(g8, server_id, envs=ENVS, agent_id=None):
    """Create the voice agent (unless agent_id reuses one) and the workflows behind each env var in envs, over a
    registered gateway -> (agent_id, {env var: its value}); RESEAU_ASK's names its workflows (ask_ids).
    g8(method, path, body=None) -> (status, data): register_graph8.g8 bound to a client and key."""
    if not agent_id:
        _, agent = g8("POST", "/api/v1/voice/agents", AGENT)
        agent_id = isinstance(agent, dict) and (agent.get("agent_id") or (agent.get("agent") or {}).get("agent_id"))
        if not agent_id:
            raise WorkflowError("voice agent create failed: %s" % agent)
    made = []

    def create(name, description, config):
        _, wf = g8("POST", "/api/v1/workflows", {"name": name, "description": description,
                                                  "config": config(server_id, agent_id)})
        if not (isinstance(wf, dict) and wf.get("action_id")):
            raise WorkflowError("workflow %s create failed: %s (created: voice agent %s, workflows %s)"
                                % (name, wf, agent_id, made))
        made.append(wf["action_id"])
        return wf["action_id"]

    created = {env: create(*WORKFLOWS[env]) for env in envs if env in WORKFLOWS}
    if ASK_ENV in envs:
        created[ASK_ENV] = ",".join("%s=%s" % (k, create(*d)) for k, d in ASK.items())
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
    if GRAPH8_DOWN in str(reply):
        raise WorkflowError("execution %s: Graph8's agent is unavailable: %r" % (execution, str(reply)[:200]), problems,
                            "graph8")
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


def declined(answer):
    """The explicit decline: one uncited "No evidence found.", its period optional. verify.parse turns an empty
    answer into NOTHING, which is one too."""
    found = answer.get("answer") if isinstance(answer, dict) else None
    return (isinstance(found, list) and len(found) == 1 and verify.is_sentence(found[0])
            and not found[0]["activity_ids"]
            and found[0]["text"].strip().rstrip(".").casefold() in ("no evidence found", "nothing to report"))


def ask_problems(answer, output):
    """An Ask Réseau answer's problems against the tool output it was written from. A decline is always allowed.
    Otherwise every sentence cites activity_ids the tool returned, and every number it states is a count in the
    output or part of a title there ("UI Critic Phase 3"). A get_business_context answer whose output links Graph8
    records cites at least one of them, so "why does it matter" is answered with the customer, not the ticket."""
    problems = [] if declined(answer) else (
        verify.citations(answer, {"answer": output})
        + verify.numbers(answer, "answer", verify.counted(output) | verify.titled(output)))
    links = output.get("links")
    if links and not any(i.startswith("graph8:") for _, s in verify.sentences(answer, "answer") for i in s["activity_ids"]):
        problems.append("answer: cites none of the %d Graph8 record(s) get_business_context linked" % len(links))
    return problems


# tool node -> (the evidence attach() fills in from the tool output, the problems check)
CHECKS = {"day_1": (lambda context: {"yesterday": context["yesterday"]["activity_ids"]}, briefing_problems),
          "team_1": (lambda summary: {s: summary["total"][s]["activity_ids"] for s in COUNTED}, report_problems),
          "ask_1": (lambda output: {}, ask_problems)}


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
    {"execution_id", "date", "me", "sections": {section: [{"text", "activity_ids"}]}, "incomplete"}. me (whose day
    it is) and incomplete (what may be missing) are the tool's own, passed through untouched. A reply that fails
    verification is retried; if every attempt fails, WorkflowError carries the last one's problems."""
    execution, context, briefing = verified(partial(my_day_once, g8, action_id), attempts)
    return {"execution_id": execution, "date": context.get("date"), "me": context.get("me"), "sections": briefing,
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


def directory(env=os.environ):
    """What the route agent needs for arguments: today in the gateway's timezone, and the names of the people and
    projects the gateway is configured with (RESEAU_IDENTITIES, RESEAU_PROJECTS). Names only, no identities."""
    tz, path = semantic.load_tz(env), env.get(evidence.IDENTITIES_ENV)
    return {"today": semantic.now().astimezone(tz).date().isoformat(), "timezone": str(tz),
            "people": sorted(json.load(open(path))) if path else [],
            "projects": sorted(semantic.load_projects(env, semantic.load_scope(env)))}


def routed(reply):
    """The route agent's reply -> ((tool, arguments), problems); tool None is its decline. Only one of ASK_TOOLS
    with every argument a non-empty string is a route, and only those arguments are kept."""
    parsed = verify.parse(reply)
    if not isinstance(parsed, dict) or "tool" not in parsed:
        return (None, {}), ["route: not a JSON object"]
    tool, args = parsed["tool"], parsed.get("arguments")
    if tool is None:
        return (None, {}), []
    if not isinstance(tool, str) or tool not in ASK_TOOLS:
        return (None, {}), ["route: %r is not one of Ask Réseau's tools" % (tool,)]
    args = args if isinstance(args, dict) else {}
    params = list(ASK_TOOLS[tool].input_schema["properties"])
    missing = [p for p in params if not (isinstance(args.get(p), str) and args[p].strip())]
    if missing:
        return (None, {}), ["route: %s needs %s" % (tool, ", ".join(missing))]
    return (tool, {p: args[p] for p in params}), []


def route_once(g8, action_id, message):
    """One route execution -> (execution_id, None, agent reply, (tool, arguments), problems)."""
    execution, nodes = execute(g8, action_id, {"message": message})
    reply = (nodes.get("agent_1", {}).get("output") or {}).get("response")
    return (execution, None, reply) + routed(reply)


def answer_once(g8, action_id, question, arguments):
    return run_once(g8, action_id, "ask_1", {"question": question, **arguments})


def ask(g8, action_ids, question, env=os.environ, attempts=2):
    """Ask Réseau, the dashboard's trigger. A Graph8 agent routes the question to one semantic tool, that tool's
    answer workflow runs, and the verified answer comes back: {"question", "tool", "arguments", "execution_ids":
    {"route", "answer"}, "sections": {"answer": [{"text", "activity_ids"}]}, "incomplete"}. action_ids is
    ask_ids(RESEAU_ASK). A question no tool fits (tool None, no answer run) or whose tool output answers nothing is
    the explicit decline, [{"text": "No evidence found.", "activity_ids": []}]. A route outside the semantic tools
    or an answer that fails verification is retried; if every attempt fails, WorkflowError carries the problems."""
    if not isinstance(question, str) or not question.strip():
        raise WorkflowError("question is empty")
    message = json.dumps({"question": question} | directory(env), ensure_ascii=False)
    route, _, (tool, arguments) = verified(partial(route_once, g8, action_ids["route"], message), attempts)
    out = {"question": question, "tool": tool, "arguments": arguments, "execution_ids": {"route": route},
           "sections": {"answer": [{"text": DECLINE, "activity_ids": []}]}, "incomplete": []}
    if tool is None:
        return out
    execution, output, answer = verified(partial(answer_once, g8, action_ids[tool], question, arguments), attempts)
    return out | {"execution_ids": {"route": route, "answer": execution},
                  "sections": out["sections"] if declined(answer) else answer,
                  "incomplete": output.get("incomplete") or []}


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


def agent_of(g8, action_id):
    """The voice agent an existing workflow's agent node runs."""
    _, got = g8("GET", "/api/v1/workflows/" + action_id)
    nodes = ((((got or {}).get("action") or {}).get("skill_config") or {}).get("nodes")) or []
    agent_id = next((n["config"].get("agent_id") for n in nodes if n.get("node_type") == "agent"), None)
    if not agent_id:
        raise WorkflowError("workflow %s: no agent node found (HTTP body: %s)" % (action_id, str(got)[:300]))
    return agent_id


def configured(env=os.environ):
    """{env var: its value} for every workflow env var that is set."""
    return {e: env[e] for e in ENVS if env.get(e)}


def update(g8, server_id, values):
    """Push this code's definitions and prompts, and the voice agent's persona, onto workflows setup() created,
    {env var: its value}, keeping their action_ids and voice agent -> agent_id. Graph8 stores the prompt in the
    workflow, so a prompt change reaches runs only through this."""
    agent_id = None
    for action_id, (name, description, config) in definitions(values):
        agent_id = agent_of(g8, action_id)
        status, out = g8("PUT", "/api/v1/workflows/" + action_id, {"name": name, "description": description,
                                                                   "config": config(server_id, agent_id)})
        if status != 200:
            raise WorkflowError("%s %s: update returned HTTP %s: %s" % (name, action_id, status, out))
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
            len(argv) == 2 and argv[0] in ("daily-report", "verify", "ask")):
        raise SystemExit(__doc__)
    key = gateway.resolve_credential(gateway.Upstream("graph8", register_graph8.BASE, "GRAPH8_API_KEY"))
    gateway.SECRETS.add(key)
    g8 = partial(register_graph8.g8, outbound.Client(), key)
    values = configured()
    if argv == ["setup"]:  # only what isn't set up yet, on the voice agent the existing workflows use
        envs = [e for e in ENVS if e not in values]
        if not envs:
            sys.exit("%s are all set: use `update`" % ", ".join(ENVS))
        existing = definitions(values)
        agent_id, created = setup(g8, gateway_server_id(g8, register_graph8.NAME), envs,
                                  agent_of(g8, existing[0][0]) if existing else None)
        print("voice agent %s" % agent_id)
        print("\n".join("%s=%s" % kv for kv in created.items()))
        return 0
    if argv == ["update"]:
        if not values:
            sys.exit("none of %s is set: run `setup` first" % ", ".join(ENVS))
        agent_id = update(g8, gateway_server_id(g8, register_graph8.NAME), values)
        print("updated %s and voice agent %s" % (", ".join(values), agent_id))
        return 0
    if argv[0] == "verify":
        sections, problems = verify_execution(g8, argv[1])
        print(json.dumps({"sections": sections, "problems": problems}, indent=2, ensure_ascii=False))
        print("VERIFIED" if not problems else "FAILED: %d problem(s)" % len(problems))
        return 1 if problems else 0
    if argv[0] == "ask":
        print(json.dumps(ask(g8, ask_ids(values.get(ASK_ENV)), argv[1]), indent=2, ensure_ascii=False))
        return 0
    env = START_MY_DAY_ENV if argv[0] == "start-my-day" else REPORT_ENV
    action_id = os.environ.get(env) or sys.exit("%s is not set: run `setup` first" % env)
    out = start_my_day(g8, action_id) if argv[0] == "start-my-day" else daily_report(g8, action_id, argv[1])
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
