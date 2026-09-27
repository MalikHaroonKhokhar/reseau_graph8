"""HAR-102 and HAR-103 live check: Graph8 runs the Start My Day and daily report workflows against the gateway
backed by mock upstream fixtures.

    set -a; . ./.env; set +a
    ssh -R 80:localhost:8080 nokey@localhost.run      # in another shell: the public URL Graph8 will call
    uv run python -m tests.live_workflows https://<public host> [--port 8080] [--runs 3] [--workflow daily-report]

The gateway (real GitHub and Linear upstream definitions: allowlists, scope, a team) serves the scripted fixture
world of tests/test_semantic.py on --port, with time frozen at its NOW; no real GitHub or Linear is read. Steps:
register the gateway under a fresh token, create the voice agent and the workflows (workflows.setup), run each
chosen workflow --runs times on the fixture world and once on an empty world, and verify every reply with no
retries, so flakiness shows. The daily report covers the fixtures' yesterday, 2026-09-26. Then delete everything
created and prove it gone. BILLABLE: ~12 credits per run. Needs GRAPH8_API_KEY.
"""
import argparse
import dataclasses
import os
import secrets
import sys
import time
from functools import partial

import anyio
import uvicorn

from reseau import front, gateway, outbound, register_graph8, semantic, workflows
from reseau.register_graph8 import say
from tests.mock_upstream import MockUpstream
from tests.test_gateway import GH_TOKEN, LIN_TOKEN
from tests.test_semantic import LOGIN, NOW, TEAM, TEAM_PEOPLE, YESTERDAY, team_world
from tests.test_verify import empty_team

ONCE = {"start-my-day": (workflows.START_MY_DAY_ENV, workflows.my_day_once),
        "daily-report": (workflows.REPORT_ENV, partial(workflows.report_once, day=YESTERDAY))}


def gone(g8, path):
    g8("DELETE", path)
    return g8("GET", path)[0] == 404


def one_run(once, label):
    t0 = time.time()
    try:
        execution, _, reply, sections, problems = once()
    except workflows.WorkflowError as e:
        execution, reply, sections, problems = None, None, None, ["run failed: %s" % e]
    say("\n== %s run %s: %s in %.0f s" % (label, execution, "VERIFIED" if not problems else "FAILED", time.time() - t0))
    for p in problems:
        say("   problem:", p)
    for section, sentences in (sections or {}).items():
        for s in sentences if isinstance(sentences, list) else []:
            say("   %-15s %s  %s" % (section, s.get("text"), s.get("activity_ids")))
    if problems and reply:
        say("   raw reply:", reply[:1500])
    return not problems


def drive(public_url, token, runs, chosen, w):
    """Everything on the Graph8 side, synchronous: runs in a worker thread while the gateway serves."""
    key = os.environ["GRAPH8_API_KEY"]
    gateway.SECRETS.update({key, token})
    http = outbound.Client()
    g8 = partial(register_graph8.g8, http, key)
    url = "%s/g8/%s/sse" % (public_url.rstrip("/"), token)
    server = agent_id = None
    created = {}
    ok = False
    try:
        _, rec = g8("POST", "/api/v1/voice/mcp-servers", {"name": register_graph8.NAME + "-live", "transport_type": "sse",
                                                          "connection_url": url})
        server = isinstance(rec, dict) and rec.get("mcp_server_id")
        if not server:
            say("gateway registration failed:", rec)
            return False
        _, tested = g8("POST", "/api/v1/voice/mcp-servers/%s/test" % server)
        say("/test:", tested)
        agent_id, created = workflows.setup(g8, server, [env for env, _ in ONCE.values()])
        say("voice agent", agent_id, "workflows", created)
        for name, _, config in workflows.WORKFLOWS.values():
            say("validate %s:" % name, g8("POST", "/api/v1/workflows/validate", {"config": config(server, agent_id)})[1])
        runs_on = lambda label: [one_run(partial(once, g8, created[env]), "%s %s" % (name, label))
                                 for name, (env, once) in ONCE.items() if name in chosen]
        results = [r for k in range(runs) for r in runs_on("fixtures #%d" % (k + 1))]
        for source, tools in empty_team().items():  # the mocks read the script at call time
            w[source].clear()
            w[source].update(tools)
        results += runs_on("empty world")
        ok = all(results)
        say("\n== %d/%d runs verified" % (sum(results), len(results)))
    except workflows.WorkflowError as e:
        say("setup failed:", e)
    finally:
        left = [what for what, done in [
            *[("workflow %s" % a, gone(g8, "/api/v1/workflows/" + a)) for a in created.values()],
            ("voice agent %s" % agent_id, not agent_id or gone(g8, "/api/v1/voice/agents/" + agent_id)),
            ("mcp server %s" % server, not server or register_graph8.cleanup(http, key, server))] if not done]
        say("== cleanup verified" if not left else "== LEFTOVERS (delete by hand): %s" % left)
    return ok and not left


async def serve_and_drive(a):
    semantic.now = lambda: NOW  # the fixtures' "yesterday" is 2026-09-26
    w, token = team_world(), secrets.token_urlsafe(32)
    github, linear = gateway.DEFAULT_UPSTREAMS[:2]
    with MockUpstream(GH_TOKEN, stateless=False, script=w["github"]) as gh, \
            MockUpstream(LIN_TOKEN, stateless=True, script=w["linear"]) as lin:
        ups = [dataclasses.replace(github, url=gh.url), dataclasses.replace(linear, url=lin.url)]
        env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN, "RESEAU_GITHUB_SCOPE": "%s,private-org" % LOGIN,
               "RESEAU_TEAM": TEAM}
        async with gateway.Gateway(ups, env, TEAM_PEOPLE) as gw:
            srv = uvicorn.Server(uvicorn.Config(front.app(gw, [token]), host="127.0.0.1", port=a.port,
                                                log_level="warning", access_log=False, lifespan="off"))
            async with anyio.create_task_group() as tg:
                tg.start_soon(srv.serve)
                while not srv.started:
                    await anyio.sleep(0.05)
                try:
                    return await anyio.to_thread.run_sync(drive, a.public_url, token, a.runs, a.workflow or list(ONCE), w)
                finally:
                    srv.should_exit = True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("public_url", help="public URL that reaches --port, e.g. https://abc.lhr.life")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--runs", type=int, default=3, help="runs per workflow on the fixture world (one more on an empty world)")
    p.add_argument("--workflow", choices=list(ONCE), action="append", help="run only this one (repeatable); default all")
    return 0 if anyio.run(serve_and_drive, p.parse_args()) else 1


if __name__ == "__main__":
    sys.exit(main())
