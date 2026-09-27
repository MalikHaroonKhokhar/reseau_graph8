"""HAR-100: get_person_activity and get_my_day_context; HAR-101: get_project_context and get_team_summary; HAR-109:
get_business_context. Pure aggregation, the tools over a scripted world of upstream answers (built from the recorded
fixtures), and the tools over MCP through the gateway."""
import copy
import dataclasses
import json
import random
import re
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
from tests.mock_upstream import G8_COMPANY, G8_DEAL, G8_MEETING, G8_TASK, MockUpstream, scripted_result
from tests.test_evidence import PEOPLE, fixture
from tests.test_front import TOK, serving
from tests.test_gateway import G8_TOKEN, GH_TOKEN, LIN_TOKEN, run

NOW = datetime(2026, 9, 27, 9, tzinfo=timezone.utc)
TODAY, YESTERDAY = "2026-09-27", "2026-09-26"
LOGIN, LINEAR_ID = PEOPLE["dev"]["github"], PEOPLE["dev"]["linear"]
RG, UI = "octo-dev/reseau_graph8", "private-org/ui-critic"  # UI is an org repo: read only once "private-org" is in scope
SCOPE = (LOGIN, "private-org")
TEAM, ANA = "Engineering", "00000000-0000-4000-8000-000000000002"
TEAM_PEOPLE = PEOPLE | {"ana": {"github": "ana-gh", "linear": ANA}}
PROJECTS = {"reseau": {"linear": "Réseau", "repos": [RG, UI]}}


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


def gh_page(items, a):
    """GitHub's numbered pages."""
    k, per = a.get("page", 1), a["perPage"]
    return items[(k - 1) * per:k * per]


def search_page(items, a):
    return {"total_count": len(items), "incomplete_results": False, "items": gh_page(items, a)}


def linear_page(items, a):
    """Linear's cursor pages."""
    k, per = int(a.get("cursor") or 0), a["limit"]
    return {"issues": items[k:k + per], "hasNextPage": k + per < len(items), "cursor": str(k + per)}


def scoped(query, repos):
    """GitHub ORs user:/repo: qualifiers: keep the repos they name."""
    owners, named = set(re.findall(r"\buser:(\S+)", query)), set(re.findall(r"\brepo:(\S+)", query))
    return [r for r in repos if r.split("/")[0] in owners or r in named]


def world(empty=False):
    """{source: {tool: fn(args) -> payload}}, paged like the real upstreams. Date qualifiers and since/until
    are ignored, as a mock can, so the day filtering under test is Réseau's own."""
    main = [commit(RG, n, "2026-09-26T%02d:00:00Z" % (8 + n)) for n in range(3)] + [
        commit(RG, 9, "2026-09-27T00:30:00Z")]  # today, not yesterday
    commits = {(RG, "main"): main,
               (RG, "feat/x"): [main[0], commit(RG, 3, "2026-09-26T11:00:00Z")],  # unmerged; main[0] is shared
               (UI, "main"): [commit(UI, 10, "2026-09-26T00:00:00Z"), commit(UI, 11, "2026-09-26T23:59:59Z")]}
    branches = {RG: ["main", "feat/x"], UI: ["main"]}
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
            found = [search_item(RG, 20)]
        elif q.startswith("author:%s " % LOGIN):  # created: and merged: both find #16 and #17
            found = fixture("github_search_prs")["items"]
        elif q.startswith("reviewed-by:%s " % LOGIN):
            found = [{"html_url": "https://github.com/octo-dev/sandbox/pull/13", "number": 13}]
        else:
            found = []
        return search_page([p for p in found if scoped(q, [github.repo(p["html_url"])])], a)

    def pull_request_read(a):
        key = ("%s/%s" % (a["owner"], a["repo"]), a["pullNumber"])
        if a["method"] == "get":
            return prs.get(key) or not_found("pull request")
        if a["method"] == "get_review_comments":
            return threads(*key) if key == (RG, 20) else {"review_threads": [], "pageInfo": {}}
        return gh_page(reviews if key == ("octo-dev/sandbox", 13) else [], a)

    def list_issues(a):
        if a.get("assignee") == "me":
            return linear_page(focus[a["state"]], a)
        return linear_page([{"id": "HAR-98"}] if a.get("assignee") == LINEAR_ID else [], a)

    repo = lambda a: "%s/%s" % (a["owner"], a["repo"])
    gh = {"get_me": lambda a: {"login": LOGIN, "id": 1000001},
          "search_repositories": lambda a: search_page([{"full_name": r} for r in scoped(a["query"], [RG, UI])], a),
          "search_pull_requests": search_prs,
          "list_branches": lambda a: gh_page([{"name": b, "sha": "0" * 40, "protected": False}
                                              for b in branches.get(repo(a), [])], a),
          "list_commits": lambda a: gh_page(commits.get((repo(a), a["sha"]), []), a),
          "pull_request_read": pull_request_read}
    lin = {"list_issues": list_issues,
           "get_issue": lambda a: issues.get(a["id"]) or not_found("issue")}
    if empty:
        gh = {"get_me": gh["get_me"], "search_repositories": lambda a: search_page([], a),
              "search_pull_requests": lambda a: search_page([], a)}
        lin = {"list_issues": lambda a: linear_page([], a)}
    return {"github": gh, "linear": lin}


