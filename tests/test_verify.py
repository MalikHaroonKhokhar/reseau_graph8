"""The shared verifiers (reseau/verify.py) against the gateway's real tool output for the fixture world:
get_my_day_context for citations (HAR-102), get_team_summary for counts and blockers (HAR-103)."""
import json

import pytest

from reseau import evidence, semantic, verify, workflows
from reseau.verify import NOTHING
from tests.test_semantic import (ENG_142, PR_9, RG, TEAM_PEOPLE, UI, YESTERDAY, FakeGateway, frozen_now,  # noqa: F401
                                 team_world, world)
from tests.test_gateway import run


def as_sent(answer):
    """A semantic tool's answer exactly as the gateway sends it: the result's JSON text, parsed."""
    return json.loads(evidence.as_result(answer).content[0].text)


def context(w=None):
    return as_sent(run(semantic.my_day(FakeGateway(w or world()), {})))


def sources(ctx):
    """What each section may cite: its own part of the tool output; the summary, all of it."""
    return {"summary": ctx} | {s: ctx[s] for s in workflows.SECTIONS if s != "summary"}


def good(ctx):
    """A hand-written briefing citing only what the tool returned, section by section."""
    return {"summary": [{"text": "Start with HAR-7, which HAR-6 blocks, then clear the 2 unresolved review threads "
                                 "on PR #20.", "activity_ids": ["linear:issue:HAR-7", "github:pr:%s#20" % RG]}],
            "focus": [{"text": "UI Critic Phase 3 (Urgent) is blocked by HAR-6 and waits on PR #9.",
                       "activity_ids": ["linear:issue:HAR-7", "linear:issue:HAR-6", "github:pr:%s#9" % UI]}],
            "needs_attention": [{"text": "Your PR #20 has 2 unresolved review threads.",
                                 "activity_ids": ["github:pr:%s#20" % RG]}],
            "yesterday": [{"text": "You made 6 commits across 2 repositories.",
                           "activity_ids": ctx["yesterday"]["activity_ids"]}]}


def nothing(sections=workflows.SECTIONS):
    return {s: [{"text": NOTHING, "activity_ids": []}] for s in sections}


def empty_team():
    w = world(empty=True)
    w["linear"]["list_users"] = lambda a: {"users": [], "hasNextPage": False}
    return w


def team(w=None, day=YESTERDAY):
    """get_team_summary's output as sent. On team_world's YESTERDAY: ENG-142 completed, PR #9 merged, 6 commits,
    and HAR-7 blocked by HAR-6, waiting on PR #9 in ui-critic."""
    return as_sent(run(semantic.team_summary(FakeGateway(w or team_world(), people=TEAM_PEOPLE), {"date": day})))


def good_report(s):
    return {"summary": [{"text": "The team completed 1 issue and merged 1 PR, while HAR-7 stays blocked by HAR-6.",
                         "activity_ids": [ENG_142, PR_9, "linear:issue:HAR-7"]}],
            "completed": [{"text": "Completed: 1 Linear issue.", "activity_ids": [ENG_142]}],
            "merged": [{"text": "Merged: 1 GitHub PR.", "activity_ids": [PR_9]}],
            "commits": [{"text": "Commits: 6.", "activity_ids": s["total"]["commits"]["activity_ids"]}],
            "blocked": [{"text": "UI Critic Phase 3 is blocked by HAR-6 until PR #9 lands.",
                         "activity_ids": ["linear:issue:HAR-7", "linear:issue:HAR-6", "github:pr:%s#9" % UI]}]}


def tallies(s):
    return {k: s["total"][k] for k in workflows.COUNTED}


def with_text(report, section, text, activity_ids=None):
    report[section][0]["text"] = text
    if activity_ids is not None:
        report[section][0]["activity_ids"] = activity_ids
    return report


# ---- citations ----

def test_green_a_briefing_citing_the_tool_output_passes():
    ctx = context()
    assert verify.citations(good(ctx), sources(ctx)) == []


