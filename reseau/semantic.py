"""Semantic tools: get_person_activity(person, date) and get_my_day_context() (HAR-100),
get_project_context(project) and get_team_summary(date) (HAR-101).

Work-native answers built on the HAR-98 records, so every fact carries activity_id(s) that get_evidence
resolves. Upstreams asks the upstreams and normalizes what comes back; the functions below it are pure
aggregation over records and payloads, tested without any upstream.

The ticket's open assumptions, checked against the live upstreams on 2026-09-27:
- "me": GitHub get_me gives the login, and the identity map (RESEAU_IDENTITIES) gives the Réseau person if
  there is one. Linear needs no mapping for the viewer: list_issues takes assignee "me" (and
  get_user(query="me") exists).
- Dates: a day runs from local midnight to local midnight in RESEAU_TIMEZONE (an IANA name). Default UTC.
- Issue <-> PR: Linear's GitHub integration attaches PRs to issues (get_issue attachments[].url; e.g. HAR-98
  -> PR #17). A focus issue's blocking PRs are the open PRs attached to it, or to an open issue in its
  blockedBy relations. With no attachment there is no link; nothing is guessed from branch names.
- Unresolved review comments: the GitHub MCP's get_review_comments returns review threads with is_resolved.
  Each unresolved thread is one fact, identified by its first comment.
- Commits: GitHub's commit search index lags by weeks (on 2026-09-27 the newest indexed commit was from
  2026-08-05). So commits come from list_commits on every branch of every in-scope repo pushed since the day
  began, unmerged feature branches included, deduplicated by SHA. A commit belongs to the day it was
  committed.
- GitHub scope is a permission: RESEAU_GITHUB_SCOPE is the complete list of owners and repos any GitHub
  read may touch (the gateway enforces it for raw tools and get_evidence too). Unset, no repo is read.
  Every search carries the scope's qualifiers and results are rechecked against it, and an empty scope
  never searches. A Linear-linked PR outside the scope is not read; incomplete says one was skipped,
  without naming its repo.
- Nothing is dropped silently: every listing follows its pages up to a safety limit, and each answer's
  incomplete names every listing that stopped with pages left or that GitHub marked incomplete.
- Linear moves: stateHistory records when an issue changed state, not who changed it. A person's moves are
  therefore the state changes of issues assigned to them.

HAR-101's assumptions:
- Project <-> repositories: configured (RESEAU_PROJECTS), never inferred; nothing in Linear links a project to
  a repo. Every configured repo must be inside the GitHub scope; the gateway checks that at startup.
- Blocked: an open issue with a Linear blockedBy relation to an issue that isn't completed or canceled. An
  unmerged PR alone doesn't make an issue blocked; it shows as what the issue waits on (blocking_prs).
  Relations have no history, so blocked is as of now, whatever date is asked for.
- Team: the Linear roster of RESEAU_TEAM (list_users), each member mapped to a person by the identity map.
  Members the map doesn't name are listed as unmapped and not counted: their GitHub activity can't be
  attributed. Completed issues are the team's, credited to the assignee on the day of completedAt; a merged
  PR is credited to its author, a commit to its author.
- Recent changes (project): issues completed and PRs merged in the last RECENT_DAYS days.
- Counts: every count is the number of distinct activity_ids returned with it, built in one place (count).
"""
import json
import os
import re
from dataclasses import dataclass
from datetime import date as Date, datetime, time, timedelta, timezone
from functools import partial
from zoneinfo import ZoneInfo

import anyio
import mcp.types as types
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter

from reseau import evidence
from reseau.evidence import github, linear
from reseau.evidence.records import Actor, Record

