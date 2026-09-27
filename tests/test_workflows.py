"""HAR-102 and HAR-103: the workflow definitions and their triggers, over a scripted Graph8 that returns the
gateway's real tool output for the fixture world. The verifiers themselves are tests/test_verify.py; the live run
(Graph8 -> gateway over the same fixtures) is tests/live_workflows.py."""
import copy
import json
import re

import pytest
from mcp import Client
from mcp.client.sse import sse_client

from reseau import semantic, workflows
from reseau.workflows import WorkflowError
from tests.mock_upstream import MockUpstream
from tests.test_front import TOK, serving
from tests.test_gateway import GH_TOKEN, LIN_TOKEN, run
from tests.test_semantic import LOGIN, PR_9, TEAM, TEAM_PEOPLE, TODAY, YESTERDAY, frozen_now  # noqa: F401 (autouse)
from tests.test_verify import context, empty_team, good, good_report, nothing, team, team_world, with_text, world

REPORT = workflows.REPORT_SECTIONS


# ---- the workflow definitions ----

@pytest.mark.parametrize("config, tool, inputs", [
    (workflows.start_my_day_config, "get_my_day_context", []),
    (workflows.daily_report_config, "get_team_summary", ["date"]),
])
def test_workflow_shape(config, tool, inputs):
    cfg = config("srv-1", "agent-1")
    trigger, node, agent = cfg["nodes"]
    assert cfg["start_node_id"] == trigger["node_id"] and trigger["node_type"] == "trigger"
    # the executor walks connections, the validator wants the same links as edges (HAR-91)
    assert [n.get("connections") for n in cfg["nodes"]] == [[node["node_id"]], [agent["node_id"]], None]
    assert [(e["source"], e["target"]) for e in cfg["edges"]] == [(trigger["node_id"], node["node_id"]),
                                                                   (node["node_id"], agent["node_id"])]
    assert {k: node["config"][k] for k in ("tool", "mcp_server_id", "mcp_tool_name")} == {
        "tool": "mcp", "mcp_server_id": "srv-1", "mcp_tool_name": tool}
    assert tool in semantic.HANDLERS  # a name the gateway serves unprefixed
    # tool arguments come only from input_mappings (tool_config is dropped), from the trigger's input (HAR-91)
    assert list(trigger["config"]["input_schema"]["properties"]) == inputs
    assert node["config"]["input_mappings"] == [{"source_expression": "${trigger.%s}" % k, "target_field": k}
                                                for k in inputs]
    [mapping] = agent["config"]["input_mappings"]
    # only a bare ${node.field} resolves, and only a message mapping reaches the model (HAR-91)
    assert mapping == {"source_expression": "${%s.content}" % node["node_id"], "target_field": "message"}
    assert agent["config"]["agent_id"] == "agent-1" and workflows.verify.NOTHING in agent["config"]["instructions"]


def test_report_instructions_name_every_section():
    assert all('"%s"' % s in workflows.REPORT_INSTRUCTIONS for s in REPORT)


def test_setup_creates_the_agent_then_each_workflow_over_the_gateway():
    posted = []

    def g8(method, path, body=None):
        posted.append((path, body))
        return 201, {"agent": {"agent_id": "agent-1"}} if path.endswith("/agents") else {"action_id": "wf-%d" % len(posted)}

    assert workflows.setup(g8, "srv-1") == ("agent-1", {"RESEAU_START_MY_DAY": "wf-2", "RESEAU_DAILY_REPORT": "wf-3"})
    assert [p for p, _ in posted] == ["/api/v1/voice/agents", "/api/v1/workflows", "/api/v1/workflows"]
    assert posted[0][1]["use_company_knowledge"] is False  # no facts from anywhere but the tool
    assert posted[1][1]["config"] == workflows.start_my_day_config("srv-1", "agent-1")
    assert posted[2][1]["config"] == workflows.daily_report_config("srv-1", "agent-1")


# ---- the triggers, over a scripted Graph8 ----