class FakeGateway:
    """What the semantic handlers use of Gateway, answering from a scripted world."""

    def __init__(self, w, tz="UTC", people=PEOPLE, scope=SCOPE, projects=PROJECTS, team=TEAM):
        self.world, self.tz, self.identities, self.calls = w, ZoneInfo(tz), evidence.identity_index(people), []
        self.github_scope, self.projects, self.team = scope, projects, team

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
    assert (y.date, y.commit_count, y.repo_count, y.repos) == (YESTERDAY, 6, 2, sorted([RG, UI]))
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
    assert (out.person, out.date, out.timezone, out.github_scope, out.incomplete) == (
        "dev", TODAY, "UTC", [LOGIN, "private-org"], [])
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
    assert d.incomplete == []
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


# ---- review fixes: commit coverage, GitHub scope as a permission, pagination ----

def test_unmerged_feature_branch_commits_count_once():
    ids = my_day().yesterday.activity_ids
    assert "github:commit:%s@%040x" % (RG, 3) in ids  # only on feat/x
    assert ids.count("github:commit:%s@%040x" % (RG, 0)) == 1  # on main and feat/x


def test_red_org_repos_are_never_read_without_permission():
    gw = FakeGateway(world(), scope=(LOGIN,))
    d = run(semantic.my_day(gw, {}))
    assert (d.github_scope, d.yesterday.commit_count, d.yesterday.repos) == ([LOGIN], 4, [RG])
    assert all("private-org" not in json.dumps(args) for _, _, args in gw.calls)  # not fetched, not even searched for
    assert "private-org" not in json.dumps(dataclasses.asdict(d))  # and not shown
    assert d.focus[0].blocking_prs == []  # PR #9 is in private-org
    assert [(g.reason, g.detail) for g in d.incomplete] == [
        ("out_of_scope", "HAR-7: 1 linked PR(s) outside the GitHub scope were not read")]


def test_search_results_outside_the_scope_are_dropped():
    w = world()  # a search that ignores its qualifiers still can't widen the scope
    w["github"]["search_repositories"] = lambda a: search_page([{"full_name": RG}, {"full_name": UI}], a)
    gw = FakeGateway(w, scope=(LOGIN,))
    assert run(semantic.my_day(gw, {})).yesterday.repos == [RG]
    assert not [args for _, tool, args in gw.calls if args.get("owner") == "private-org"]


def test_empty_scope_reads_no_github_repo():
    gw = FakeGateway(world(), scope=())
    d = run(semantic.my_day(gw, {}))
    assert (d.github_scope, d.needs_attention, d.yesterday.commit_count) == ([], [], 0)
    assert [tool for source, tool, _ in gw.calls if source == "github"] == ["get_me"]  # never an unscoped search


@pytest.mark.parametrize("scope, owner, repo, ok", [
    ((LOGIN,), "Octo-Dev", "anything", True), ((LOGIN,), "private-org", "ui-critic", False),
    (("private-org/ui-critic",), "private-org", "ui-critic", True), (("private-org/ui-critic",), "private-org", "x", False),
    (("private-org/ui-critic",), "private-org", None, False), ((), LOGIN, "reseau_graph8", False)])
def test_in_scope(scope, owner, repo, ok):
    assert semantic.in_scope(scope, owner, repo) is ok


@pytest.mark.parametrize("value, scope", [("", ()), (" private-org , octo-dev/sandbox ", ("private-org", "octo-dev/sandbox"))])
def test_scope_config(value, scope):
    assert semantic.load_scope({"RESEAU_GITHUB_SCOPE": value}) == scope


@pytest.mark.parametrize("value", ["private-org/app/x", "user:private-org", "private-org app"])
def test_scope_config_rejects_malformed_entries(value):
    with pytest.raises(ValueError, match="RESEAU_GITHUB_SCOPE"):
        semantic.load_scope({"RESEAU_GITHUB_SCOPE": value})


def many_commits(n):
    w = world()
    found = [commit(RG, 1000 + k, "2026-09-26T12:00:%02dZ" % (k % 60)) for k in range(n)]
    w["github"]["list_commits"] = lambda a: gh_page(found if (a["repo"], a["sha"]) == ("reseau_graph8", "main") else [], a)
    return w


def test_red_pagination_counts_all_101_commits():
    d = my_day(many_commits(101))
    assert (d.yesterday.commit_count, d.incomplete) == (101, [])


