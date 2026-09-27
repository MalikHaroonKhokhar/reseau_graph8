"""Semantic tools (HAR-100): get_person_activity(person, date) and get_my_day_context().

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
- GitHub scope is a permission: the tools read only the person's own account plus the owners and repos
  listed in RESEAU_GITHUB_SCOPE. Every search carries those qualifiers and results are rechecked against
  them, so an org's repos are never read until the org is listed. A Linear-linked PR outside the scope is
  not read either; incomplete says one was skipped, without naming its repo.
- Nothing is dropped silently: every listing follows its pages up to a safety limit, and each answer's
  incomplete names every listing that stopped with pages left or that GitHub marked incomplete.
- Linear moves: stateHistory records when an issue changed state, not who changed it. A person's moves are
  therefore the state changes of issues assigned to them.
"""
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
SCOPE_ENTRY = re.compile(r"[\w.-]+(/[\w.-]+)?")
INVALID_PARAMS = -32602
UNMAPPED_PERSON = -32011  # continues evidence's codes
PAGE = 100
MAX_PAGES = evidence.MAX_PAGES  # safety limit per listing; GitHub search serves at most 10 pages of 100 anyway
BRANCH_PAGES = 1  # 100 branches per repo: each costs a list_commits call
FOCUS_MAX = 3
PRIORITY_RANK = {1: 0, 2: 1, 3: 2, 4: 3}  # Linear: 1 Urgent .. 4 Low; 0 (no priority) sorts after Low
OPEN_STATES = ("started", "unstarted", "backlog")  # Linear state types a focus issue can be in, in focus order
CLOSED_STATES = ("completed", "canceled")
PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)$")
PR_FIELDS = ["number", "title", "html_url", "user", "created_at", "updated_at", "pull_request"]
ISSUE_FIELDS = ["id", "title", "priority", "status", "statusType", "updatedAt"]


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
    github_scope: list[str]  # the GitHub owners and repos read: the person's account, then RESEAU_GITHUB_SCOPE
    activities: list[Activity]  # oldest first
    incomplete: list[Gap]


@dataclass(frozen=True)
class LinkedPR:
    activity_id: str
    record: Record
    via: str  # the Linear issue it's attached to: the focus issue, or one blocking it


@dataclass(frozen=True)
class Focus:
    activity_id: str
    record: Record
    priority: str | None  # Linear's name: Urgent, High, Medium, Low, No priority
    status: str | None
    blocking_prs: list[LinkedPR]  # open PRs the issue waits on
    blocked_by: list[str]  # activity_ids of open Linear issues blocking it


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
    focus: list[Focus]  # highest-priority open Linear issues assigned to me, at most FOCUS_MAX
    needs_attention: list[Attention]  # my open PRs with unresolved review threads
    yesterday: Commits
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
]


def load_tz(env=os.environ):
    return ZoneInfo(env.get(TZ_ENV) or "UTC")