def test_red_a_fabricated_id_fails():
    ctx = context()
    b = good(ctx)
    b["focus"][0]["activity_ids"] = ["linear:issue:HAR-999"]
    assert verify.citations(b, sources(ctx)) == [
        "focus[0]: cites 'linear:issue:HAR-999', which the tool did not return for focus"]


def test_red_an_uncited_sentence_fails():
    ctx = context()
    b = good(ctx)
    b["needs_attention"].append({"text": "Reviewers are waiting on you.", "activity_ids": []})
    assert verify.citations(b, sources(ctx)) == ["needs_attention[1]: no citation: 'Reviewers are waiting on you.'"]


def test_red_an_id_from_another_section_fails():
    ctx = context()  # a real id, but yesterday's facts are commits: citing the focus issue there proves nothing
    b = good(ctx)
    b["yesterday"][0]["activity_ids"] = ["linear:issue:HAR-7"]
    assert verify.citations(b, sources(ctx)) == [
        "yesterday[0]: cites 'linear:issue:HAR-7', which the tool did not return for yesterday"]


def test_red_two_sentences_under_one_citation_fail():
    ctx = context()  # the second sentence would ride on the first one's citation
    b = good(ctx)
    b["needs_attention"][0]["text"] = "Your PR #20 has 2 unresolved review threads. Ship it today."
    assert [p.split(":")[1] for p in verify.citations(b, sources(ctx))] == [" more than one sentence"]


@pytest.mark.parametrize("briefing, problem", [
    (None, "not a JSON object of sections"),
    ({"focus": [], "needs_attention": [], "yesterday": []}, "focus: no sentences"),
    ({"focus": [{"text": "x", "activity_ids": "linear:issue:HAR-7"}]}, "focus[0]: not a {text, activity_ids} sentence"),
    ({"headline": [{"text": "A great day.", "activity_ids": []}]}, "headline: not a known section"),
])
def test_red_malformed_briefings_fail(briefing, problem):
    assert problem in verify.citations(briefing, sources(context()))


def test_empty_data_is_nothing_to_report_per_section():
    empty = context(world(empty=True))
    assert (empty["focus"], empty["needs_attention"], empty["yesterday"]["commit_count"]) == ([], [], 0)
    assert verify.citations(nothing(), sources(empty)) == []
    # invented content fails: none of it is in the tool output
    assert len(verify.citations(good(context()), sources(empty))) == 2 + 3 + 1 + 6  # one problem per cited id
    # and "nothing to report" is itself a claim, false when the tool returned activity
    ctx = context()
    assert [p.split(":")[0] for p in verify.citations(nothing(), sources(ctx))] == [
        "summary[0]", "focus[0]", "needs_attention[0]", "yesterday[0]"]


def test_parse():
    b = good(context())
    assert verify.parse("Hi, thanks for connecting!\n```json\n%s\n```" % json.dumps(b)) == b
    assert verify.parse(json.dumps({"focus": [], "needs_attention": [{"text": "nothing to report", "activity_ids": []}]})) \
        == {"focus": [{"text": NOTHING, "activity_ids": []}], "needs_attention": [{"text": NOTHING, "activity_ids": []}]}
    assert verify.parse("I can't help with that.") is None
    assert verify.parse(None) is None


# ---- counts ----

def test_green_counts_that_match_the_tool_pass():
    s = team()
    assert (s["total"]["completed"]["count"], s["total"]["merged"]["count"], s["total"]["commits"]["count"]) == (1, 1, 6)
    assert verify.counts(good_report(s), tallies(s)) == []


def test_red_merged_2_prs_when_the_tool_says_1_fails():
    s = team()
    r = with_text(good_report(s), "merged", "Merged: 2 GitHub PRs.")
    assert verify.counts(r, tallies(s)) == ["merged[0]: says 2, but the tool counted 1"]


