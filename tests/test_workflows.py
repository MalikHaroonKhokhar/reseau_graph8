"""HAR-102: the citation verifier, the Start My Day workflow definition, and its trigger over a scripted Graph8
that returns the gateway's real get_my_day_context output for the fixture world. The live run (Graph8 -> gateway
over the same fixtures) is tests/live_start_my_day.py."""
import copy
import json
import re

import pytest

from reseau import evidence, semantic, workflows
from reseau.workflows import NOTHING, WorkflowError, check, parse_briefing
from tests.test_semantic import RG, TODAY, UI, FakeGateway, frozen_now, world  # noqa: F401 (frozen_now: autouse)
from tests.test_gateway import run


def context(w=None):
    """get_my_day_context's output exactly as the gateway sends it: the result's JSON text, parsed."""
    return json.loads(evidence.as_result(run(semantic.my_day(FakeGateway(w or world()), {}))).content[0].text)


def sources(ctx):
    return {s: ctx[s] for s in workflows.SECTIONS}


def good(ctx):
    """A hand-written briefing citing only what the tool returned, section by section."""
    return {"focus": [{"text": "UI Critic Phase 3 (Urgent) is blocked by HAR-6 and waits on PR #9.",
                       "activity_ids": ["linear:issue:HAR-7", "linear:issue:HAR-6", "github:pr:%s#9" % UI]}],
            "needs_attention": [{"text": "Your PR #20 has 2 unresolved review threads.",
                                 "activity_ids": ["github:pr:%s#20" % RG]}],
            "yesterday": [{"text": "You made 6 commits across 2 repositories.",
                           "activity_ids": ctx["yesterday"]["activity_ids"]}]}


def nothing():
    return {s: [{"text": NOTHING, "activity_ids": []}] for s in workflows.SECTIONS}


# ---- the citation verifier ----

def test_green_a_briefing_citing_the_tool_output_passes():
    ctx = context()
    assert check(good(ctx), sources(ctx)) == []


def test_red_a_fabricated_id_fails():
    ctx = context()
    b = good(ctx)
    b["focus"][0]["activity_ids"] = ["linear:issue:HAR-999"]
    assert check(b, sources(ctx)) == ["focus[0]: cites 'linear:issue:HAR-999', which the tool did not return for focus"]


def test_red_an_uncited_sentence_fails():
    ctx = context()
    b = good(ctx)
    b["needs_attention"].append({"text": "Reviewers are waiting on you.", "activity_ids": []})
    assert check(b, sources(ctx)) == ["needs_attention[1]: no citation: 'Reviewers are waiting on you.'"]


def test_red_an_id_from_another_section_fails():
    ctx = context()  # a real id, but yesterday's facts are commits: citing the focus issue there proves nothing
    b = good(ctx)
    b["yesterday"][0]["activity_ids"] = ["linear:issue:HAR-7"]
    assert check(b, sources(ctx)) == ["yesterday[0]: cites 'linear:issue:HAR-7', which the tool did not return for yesterday"]


def test_red_two_sentences_under_one_citation_fail():
    ctx = context()  # the second sentence would ride on the first one's citation
    b = good(ctx)
    b["needs_attention"][0]["text"] = "Your PR #20 has 2 unresolved review threads. Ship it today."
    assert [p.split(":")[1] for p in check(b, sources(ctx))] == [" more than one sentence"]


@pytest.mark.parametrize("briefing, problem", [
    (None, "not a JSON object of sections"),
    ({"focus": [], "needs_attention": [], "yesterday": []}, "focus: no sentences"),
    ({"focus": [{"text": "x", "activity_ids": "linear:issue:HAR-7"}]}, "focus[0]: not a {text, activity_ids} sentence"),
    ({"summary": [{"text": "A great day.", "activity_ids": []}]}, "summary: not a known section"),
])
def test_red_malformed_briefings_fail(briefing, problem):
    assert problem in check(briefing, sources(context()))


def test_empty_data_is_nothing_to_report_per_section():
    empty = context(world(empty=True))
    assert (empty["focus"], empty["needs_attention"], empty["yesterday"]["commit_count"]) == ([], [], 0)
    assert check(nothing(), sources(empty)) == []
    # invented content fails: none of it is in the tool output
    assert len(check(good(context()), sources(empty))) == 3 + 1 + 6  # one problem per cited id
    # and "nothing to report" is itself a claim, false when the tool returned activity
    ctx = context()
    assert [p.split(":")[0] for p in check(nothing(), sources(ctx))] == ["focus[0]", "needs_attention[0]", "yesterday[0]"]


def test_parse_briefing():
    b = good(context())
    assert parse_briefing("Hi, thanks for connecting!\n```json\n%s\n```" % json.dumps(b)) == b
    assert parse_briefing(json.dumps({"focus": [], "needs_attention": [{"text": "nothing to report", "activity_ids": []}]})) \
        == {"focus": [{"text": NOTHING, "activity_ids": []}], "needs_attention": [{"text": NOTHING, "activity_ids": []}]}
    assert parse_briefing("I can't help with that.") is None
    assert parse_briefing(None) is None


# ---- the workflow definition ----