def test_page_limit_is_reported_not_silent(monkeypatch):
    monkeypatch.setattr(semantic, "MAX_PAGES", 1)
    d = my_day(many_commits(101))
    assert d.yesterday.commit_count == 100
    [gap] = d.incomplete
    assert (gap.source, gap.tool, gap.reason) == ("github", "list_commits", "page_limit")
    assert "reseau_graph8" in gap.detail and "'sha': 'main'" in gap.detail


def test_focus_reads_every_page_of_issues():
    w = world()  # the only Urgent issue comes after 150 Low ones
    low = [listed("HAR-%d" % (200 + k), "Chore", 4, "Low", "started", "2026-09-26T12:00:00Z") for k in range(150)]
    urgent = listed("HAR-7", "UI Critic Phase 3", 1, "Urgent", "started", "2026-09-20T00:00:00Z")
    w["linear"]["list_issues"] = lambda a: linear_page(low + [urgent] if a.get("state") == "started" else [], a)
    assert [f.activity_id for f in my_day(w).focus] == ["linear:issue:HAR-7"]


def test_search_github_marks_incomplete_is_reported():
    w = world()
    w["github"]["search_pull_requests"] = lambda a: {"incomplete_results": True, "items": []}
    assert {(g.tool, g.reason) for g in my_day(w).incomplete} == {("search_pull_requests", "search_incomplete")}


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

        tools, results = run(serving((gh, lin), body, identities=PEOPLE,
                                     env={"RESEAU_GITHUB_SCOPE": "%s,private-org" % LOGIN}))

    for name, res in zip(("get_person_activity", "get_my_day_context"), results):
        assert not res.is_error
        jsonschema.validate(res.structured_content, tools[name].output_schema)
    activity, day = (r.structured_content for r in results)
    assert len(activity["activities"]) == 8
    assert (activity["github_scope"], activity["incomplete"], day["incomplete"]) == ([LOGIN, "private-org"], [], [])
    assert (day["yesterday"]["commit_count"], day["yesterday"]["repo_count"]) == (6, 2)
    assert day["focus"][0]["blocking_prs"][0]["activity_id"] == "github:pr:%s#9" % UI
    assert len(day["needs_attention"][0]["unresolved"]) == 2


# ---- HAR-101: get_project_context and get_team_summary ----

ENG_142, PR_9 = "linear:issue:ENG-142", "github:pr:%s#9" % RG


def done(ident, completed, assignee):
    """A completed issue as list_issues returns it with COMPLETED_FIELDS."""
    return {"id": ident, "title": "Ship " + ident, "url": "https://linear.app/acme/issue/" + ident, "status": "Done",
            "createdBy": "Dev Person", "createdById": LINEAR_ID, "createdAt": "2026-09-01T00:00:00.000Z",
            "updatedAt": completed, "completedAt": completed, "assigneeId": assignee}


def merged_item(repo, number, merged_at, login):
    item = search_item(repo, number)
    return item | {"user": item["user"] | {"login": login}, "pull_request": {"merged_at": merged_at}}


def team_world():
    """world() plus a team and a project. Yesterday dev completed ENG-142 and merged PR #9; everything else is
    what the tools must leave out: another day, an unmapped member, no assignee, someone outside the team."""
    w = world()
    gh, lin = w["github"], w["linear"]
    completed = [done("ENG-142", "2026-09-26T15:00:00.000Z", LINEAR_ID),
                 done("ENG-141", "2026-09-26T16:00:00.000Z", "u-sam"),  # an unmapped member's
                 done("ENG-140", "2026-09-26T17:00:00.000Z", None),  # nobody's
                 done("ENG-139", "2026-09-25T12:00:00.000Z", LINEAR_ID),  # the day before: still recent
                 done("ENG-100", "2026-09-10T12:00:00.000Z", LINEAR_ID)]  # not even recent
    opened = {"started": [listed("HAR-8", "Docs pass", 3, "Medium", "started", "2026-09-26T12:00:00Z"),
                          listed("HAR-7", "UI Critic Phase 3", 1, "Urgent", "started", "2026-09-26T10:00:00Z")],
              "unstarted": [listed("HAR-9", "Phase 4 spec", 2, "High", "unstarted", "2026-09-26T11:00:00Z")],
              "backlog": []}
    merged = [merged_item(RG, 9, "2026-09-26T14:00:00Z", LOGIN),
              merged_item("octo-dev/sandbox", 30, "2026-09-26T14:30:00Z", "stranger")]
    stranger = commit(UI, 12, "2026-09-26T12:00:00Z") | {"author": {"login": "stranger"}}
    extra = {"HAR-8": issue("HAR-8", "Docs pass", "started", prs=[(RG, 20)]),  # waits on its own open PR, blocked by nothing
             "HAR-9": issue("HAR-9", "Phase 4 spec", "unstarted")}
    list_issues, get_issue, list_commits = lin["list_issues"], lin["get_issue"], gh["list_commits"]
    search_prs, pull_request_read = gh["search_pull_requests"], gh["pull_request_read"]

    def issues(a):
        if "team" in a or "project" in a:
            return linear_page(completed if a["state"] == "completed" else opened[a["state"]], a)
        return list_issues(a)

    def list_users(a):
        if a["team"] != TEAM:
            raise RuntimeError("Team not found")  # Linear's live answer
        return {"users": [{"id": LINEAR_ID, "name": "Dev Person"}, {"id": ANA, "name": "Ana"},
                          {"id": "u-sam", "name": "Sam"}], "hasNextPage": False}

    def search(a):
        q = a["query"]
        found = merged if q.startswith("merged:") else [search_item(RG, 20), search_item(UI, 9)] \
            if q.startswith("is:open") else None
        return search_prs(a) if found is None else search_page(
            [p for p in found if scoped(q, [github.repo(p["html_url"])])], a)

    def read(a):
        if (a["method"], a["repo"], a["pullNumber"]) == ("get", "reseau_graph8", 20):
            return pr(RG, 20, "open")
        return pull_request_read(a)

    lin |= {"list_issues": issues, "list_users": list_users, "get_issue": lambda a: extra.get(a["id"]) or get_issue(a)}
    gh |= {"search_pull_requests": search, "pull_request_read": read, "list_commits": lambda a: list_commits(a) + (
        [stranger] if (a["repo"], a["sha"], a.get("page", 1), a.get("author")) == ("ui-critic", "main", 1, None) else [])}
    return w