@pytest.mark.parametrize("text, problem", [
    ("Merged: a GitHub PR.", "merged[0]: states no count: 'Merged: a GitHub PR.'"),
    ("Merged: 1 GitHub PR across 2 repositories.", "merged[0]: says 2, but the tool counted 1"),  # an uncounted number
])
def test_red_every_number_must_be_the_count(text, problem):
    s = team()
    assert verify.counts(with_text(good_report(s), "merged", text), tallies(s)) == [problem]


@pytest.mark.parametrize("text", [
    "Merged: 1 GitHub PR, #9.", "Merged: 1 GitHub PR for ENG-142.", "Merged on 2026-09-26: 1 GitHub PR.",
    "Merged: 1 GitHub PR in reseau_graph8.",
])
def test_identifiers_are_not_counts(text):
    s = team()
    assert verify.counts(with_text(good_report(s), "merged", text), tallies(s)) == []


def test_red_a_count_must_cite_all_of_its_evidence():
    s = team()
    ids = s["total"]["commits"]["activity_ids"]
    r = with_text(good_report(s), "commits", "Commits: 6.", ids[:4])
    assert verify.counts(r, tallies(s)) == ["commits[0]: doesn't cite 2 of the 6 activity_ids behind the count: %r" % ids[4:]]


def test_a_count_of_zero_is_nothing_to_report():
    s = team(empty_team())
    assert verify.counts(nothing(workflows.REPORT_SECTIONS), tallies(s)) == []
    # "0 issues" cites nothing, so it is refused as uncited: a zero count says NOTHING
    r = with_text(nothing(workflows.REPORT_SECTIONS), "completed", "Completed: 0 Linear issues.")
    assert verify.counts(r, tallies(s)) == []
    assert workflows.report_problems(r, s) == ["completed[0]: no citation: 'Completed: 0 Linear issues.'"]


def test_counts_skip_what_citations_reports():
    s = team()
    assert verify.counts(None, tallies(s)) == []
    assert verify.counts({"merged": "Merged: 2", "commits": [{"text": "Commits: 9."}]}, tallies(s)) == []


# ---- blockers ----

def test_green_a_blocked_issue_names_its_blocker():
    s = team()
    assert verify.blockers(good_report(s), "blocked", s["blocked"]) == []


@pytest.mark.parametrize("text, cited", [
    ("UI Critic Phase 3 is blocked until PR #9 lands.", None),  # cited, but not named
    ("UI Critic Phase 3 is blocked by HAR-60.", None),  # another issue
    ("UI Critic Phase 3 is blocked by HAR-6.", ["linear:issue:HAR-7"]),  # named, but not cited
])
def test_red_a_blocked_issue_must_name_its_blocker(text, cited):
    s = team()
    r = with_text(good_report(s), "blocked", text, cited)
    assert verify.blockers(r, "blocked", s["blocked"]) == [
        "blocked: linear:issue:HAR-7's sentence doesn't name and cite its blocker linear:issue:HAR-6"]


def test_red_every_blocked_issue_has_a_sentence():
    s = team()
    assert verify.blockers(nothing(workflows.REPORT_SECTIONS), "blocked", s["blocked"]) == [
        "blocked: no sentence for blocked linear:issue:HAR-7"]


# ---- numbers (the summary) ----

def test_counted_is_every_integer_and_list_length():
    assert verify.counted({"a": 3, "b": [{"c": 5, "ok": True}, "x"], "d": []}) == {3, 5, 2, 0}


def test_green_a_summary_states_only_counts_the_tool_returned():
    s = team()
    assert verify.numbers(good_report(s), "summary", verify.counted(s)) == []
    ctx = context()
    assert verify.numbers(good(ctx), "summary", verify.counted(ctx)) == []


def test_red_a_summary_with_an_invented_number_fails():
    s = team()
    r = with_text(good_report(s), "summary", "A strong day: 9 issues completed and 1 PR merged.")
    assert verify.numbers(r, "summary", verify.counted(s)) == ["summary[0]: says 9, which is no count the tool returned"]
