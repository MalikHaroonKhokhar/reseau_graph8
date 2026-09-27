"""HAR-104 live check: real Graph8 agents answer a fixed question set through the registered gateway.

    set -a; . ./.env; set +a
    uv run python -m reseau.front --port 8080 & uv run python -m reseau.tunnel --port 8080   # README, Run
    uv run python -m tests.live_ask [--keep]

The questions are about the live data behind the running gateway: the Réseau Linear team and repos, and the
Graph8 demo customers of spikes/graph8_entities/seed_demo.py. Steps: with RESEAU_ASK set, push this code's prompts
onto its workflows (workflows.update); otherwise create a voice agent and the Ask Réseau workflows on the
"reseau-gateway" registration. Ask each question once with no retries (so flakiness shows), and check the answer
passed the citation verifier, cites what the question needs, or declines. Then delete what this run created
(--keep leaves it and prints RESEAU_ASK). BILLABLE: ~12 credits per agent run, two per answered question.
"""
import argparse
import os
import sys
import time
from functools import partial

from reseau import gateway, outbound, register_graph8, workflows
from reseau.register_graph8 import say
from tests.live_workflows import gone

DECLINE = [{"text": workflows.DECLINE, "activity_ids": []}]
GRAPH8 = "graph8:"
# question -> what the answer must cite: these activity_ids, any Graph8 record (GRAPH8), or nothing (a decline)
QUESTIONS = [
    ("What is blocking the Réseau Dashboard?", {"linear:issue:HAR-106", "linear:issue:HAR-104"}),
    ("What did haroon do yesterday?", set()),
    ("Why does HAR-104 matter?", GRAPH8),
    ("Which customer is waiting on HAR-103?", GRAPH8),
    ("Is any customer waiting on HAR-100?", {"linear:issue:HAR-100"}),  # no link: says so, citing the issue
    ("What will the weather be in Lahore tomorrow?", None),
    ("What was our revenue last quarter?", None),
]


def meets(answer, must):
    cited = {i for s in answer for i in s["activity_ids"]}
    if must is None:
        return answer == DECLINE
    if must == GRAPH8:
        return any(i.startswith(GRAPH8) for i in cited)
    return answer != DECLINE and bool(cited) and must <= cited


def one(g8, ids, question, must):
    t0 = time.time()
    try:
        out = workflows.ask(g8, ids, question, attempts=1)
        ok, problems = meets(out["sections"]["answer"], must), []
    except workflows.WorkflowError as e:
        out, ok, problems = None, False, [str(e)[:1500]] + e.problems
    say("\n== %s  %s in %.0f s" % (question, "PASS" if ok else "FAIL", time.time() - t0))
    if out:
        say("   route: %s %s  executions %s" % (out["tool"], out["arguments"], out["execution_ids"]))
        for s in out["sections"]["answer"]:
            say("   %s  %s" % (s["text"], s["activity_ids"]))
        if not ok:
            say("   expected to cite:", "a decline" if must is None else must)
    for p in problems:
        say("   problem:", p)
    return ok


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--keep", action="store_true", help="leave the workflows and voice agent in place")
    a = p.parse_args()
    key = gateway.resolve_credential(gateway.Upstream("graph8", register_graph8.BASE, "GRAPH8_API_KEY"))
    gateway.SECRETS.add(key)
    g8 = partial(register_graph8.g8, outbound.Client(), key)
    server = workflows.gateway_server_id(g8, register_graph8.NAME)
    agent_id, created = None, {}
    ok = False
    try:
        existing = os.environ.get(workflows.ASK_ENV)
        if existing:
            say("updated %s on voice agent %s" % (workflows.ASK_ENV, workflows.update(
                g8, server, {workflows.ASK_ENV: existing})))
        else:
            agent_id, created = workflows.setup(g8, server, [workflows.ASK_ENV])
            for _, (name, _, config) in workflows.definitions(created):
                say("validate %s:" % name, g8("POST", "/api/v1/workflows/validate",
                                              {"config": config(server, agent_id)})[1])
        ids = workflows.ask_ids(existing or created[workflows.ASK_ENV])
        results = [one(g8, ids, q, must) for q, must in QUESTIONS]
        ok = all(results)
        say("\n== %d/%d questions passed" % (sum(results), len(results)))
    except workflows.WorkflowError as e:
        say("setup failed:", e)
    finally:
        if a.keep and created:
            say("kept: voice agent %s\n%s=%s" % (agent_id, workflows.ASK_ENV, created[workflows.ASK_ENV]))
        elif created:
            left = [what for what, done in [
                *[("workflow %s" % w, gone(g8, "/api/v1/workflows/" + w)) for w, _ in workflows.definitions(created)],
                ("voice agent %s" % agent_id, not agent_id or gone(g8, "/api/v1/voice/agents/" + agent_id))] if not done]
            say("== cleanup verified" if not left else "== LEFTOVERS (delete by hand): %s" % left)
            ok = ok and not left
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
