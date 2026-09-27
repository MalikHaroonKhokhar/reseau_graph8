"""HAR-104: Ask Réseau. A Graph8 agent routes a question to one of the semantic tools, a second one answers from
that tool's output, and the trigger returns the answer only once the citation verifier passes. The fixed question
set runs over the gateway's real tool output for the fixture world, with scripted agent replies; the live run (real
Graph8 agents) is tests/live_ask.py."""
import json

import pytest
from mcp import Client
from mcp.client.sse import sse_client

from reseau import evidence, gateway, semantic, workflows
from reseau.workflows import WorkflowError
from tests.mock_upstream import MockUpstream
from tests.test_front import TOK, serving
from tests.test_gateway import G8_TOKEN, GH_TOKEN, LIN_TOKEN, run
from tests.test_semantic import (COMMITMENT, CUSTOMER, ENG_142, LOGIN, OPP, TEAM, TEAM_PEOPLE, TODAY, UI, YESTERDAY,
                                 business_world, frozen_now, team_world)  # noqa: F401 (autouse)
from tests.test_verify import with_text

SIX = {"get_person_activity", "get_project_context", "get_team_summary", "get_evidence", "get_my_day_context",
       "get_business_context"}
IDS = {k: "wf-" + k for k in workflows.ASK}
HAR_7, HAR_6, HAR_8, PR_UI_9 = "linear:issue:HAR-7", "linear:issue:HAR-6", "linear:issue:HAR-8", "github:pr:%s#9" % UI
DECLINE = {"answer": [{"text": workflows.DECLINE, "activity_ids": []}]}


def said(*sentences):
    return {"answer": [{"text": t, "activity_ids": list(ids)} for t, ids in sentences]}


# ---- only the semantic tools ----

def test_only_the_semantic_tools_are_available():
    assert set(workflows.ASK_TOOLS) == SIX
    assert set(workflows.ASK) == SIX | {"route"}
    raw = {(u.prefix if u.prefix is not None else u.name + "_") + t for u in gateway.DEFAULT_UPSTREAMS for t in u.allow}
    route = workflows.route_config("srv-1", "agent-1")
    assert [n["node_type"] for n in route["nodes"]] == ["trigger", "agent"]  # the router calls nothing itself
    assert all("tools" not in n["config"] for n in route["nodes"])
    for tool in SIX:
        assert "\n- %s(" % tool in workflows.ROUTE_INSTRUCTIONS
    assert not [t for t in raw if t in workflows.ROUTE_INSTRUCTIONS]
    for tool in SIX:  # each answer workflow is pinned to its one semantic tool on the gateway
        cfg = workflows.ASK[tool][2]("srv-1", "agent-1")
        [node] = [n for n in cfg["nodes"] if n["node_type"] == "tool"]
        assert (node["config"]["mcp_server_id"], node["config"]["mcp_tool_name"]) == ("srv-1", tool)
        params = list(workflows.ASK_TOOLS[tool].input_schema["properties"])
        assert node["config"]["input_mappings"] == [{"source_expression": "${trigger.%s}" % p, "target_field": p}
                                                    for p in params]


def test_answer_workflow_shape():
    cfg = workflows.answer_config("get_business_context", "srv-1", "agent-1")
    trigger, tool, merge, agent = cfg["nodes"]
    assert [n.get("connections") for n in cfg["nodes"]] == [["ask_1"], ["merge_1"], ["agent_1"], None]
    assert list(trigger["config"]["input_schema"]["properties"]) == ["question", "activity_id"]
    # only bare ${node.field} resolves (HAR-91): the question and the tool output meet in a run_javascript node
    assert merge["config"]["input_mappings"] == [{"source_expression": "${trigger.question}", "target_field": "question"},
                                                 {"source_expression": "${ask_1.content}", "target_field": "output"}]
    assert agent["config"]["input_mappings"] == [{"source_expression": "${merge_1.result}", "target_field": "message"}]
    assert workflows.DECLINE in agent["config"]["instructions"]


# ---- routing ----