def team_summary(day=YESTERDAY, w=None, **kw):
    return run(semantic.team_summary(FakeGateway(w or team_world(), **{"people": TEAM_PEOPLE} | kw), {"date": day}))


def project_context(project="reseau", w=None, **kw):
    return run(semantic.project_context(FakeGateway(w or team_world(), **{"people": TEAM_PEOPLE} | kw),
                                        {"project": project}))


def counts(value):
    """Every {count, activity_ids} anywhere in an answer."""
    if isinstance(value, dict):
        return ([value] if "count" in value else []) + [c for v in value.values() for c in counts(v)]
    return [c for v in value for c in counts(v)] if isinstance(value, list) else []


def test_red_team_day_counts_one_completed_issue_and_one_merged_pr():
    total = team_summary().total
    assert total.completed == semantic.Count(1, [ENG_142])
    assert total.merged == semantic.Count(1, [PR_9])


def test_team_summary_per_person():
    s = team_summary()
    assert (s.team, s.date, s.timezone, s.github_scope, s.incomplete) == (TEAM, YESTERDAY, "UTC", [LOGIN, "private-org"], [])
    assert s.total.commits.count == 6  # yesterday's six; the stranger's commit is not the team's
    assert s.people == {"ana": semantic.Tally(*[semantic.Count(0, [])] * 3), "dev": s.total}
    assert s.unmapped == [Actor("linear", "u-sam", "Sam")]  # listed, and ENG-141 is not counted


def test_counts_always_equal_their_evidence():
    empty = world(empty=True)
    empty["linear"]["list_users"] = lambda a: {"users": [], "hasNextPage": False}
    answers = [team_summary(), team_summary(tz="Asia/Karachi"), team_summary(TODAY), team_summary(w=empty)]
    for s in answers:
        found = counts(dataclasses.asdict(s))
        assert len(found) == 3 * (1 + len(s.people))
        for c in found:
            assert c["count"] == len(c["activity_ids"]) == len(set(c["activity_ids"]))
    # and for any mix of activity: duplicates, people outside the team, nobody
    rng = random.Random(101)
    for _ in range(200):
        credited = [(rng.choice(["dev", "ana", "sam", None]),
                     semantic.Activity("github:commit:o/r@%07x" % rng.randrange(20) if action == "commit" else
                                       "linear:issue:ENG-%d" % rng.randrange(20) if action == "issue_completed" else
                                       "github:pr:o/r#%d" % rng.randrange(20),
                                       action, "2026-09-26T%02d:00:00Z" % rng.randrange(24), None))
                    for action in rng.choices(["commit", "issue_completed", "pr_merged"], k=rng.randrange(30))]
        total, people = semantic.team_tally(credited, ["ana", "dev"])
        for c in counts(dataclasses.asdict(total)) + counts([dataclasses.asdict(t) for t in people.values()]):
            assert c["count"] == len(c["activity_ids"]) == len(set(c["activity_ids"]))
        for field in ("completed", "merged", "commits"):  # the total is exactly the members' activity
            assert set(getattr(total, field).activity_ids) == {
                i for t in people.values() for i in getattr(t, field).activity_ids}