TZ_ENV = "RESEAU_TIMEZONE"
SCOPE_ENV = "RESEAU_GITHUB_SCOPE"
PROJECTS_ENV = "RESEAU_PROJECTS"
TEAM_ENV = "RESEAU_TEAM"
SCOPE_ENTRY = re.compile(r"[\w.-]+(/[\w.-]+)?")
REPO_ENTRY = re.compile(r"[\w.-]+/[\w.-]+")
INVALID_PARAMS = -32602
UNMAPPED_PERSON = -32011  # continues evidence's codes
UNKNOWN_PROJECT = -32013  # after the gateway's -32012
TEAM_NOT_CONFIGURED = -32014
PAGE = 100
MAX_PAGES = evidence.MAX_PAGES  # safety limit per listing; GitHub search serves at most 10 pages of 100 anyway
BRANCH_PAGES = 1  # 100 branches per repo: each costs a list_commits call
FOCUS_MAX = 3
RECENT_DAYS = 7
PRIORITY_RANK = {1: 0, 2: 1, 3: 2, 4: 3}  # Linear: 1 Urgent .. 4 Low; 0 (no priority) sorts after Low
OPEN_STATES = ("started", "unstarted", "backlog")  # Linear state types a focus issue can be in, in focus order
CLOSED_STATES = ("completed", "canceled")
PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)$")
PR_FIELDS = ["number", "title", "html_url", "user", "created_at", "updated_at", "pull_request"]
ISSUE_FIELDS = ["id", "title", "priority", "status", "statusType", "updatedAt"]
# what linear.issue needs for a record, straight from list_issues, plus when and to whom it was completed
COMPLETED_FIELDS = ["id", "title", "url", "status", "createdBy", "createdById", "createdAt", "updatedAt",
                    "completedAt", "assigneeId"]


# ---- output: every fact is a record with its activity_id ----

@dataclass(frozen=True)
class Activity:
    activity_id: str
    action: str  # commit | pr_opened | pr_merged | review | issue_moved | issue_completed
    at: str  # when it happened
    record: Record
    detail: str | None = None  # review state, or the state an issue moved to


@dataclass(frozen=True)
class Gap:
    """A listing the answer may be missing results from, stated instead of dropped silently."""
    source: str
    tool: str
    reason: str  # page_limit | search_incomplete | out_of_scope
    detail: str


@dataclass(frozen=True)
class PersonActivity:
    person: str
    date: str
    timezone: str
    github_scope: list[str]  # the GitHub owners and repos that may be read: RESEAU_GITHUB_SCOPE
    activities: list[Activity]  # oldest first
    incomplete: list[Gap]


@dataclass(frozen=True)
class LinkedPR:
    activity_id: str
    record: Record
    via: str  # the Linear issue it's attached to: the focus issue, or one blocking it


@dataclass(frozen=True)
class OpenIssue:
    activity_id: str
    record: Record
    priority: str | None  # Linear's name: Urgent, High, Medium, Low, No priority
    status: str | None
    blocking_prs: list[LinkedPR]  # open PRs the issue waits on
    blocked_by: list[str]  # activity_ids of open Linear issues blocking it: non-empty = blocked


@dataclass(frozen=True)
class Thread:
    activity_id: str  # the unresolved review thread's first comment
    record: Record
    comment_count: int


@dataclass(frozen=True)
class Attention:
    activity_id: str  # the caller's open PR
    record: Record
    unresolved: list[Thread]


@dataclass(frozen=True)
class Commits:
    date: str
    commit_count: int
    repo_count: int
    repos: list[str]
    activity_ids: list[str]


@dataclass(frozen=True)
class MyDay:
    me: Actor  # GitHub identity of the gateway's token, with the Réseau person when mapped
    date: str
    timezone: str
    github_scope: list[str]
    focus: list[OpenIssue]  # highest-priority open Linear issues assigned to me, at most FOCUS_MAX
    needs_attention: list[Attention]  # my open PRs with unresolved review threads
    yesterday: Commits
    incomplete: list[Gap]


@dataclass(frozen=True)
class ProjectContext:
    project: str
    linear_project: str
    repos: list[str]  # the project's GitHub repositories, from RESEAU_PROJECTS
    since: str  # start of the recent window: RECENT_DAYS ago
    open: list[OpenIssue]  # not started (backlog, unstarted), highest priority first
    in_progress: list[OpenIssue]  # started
    blocked: list[OpenIssue]  # the open and in-progress issues with an open blocker
    open_prs: list[Activity]  # pr_opened, in the project's repos
    recent: list[Activity]  # issue_completed and pr_merged since `since`, oldest first
    incomplete: list[Gap]