@pytest.mark.parametrize("reply, problem", [
    ('{"tool": "github_list_issues", "arguments": {}}', "route: 'github_list_issues' is not one of Ask Réseau's tools"),
    ('{"tool": "g8_get_deals", "arguments": {}}', "route: 'g8_get_deals' is not one of Ask Réseau's tools"),
    ('{"tool": "get_person_activity", "arguments": {"person": "dev"}}', "route: get_person_activity needs date"),
    ('{"tool": "get_evidence", "arguments": {"activity_id": 7}}', "route: get_evidence needs activity_id"),
    ("I would use get_team_summary.", "route: not a JSON object"),
])
def test_red_a_route_outside_the_semantic_tools_is_refused(reply, problem):
    assert workflows.routed(reply)[1] == [problem]


def test_route_keeps_only_the_tools_arguments():
    assert workflows.routed('Sure! {"tool": "get_team_summary", "arguments": {"date": "%s", "team": "x"}}' % TODAY) == (
        ("get_team_summary", {"date": TODAY}), [])
    assert workflows.routed('{"tool": null, "arguments": {}}') == ((None, {}), [])


# ---- the answer checks: the citation verifier, and the decline ----

def test_decline_is_always_allowed_and_nothing_else_goes_uncited():
    out = {"activity_id": HAR_8, "links": []}
    for reply in (DECLINE, {"answer": [{"text": "No evidence found", "activity_ids": []}]}, {"answer": []}):
        assert workflows.ask_problems(workflows.verify.parse(json.dumps(reply)), out) == []
    assert workflows.ask_problems(said(("Nobody is waiting on it.", [])), out) == [
        "answer[0]: no citation: 'Nobody is waiting on it.'"]


def test_red_numbers_must_come_from_the_tool_output():
    out = {"blocked": [{"activity_id": HAR_7, "title": "UI Critic Phase 3", "blocked_by": [HAR_6]}]}
    assert workflows.ask_problems(said(("UI Critic Phase 3 is blocked by HAR-6.", [HAR_7, HAR_6])), out) == []
    assert workflows.ask_problems(said(("2 issues block UI Critic Phase 3.", [HAR_7])), out) == [
        "answer[0]: says 2, which is no count the tool returned"]


def test_red_linked_business_context_must_cite_a_graph8_record():
    out = {"activity_id": ENG_142, "links": [{"activity_id": CUSTOMER}]}
    assert workflows.ask_problems(said(("ENG-142 ships SSO.", [ENG_142])), out) == [
        "answer: cites none of the 1 Graph8 record(s) get_business_context linked"]
    assert workflows.ask_problems(DECLINE, out) == [
        "answer: cites none of the 1 Graph8 record(s) get_business_context linked"]


# ---- the trigger, over a scripted Graph8 ----

class FakeGraph8:
    """Every execute runs the next scripted node results for that action_id, completed on the first poll."""

    def __init__(self, runs):
        self.runs, self.executed = {a: list(r) for a, r in runs.items()}, []

    def __call__(self, method, path, body=None):
        if method == "POST":
            action = path.split("/")[-2]
            self.executed.append((action, body["input_data"]))
            self.current = self.runs[action].pop(0)
            return 200, {"execution_id": "ex-%d" % len(self.executed)}
        return 200, {"status": "completed", "output_data": {"node_results": self.current}}


def routing(reply):
    return {"trigger_1": {"status": "completed", "output": {}},
            "agent_1": {"status": "completed", "output": {"response": reply}}}


def answering(content, reply, is_error=False):
    return {"ask_1": {"status": "completed", "output": {"content": content, "is_error": is_error}},
            "merge_1": {"status": "completed", "output": {}},
            "agent_1": {"status": "completed", "output": {"response": reply}}}


def route(tool, **arguments):
    return json.dumps({"tool": tool, "arguments": arguments})


def test_red_a_raw_tool_is_never_run():
    g8 = FakeGraph8({"wf-route": [routing(route("github_list_issues"))] * 2})
    with pytest.raises(WorkflowError) as e:
        workflows.ask(g8, IDS, "Which issues are open?", {})
    assert e.value.problems == ["route: 'github_list_issues' is not one of Ask Réseau's tools"]
    assert [a for a, _ in g8.executed] == ["wf-route", "wf-route"]  # routed twice, answered never


