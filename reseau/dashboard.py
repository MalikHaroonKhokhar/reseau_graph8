"""Réseau dashboard (HAR-106): Start My Day, the daily report and Ask Réseau in a browser, every claim linked to its
evidence. The page (reseau/static/: plain HTML, CSS and JS, no build step) talks only to this server, which runs the
Graph8 workflows (reseau/workflows.py) and get_evidence through its own Gateway. GRAPH8_API_KEY and the upstream
tokens stay here, and every response body is redacted against gateway.SECRETS.

Local and single-user (HAR-106 leaves auth out): it binds 127.0.0.1, answers only Host 127.0.0.1 or localhost (a
DNS-rebinding page can't reach it) and takes only JSON POSTs (another site's form can't start a billable run). Never
put it behind the tunnel. Workflow errors, which quote customer and deal data, are logged at debug only, and the
access log (which would print activity_ids) is off.

    python -m reseau.dashboard [--port 8081]     # needs the env of `python -m reseau.workflows` (README)

API: POST /api/start-my-day {}, POST /api/daily-report {"date": "YYYY-MM-DD"}, POST /api/ask {"question"}: the
workflow's result as reseau.workflows returns it. GET /api/evidence?activity_id=...: get_evidence's record.
An error is {"error": {"kind", "message", "upstreams"?, "problems"?}}, kind one of:
- upstream_unavailable (503): Graph8 or its agent isn't answering, or the gateway can't reach an upstream;
  upstreams names them
- unverified (502): Graph8's reply failed Réseau's checks on every attempt; problems says why
- workflow_failed (502): the run itself failed, e.g. its tool node returned an error
- not_configured (503), invalid_input (400/415), out_of_scope (403), not_found (404), upstream_error (502)
"""
import argparse
import dataclasses
import json
import logging
import os
import pathlib
from datetime import date as Date

import anyio
import uvicorn
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from reseau import evidence, gateway, outbound, register_graph8, workflows

log = logging.getLogger("reseau.dashboard")

STATIC = pathlib.Path(__file__).parent / "static"
HOSTS = ["127.0.0.1", "localhost"]
MAX_QUESTION = 500
NAMES = {"github": "GitHub", "linear": "Linear", "graph8": "Graph8"}
DOWN = {"missing_credential", "unauthorized", "unavailable", "context_not_established"}  # UpstreamError kinds
EVIDENCE_STATUS = {"invalid_activity_id": 400, "out_of_scope": 403, "not_found": 404}


class Unavailable(Exception):
    """Graph8 didn't answer: a network error, or a 429 or 5xx the client's retries didn't get past."""


class Failure(Exception):
    def __init__(self, status, kind, message, **extra):
        super().__init__(message)
        self.status, self.error = status, {"kind": kind, "message": message, **extra}


def graph8(key):
    """register_graph8.g8 bound to a client and key; raises Unavailable when Graph8 doesn't answer."""
    http = outbound.Client()

    def g8(method, path, body=None):
        status, data = register_graph8.g8(http, key, method, path, body)
        if status is None or status == 429 or status >= 500:
            raise Unavailable()
        return status, data

    return g8


def reply(data, status=200):
    return Response(gateway.redact(json.dumps(data, ensure_ascii=False), gateway.SECRETS), status,
                    media_type="application/json")


def unavailable(upstreams, what):
    names = " and ".join(NAMES.get(u, u) for u in upstreams)
    return Failure(503, "upstream_unavailable", "%s %s unavailable right now, so %s. Try again in a minute."
                   % (names, "are" if len(upstreams) > 1 else "is", what), upstreams=upstreams)