class FakeGraph8:
    """execute -> pending; the first poll says running, the next one completed, with the tool node's content and
    the next scripted agent reply as node results. tool_error: the tool node reports an MCP error instead."""

    def __init__(self, content, replies, node="day_1", tool_error=False):
        self.content, self.replies, self.node, self.tool_error, self.calls = content, list(replies), node, tool_error, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            self.polls = 0
            return 200, {"success": True, "execution_id": "ex-%d" % len(self.calls), "status": "pending"}
        self.polls += 1
        if self.polls == 1:
            return 200, {"status": "running"}
        return 200, {"status": "completed", "output_data": {"node_results": {
            "trigger_1": {"status": "completed", "output": {}},
            self.node: {"status": "completed", "output": {"mcp_server": "reseau-gateway", "tool": "a gateway tool",
                                                          "content": self.content, "is_error": self.tool_error}},
            "agent_1": {"status": "completed", "output": {"response": self.replies.pop(0)}}}}}

    def executions(self):
        return [body for method, _, body in self.calls if method == "POST"]


def test_green_trigger_returns_the_verified_briefing():
    ctx = context()
    g8 = FakeGraph8(json.dumps(ctx), [json.dumps(good(ctx))])
    out = workflows.start_my_day(g8, "wf-1")
    assert (out["execution_id"], out["date"], out["sections"], out["incomplete"]) == ("ex-1", TODAY, good(ctx), [])
    assert g8.calls[0] == ("POST", "/api/v1/workflows/wf-1/execute", {"input_data": {}}) and len(g8.executions()) == 1


def test_red_trigger_never_returns_an_unverified_briefing():
    ctx = context()
    bad = copy.deepcopy(good(ctx))
    bad["focus"][0]["activity_ids"] = ["linear:issue:HAR-999"]
    g8 = FakeGraph8(json.dumps(ctx), [json.dumps(bad)] * 2)
    with pytest.raises(WorkflowError) as e:
        workflows.start_my_day(g8, "wf-1")
    assert len(g8.executions()) == 2 and e.value.problems == [
        "focus[0]: cites 'linear:issue:HAR-999', which the tool did not return for focus",
        "focus: no sentence for blocked linear:issue:HAR-7"]
    # a flaky first reply is retried once
    g8 = FakeGraph8(json.dumps(ctx), [json.dumps(bad), json.dumps(good(ctx))])
    assert workflows.start_my_day(g8, "wf-1")["sections"] == good(ctx) and len(g8.executions()) == 2


def test_empty_day_trigger_says_nothing_to_report():
    g8 = FakeGraph8(json.dumps(context(world(empty=True))), ['{"summary": [], "focus": [], "needs_attention": [], "yesterday": []}'])
    assert workflows.start_my_day(g8, "wf-1")["sections"] == nothing()


def test_a_failed_tool_call_is_an_error_not_a_briefing():
    g8 = FakeGraph8("unhandled errors in a TaskGroup (1 sub-exception)", [json.dumps(nothing())], tool_error=True)
    with pytest.raises(WorkflowError, match="day_1: unhandled errors in a TaskGroup"):
        workflows.start_my_day(g8, "wf-1")
    assert len(g8.executions()) == 1  # not retried: the tool failed, not the model


def test_a_failed_execution_is_an_error():
    def g8(method, path, body=None):
        return 200, {"execution_id": "ex-1"} if method == "POST" else {"status": "failed", "error_message": "boom",
                                                                         "output_data": {"node_results": {}}}
    with pytest.raises(WorkflowError, match=re.escape("execution ex-1 failed: boom")):
        workflows.start_my_day(g8, "wf-1")


# ---- the daily report ----

def test_green_daily_report_returns_the_verified_report():
    s = team()
    g8 = FakeGraph8(json.dumps(s), ["```json\n%s\n```" % json.dumps(good_report(s))], node="team_1")
    out = workflows.daily_report(g8, "wf-2", YESTERDAY)
    assert (out["execution_id"], out["team"], out["date"], out["sections"], out["incomplete"]) == (
        "ex-1", TEAM, YESTERDAY, good_report(s), [])
    assert [u["name"] for u in out["unmapped"]] == ["Sam"]  # not counted, and the report says so
    assert g8.executions() == [{"input_data": {"date": YESTERDAY}}]  # the date reaches the tool node's mapping