def test_blocked_from_linear_relations():
    s = team_summary()
    [b] = s.blocked  # HAR-8 and HAR-9 have no blocker
    assert (b.activity_id, b.blocked_by) == ("linear:issue:HAR-7", ["linear:issue:HAR-6"])  # HAR-5 is done
    assert [(p.activity_id, p.via) for p in b.blocking_prs] == [("github:pr:%s#9" % UI, "linear:issue:HAR-6")]


def test_team_reads_everyone_once_and_no_pr_of_an_unblocked_issue():
    gw = FakeGateway(team_world(), people=TEAM_PEOPLE)
    run(semantic.team_summary(gw, {"date": YESTERDAY}))
    assert all("author" not in args for _, tool, args in gw.calls if tool == "list_commits")
    assert (RG, 20) not in {("%s/%s" % (a["owner"], a["repo"]), a["pullNumber"])
                            for _, tool, a in gw.calls if tool == "pull_request_read"}  # HAR-8's own PR


def test_team_errors_are_structured():
    with pytest.raises(MCPError) as e:
        team_summary(team=None)
    assert (e.value.code, e.value.data["kind"]) == (-32014, "team_not_configured")
    with pytest.raises(MCPError) as e:
        team_summary("26/09/2026")
    assert (e.value.code, e.value.data["kind"]) == (-32602, "invalid_params")
    with pytest.raises(MCPError) as e:
        team_summary(team="Nope")
    assert (e.value.code, e.value.data["kind"]) == (-32009, "upstream_error")
    assert "Team not found" in e.value.message


def test_project_context():
    c = project_context()
    assert (c.project, c.linear_project, c.repos, c.since, c.incomplete) == (
        "reseau", "Réseau", [RG, UI], "2026-09-20T09:00:00Z", [])
    assert [i.activity_id for i in c.open] == ["linear:issue:HAR-9"]
    assert [i.activity_id for i in c.in_progress] == ["linear:issue:HAR-7", "linear:issue:HAR-8"]  # Urgent first
    [b] = c.blocked
    assert (b.activity_id, b.blocked_by) == ("linear:issue:HAR-7", ["linear:issue:HAR-6"])
    assert [(p.activity_id, p.via) for p in b.blocking_prs] == [("github:pr:%s#9" % UI, "linear:issue:HAR-6")]
    assert [(p.activity_id, p.via) for p in c.in_progress[1].blocking_prs] == [
        ("github:pr:%s#20" % RG, "linear:issue:HAR-8")]
    assert [(a.action, a.activity_id) for a in c.open_prs] == [
        ("pr_opened", "github:pr:%s#20" % RG), ("pr_opened", "github:pr:%s#9" % UI)]
    # the last 7 days, oldest first: not ENG-100, and not sandbox#30, which is outside the project's repos
    assert [(a.action, a.activity_id) for a in c.recent] == [
        ("issue_completed", "linear:issue:ENG-139"), ("pr_merged", PR_9), ("issue_completed", ENG_142),
        ("issue_completed", "linear:issue:ENG-141"), ("issue_completed", "linear:issue:ENG-140")]


def test_every_project_item_carries_an_activity_id():
    c = dataclasses.asdict(project_context())
    items = [i for key in ("open", "in_progress", "blocked", "open_prs", "recent") for i in c[key]]
    assert items and all(evidence.parse(i["activity_id"]) and i["record"]["activity_id"] == i["activity_id"]
                         for i in items)


def test_red_unknown_project_is_a_structured_error():
    gw = FakeGateway(team_world())
    with pytest.raises(MCPError) as e:
        run(semantic.project_context(gw, {"project": "nope"}))
    assert (e.value.code, e.value.data) == (-32013, {"kind": "unknown_project", "project": "nope", "known": ["reseau"]})
    assert gw.calls == []


def test_project_without_repos_never_searches_github():
    gw = FakeGateway(team_world(), projects={"docs": {"linear": "Réseau", "repos": []}})
    c = run(semantic.project_context(gw, {"project": "docs"}))
    assert (c.open_prs, [a.action for a in c.recent].count("pr_merged")) == ([], 0)
    assert not [tool for _, tool, _ in gw.calls if tool.startswith("search_")]  # never the whole scope


def test_projects_config(tmp_path):
    path = tmp_path / "projects.json"

    def load(projects, scope=SCOPE):
        path.write_text(json.dumps(projects))
        return semantic.load_projects({"RESEAU_PROJECTS": str(path)}, scope)

    assert semantic.load_projects({}) == {}
    assert load(PROJECTS) == PROJECTS
    for bad in ({"x": {"linear": "X", "repos": ["private-org"]}}, {"x": {"repos": [RG]}}, {"x": [RG]},
                {"x": {"linear": "X", "repos": RG}}):
        with pytest.raises(ValueError, match="RESEAU_PROJECTS"):
            load(bad)
    with pytest.raises(ValueError, match="inside RESEAU_GITHUB_SCOPE"):  # a project can't widen the scope
        load(PROJECTS, scope=(LOGIN,))