def test_start_my_day_workflow_shape():
    cfg = workflows.start_my_day_config("srv-1", "agent-1")
    trigger, tool, agent = cfg["nodes"]
    assert cfg["start_node_id"] == trigger["node_id"] and trigger["node_type"] == "trigger"
    # the executor walks connections, the validator wants the same links as edges (HAR-91)
    assert [n.get("connections") for n in cfg["nodes"]] == [[tool["node_id"]], [agent["node_id"]], None]
    assert [(e["source"], e["target"]) for e in cfg["edges"]] == [(trigger["node_id"], tool["node_id"]),
                                                                   (tool["node_id"], agent["node_id"])]
    assert {k: tool["config"][k] for k in ("tool", "mcp_server_id", "mcp_tool_name")} == {
        "tool": "mcp", "mcp_server_id": "srv-1", "mcp_tool_name": "get_my_day_context"}
    assert tool["config"]["mcp_tool_name"] in semantic.HANDLERS  # a name the gateway serves unprefixed
    [mapping] = agent["config"]["input_mappings"]
    # only a bare ${node.field} resolves, and only a message mapping reaches the model (HAR-91)
    assert mapping == {"source_expression": "${%s.content}" % tool["node_id"], "target_field": "message"}
    assert agent["config"]["agent_id"] == "agent-1" and NOTHING in agent["config"]["instructions"]


def test_setup_creates_the_agent_then_the_workflow_over_the_gateway():
    posted = []

    def g8(method, path, body=None):
        posted.append((path, body))
        return 201, {"agent": {"agent_id": "agent-1"}} if path.endswith("/agents") else {"action_id": "wf-1"}

    assert workflows.setup(g8, "srv-1") == ("agent-1", "wf-1")
    assert [p for p, _ in posted] == ["/api/v1/voice/agents", "/api/v1/workflows"]
    assert posted[0][1]["use_company_knowledge"] is False  # no facts from anywhere but the tool
    assert posted[1][1]["config"] == workflows.start_my_day_config("srv-1", "agent-1")


# ---- the trigger, over a scripted Graph8 ----

class FakeGraph8:
    """execute -> pending; the first poll says running, the next one completed, with the day_1 tool content and
    the next scripted agent reply as node results. tool_error: day_1 reports an MCP error instead."""

    def __init__(self, content, replies, tool_error=False):
        self.content, self.replies, self.tool_error, self.calls = content, list(replies), tool_error, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "POST":
            self.polls = 0
            return 200, {"success": True, "execution_id": "ex-%d" % len(self.calls), "status": "pending"}
        self.polls += 1
        if self.polls == 1:
            return 200, {"status": "running"}
        return 200, {"status": "completed", "output_data": {"node_results": {
            "trigger_1": {"status": "completed", "output": {}},
            "day_1": {"status": "completed", "output": {"mcp_server": "reseau-gateway", "tool": "get_my_day_context",
                                                        "content": self.content, "is_error": self.tool_error}},
            "agent_1": {"status": "completed", "output": {"response": self.replies.pop(0)}}}}}

    def executions(self):
        return sum(m == "POST" for m, _ in self.calls)


def test_green_trigger_returns_the_verified_briefing():
    ctx = context()
    g8 = FakeGraph8(json.dumps(ctx), [json.dumps(good(ctx))])
    out = workflows.start_my_day(g8, "wf-1")
    assert (out["execution_id"], out["date"], out["sections"], out["incomplete"]) == ("ex-1", TODAY, good(ctx), [])
    assert g8.calls[0] == ("POST", "/api/v1/workflows/wf-1/execute") and g8.executions() == 1


def test_red_trigger_never_returns_an_unverified_briefing():
    ctx = context()
    bad = copy.deepcopy(good(ctx))
    bad["focus"][0]["activity_ids"] = ["linear:issue:HAR-999"]
    g8 = FakeGraph8(json.dumps(ctx), [json.dumps(bad)] * 2)
    with pytest.raises(WorkflowError) as e:
        workflows.start_my_day(g8, "wf-1")
    assert g8.executions() == 2 and e.value.problems == [
        "focus[0]: cites 'linear:issue:HAR-999', which the tool did not return for focus"]
    # a flaky first reply is retried once
    g8 = FakeGraph8(json.dumps(ctx), [json.dumps(bad), json.dumps(good(ctx))])
    assert workflows.start_my_day(g8, "wf-1")["sections"] == good(ctx) and g8.executions() == 2


def test_empty_day_trigger_says_nothing_to_report():
    g8 = FakeGraph8(json.dumps(context(world(empty=True))), ['{"focus": [], "needs_attention": [], "yesterday": []}'])
    assert workflows.start_my_day(g8, "wf-1")["sections"] == nothing()


def test_a_failed_tool_call_is_an_error_not_a_briefing():
    g8 = FakeGraph8("unhandled errors in a TaskGroup (1 sub-exception)", [json.dumps(nothing())], tool_error=True)
    with pytest.raises(WorkflowError, match="day_1: unhandled errors in a TaskGroup"):
        workflows.start_my_day(g8, "wf-1")
    assert g8.executions() == 1  # not retried: the tool failed, not the model


def test_a_failed_execution_is_an_error():
    def g8(method, path, body=None):
        return 200, {"execution_id": "ex-1"} if method == "POST" else {"status": "failed", "error_message": "boom",
                                                                         "output_data": {"node_results": {}}}
    with pytest.raises(WorkflowError, match=re.escape("execution ex-1 failed: boom")):
        workflows.start_my_day(g8, "wf-1")