def test_red_daily_report_says_merged_2_prs_when_the_tool_says_1():
    s = team()
    bad = with_text(good_report(s), "merged", "Merged: 2 GitHub PRs.")
    assert workflows.report_problems(bad, s) == ["merged[0]: says 2, but the tool counted 1"]
    g8 = FakeGraph8(json.dumps(s), [json.dumps(bad)] * 2, node="team_1")
    with pytest.raises(WorkflowError) as e:
        workflows.daily_report(g8, "wf-2", YESTERDAY)
    assert e.value.problems == ["merged[0]: says 2, but the tool counted 1"] and len(g8.executions()) == 2


def test_red_every_verifier_runs_on_the_report():
    s = team()
    r = good_report(s)
    r["completed"][0]["activity_ids"] = ["linear:issue:ENG-999"]  # citations
    r["blocked"][0]["text"] = "UI Critic Phase 3 is blocked."  # blockers
    assert workflows.report_problems(r, s) == [
        "completed[0]: cites 'linear:issue:ENG-999', which the tool did not return for completed",
        "completed[0]: doesn't cite 1 of the 1 activity_ids behind the count: ['linear:issue:ENG-142']",
        "blocked: linear:issue:HAR-7's sentence doesn't name and cite its blocker linear:issue:HAR-6"]


def test_empty_day_is_an_explicit_empty_report():
    s = team(empty_team())
    assert (s["total"], s["blocked"]) == ({k: {"count": 0, "activity_ids": []} for k in workflows.COUNTED}, [])
    g8 = FakeGraph8(json.dumps(s), [json.dumps({k: [] for k in REPORT})], node="team_1")
    assert workflows.daily_report(g8, "wf-2", YESTERDAY)["sections"] == nothing(REPORT)
    # and the full report is invented on an empty day
    # every cited id, every count, and the summary's two 1s
    assert len(workflows.report_problems(good_report(team()), s)) == (3 + 1 + 1 + 6 + 3) + 3 + 2


def test_red_a_bad_date_never_runs_the_workflow():
    g8 = FakeGraph8("{}", [])
    with pytest.raises(WorkflowError, match="date must be YYYY-MM-DD"):
        workflows.daily_report(g8, "wf-2", "26/09/2026")
    assert g8.calls == []


def test_integration_a_report_on_the_gateways_own_answer_passes_both_verifiers():
    """The tool node's content as Graph8 gets it: get_team_summary called over the gateway's SSE front, on the
    mock upstreams. A report true to the fixtures verifies against it."""
    w = team_world()
    with MockUpstream(GH_TOKEN, stateless=False, script=w["github"]) as gh, \
            MockUpstream(LIN_TOKEN, stateless=True, script=w["linear"]) as lin:
        async def body(base, gw):
            async with Client(sse_client(base + "/g8/%s/sse" % TOK), mode="legacy") as c:
                return (await c.call_tool("get_team_summary", {"date": YESTERDAY})).content[0].text

        content = run(serving((gh, lin), body, identities=TEAM_PEOPLE, env={
            "RESEAU_GITHUB_SCOPE": "%s,private-org" % LOGIN, "RESEAU_TEAM": TEAM}))
    report = good_report(json.loads(content))
    assert workflows.daily_report(FakeGraph8(content, [json.dumps(report)], node="team_1"), "wf-2", YESTERDAY)[
        "sections"] == report


def test_count_citations_are_attached_not_copied_by_the_agent():
    """Live, 34 commit ids overran the agent's reply mid-id. It now leaves a count's activity_ids empty and the
    trigger attaches the tool's list; the number stays the agent's, and counts() still checks it."""
    ctx = context()
    b = good(ctx)
    b["yesterday"][0]["activity_ids"] = []
    truncated = json.dumps(good(ctx))[:400]
    g8 = FakeGraph8(json.dumps(ctx), [truncated, json.dumps(b)])
    assert workflows.start_my_day(g8, "wf-1")["sections"] == good(ctx) and len(g8.executions()) == 2

    s = team()
    r = good_report(s)
    for k in workflows.COUNTED:
        r[k][0]["activity_ids"] = []
    g8 = FakeGraph8(json.dumps(s), [json.dumps(r)], node="team_1")
    assert workflows.daily_report(g8, "wf-2", YESTERDAY)["sections"] == good_report(s)
    bad = with_text(copy.deepcopy(r), "merged", "Merged: 2 GitHub PRs.")
    attached = workflows.attach(bad, {k: s["total"][k]["activity_ids"] for k in workflows.COUNTED})
    assert attached["merged"][0]["activity_ids"] == [PR_9] and workflows.report_problems(attached, s) == [
        "merged[0]: says 2, but the tool counted 1"]
    # NOTHING never gets ids attached: on a day with commits it's still refused
    n = workflows.attach(nothing(), {"yesterday": ctx["yesterday"]["activity_ids"]})
    assert n["yesterday"] == [{"text": workflows.verify.NOTHING, "activity_ids": []}]