def test_green_project_and_team_over_mcp_return_schema_valid_output(tmp_path):
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps(PROJECTS))
    w = team_world()
    with MockUpstream(GH_TOKEN, stateless=False, script=w["github"]) as gh, \
            MockUpstream(LIN_TOKEN, stateless=True, script=w["linear"]) as lin:
        async def body(base, gw):
            async with Client(sse_client(base + "/g8/%s/sse" % TOK), mode="legacy") as c:
                tools = {t.name: t for t in (await c.list_tools()).tools}
                return tools, (await c.call_tool("get_project_context", {"project": "reseau"}),
                               await c.call_tool("get_team_summary", {"date": YESTERDAY}))

        tools, results = run(serving((gh, lin), body, identities=TEAM_PEOPLE, env={
            "RESEAU_GITHUB_SCOPE": "%s,private-org" % LOGIN, "RESEAU_PROJECTS": str(projects), "RESEAU_TEAM": TEAM}))

    for name, res in zip(("get_project_context", "get_team_summary"), results):
        assert not res.is_error
        jsonschema.validate(res.structured_content, tools[name].output_schema)
    project, team = (r.structured_content for r in results)
    assert [i["activity_id"] for i in project["blocked"]] == ["linear:issue:HAR-7"]
    assert len(project["recent"]) == 5 and project["incomplete"] == []
    assert team["total"]["completed"] == {"count": 1, "activity_ids": [ENG_142]}
    assert team["total"]["merged"] == {"count": 1, "activity_ids": [PR_9]}
    assert team["blocked"][0]["blocking_prs"][0]["activity_id"] == "github:pr:%s#9" % UI


# ---- HAR-109: get_business_context ----

PR_15 = "github:pr:%s#15" % RG  # tests/fixtures/github_pr.json
OPP, CUSTOMER = "graph8:opportunity:" + G8_DEAL, "graph8:customer:%d" % G8_COMPANY
COMMITMENT, MEETING = "graph8:commitment:" + G8_TASK, "graph8:conversation:meeting/" + G8_MEETING
DEAL_2, TASK_2, TASK_3 = (G8_DEAL[:-12] + "000000000002", G8_TASK[:-12] + "000000000002",
                          G8_TASK[:-12] + "000000000003")


def g8_page(items, a):
    """Graph8's offset pages."""
    k, per = a.get("offset", 0), a["limit"]
    return {"tasks": items[k:k + per], "total": len(items), "limit": per, "offset": k, "has_next": k + per < len(items)}


def lookup(table, key, missing):
    """table[key], or Graph8's live answer for a record it doesn't have (HAR-108's probe)."""
    if key not in table:
        raise RuntimeError(missing)
    return table[key]


def business_world(description="", tasks=None, attachments=((RG, 15),), pr_fields=None, deals=(), companies=()):
    """Linear issue ENG-142 with this description, PR #15 (titled with ENG-142's key, and attached to it) and
    Graph8: task G8_TASK, created from ENG-142 (its source_url), linked to deal G8_DEAL of company 4242."""
    tasks = [fixture("graph8_task")] if tasks is None else tasks
    deals = {G8_DEAL: fixture("graph8_deal"), **dict(deals)}
    companies = {G8_COMPANY: fixture("graph8_company"), **dict(companies)}
    eng = issue("ENG-142", "Ship SSO", "started", prs=attachments) | {"description": description}
    pr15 = fixture("github_pr") | {"title": "ENG-142: ship SSO", "body": None} | (pr_fields or {})

    def pull_request_read(a):
        if (a["method"], a["owner"].casefold(), a["repo"].casefold(), a["pullNumber"]) == ("get", *RG.split("/"), 15):
            return pr15
        return not_found("pull request")

    return {"github": {"pull_request_read": pull_request_read},
            "linear": {"get_issue": lambda a: eng if a["id"] == "ENG-142" else not_found("issue")},
            "graph8": {"g8_get_tasks": lambda a: g8_page(tasks, a),
                       "g8_get_deal": lambda a: lookup(deals, a["deal_id"], "Error: Deal: Deal not found"),
                       "g8_crm_get_company": lambda a: lookup(companies, a["company_id"],
                                                              "Error: g8_crm_get_company: Company not found"),
                       "g8_get_task": lambda a: lookup({t["id"]: t for t in tasks}, a["task_id"],
                                                       "Task: Error: Task lookup: Task not found"),
                       "g8_get_meeting": lambda a: lookup({G8_MEETING: fixture("graph8_meeting")}, a["meeting_id"],
                                                          "Error: API error (400): Invalid meeting ID")}}


def business_context(aid, w=None, **kw):
    return run(semantic.business_context(FakeGateway(w or business_world(), **kw), {"activity_id": aid}))


def links(ctx):
    return [(link.activity_id, link.link_type, link.via) for link in ctx.links]


