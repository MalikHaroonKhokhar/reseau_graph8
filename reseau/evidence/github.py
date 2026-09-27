"""GitHub normalizers. Payloads are the JSON text of the GitHub MCP server's tool results
(tests/fixtures/github_*.json, captured live). Owner/repo in activity_ids come from the upstream's own
html_url, so the ID is canonical whatever casing the caller asked with."""
import re

from reseau.evidence.records import Actor, Kind, Record, first_line

REPO = r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)"
REPO_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/")
PER_PAGE = 100


def _repo(url):
    return "%s/%s" % REPO_URL.match(url).groups()


def _actor(login):
    return Actor("github", login)


def pr(p, fields, fetched_at):
    return Record("github:pr:%s#%d" % (_repo(p["html_url"]), p["number"]), "github", "pr", str(p["number"]),
                  p["html_url"], p["title"], _actor((p.get("user") or {}).get("login")),
                  p.get("created_at"), p.get("updated_at"), fetched_at)


def commit(c, fields, fetched_at):
    meta = c["commit"]
    # author is null when the commit email isn't linked to an account: name only, never a guessed login
    return Record("github:commit:%s@%s" % (_repo(c["html_url"]), c["sha"]), "github", "commit", c["sha"],
                  c["html_url"], first_line(meta.get("message")),
                  Actor("github", (c.get("author") or {}).get("login"), (meta.get("author") or {}).get("name")),
                  (meta.get("author") or {}).get("date"), (meta.get("committer") or {}).get("date"), fetched_at)


def review_comment(page, fields, fetched_at):
    """The MCP tool returns review threads without comment ids; the id is only in html_url's #discussion_r<id>."""
    suffix = "#discussion_r" + fields["comment_id"]
    for thread in page.get("review_threads") or []:
        for cm in thread.get("comments") or []:
            url = cm.get("html_url") or ""
            if url.endswith(suffix):
                number = re.search(r"/pull/(\d+)#", url).group(1)
                return Record("github:review_comment:%s#%s/%s" % (_repo(url), number, fields["comment_id"]),
                              "github", "review_comment", fields["comment_id"], url, first_line(cm.get("body")),
                              _actor(cm.get("author")), cm.get("created_at"), cm.get("updated_at"), fetched_at)
    return None


def review_comment_next(page, args):
    info = page.get("pageInfo") or {}
    return dict(args, after=info["endCursor"]) if info.get("hasNextPage") and info.get("endCursor") else None


def _pr_args(method):
    return lambda f: {"method": method, "owner": f["owner"], "repo": f["repo"], "pullNumber": int(f["number"])}


KINDS = {
    "pr": Kind(REPO + r"#(?P<number>\d+)", "{owner}/{repo}#{number}", "pull_request_read", _pr_args("get"), pr),
    "commit": Kind(REPO + r"@(?P<sha>[0-9a-f]{7,40})", "{owner}/{repo}@{sha}", "get_commit",
                   lambda f: {"owner": f["owner"], "repo": f["repo"], "sha": f["sha"], "detail": "none"}, commit),
    "review_comment": Kind(REPO + r"#(?P<number>\d+)/(?P<comment_id>\d+)", "{owner}/{repo}#{number}/{comment_id}",
                           "pull_request_read",
                           lambda f: dict(_pr_args("get_review_comments")(f), perPage=PER_PAGE),
                           review_comment, review_comment_next),
}