def test_router_gets_the_question_and_what_it_needs_for_arguments(tmp_path):
    people = tmp_path / "people.json"
    people.write_text(json.dumps(TEAM_PEOPLE))
    g8 = FakeGraph8({"wf-route": [routing(route(None))]})
    out = workflows.ask(g8, IDS, "What's the weather?", {evidence.IDENTITIES_ENV: str(people),
                                                         semantic.TZ_ENV: "Asia/Karachi"})
    assert json.loads(g8.executed[0][1]["message"]) == {
        "question": "What's the weather?", "today": "2026-09-27", "timezone": "Asia/Karachi",
        "people": ["ana", "dev"], "projects": []}
    assert (out["tool"], out["sections"], out["execution_ids"]) == (None, DECLINE, {"route": "ex-1"})


def test_a_bad_answer_is_retried_then_refused():
    out = json.dumps({"activity_id": HAR_8, "links": [], "incomplete": []})
    bad = json.dumps(said(("HAR-8 waits on PR #99.", ["github:pr:%s#99" % UI])))
    good = json.dumps(said(("No Graph8 record is linked to HAR-8.", [HAR_8])))
    g8 = FakeGraph8({"wf-route": [routing(route("get_business_context", activity_id=HAR_8))],
                     "wf-get_business_context": [answering(out, bad), answering(out, good)]})
    ask = workflows.ask(g8, IDS, "Who waits on HAR-8?", {})
    assert ask["sections"] == json.loads(good) and ask["execution_ids"] == {"route": "ex-1", "answer": "ex-3"}
    assert g8.executed[1] == ("wf-get_business_context", {"question": "Who waits on HAR-8?", "activity_id": HAR_8})
    g8 = FakeGraph8({"wf-route": [routing(route("get_business_context", activity_id=HAR_8))],
                     "wf-get_business_context": [answering(out, bad)] * 2})
    with pytest.raises(WorkflowError) as e:
        workflows.ask(g8, IDS, "Who waits on HAR-8?", {})
    assert e.value.problems == ["answer[0]: cites 'github:pr:%s#99', which the tool did not return for answer" % UI]


def test_a_failed_tool_call_is_an_error_not_a_decline():
    g8 = FakeGraph8({"wf-route": [routing(route("get_team_summary", date=TODAY))],
                     "wf-get_team_summary": [answering("unhandled errors in a TaskGroup", json.dumps(DECLINE), True)]})
    with pytest.raises(WorkflowError, match="ask_1: unhandled errors in a TaskGroup"):
        workflows.ask(g8, IDS, "What is blocked?", {})


def test_red_an_empty_question_never_runs():
    g8 = FakeGraph8({})
    with pytest.raises(WorkflowError, match="empty"):
        workflows.ask(g8, IDS, "  ", {})
    assert g8.executed == []


def test_ask_ids_name_every_workflow():
    value = ",".join("%s=%s" % kv for kv in IDS.items())
    assert workflows.ask_ids(value) == IDS
    with pytest.raises(WorkflowError, match="RESEAU_ASK lacks get_evidence"):
        workflows.ask_ids(value.replace("get_evidence=", "x="))


# ---- the fixed question set, over the gateway's real answers for the fixture world ----

def ask_world():
    """team_world (yesterday: PR #9 merged, HAR-7 "UI Critic Phase 3" blocked by HAR-6, which waits on PR #9) plus
    business_world's Graph8: ENG-142 is a commitment on a deal of customer 4242. HAR-8 has no Graph8 link."""
    w, b = team_world(), business_world()
    get_issue = w["linear"]["get_issue"]
    w["linear"]["get_issue"] = lambda a: b["linear"]["get_issue"](a) if a["id"] == "ENG-142" else get_issue(a)
    w["graph8"] = b["graph8"] | {"g8_current_org": lambda a: "org"}
    return w