@dataclass(frozen=True)
class Count:
    count: int  # always len(activity_ids)
    activity_ids: list[str]  # the evidence, oldest first


@dataclass(frozen=True)
class Tally:
    completed: Count  # Linear issues completed
    merged: Count  # GitHub PRs merged
    commits: Count


@dataclass(frozen=True)
class TeamSummary:
    team: str
    date: str
    timezone: str
    github_scope: list[str]
    total: Tally  # the members' activity
    people: dict[str, Tally]  # every member in the identity map, zeros included
    unmapped: list[Actor]  # members the identity map doesn't name: not counted
    blocked: list[OpenIssue]  # the team's open issues with an open blocker, as of now
    incomplete: list[Gap]


TOOLS = [
    types.Tool(
        name="get_person_activity",
        description="One person's activity on one day, as records with activity_ids: commits, PRs opened and "
                    "merged, reviews, and Linear issues moved or completed. person is a name from Réseau's "
                    "identity map; date is YYYY-MM-DD in the gateway's timezone. GitHub facts cover "
                    "github_scope only; incomplete lists anything that may be missing.",
        input_schema={"type": "object", "properties": {"person": {"type": "string"}, "date": {"type": "string"}},
                      "required": ["person", "date"]},
        output_schema=TypeAdapter(PersonActivity).json_schema()),
    types.Tool(
        name="get_my_day_context",
        description="Structured facts for starting the caller's day, each with activity_ids: focus (the "
                    "highest-priority open Linear issues assigned to them, with the open PRs they wait on), "
                    "needs_attention (unresolved review threads on their open PRs), and yesterday (commit "
                    "and repository counts). GitHub facts cover github_scope only; incomplete lists anything "
                    "that may be missing.",
        input_schema={"type": "object", "properties": {}},
        output_schema=TypeAdapter(MyDay).json_schema()),
    types.Tool(
        name="get_project_context",
        description="A project's state as records with activity_ids: its open (not started), in_progress and "
                    "blocked Linear issues (blocked = an open Linear blocked-by relation; blocked_by names the "
                    "blockers, blocking_prs the open PRs it waits on), the open PRs in its repositories, and "
                    "recent changes (issues completed and PRs merged in the last %d days). project is a name "
                    "from Réseau's project map; incomplete lists anything that may be missing." % RECENT_DAYS,
        input_schema={"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]},
        output_schema=TypeAdapter(ProjectContext).json_schema()),
    types.Tool(
        name="get_team_summary",
        description="One day's counts for the team, per person and in total: Linear issues completed, GitHub "
                    "PRs merged and commits. Every count comes with the activity_ids behind it, and equals "
                    "their number. Also the team's blocked Linear issues as of now, with what blocks them. "
                    "date is YYYY-MM-DD in the gateway's timezone. Team members outside Réseau's identity map "
                    "are listed in unmapped and not counted; incomplete lists anything that may be missing.",
        input_schema={"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]},
        output_schema=TypeAdapter(TeamSummary).json_schema()),
]


def load_tz(env=os.environ):
    return ZoneInfo(env.get(TZ_ENV) or "UTC")


def load_scope(env=os.environ):
    """RESEAU_GITHUB_SCOPE: comma-separated owners (users or orgs) and owner/repo entries; the complete list
    of what GitHub reads may touch. Unset = nothing."""
    entries = [e.strip() for e in (env.get(SCOPE_ENV) or "").split(",") if e.strip()]
    bad = [e for e in entries if not SCOPE_ENTRY.fullmatch(e)]
    if bad:
        raise ValueError("%s: not an owner or owner/repo: %s" % (SCOPE_ENV, ", ".join(bad)))
    return tuple(entries)


def load_projects(env=os.environ, scope=()):
    """RESEAU_PROJECTS = path to JSON {"<project>": {"linear": "<Linear project name, ID or slug>",
    "repos": ["owner/repo", ...]}}. Every repo must be inside the GitHub scope. Unset = no projects."""
    path = env.get(PROJECTS_ENV)
    projects = json.load(open(path)) if path else {}
    for name, p in projects.items():
        ok = isinstance(p, dict) and isinstance(p.get("linear"), str) and isinstance(p.get("repos"), list) and all(
            isinstance(r, str) and REPO_ENTRY.fullmatch(r) and in_scope(scope, *r.split("/")) for r in p["repos"])
        if not ok:
            raise ValueError('%s: %r needs "linear" (a Linear project) and "repos" (owner/repo entries inside %s)'
                             % (PROJECTS_ENV, name, SCOPE_ENV))
    return projects


def in_scope(scope, owner, repo=None):
    """An owner entry covers all of that owner's repos; an owner/repo entry covers that one repo."""
    owner = str(owner).casefold()
    full = "%s/%s" % (owner, str(repo).casefold()) if repo is not None else None
    return any(e.casefold() in (owner, full) for e in scope)


def now():
    return datetime.now(timezone.utc)


def utc(dt):
    """The one timestamp format GitHub search, list_commits and Linear all accept."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def error(code, kind, message, **data):
    return MCPError(code, message, {"kind": kind, **data})


# ---- upstream fetching ----

async def gather(*calls):
    """Run zero-argument async callables concurrently; results in order. The gateway's per-host cap bounds the
    fan-out. The first failure cancels the rest and is raised as itself, not wrapped in an ExceptionGroup."""
    out = [None] * len(calls)

    async def one(k, call):
        out[k] = await call()

    try:
        async with anyio.create_task_group() as tg:
            for k, call in enumerate(calls):
                tg.start_soon(one, k, call)
    except BaseExceptionGroup as eg:
        while isinstance(eg, BaseExceptionGroup):
            eg = eg.exceptions[0]
        raise eg from None
    return out


def repo_args(full):
    owner, name = full.split("/")
    return {"owner": owner, "repo": name}


class Upstreams:
    """The upstream questions behind the tools, answered with normalized records. call_tool is
    Gateway.call_tool, so the allowlist, the GitHub scope and redaction apply. Independent calls run
    concurrently. GitHub reads stay inside scope (RESEAU_GITHUB_SCOPE); gaps collects what may be missing."""

    def __init__(self, call_tool, index, at, scope):
        self.call, self.index, self.at = call_tool, index, at.isoformat(timespec="seconds")
        self.scope, self.gaps = list(scope), []

    async def json(self, source, tool, args):
        return await evidence.fetch_json(self.call, source, tool, args)

    def rec(self, normalize, payload):
        return evidence.resolve(normalize(payload, {}, self.at), self.index)

    def in_scope(self, full):
        return in_scope(self.scope, *full.split("/"))

    async def collect(self, source, tool, args, items, advance, limit=None):
        """Every page of a listing, up to a safety limit of pages; stopping with pages left adds a gap."""
        limit = limit or MAX_PAGES
        out = []
        for _ in range(limit):
            payload = await self.json(source, tool, args)
            if payload is None:
                return out
            out += items(payload)
            args = advance(payload, args)
            if not args:
                return out
        what = {k: v for k, v in args.items() if k not in ("page", "after", "cursor", "perPage", "limit", "fields")}
        self.gaps.append(Gap(source, tool, "page_limit", "stopped after %d pages with more left: %s" % (limit, what)))
        return out

    async def listing(self, tool, args, limit=None):
        """A GitHub tool answering with a JSON list, in numbered pages."""
        return await self.collect("github", tool, args | {"perPage": PAGE}, lambda page: page, github.next_page,
                                  limit)

    async def search(self, tool, query, within=None):
        """A GitHub search inside the scope, or inside `within` (a project's repos, all in the scope): GitHub ORs
        the user:/repo: qualifiers, and every result is rechecked, so nothing outside is ever returned. An empty
        scope never searches: without a user:/repo: qualifier GitHub would search every repo the token sees."""
        within = self.scope if within is None else within
        if not within:
            return []
        query = " ".join([query] + [("repo:" if "/" in e else "user:") + e for e in within])

        def items(page):
            if page.get("incomplete_results"):
                self.gaps.append(Gap("github", tool, "search_incomplete", "GitHub timed out on %r" % query))
            return page.get("items") or []

        found = await self.collect(
            "github", tool, {"query": query, "perPage": PAGE} | ({"fields": PR_FIELDS} if "pull" in tool else {}),
            items, lambda page, args: github.next_page(page.get("items") or [], args))
        repos = [(i, i["full_name"] if "full_name" in i else github.repo(i["html_url"])) for i in found]
        return [i for i, r in repos if self.in_scope(r) and in_scope(within, *r.split("/"))]

    async def issues(self, args):
        return await self.collect("linear", "list_issues", args | {"limit": PAGE},
                                  lambda page: page.get("issues") or [], next_cursor)

    async def open_issues(self, args):
        """Open issues matching args (assignee, project or team), one listing per open state."""
        pages = await gather(*[partial(self.issues, args | {"state": state, "fields": ISSUE_FIELDS})
                               for state in OPEN_STATES])
        return [i for page in pages for i in page]

    async def completed(self, args, start, end):
        """Issues matching args (project or team) completed in [start, end) -> [(assignee id, issue_completed)]."""
        found = await self.issues(args | {"state": "completed", "updatedAt": utc(start), "fields": COMPLETED_FIELDS})
        return [(i.get("assigneeId"), Activity(rec.activity_id, "issue_completed", i["completedAt"], rec, i.get("status")))
                for i in found if in_window(i.get("completedAt"), start, end) for rec in [self.rec(linear.issue, i)]]

    async def roster(self, team):
        """The Linear team's members, resolved through the identity map."""
        users = await self.collect("linear", "list_users", {"team": team, "limit": PAGE},
                                   lambda page: page.get("users") or [], next_cursor)
        return [evidence.resolve_actor(Actor("linear", u["id"], u.get("name")), self.index) for u in users]

    async def commits(self, login, start, end):
        """Commits on every branch of every in-scope repo pushed since the day began: login's, or everyone's."""
        repos = sorted({r["full_name"] for r in await self.search("search_repositories",
                                                                  "fork:true pushed:>=%s" % utc(start))})
        branches = await gather(*[partial(self.listing, "list_branches", repo_args(r), BRANCH_PAGES) for r in repos])
        window = ({"author": login} if login else {}) | {"since": utc(start), "until": utc(end)}
        pages = await gather(*[partial(self.listing, "list_commits", repo_args(r) | {"sha": b["name"]} | window)
                               for r, names in zip(repos, branches) for b in names])
        recs = {rec.activity_id: rec for page in pages for c in page for rec in [self.rec(github.commit, c)]}
        return [Activity(r.activity_id, "commit", r.updated_at, r) for r in recs.values()]  # at = committed at

    async def prs(self, login, start, end):
        found = {p["html_url"]: p for page in await gather(*[
            partial(self.search, "search_pull_requests", "author:%s %s:%s..%s" % (login, q, utc(start), utc(end)))
            for q in ("created", "merged")]) for p in page}
        return [a for p in found.values() for a in pr_activities(p, self.rec(github.pr, p))]

    async def merged(self, start, end, within=None):
        """Everyone's PRs merged in [start, end), as pr_merged activities."""
        found = await self.search("search_pull_requests", "merged:%s..%s" % (utc(start), utc(end)), within)
        return on_day([a for p in found for a in pr_activities(p, self.rec(github.pr, p)) if a.action == "pr_merged"],
                      start, end)

    async def open_prs(self, within):
        found = await self.search("search_pull_requests", "is:open archived:false", within)
        return sorted((a for p in found for a in pr_activities(p, self.rec(github.pr, p))), key=moment)

    async def reviews(self, login, start):
        prs = await self.search("search_pull_requests", "reviewed-by:%s updated:>=%s" % (login, utc(start)))
        pages = await gather(*[partial(self.listing, "pull_request_read", repo_args(github.repo(p["html_url"]))
                                       | {"method": "get_reviews", "pullNumber": p["number"]}) for p in prs])
        return [Activity(rec.activity_id, "review", r["submitted_at"], rec, r.get("state"))
                for page in pages for r in reviews_by(login, page) for rec in [self.rec(github.review, r)]]

    async def issue_moves(self, linear_id, start):
        listed = await self.issues({"assignee": linear_id, "updatedAt": utc(start), "fields": ["id"]})
        # ponytail: one get_issue per issue, for its stateHistory
        issues = await gather(*[partial(self.json, "linear", "get_issue", {"id": i["id"]}) for i in listed])
        return [a for i in issues if i for a in issue_moves(i, self.rec(linear.issue, i))]

    async def focus(self):
        items = await gather(*[partial(self.open_issue, i) for i in focus_order(await self.open_issues({"assignee": "me"}))])
        return [f for f in items if f]

    async def open_issue(self, listed, blocked_only=False):
        """A listed open issue with its open blockers (Linear blockedBy relations) and the open PRs it waits on:
        attached to it or to a blocker. blocked_only: None unless something blocks it, and no PR is read."""
        # ponytail: one get_issue per open issue and per blocker, uncached; list_issues has no relations field
        issue = await self.json("linear", "get_issue", {"id": listed["id"], "includeRelations": True})
        if not issue:
            return None
        blockers = [b for b in await gather(*[partial(self.json, "linear", "get_issue", {"id": b["id"]})
                                              for b in (issue.get("relations") or {}).get("blockedBy") or []])
                    if b and b.get("statusType") not in CLOSED_STATES]
        if blocked_only and not blockers:
            return None
        links = [(src, link) for src in [issue] + blockers for link in pr_links(src)]
        skipped = [link for _, link in links if not self.in_scope("%s/%s" % link[:2])]
        if skipped:  # the repo stays unnamed: outside the scope means not shown
            self.gaps.append(Gap("github", "pull_request_read", "out_of_scope", "%s: %d linked PR(s) outside the "
                                 "GitHub scope were not read" % (issue["id"], len(skipped))))
        links = [(src, link) for src, link in links if link not in skipped]
        prs = await gather(*[partial(self.json, "github", "pull_request_read", {
            "method": "get", "owner": owner, "repo": name, "pullNumber": int(number)})
            for _, (owner, name, number) in links])
        blocking = [LinkedPR(rec.activity_id, rec, self.rec(linear.issue, src).activity_id)
                    for (src, _), p in zip(links, prs) if p and p.get("state") == "open"
                    for rec in [self.rec(github.pr, p)]]
        rec = self.rec(linear.issue, issue)
        return OpenIssue(rec.activity_id, rec, (listed.get("priority") or {}).get("name"), listed.get("status"),
                         blocking, [self.rec(linear.issue, b).activity_id for b in blockers])

    async def needs_attention(self, login):
        prs = await self.search("search_pull_requests", "author:%s is:open archived:false" % login)
        return [a for a in await gather(*[partial(self.attention, p) for p in prs]) if a]

    async def attention(self, p):
        threads = await self.collect(
            "github", "pull_request_read", repo_args(github.repo(p["html_url"])) | {
                "method": "get_review_comments", "pullNumber": p["number"], "perPage": PAGE},
            lambda page: page.get("review_threads") or [], github.review_comment_next)
        found = [Thread(r.activity_id, r, len(t["comments"]))
                 for t in unresolved(threads) for r in [self.rec(github.comment, t["comments"][0])]]
        rec = self.rec(github.pr, p)
        return Attention(rec.activity_id, rec, found) if found else None


# ---- pure aggregation ----

def day_window(day, tz):
    """[local midnight, next local midnight) as aware datetimes; 23 or 25 hours across a DST change."""
    return datetime.combine(day, time(), tz), datetime.combine(day + timedelta(days=1), time(), tz)


def in_window(at, start, end):
    return bool(at) and start <= datetime.fromisoformat(at) < end


def moment(a):
    """Sort key: oldest first, ties by activity_id."""
    return datetime.fromisoformat(a.at), a.activity_id


def on_day(activities, start, end):
    """The activities in [start, end), oldest first."""
    return sorted((a for a in activities if in_window(a.at, start, end)), key=moment)


def count(activities):
    """A count and its evidence: the distinct activity_ids, oldest first. The count is their number, always."""
    ids = list(dict.fromkeys(a.activity_id for a in sorted(activities, key=moment)))
    return Count(len(ids), ids)


def tally(activities):
    return Tally(*[count([a for a in activities if a.action == action])
                   for action in ("issue_completed", "pr_merged", "commit")])


def team_tally(credited, members):
    """[(person, activity)] -> (total, {member: tally}). Activity credited to anyone else is left out, so the
    total is exactly the members' activity."""
    return (tally([a for p, a in credited if p in members]),
            {m: tally([a for p, a in credited if p == m]) for m in members})


def commit_summary(day, activities):
    ids = [a.activity_id for a in activities if a.action == "commit"]
    repos = sorted({"%(owner)s/%(repo)s" % evidence.parse(i)[2] for i in ids})
    return Commits(day.isoformat(), len(ids), len(repos), repos, ids)


def pr_activities(p, rec):
    """A PR (pull_request_read get, or a search result) -> opened, and merged if it was."""
    merged = p.get("merged_at") or (p.get("pull_request") or {}).get("merged_at")
    return [Activity(rec.activity_id, "pr_opened", p["created_at"], rec)] + (
        [Activity(rec.activity_id, "pr_merged", merged, rec)] if merged else [])


def reviews_by(login, reviews):
    """Submitted reviews by login (pending ones have no submitted_at)."""
    return [r for r in reviews
            if r.get("submitted_at") and ((r.get("user") or {}).get("login") or "").casefold() == login.casefold()]


def issue_moves(issue, rec):
    """Each state the issue entered after its first -> issue_moved, or issue_completed for a completed state."""
    return [Activity(rec.activity_id, "issue_completed" if h["state"]["type"] == "completed" else "issue_moved",
                     h["startedAt"], rec, h["state"]["name"]) for h in (issue.get("stateHistory") or [])[1:]]


def rank(issue):
    return PRIORITY_RANK.get((issue.get("priority") or {}).get("value"), len(PRIORITY_RANK))


def by_priority(issues):
    """Open issues, highest priority first: then started before unstarted before backlog, then the most
    recently updated."""
    return sorted(sorted((i for i in issues if i.get("statusType") in OPEN_STATES),
                         key=lambda i: i.get("updatedAt") or "", reverse=True),
                  key=lambda i: (rank(i), OPEN_STATES.index(i["statusType"])))


def focus_order(issues):
    """Open issues at the highest priority present, in by_priority order. At most FOCUS_MAX."""
    ranked = by_priority(issues)
    return [i for i in ranked if rank(i) == rank(ranked[0])][:FOCUS_MAX]


def pr_links(issue):
    """GitHub PRs the Linear GitHub integration attached to an issue -> [(owner, repo, number)]."""
    return [m.groups() for a in issue.get("attachments") or [] if (m := PR_URL.match(a.get("url") or ""))]


def next_cursor(page, args):
    """Linear's cursor pages."""
    return dict(args, cursor=page["cursor"]) if page.get("hasNextPage") and page.get("cursor") else None


def unresolved(threads):
    return [t for t in threads if not t.get("is_resolved") and t.get("comments")]


# ---- tools ----

def parse_date(value):
    try:
        return Date.fromisoformat(value)
    except (TypeError, ValueError):
        raise error(INVALID_PARAMS, "invalid_params", "date must be YYYY-MM-DD, got %r" % (value,),
                    date=value) from None


async def person_activity(gw, args):
    person, day = args.get("person"), parse_date(args.get("date"))
    ids = {source: uid for (source, uid), p in gw.identities.items() if p == person}  # uids are casefolded
    if not ids:
        raise error(UNMAPPED_PERSON, "unmapped_person", "%r is not in the identity map (%s)"
                    % (person, evidence.IDENTITIES_ENV), person=person)
    start, end = day_window(day, gw.tz)
    scope = list(gw.github_scope) if "github" in ids else []
    up = Upstreams(gw.call_tool, gw.identities, now(), scope)
    calls = []
    if "github" in ids:
        login = ids["github"]
        calls += [partial(up.commits, login, start, end), partial(up.prs, login, start, end),
                  partial(up.reviews, login, start)]
    if "linear" in ids:
        calls.append(partial(up.issue_moves, ids["linear"], start))
    found = [a for part in await gather(*calls) for a in part]
    return PersonActivity(person, day.isoformat(), str(gw.tz), scope, on_day(found, start, end), up.gaps)


async def my_day(gw, args):
    at = now()
    login = (await evidence.fetch_json(gw.call_tool, "github", "get_me", {}))["login"]
    up = Upstreams(gw.call_tool, gw.identities, at, gw.github_scope)
    today = at.astimezone(gw.tz).date()
    yesterday = today - timedelta(days=1)
    start, end = day_window(yesterday, gw.tz)
    focus, attention, commits = await gather(up.focus, partial(up.needs_attention, login),
                                             partial(up.commits, login, start, end))
    return MyDay(evidence.resolve_actor(Actor("github", login), gw.identities), today.isoformat(), str(gw.tz),
                 up.scope, focus, attention, commit_summary(yesterday, on_day(commits, start, end)), up.gaps)


async def project_context(gw, args):
    name = args.get("project")
    spec = gw.projects.get(name) if isinstance(name, str) else None
    if spec is None:
        raise error(UNKNOWN_PROJECT, "unknown_project", "%r is not in the project map (%s)" % (name, PROJECTS_ENV),
                    project=name, known=sorted(gw.projects))
    at = now()
    since = at - timedelta(days=RECENT_DAYS)
    up = Upstreams(gw.call_tool, gw.identities, at, gw.github_scope)
    project, repos = {"project": spec["linear"]}, spec["repos"]
    listed, open_prs, merged, done = await gather(
        partial(up.open_issues, project), partial(up.open_prs, repos), partial(up.merged, since, at, repos),
        partial(up.completed, project, since, at))
    listed = by_priority(listed)
    items = await gather(*[partial(up.open_issue, i) for i in listed])
    found = [(i["statusType"], item) for i, item in zip(listed, items) if item]
    return ProjectContext(name, spec["linear"], repos, utc(since),
                          [item for state, item in found if state != "started"],
                          [item for state, item in found if state == "started"],
                          [item for _, item in found if item.blocked_by], open_prs,
                          on_day(merged + [a for _, a in done], since, at), up.gaps)


async def team_summary(gw, args):
    day = parse_date(args.get("date"))
    if not gw.team:
        raise error(TEAM_NOT_CONFIGURED, "team_not_configured", "%s is not set: no Linear team to summarize" % TEAM_ENV)
    start, end = day_window(day, gw.tz)
    up = Upstreams(gw.call_tool, gw.identities, now(), gw.github_scope)
    team = {"team": gw.team}
    roster, commits, merged, done, listed = await gather(
        partial(up.roster, gw.team), partial(up.commits, None, start, end), partial(up.merged, start, end),
        partial(up.completed, team, start, end), partial(up.open_issues, team))
    blocked = await gather(*[partial(up.open_issue, i, True) for i in by_priority(listed)])
    credited = [(a.record.actor.person, a) for a in on_day(commits, start, end) + merged] + [
        (evidence.resolve_actor(Actor("linear", assignee), gw.identities).person, a) for assignee, a in done]
    total, people = team_tally(credited, sorted({m.person for m in roster if m.person}))
    return TeamSummary(gw.team, day.isoformat(), str(gw.tz), list(gw.github_scope), total, people,
                       [m for m in roster if not m.person], [b for b in blocked if b], up.gaps)


HANDLERS = {"get_person_activity": person_activity, "get_my_day_context": my_day,
            "get_project_context": project_context, "get_team_summary": team_summary}
