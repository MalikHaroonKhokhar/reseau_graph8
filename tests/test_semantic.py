"""HAR-100: get_person_activity and get_my_day_context. Pure aggregation, both tools over a scripted world of
upstream answers (built from the recorded fixtures), and both tools over MCP through the gateway."""
import copy
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from mcp import Client
from mcp.client.sse import sse_client
from mcp.shared.exceptions import MCPError

from reseau import evidence, semantic
from reseau.evidence import github
from reseau.evidence.records import Actor
from tests.mock_upstream import MockUpstream, scripted_result
from tests.test_evidence import PEOPLE, fixture
from tests.test_front import TOK, serving
from tests.test_gateway import GH_TOKEN, LIN_TOKEN, run

NOW = datetime(2026, 9, 27, 9, tzinfo=timezone.utc)
TODAY, YESTERDAY = "2026-09-27", "2026-09-26"
LOGIN, LINEAR_ID = PEOPLE["dev"]["github"], PEOPLE["dev"]["linear"]
RG, UI = "octo-dev/reseau_graph8", "octo-dev/ui-critic"


# ---- the scripted world: what GitHub and Linear answer, in their recorded shapes ----

def commit(repo, n, committed):
    c = fixture("github_commit")
    c["sha"] = "%040x" % n
    c["html_url"] = "https://github.com/%s/commit/%s" % (repo, c["sha"])
    c["commit"]["author"]["date"] = c["commit"]["committer"]["date"] = committed
    return c


def pr(repo, number, state):
    return fixture("github_pr") | {"number": number, "html_url": "https://github.com/%s/pull/%d" % (repo, number),
                                   "state": state, "merged": state == "closed"}


def search_item(repo, number):
    item = fixture("github_search_prs")["items"][0]
    return item | {"number": number, "html_url": "https://github.com/%s/pull/%d" % (repo, number),
                   "pull_request": {"merged_at": None}}


def threads(repo, number):
    """The recorded page with a third thread; only the first is resolved."""
    page = fixture("github_review_comments")
    page["review_threads"].append(copy.deepcopy(page["review_threads"][1]))
    for k, t in enumerate(page["review_threads"]):
        t["is_resolved"] = k == 0
        for j, cm in enumerate(t["comments"]):
            cm["html_url"] = "https://github.com/%s/pull/%d#discussion_r%d" % (repo, number, 100 * k + j + 1)
    page["pageInfo"] = {"hasNextPage": False}
    return page


def issue(ident, title, state_type, prs=(), blocked_by=()):
    return fixture("linear_issue") | {
        "id": ident, "title": title, "statusType": state_type, "url": "https://linear.app/acme/issue/" + ident,
        "attachments": [{"id": "a-%s" % n, "title": "PR", "subtitle": None, "url": "https://github.com/%s/pull/%d" % (r, n)}
                        for r, n in prs],
        "relations": {"blocks": [], "blockedBy": [{"id": b, "title": b} for b in blocked_by], "relatedTo": []}}


def listed(ident, title, value, name, state_type, updated):
    return {"id": ident, "title": title, "priority": {"value": value, "name": name}, "status": state_type.title(),
            "statusType": state_type, "updatedAt": updated}


def not_found(what):
    raise RuntimeError("failed to get %s: 404 Not Found []" % what)