def gateway_answers(calls):
    """[(tool, arguments)] -> the gateway's JSON text for each, over its SSE front, as Graph8's tool node gets it."""
    w = ask_world()
    with MockUpstream(GH_TOKEN, stateless=False, script=w["github"]) as gh, \
            MockUpstream(LIN_TOKEN, stateless=True, script=w["linear"]) as lin, \
            MockUpstream(G8_TOKEN, stateless=True, script=w["graph8"]) as g8:
        async def body(base, gw):
            async with Client(sse_client(base + "/g8/%s/sse" % TOK), mode="legacy") as c:
                return [(await c.call_tool(tool, args)).content[0].text for tool, args in calls]

        return run(serving((gh, lin, g8), body, identities=TEAM_PEOPLE, env={
            "RESEAU_GITHUB_SCOPE": "%s,private-org" % LOGIN, "RESEAU_TEAM": TEAM}))


def dev_yesterday(out):
    ids = [a["activity_id"] for a in out["activities"] if a["action"] == "commit"]
    return said(("dev made %d commits yesterday." % len(ids), ids))


# question, the route the agent picks, the answer it writes (from the tool output), what it must cite
QUESTIONS = [
    ("What is blocking Phase 3?", ("get_team_summary", {"date": TODAY}),
     lambda out: said(("UI Critic Phase 3 is blocked by HAR-6, which waits on PR #9.", [HAR_7, HAR_6, PR_UI_9]))),
    ("What did dev do yesterday?", ("get_person_activity", {"person": "dev", "date": YESTERDAY}), dev_yesterday),
    ("Why does ENG-142 matter?", ("get_business_context", {"activity_id": ENG_142}),
     lambda out: said(("ENG-142 is a commitment on Example Customer Co's annual plan.", [ENG_142, COMMITMENT, OPP,
                                                                                         CUSTOMER]))),
    ("Which customer is waiting on HAR-8?", ("get_business_context", {"activity_id": HAR_8}),
     lambda out: said(("No Graph8 customer, opportunity or commitment is linked to HAR-8.", [HAR_8]))),
    ("What is our Q3 revenue?", ("get_team_summary", {"date": TODAY}), lambda out: DECLINE),
]
MUST_CITE = [{PR_UI_9, HAR_6}, None, {CUSTOMER}, {HAR_8}, set()]


def test_integration_fixed_questions_answer_with_verified_citations():
    outputs = gateway_answers([r for _, r, _ in QUESTIONS])
    for (question, (tool, args), write), content, must in zip(QUESTIONS, outputs, MUST_CITE):
        out = json.loads(content)
        answer = write(out)
        g8 = FakeGraph8({"wf-route": [routing(route(tool, **args))], "wf-" + tool: [answering(content, json.dumps(answer))]})
        got = workflows.ask(g8, IDS, question, {})
        cited = {i for s in got["sections"]["answer"] for i in s["activity_ids"]}
        assert (got["tool"], got["arguments"], got["sections"]) == (tool, args, answer), question
        assert must is None or must <= cited, question
        if tool == "get_business_context":  # graph8:* records, or an explicit no link
            assert any(i.startswith("graph8:") for i in cited) or out["reason"] == "no_link_found"
    # the same answers, with a PR the tool didn't return, are refused
    phase3 = json.loads(outputs[0])
    wrong = said(("UI Critic Phase 3 waits on PR #10.", [HAR_7, "github:pr:%s#10" % UI]))
    assert workflows.ask_problems(wrong, phase3) == [
        "answer[0]: cites 'github:pr:%s#10', which the tool did not return for answer" % UI]
    assert len(dev_yesterday(json.loads(outputs[1]))["answer"][0]["activity_ids"]) == 6  # the fixture's commits
    wrong = with_text(dev_yesterday(json.loads(outputs[1])), "answer", "dev made 7 commits yesterday.")
    assert workflows.ask_problems(wrong, json.loads(outputs[1])) == [
        "answer[0]: says 7, which is no count the tool returned"]


def test_unanswerable_question_declines_at_the_route():
    g8 = FakeGraph8({"wf-route": [routing(route(None))]})
    assert workflows.ask(g8, IDS, "Will it rain tomorrow?", {})["sections"] == DECLINE
    assert len(g8.executed) == 1  # no tool ran