def app(g8, get_evidence, down=lambda: [], env=os.environ):
    """The ASGI app. g8(method, path, body) -> (status, data) calls Graph8 (graph8()); get_evidence(activity_id) ->
    Record; down() -> the upstreams the gateway can't reach now, named when a workflow fails."""

    def action(name):
        if not env.get(name):
            raise Failure(503, "not_configured", "%s is not set: run `python -m reseau.workflows setup`." % name)
        return env[name]

    def start_my_day(body):
        return workflows.start_my_day(g8, action(workflows.START_MY_DAY_ENV))

    def daily_report(body):
        try:
            Date.fromisoformat(body.get("date"))
        except (TypeError, ValueError):
            raise Failure(400, "invalid_input", "Pick a day (YYYY-MM-DD).") from None
        return workflows.daily_report(g8, action(workflows.REPORT_ENV), body["date"])

    def ask(body):
        question = body.get("question")
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
            raise Failure(400, "invalid_input", "Ask a question of 1 to %d characters." % MAX_QUESTION)
        return workflows.ask(g8, workflows.ask_ids(action(workflows.ASK_ENV)), question.strip(), env)

    def trigger(run):
        async def endpoint(request):
            try:
                if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    raise Failure(415, "invalid_input", "Send JSON (Content-Type: application/json).")
                try:
                    body = await request.json()
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    raise Failure(400, "invalid_input", "The body must be a JSON object.")
                # the Graph8 client blocks, spacing its calls 3 s apart: keep it off the event loop
                return reply(await anyio.to_thread.run_sync(run, body))
            except Failure as f:
                return reply({"error": f.error}, f.status)
            except Unavailable:
                f = unavailable(["graph8"], "the workflow couldn't run")
                return reply({"error": f.error}, f.status)
            except workflows.WorkflowError as e:
                log.debug("%s failed: %s %s", request.url.path, e, e.problems)
                if e.upstream:
                    f = unavailable([e.upstream], "nothing could be written")
                    return reply({"error": f.error}, f.status)
                if e.problems:
                    return reply({"error": {"kind": "unverified", "problems": e.problems, "message":
                                            "Graph8's reply failed Réseau's checks on every attempt, so it isn't shown."}}, 502)
                if upstreams := down():
                    f = unavailable(upstreams, "the workflow's tool couldn't read it")
                    return reply({"error": f.error}, f.status)
                return reply({"error": {"kind": "workflow_failed", "message": str(e)}}, 502)
        return endpoint

    async def evidence_endpoint(request):
        activity_id = request.query_params.get("activity_id", "")
        try:
            return reply(dataclasses.asdict(await get_evidence(activity_id)))
        except MCPError as e:
            data = e.data if isinstance(e.data, dict) else {}
            kind, source = data.get("kind"), activity_id.split(":")[0]
            log.debug("evidence %s failed: %s", kind, e.message)
            if isinstance(e, gateway.UpstreamError) and kind in DOWN:
                f = unavailable([data.get("upstream") or source], "this record can't be opened")
            elif kind == "not_found":
                f = Failure(404, kind, "%s has no record %s. It may have been deleted." % (NAMES.get(source, source),
                                                                                         activity_id))
            elif kind in EVIDENCE_STATUS:
                f = Failure(EVIDENCE_STATUS[kind], kind, e.message)
            else:
                f = Failure(502, "upstream_error", e.message, upstreams=[source])
            return reply({"error": f.error}, f.status)

    return Starlette(routes=[
        Route("/api/start-my-day", trigger(start_my_day), methods=["POST"]),
        Route("/api/daily-report", trigger(daily_report), methods=["POST"]),
        Route("/api/ask", trigger(ask), methods=["POST"]),
        Route("/api/evidence", evidence_endpoint),
        Mount("/", StaticFiles(directory=STATIC, html=True)),
    ], middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=HOSTS)])


async def serve(port):
    key = gateway.resolve_credential(gateway.Upstream("graph8", register_graph8.BASE, "GRAPH8_API_KEY"))
    gateway.SECRETS.add(key)
    async with gateway.Gateway() as gw:
        web = app(graph8(key), lambda activity_id: evidence.get_evidence(gw.call_tool, activity_id, gw.identities),
                  lambda: [n for n, h in gw.health().items() if not h["ok"]])
        log.info("dashboard on http://127.0.0.1:%d", port)
        await uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, access_log=False,
                                            lifespan="off")).serve()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8081)
    logging.basicConfig(level=logging.INFO)
    anyio.run(serve, p.parse_args().port)