def test_red_issue_naming_an_opportunity_links_to_it_by_explicit_reference():
    ctx = business_context(ENG_142, business_world("Blocks the renewal: graph8:opportunity:%s." % G8_DEAL, tasks=[]))
    assert links(ctx) == [(CUSTOMER, "graph8_link", OPP), (OPP, "explicit_reference", ENG_142)]
    assert (ctx.activity_id, ctx.linked_issues, ctx.reason, ctx.incomplete) == (ENG_142, [], None, [])
    assert [(link.record.kind, link.record.title) for link in ctx.links] == [
        ("customer", "Example Customer Co"), ("opportunity", "Example Customer Co - annual plan")]


def test_task_created_from_the_issue_is_its_commitment_and_leads_to_the_deal_and_customer():
    gw = FakeGateway(business_world())
    ctx = run(semantic.business_context(gw, {"activity_id": ENG_142}))
    assert links(ctx) == [(CUSTOMER, "graph8_link", OPP), (OPP, "graph8_link", COMMITMENT),
                          (COMMITMENT, "source_url", ENG_142)]
    assert ctx.links[2].record.actor.person == "dev"  # the task's assignee, through the identity map
    assert "g8_get_task" not in [tool for _, tool, _ in gw.calls]  # already listed, not fetched again


def test_direct_link_wins_and_each_record_is_read_once():
    gw = FakeGateway(business_world("graph8:opportunity:" + G8_DEAL))
    ctx = run(semantic.business_context(gw, {"activity_id": ENG_142}))
    assert links(ctx) == [(CUSTOMER, "graph8_link", OPP), (OPP, "explicit_reference", ENG_142),
                          (COMMITMENT, "source_url", ENG_142)]
    assert [tool for _, tool, _ in gw.calls].count("g8_get_deal") == 1


def test_commitment_from_a_meeting_links_the_conversation():
    ctx = business_context(ENG_142, business_world(tasks=[fixture("graph8_task") | {"source_meeting_id": G8_MEETING}]))
    assert [link.record.kind for link in ctx.links] == ["customer", "opportunity", "commitment", "conversation"]
    assert links(ctx)[-1] == (MEETING, "graph8_link", COMMITMENT)


def test_red_no_link_is_an_explicit_empty_result_and_nothing_is_guessed():
    task = fixture("graph8_task")
    near_misses = [task | {"source_url": "https://linear.app/example/issue/ENG-1420"},  # another issue
                   task | {"id": TASK_2, "source_url": None, "title": "ENG-142: Ship SSO"},  # named in its title only
                   task | {"id": TASK_3, "entity_type": None, "entity_id": None, "links": []}]  # no deal or company
    gw = FakeGateway(business_world("For Example Customer Co", tasks=near_misses))  # the customer's name, no ID
    ctx = run(semantic.business_context(gw, {"activity_id": ENG_142}))
    assert (ctx.links, ctx.reason, ctx.incomplete) == ([], "no_link_found", [])
    assert [tool for source, tool, _ in gw.calls if source == "graph8"] == ["g8_get_tasks"]


def test_red_pr_links_through_the_issue_it_is_attached_to():
    ctx = business_context(PR_15)
    assert (ctx.activity_id, ctx.linked_issues) == (PR_15, [ENG_142])
    assert links(ctx) == [(CUSTOMER, "graph8_link", OPP), (OPP, "graph8_link", COMMITMENT),
                          (COMMITMENT, "source_url", ENG_142)]


def test_pr_naming_an_issue_it_is_not_attached_to_has_no_link():
    ctx = business_context(PR_15, business_world(attachments=()))
    assert (ctx.linked_issues, ctx.links, ctx.reason) == ([], [], "no_link_found")


def test_pr_issue_key_can_come_from_its_branch():
    w = business_world(pr_fields={"title": "Ship SSO", "head": {"ref": "dev/eng-142-sso"}})
    assert business_context(PR_15, w).linked_issues == [ENG_142]


def test_task_created_from_the_pr_links_it_directly():
    task = fixture("graph8_task") | {"source_url": "https://github.com/Octo-Dev/Reseau_Graph8/pull/15/files"}
    ctx = business_context("github:pr:octo-dev/RESEAU_GRAPH8#15", business_world(tasks=[task], attachments=()))
    assert (ctx.activity_id, ctx.linked_issues) == (PR_15, [])
    assert links(ctx)[-1] == (COMMITMENT, "source_url", PR_15)


