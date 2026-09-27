"""reseau.dashboard over fixtures (HAR-106): the real app, workflow triggers and verifiers, with a scripted Graph8
whose tool nodes return the gateway's real output for the test fixture world, and get_evidence over that world. No
network, no credits. The E2E test runs against it; run it to work on the UI:

    uv run python -m tests.fixture_dashboard [--port 8082]

The world is frozen at 2026-09-27 (tests.test_semantic.NOW). Start My Day, the report for any day, and Ask Réseau
for tests.test_ask.QUESTIONS work. Any other question is declined at the route.
"""
import argparse
import itertools
import json
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn

from reseau import dashboard, evidence, semantic, verify, workflows
from tests import test_semantic as ts
from tests.test_ask import QUESTIONS, ask_world
from tests.test_gateway import run
from tests.test_verify import as_sent, good

ENV = {workflows.START_MY_DAY_ENV: "wf-start", workflows.REPORT_ENV: "wf-report",
       workflows.ASK_ENV: ",".join("%s=wf-%s" % (k, k) for k in workflows.ASK)}
ROUTES = {q.casefold().rstrip("?"): (tool, args, write) for q, (tool, args), write in QUESTIONS}


def world():
    """ask_world(), plus what get_evidence needs for every id the surfaces cite: commits by SHA, PRs #9 and #20."""
    w = ask_world()
    gh = w["github"]
    commits = {c["sha"]: c for repo in (ts.RG, ts.UI) for branch in gh["list_branches"](repo_args(repo))
               for c in gh["list_commits"](repo_args(repo, sha=branch["name"]))}
    prs = {("get", ts.RG, 9): ts.pr(ts.RG, 9, "closed"), ("get", ts.RG, 20): ts.pr(ts.RG, 20, "open")}
    pull_request_read = gh["pull_request_read"]
    gh["get_commit"] = lambda a: commits.get(a["sha"]) or ts.not_found("commit")
    gh["pull_request_read"] = lambda a: prs.get((a["method"], "%s/%s" % (a["owner"], a["repo"]), a["pullNumber"])) or (
        pull_request_read(a))
    return w


def repo_args(repo, **extra):
    owner, name = repo.split("/")
    return {"owner": owner, "repo": name, "perPage": 100, **extra}


def said(text, ids):
    return [{"text": text, "activity_ids": list(ids)}]


def report(s):
    """The report REPORT_INSTRUCTIONS asks for, written from any get_team_summary output."""
    total = {k: s["total"][k]["count"] for k in workflows.COUNTED}
    nothing = said(verify.NOTHING, [])
    blocked = [said("%s is blocked by %s%s." % (
        b["record"]["title"], " and ".join(i.split(":")[-1] for i in b["blocked_by"]),
        "".join(" until PR #%s lands" % p["record"]["source_id"] for p in b["blocking_prs"])),
        [b["activity_id"], *b["blocked_by"], *(p["activity_id"] for p in b["blocking_prs"])])[0] for b in s["blocked"]]
    cited = [i for k in workflows.COUNTED for i in s["total"][k]["activity_ids"]] + [b["activity_id"] for b in s["blocked"]]
    line = lambda key, text: said(text % total[key], []) if total[key] else nothing  # attach() adds the ids
    return {"summary": said("The team's day: %d issues completed, %d pull requests merged, %d commits and %d blocked "
                            "issues." % (*total.values(), len(s["blocked"])), cited) if cited else nothing,
            "completed": line("completed", "Linear issues completed: %d."),
            "merged": line("merged", "GitHub pull requests merged: %d."),
            "commits": line("commits", "Commits pushed: %d."),
            "blocked": blocked or nothing}


class Graph8:
    """Scripted Graph8: execute runs the workflow's tool on the gateway's fixture world and writes the reply its prompt
    asks for; the first poll finds the run completed."""

    def __init__(self, gw):
        self.gw, self.runs, self.ids = gw, {}, itertools.count(1)

    def __call__(self, method, path, body=None):
        if method == "POST":
            execution = "ex-%d" % next(self.ids)
            self.runs[execution] = self.nodes(path.split("/")[-2], body["input_data"])
            return 200, {"execution_id": execution, "status": "pending"}
        return 200, {"status": "completed", "output_data": {"node_results": self.runs.pop(path.split("/")[-1])}}

    def call(self, tool, args):
        if tool == evidence.TOOL.name:
            return as_sent(run(evidence.get_evidence(self.gw.call_tool, args["activity_id"], self.gw.identities)))
        return as_sent(run(semantic.HANDLERS[tool](self.gw, args)))

    def nodes(self, action, inputs):
        if action == "wf-route":
            tool, args, _ = ROUTES.get(json.loads(inputs["message"])["question"].casefold().rstrip("?"), (None, {}, None))
            return {"agent_1": completed(response=json.dumps({"tool": tool, "arguments": args}))}
        if action == "wf-start":
            node, tool, args, write = "day_1", "get_my_day_context", {}, good
        elif action == "wf-report":
            node, tool, args, write = "team_1", "get_team_summary", inputs, report
        else:
            node, tool, args = "ask_1", action.removeprefix("wf-"), {k: v for k, v in inputs.items() if k != "question"}
            write = ROUTES[inputs["question"].casefold().rstrip("?")][2]
        out = self.call(tool, args)
        return {node: completed(content=json.dumps(out), is_error=False),
                "agent_1": completed(response=json.dumps(write(out)))}


def completed(**output):
    return {"status": "completed", "output": output}


def app():
    gw = ts.FakeGateway(world(), people=ts.TEAM_PEOPLE)
    return dashboard.app(Graph8(gw), lambda activity_id: evidence.get_evidence(gw.call_tool, activity_id, gw.identities),
                         env=ENV)


@contextmanager
def serving(web, port=0):
    """web served on 127.0.0.1 from a thread -> its base URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    server = uvicorn.Server(uvicorn.Config(web, log_level="warning", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    try:
        yield "http://127.0.0.1:%d" % sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(5)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8082)
    semantic.now = lambda: ts.NOW
    with serving(app(), p.parse_args().port) as url:
        print("fixture dashboard on %s (Ctrl-C to stop)" % url)
        threading.Event().wait()