def test_red_a_focus_sentence_must_name_and_cite_its_blocker():
    """Live, focus sentences named HAR-73 but cited only their own issue: the report's blocker check covers it."""
    ctx = context()
    b = good(ctx)
    b["focus"][0]["activity_ids"] = ["linear:issue:HAR-7"]
    assert workflows.briefing_problems(b, ctx) == [
        "focus: linear:issue:HAR-7's sentence doesn't name and cite its blocker linear:issue:HAR-6"]
    assert workflows.briefing_problems(good(ctx), ctx) == []


def test_red_a_summary_with_an_invented_number_is_refused():
    ctx = context()
    b = good(ctx)
    b["summary"][0]["text"] = "Start with HAR-7, then clear the 5 unresolved review threads on PR #20."
    assert workflows.briefing_problems(b, ctx) == ["summary[0]: says 5, which is no count the tool returned"]


def execution(node, content, reply, status="completed", is_error=False):
    return {"status": status, "output_data": {"node_results": {
        node: {"status": "completed", "output": {"content": content, "is_error": is_error}},
        "agent_1": {"status": "completed", "output": {"response": reply}}}}}


def test_verify_checks_a_dashboard_run_like_the_trigger():
    s, ctx = team(), context()
    r = good_report(s)
    for k in workflows.COUNTED:
        r[k][0]["activity_ids"] = []  # attached, as the trigger does
    runs = {"ex-report": execution("team_1", json.dumps(s), json.dumps(r)),
            "ex-day": execution("day_1", json.dumps(ctx), json.dumps(with_text(good(ctx), "summary", "A 9 PR day.")))}
    g8 = lambda method, path, body=None: (200, runs[path.rsplit("/", 1)[1]])
    assert workflows.verify_execution(g8, "ex-report") == (good_report(s), [])
    sections, problems = workflows.verify_execution(g8, "ex-day")
    assert problems == ["summary[0]: says 9, which is no count the tool returned"]


@pytest.mark.parametrize("run, error", [
    (execution("day_1", "boom", "{}", is_error=True), "day_1: boom"),
    (execution("day_1", "{}", "{}", status="running"), "is still running"),
    (execution("ms_1", "{}", "{}"), "ran none of Réseau's workflows"),
])
def test_verify_refuses_what_it_cant_check(run, error):
    with pytest.raises(WorkflowError, match=re.escape(error)):
        workflows.verify_execution(lambda method, path, body=None: (200, run), "ex-1")


def test_update_pushes_prompts_onto_the_existing_workflows_and_agent():
    stored = {"success": True, "action": {"skill_config": workflows.start_my_day_config("srv-old", "agent-1")}}
    calls = []

    def g8(method, path, body=None):
        calls.append((method, path, body))
        return 200, stored if method == "GET" else {"success": True}

    assert workflows.update(g8, "srv-1", {"RESEAU_START_MY_DAY": "wf-1", "RESEAU_DAILY_REPORT": "wf-2"}) == "agent-1"
    puts = [(path, body) for method, path, body in calls if method == "PUT"]
    assert puts == [("/api/v1/workflows/wf-1", {"name": "reseau-start-my-day", "description": "Réseau Start My Day (HAR-102)",
                                                "config": workflows.start_my_day_config("srv-1", "agent-1")}),
                    ("/api/v1/workflows/wf-2", {"name": "reseau-daily-report", "description": "Réseau team daily report (HAR-103)",
                                                "config": workflows.daily_report_config("srv-1", "agent-1")}),
                    ("/api/v1/voice/agents/agent-1", workflows.AGENT)]