def world(empty=False):
    """{source: {tool: fn(args) -> payload}}. Searches ignore their date qualifiers, as a mock can, so the
    day filtering under test is Réseau's own."""
    commits = {RG: [commit(RG, n, "2026-09-26T%02d:00:00Z" % (8 + n)) for n in range(4)]
                   + [commit(RG, 9, "2026-09-27T00:30:00Z")],  # today, not yesterday
               UI: [commit(UI, 10, "2026-09-26T00:00:00Z"), commit(UI, 11, "2026-09-26T23:59:59Z")]}
    reviews = fixture("github_reviews")
    reviews[1]["submitted_at"] = "2026-09-27T03:00:00Z"
    done = fixture("linear_issue")
    done["stateHistory"].append({"state": {"id": "s3", "name": "Done", "type": "completed"},
                                 "startedAt": "2026-09-27T05:08:22.331Z", "endedAt": None})
    issues = {"HAR-7": issue("HAR-7", "UI Critic Phase 3", "started", prs=[(RG, 17)], blocked_by=["HAR-6", "HAR-5"]),
              "HAR-6": issue("HAR-6", "UI Critic Phase 2", "started", prs=[(UI, 9)]),
              "HAR-5": issue("HAR-5", "UI Critic Phase 1", "completed", prs=[(UI, 8)]),
              "HAR-98": done}
    focus = {"started": [listed("HAR-8", "Docs pass", 3, "Medium", "started", "2026-09-26T12:00:00Z"),
                         listed("HAR-7", "UI Critic Phase 3", 1, "Urgent", "started", "2026-09-26T10:00:00Z")],
             "unstarted": [listed("HAR-9", "Phase 4 spec", 2, "High", "unstarted", "2026-09-26T11:00:00Z")],
             "backlog": []}
    prs = {(UI, 9): pr(UI, 9, "open"), (RG, 17): pr(RG, 17, "closed"), (UI, 8): pr(UI, 8, "closed")}

    def search_prs(a):
        q = a["query"]
        if q.startswith("author:%s is:open" % LOGIN):
            return {"items": [search_item(RG, 20)]}
        if q.startswith("involves:%s " % LOGIN):
            return {"items": [search_item(UI, 9)]}
        if q.startswith("author:%s " % LOGIN):  # created: and merged: both find #16 and #17
            return fixture("github_search_prs")
        if q.startswith("reviewed-by:%s " % LOGIN):
            return {"items": [{"html_url": "https://github.com/octo-dev/sandbox/pull/13", "number": 13}]}
        return {"items": []}

    def pull_request_read(a):
        key = ("%s/%s" % (a["owner"], a["repo"]), a["pullNumber"])
        if a["method"] == "get":
            return prs.get(key) or not_found("pull request")
        if a["method"] == "get_review_comments":
            return threads(*key) if key == (RG, 20) else {"review_threads": [], "pageInfo": {}}
        return reviews if key == ("octo-dev/sandbox", 13) else []

    def list_issues(a):
        if a.get("assignee") == "me":
            return {"issues": focus[a["state"]], "hasNextPage": False}
        return {"issues": [{"id": "HAR-98"}] if a.get("assignee") == LINEAR_ID else [], "hasNextPage": False}

    gh = {"get_me": lambda a: {"login": LOGIN, "id": 1000001},
          "search_repositories": lambda a: {"items": [{"full_name": RG}] if a["query"].startswith("user:" + LOGIN) else []},
          "search_pull_requests": search_prs,
          "list_commits": lambda a: commits.get("%s/%s" % (a["owner"], a["repo"]), []),
          "pull_request_read": pull_request_read}
    lin = {"list_issues": list_issues,
           "get_issue": lambda a: issues.get(a["id"]) or not_found("issue")}
    if empty:
        gh = {"get_me": gh["get_me"], "search_repositories": lambda a: {"items": []},
              "search_pull_requests": lambda a: {"items": []}}
        lin = {"list_issues": lambda a: {"issues": [], "hasNextPage": False}}
    return {"github": gh, "linear": lin}


class FakeGateway:
    """What the semantic handlers use of Gateway, answering from a scripted world."""

    def __init__(self, w, tz="UTC", people=PEOPLE):
        self.world, self.tz, self.identities, self.calls = w, ZoneInfo(tz), evidence.identity_index(people), []

    async def call_tool(self, source, tool, args):
        self.calls.append((source, tool, args))
        return scripted_result(self.world[source], tool, args)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(semantic, "now", lambda: NOW)


def my_day(w=None, **kw):
    return run(semantic.my_day(FakeGateway(w or world(), **kw), {}))


def person_activity(person, day, w=None, **kw):
    return run(semantic.person_activity(FakeGateway(w or world(), **kw), {"person": person, "date": day}))


# ---- the ticket's test plan ----

def test_red_yesterday_counts_six_commits_across_two_repos():
    y = my_day().yesterday
    assert (y.date, y.commit_count, y.repo_count, y.repos) == (YESTERDAY, 6, 2, [RG, UI])
    assert len(y.activity_ids) == len(set(y.activity_ids)) == 6
    assert all(evidence.parse(i)[:2] == ("github", "commit") for i in y.activity_ids)