def test_several_customers_come_back_in_a_stable_order():
    second = fixture("graph8_task") | {"id": TASK_2, "entity_id": DEAL_2,
                                       "links": [{"entity_type": "deal", "entity_id": DEAL_2}]}
    deals = {DEAL_2: fixture("graph8_deal") | {"id": DEAL_2, "name": "Second Co - pilot", "company_id": 99}}
    companies = {99: {"data": fixture("graph8_company")["data"] | {"id": 99, "name": "Second Co"}}}
    found = [links(business_context(ENG_142, business_world(tasks=tasks, deals=deals, companies=companies)))
             for tasks in ([fixture("graph8_task"), second], [second, fixture("graph8_task")])]
    assert found[0] == found[1]
    assert [aid for aid, _, _ in found[0]] == [CUSTOMER, "graph8:customer:99", "graph8:opportunity:" + DEAL_2, OPP,
                                               "graph8:commitment:" + TASK_2, COMMITMENT]


def test_tasks_are_read_across_pages_and_a_page_limit_is_reported(monkeypatch):
    others = [fixture("graph8_task") | {"id": G8_TASK[:-12] + "%012d" % k, "source_url": None} for k in range(150)]
    w = business_world(tasks=others + [fixture("graph8_task")])
    assert links(business_context(ENG_142, w))[-1] == (COMMITMENT, "source_url", ENG_142)
    monkeypatch.setattr(semantic, "MAX_PAGES", 1)
    ctx = business_context(ENG_142, w)
    assert (ctx.links, ctx.reason) == ([], "no_link_found")
    assert [(g.source, g.tool, g.reason) for g in ctx.incomplete] == [("graph8", "g8_get_tasks", "page_limit")]


def test_reference_to_a_missing_malformed_or_unlinked_record_is_reported_not_linked():
    gone = G8_DEAL[:-12] + "00000000dead"
    unlinked = fixture("graph8_task") | {"id": TASK_2, "entity_type": None, "entity_id": None, "links": []}
    w = business_world("graph8:opportunity:%s graph8:deal:%s graph8:commitment:%s" % (gone, G8_DEAL, TASK_2),
                       tasks=[unlinked])
    ctx = business_context(ENG_142, w)
    assert (ctx.links, ctx.reason) == ([], "no_link_found")
    assert sorted((g.reason, g.detail.split(" ")[0]) for g in ctx.incomplete) == [
        ("invalid_activity_id", "graph8:deal:" + G8_DEAL), ("not_found", "graph8:commitment:" + TASK_2),
        ("not_found", "graph8:opportunity:" + gone)]


@pytest.mark.parametrize("aid", ["github:commit:%s@%040x" % (RG, 1), OPP, "ENG-142", None])
def test_only_an_issue_or_a_pr_is_accepted(aid):
    gw = FakeGateway(business_world())
    with pytest.raises(MCPError) as e:
        run(semantic.business_context(gw, {"activity_id": aid}))
    assert (e.value.code, e.value.data["kind"]) == (-32602, "invalid_activity_id")
    assert gw.calls == []


def test_unknown_work_item_is_not_found():
    with pytest.raises(MCPError) as e:
        business_context("linear:issue:ENG-9")
    assert (e.value.code, e.value.data) == (-32008, {"kind": "not_found", "activity_id": "linear:issue:ENG-9"})


def test_green_business_context_over_mcp_through_the_shipped_graph8_upstream():
    w = business_world("graph8:opportunity:" + G8_DEAL)
    w["graph8"]["g8_current_org"] = lambda a: "org"  # the shipped upstream establishes org context first
    get_issue = w["linear"]["get_issue"]
    w["linear"]["get_issue"] = lambda a: issue("ENG-7", "Chore", "started") if a["id"] == "ENG-7" else get_issue(a)
    with MockUpstream(GH_TOKEN, stateless=False, script=w["github"]) as gh, \
            MockUpstream(LIN_TOKEN, stateless=True, script=w["linear"]) as lin, \
            MockUpstream(G8_TOKEN, stateless=True, script=w["graph8"]) as g8:
        async def body(base, gw):
            async with Client(sse_client(base + "/g8/%s/sse" % TOK), mode="legacy") as c:
                tools = {t.name: t for t in (await c.list_tools()).tools}
                return tools["get_business_context"], [
                    await c.call_tool("get_business_context", {"activity_id": aid}) for aid in (PR_15, "linear:issue:ENG-7")]

        tool, results = run(serving((gh, lin, g8), body, identities=PEOPLE, env={"RESEAU_GITHUB_SCOPE": LOGIN}))

    for res in results:
        assert not res.is_error
        jsonschema.validate(res.structured_content, tool.output_schema)
    linked, unlinked = (r.structured_content for r in results)
    assert linked["linked_issues"] == [ENG_142]
    assert [(link["activity_id"], link["link_type"], link["via"]) for link in linked["links"]] == [
        (CUSTOMER, "graph8_link", OPP), (OPP, "explicit_reference", ENG_142), (COMMITMENT, "source_url", ENG_142)]
    assert all(link["record"]["activity_id"] == link["activity_id"] for link in linked["links"])
    assert (unlinked["links"], unlinked["reason"], unlinked["incomplete"]) == ([], "no_link_found", [])