def load_scope(env=os.environ):
    """RESEAU_GITHUB_SCOPE: comma-separated owners (users or orgs) and owner/repo entries the semantic tools
    may read besides the person's own account. Unset = nothing else."""
    entries = [e.strip() for e in (env.get(SCOPE_ENV) or "").split(",") if e.strip()]
    bad = [e for e in entries if not SCOPE_ENTRY.fullmatch(e)]
    if bad:
        raise ValueError("%s: not an owner or owner/repo: %s" % (SCOPE_ENV, ", ".join(bad)))
    return tuple(entries)


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
    Gateway.call_tool, so the allowlist and redaction apply. Independent calls run concurrently. GitHub reads
    stay inside scope (the person's login, then RESEAU_GITHUB_SCOPE); gaps collects what may be missing."""

    def __init__(self, call_tool, index, at, scope):
        self.call, self.index, self.at = call_tool, index, at.isoformat(timespec="seconds")
        self.scope, self.gaps = list(scope), []

    async def json(self, source, tool, args):
        return await evidence.fetch_json(self.call, source, tool, args)

    def rec(self, normalize, payload):
        return evidence.resolve(normalize(payload, {}, self.at), self.index)

    def in_scope(self, full):
        owner, full = full.split("/")[0].casefold(), full.casefold()
        return any(e.casefold() in (owner, full) for e in self.scope)

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

    async def search(self, tool, query):
        """A GitHub search inside the scope: GitHub ORs the user:/repo: qualifiers, and every result is
        rechecked, so nothing outside the scope is ever returned."""
        query = " ".join([query] + [("repo:" if "/" in e else "user:") + e for e in self.scope])

        def items(page):
            if page.get("incomplete_results"):
                self.gaps.append(Gap("github", tool, "search_incomplete", "GitHub timed out on %r" % query))
            return page.get("items") or []

        found = await self.collect(
            "github", tool, {"query": query, "perPage": PAGE} | ({"fields": PR_FIELDS} if "pull" in tool else {}),
            items, lambda page, args: github.next_page(page.get("items") or [], args))
        return [i for i in found if self.in_scope(i["full_name"] if "full_name" in i else github.repo(i["html_url"]))]

    async def issues(self, args):
        return await self.collect("linear", "list_issues", args | {"limit": PAGE},
                                  lambda page: page.get("issues") or [], next_cursor)

    async def commits(self, login, start, end):
        """The person's commits on every branch of every in-scope repo pushed since the day began."""
        repos = sorted({r["full_name"] for r in await self.search("search_repositories",
                                                                  "fork:true pushed:>=%s" % utc(start))})
        branches = await gather(*[partial(self.listing, "list_branches", repo_args(r), BRANCH_PAGES) for r in repos])
        window = {"author": login, "since": utc(start), "until": utc(end)}
        pages = await gather(*[partial(self.listing, "list_commits", repo_args(r) | {"sha": b["name"]} | window)
                               for r, names in zip(repos, branches) for b in names])
        recs = {rec.activity_id: rec for page in pages for c in page for rec in [self.rec(github.commit, c)]}
        return [Activity(r.activity_id, "commit", r.updated_at, r) for r in recs.values()]  # at = committed at

    async def prs(self, login, start, end):
        found = {p["html_url"]: p for page in await gather(*[
            partial(self.search, "search_pull_requests", "author:%s %s:%s..%s" % (login, q, utc(start), utc(end)))
            for q in ("created", "merged")]) for p in page}
        return [a for p in found.values() for a in pr_activities(p, self.rec(github.pr, p))]

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
        pages = await gather(*[partial(self.issues, {"assignee": "me", "state": state, "fields": ISSUE_FIELDS})
                               for state in OPEN_STATES])
        items = await gather(*[partial(self.focus_item, i) for i in focus_order([i for page in pages for i in page])])
        return [f for f in items if f]

    async def focus_item(self, listed):
        issue = await self.json("linear", "get_issue", {"id": listed["id"], "includeRelations": True})
        if not issue:
            return None
        blockers = [b for b in await gather(*[partial(self.json, "linear", "get_issue", {"id": b["id"]})
                                              for b in (issue.get("relations") or {}).get("blockedBy") or []])
                    if b and b.get("statusType") not in CLOSED_STATES]
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
        return Focus(rec.activity_id, rec, (listed.get("priority") or {}).get("name"), listed.get("status"), blocking,
                     [self.rec(linear.issue, b).activity_id for b in blockers])

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


def on_day(activities, start, end):
    """The activities in [start, end), oldest first."""
    at = lambda a: datetime.fromisoformat(a.at)
    return sorted((a for a in activities if a.at and start <= at(a) < end), key=lambda a: (at(a), a.activity_id))


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


def focus_order(issues):
    """Open issues at the highest priority present: started before unstarted before backlog, then the most
    recently updated. At most FOCUS_MAX."""
    def rank(i):
        return PRIORITY_RANK.get((i.get("priority") or {}).get("value"), len(PRIORITY_RANK))

    ranked = sorted(sorted((i for i in issues if i.get("statusType") in OPEN_STATES),
                           key=lambda i: i.get("updatedAt") or "", reverse=True),
                    key=lambda i: (rank(i), OPEN_STATES.index(i["statusType"])))
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
    scope = [ids["github"], *gw.github_scope] if "github" in ids else []
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
    up = Upstreams(gw.call_tool, gw.identities, at, [login, *gw.github_scope])
    today = at.astimezone(gw.tz).date()
    yesterday = today - timedelta(days=1)
    start, end = day_window(yesterday, gw.tz)
    focus, attention, commits = await gather(up.focus, partial(up.needs_attention, login),
                                             partial(up.commits, login, start, end))
    return MyDay(evidence.resolve_actor(Actor("github", login), gw.identities), today.isoformat(), str(gw.tz),
                 up.scope, focus, attention, commit_summary(yesterday, on_day(commits, start, end)), up.gaps)


HANDLERS = {"get_person_activity": person_activity, "get_my_day_context": my_day}