def test_priority_ordering_picks_the_top_issue():
    def item(ident, value, state_type, updated="2026-09-26T00:00:00Z"):
        return {"id": ident, "priority": {"value": value}, "statusType": state_type, "updatedAt": updated}

    assert [i["id"] for i in semantic.focus_order([
        item("low", 4, "started"), item("none", 0, "started"), item("done", 1, "completed"),
        item("urgent", 1, "unstarted"), item("high", 2, "started")])] == ["urgent"]
    # ties at the top: started before unstarted, then most recently updated; capped at FOCUS_MAX
    assert [i["id"] for i in semantic.focus_order([
        item("a", 2, "unstarted"), item("b", 2, "started", "2026-09-25T00:00:00Z"),
        item("c", 2, "started", "2026-09-26T00:00:00Z"), item("d", 2, "backlog"), item("e", 3, "started")])] == ["c", "b", "a"]
    assert semantic.focus_order([item("none", 0, "backlog")])[0]["id"] == "none"
    assert semantic.focus_order([item("x", 1, "canceled")]) == []


def test_day_boundary_respects_the_timezone():
    late = semantic.Activity("github:commit:o/r@abcdef1", "commit", "2026-09-26T20:30:00Z", None)  # 01:30 on the 27th in Karachi
    utc = semantic.day_window(date(2026, 9, 26), ZoneInfo("UTC"))
    khi_26 = semantic.day_window(date(2026, 9, 26), ZoneInfo("Asia/Karachi"))
    khi_27 = semantic.day_window(date(2026, 9, 27), ZoneInfo("Asia/Karachi"))
    assert semantic.on_day([late], *utc) == [late]
    assert semantic.on_day([late], *khi_26) == []
    assert semantic.on_day([late], *khi_27) == [late]
    start, end = semantic.day_window(date(2026, 11, 1), ZoneInfo("America/New_York"))  # DST ends
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=25)


def test_yesterday_follows_the_configured_timezone():
    # Karachi's 26th is 19:00Z on the 25th to 19:00Z on the 26th: the 23:59:59Z commit falls on its 27th
    y = my_day(tz="Asia/Karachi").yesterday
    assert (y.date, y.commit_count, y.repo_count) == (YESTERDAY, 5, 2)


def test_red_unmapped_person_is_an_explicit_error():
    gw = FakeGateway(world())
    with pytest.raises(MCPError) as e:
        run(semantic.person_activity(gw, {"person": LOGIN, "date": YESTERDAY}))  # a login, not a mapped person
    assert (e.value.code, e.value.data) == (-32011, {"kind": "unmapped_person", "person": LOGIN})
    assert gw.calls == []


@pytest.mark.parametrize("day", [None, "", "yesterday", "2026-13-01", 20260926])
def test_bad_date_is_invalid_params(day):
    with pytest.raises(MCPError) as e:
        person_activity("dev", day)
    assert (e.value.code, e.value.data["kind"]) == (-32602, "invalid_params")


def test_empty_day_is_empty_lists_not_an_error():
    assert person_activity("dev", YESTERDAY, world(empty=True)).activities == []
    assert person_activity("dev", "2020-01-01").activities == []
    d = my_day(world(empty=True))
    assert (d.focus, d.needs_attention) == ([], [])
    assert (d.yesterday.commit_count, d.yesterday.repo_count, d.yesterday.activity_ids) == (0, 0, [])


# ---- the rest of both tools ----

def test_person_activity_covers_every_kind_in_order():
    out = person_activity("dev", TODAY)
    assert (out.person, out.date, out.timezone) == ("dev", TODAY, "UTC")
    assert [(a.action, a.activity_id, a.detail) for a in out.activities] == [
        ("commit", "github:commit:%s@%040x" % (RG, 9), None),
        ("review", "github:review:octo-dev/sandbox#13/4779069846", "COMMENTED"),
        ("pr_opened", "github:pr:%s#16" % RG, None),
        ("pr_merged", "github:pr:%s#16" % RG, None),
        ("issue_moved", "linear:issue:HAR-98", "In Progress"),
        ("pr_opened", "github:pr:%s#17" % RG, None),
        ("pr_merged", "github:pr:%s#17" % RG, None),
        ("issue_completed", "linear:issue:HAR-98", "Done")]
    for a in out.activities:
        assert a.activity_id == a.record.activity_id and evidence.parse(a.activity_id)
        assert (a.record.actor.person, a.record.actor.identity) == ("dev", "mapped")


def test_person_with_only_a_github_identity_skips_linear():
    gw = FakeGateway(world(), people={"gh-only": {"github": LOGIN}})
    run(semantic.person_activity(gw, {"person": "gh-only", "date": TODAY}))
    assert {source for source, _, _ in gw.calls} == {"github"}


def test_my_day_focus_and_needs_attention():
    d = my_day()
    assert (d.me, d.date, d.timezone) == (Actor("github", LOGIN, person="dev", identity="mapped"), TODAY, "UTC")
    [f] = d.focus  # the only Urgent issue; HAR-9 (High) and HAR-8 (Medium) wait
    assert (f.activity_id, f.record.title, f.priority) == ("linear:issue:HAR-7", "UI Critic Phase 3", "Urgent")
    assert f.blocked_by == ["linear:issue:HAR-6"]  # HAR-5 is done
    # its own PR #17 is merged; it waits on PR #9, attached to its blocker HAR-6
    assert [(p.activity_id, p.via) for p in f.blocking_prs] == [("github:pr:%s#9" % UI, "linear:issue:HAR-6")]
    [a] = d.needs_attention
    assert a.activity_id == "github:pr:%s#20" % RG
    assert [(t.activity_id, t.comment_count) for t in a.unresolved] == [
        ("github:review_comment:%s#20/101" % RG, 2), ("github:review_comment:%s#20/201" % RG, 2)]


def test_pr_activities_from_a_search_result():
    item = fixture("github_search_prs")["items"][0]
    rec = github.pr(item, {}, "t")
    assert [(a.action, a.at) for a in semantic.pr_activities(item, rec)] == [
        ("pr_opened", "2026-09-27T05:04:03Z"), ("pr_merged", "2026-09-27T05:08:20Z")]
    assert [a.action for a in semantic.pr_activities(search_item(RG, 20), rec)] == ["pr_opened"]


@pytest.mark.parametrize("source, tool", [("github", "get_me"), ("github", "list_commits"), ("linear", "get_issue")])
def test_upstream_failure_is_structured_even_inside_a_fan_out(source, tool):
    w = world()
    w[source][tool] = lambda a: (_ for _ in ()).throw(RuntimeError("502 Bad Gateway"))
    with pytest.raises(MCPError) as e:  # not an ExceptionGroup from the concurrent calls
        my_day(w)
    assert (e.value.code, e.value.data["kind"]) == (-32009, "upstream_error")


# ---- integration: both tools over MCP, through the gateway, against scripted mock upstreams ----

def test_green_both_tools_over_mcp_return_schema_valid_output():
    w = world()
    with MockUpstream(GH_TOKEN, stateless=False, script=w["github"]) as gh, \
            MockUpstream(LIN_TOKEN, stateless=True, script=w["linear"]) as lin:
        async def body(base, gw):
            async with Client(sse_client(base + "/g8/%s/sse" % TOK), mode="legacy") as c:
                tools = {t.name: t for t in (await c.list_tools()).tools}
                # the SDK client also validates structured content against the listed output schema
                return tools, (await c.call_tool("get_person_activity", {"person": "dev", "date": TODAY}),
                               await c.call_tool("get_my_day_context", {}))

        tools, results = run(serving((gh, lin), body, identities=PEOPLE))

    for name, res in zip(("get_person_activity", "get_my_day_context"), results):
        assert not res.is_error
        jsonschema.validate(res.structured_content, tools[name].output_schema)
    activity, day = (r.structured_content for r in results)
    assert len(activity["activities"]) == 8
    assert (day["yesterday"]["commit_count"], day["yesterday"]["repo_count"]) == (6, 2)
    assert day["focus"][0]["blocking_prs"][0]["activity_id"] == "github:pr:%s#9" % UI
    assert len(day["needs_attention"][0]["unresolved"]) == 2
