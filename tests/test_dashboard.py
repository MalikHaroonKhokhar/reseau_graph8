"""HAR-106: the dashboard's backend. The trigger endpoints over a scripted Graph8 (tests/fixture_dashboard.py: the
real workflow triggers and verifiers over the fixture world), get_evidence over the same world, and the security
edges: no secret in any response or in the page, only JSON POSTs, only local Host headers. The page itself is
tests/test_dashboard_ui.py."""
import json
import pathlib
import re

import pytest
from starlette.testclient import TestClient

from reseau import dashboard, evidence, gateway, register_graph8, workflows
from tests import fixture_dashboard as fd
from tests.test_semantic import TEAM, TEAM_PEOPLE, TODAY, UI, YESTERDAY, FakeGateway, frozen_now  # noqa: F401 (autouse)
from tests.test_verify import context, good
from tests.test_workflows import FakeGraph8

SECRET = "g8_live_" + "s" * 40
KEY_SHAPES = re.compile(r"g8_live_|lin_api_|ghp_|github_pat_|Bearer ")


class Recorder:
    """A Graph8 that must not be called."""

    def __init__(self):
        self.calls = []

    def __call__(self, *call):
        self.calls.append(call)
        raise AssertionError("Graph8 was called")


def client(g8=None, get_evidence=None, down=lambda: [], env=fd.ENV, host="127.0.0.1"):
    gw = FakeGateway(fd.world(), people=TEAM_PEOPLE)
    web = dashboard.app(g8 or fd.Graph8(gw), get_evidence or (
        lambda activity_id: evidence.get_evidence(gw.call_tool, activity_id, gw.identities)), down, env)
    return TestClient(web, base_url="http://%s" % host)


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr(gateway, "SECRETS", gateway.SECRETS | {SECRET})


def tool_error(content):
    """A Start My Day run whose tool node failed with content."""
    return FakeGraph8(content, ["{}"], tool_error=True)


# ---- the three workflows ----

def test_green_start_my_day_returns_the_verified_briefing():
    r = client().post("/api/start-my-day", json={})
    assert r.status_code == 200
    out = r.json()
    assert (out["date"], out["me"]["person"], list(out["sections"])) == (TODAY, "dev", list(workflows.SECTIONS))
    assert all(s["activity_ids"] for section in out["sections"].values() for s in section)


def test_daily_report_and_ask_run_their_workflows():
    c = client()
    report = c.post("/api/daily-report", json={"date": YESTERDAY}).json()
    assert (report["team"], report["date"], list(report["sections"])) == (TEAM, YESTERDAY, list(workflows.REPORT_SECTIONS))
    ask = c.post("/api/ask", json={"question": "What is blocking Phase 3?"}).json()
    assert (ask["tool"], ask["sections"]["answer"][0]["activity_ids"][0]) == ("get_team_summary", "linear:issue:HAR-7")
    declined = c.post("/api/ask", json={"question": "Will it rain tomorrow?"}).json()
    assert (declined["tool"], declined["sections"]) == (None, {"answer": [{"text": workflows.DECLINE, "activity_ids": []}]})


@pytest.mark.parametrize("path, kwargs", [
    ("/api/daily-report", {"json": {"date": "yesterday"}}),
    ("/api/daily-report", {"json": {}}),
    ("/api/ask", {"json": {"question": "  "}}),
    ("/api/ask", {"json": {"question": "x" * 501}}),
    ("/api/ask", {"json": ["What is blocked?"]}),
    ("/api/start-my-day", {"content": "{}", "headers": {"Content-Type": "text/plain"}}),  # a cross-site form's type
])
def test_red_bad_input_is_refused_before_graph8_runs(path, kwargs):
    g8 = Recorder()
    r = client(g8).post(path, **kwargs)
    assert r.status_code in (400, 415) and r.json()["error"]["kind"] == "invalid_input"
    assert g8.calls == []


def test_a_workflow_not_set_up_says_so():
    r = client(Recorder(), env={}).post("/api/start-my-day", json={})
    assert r.status_code == 503 and r.json()["error"]["kind"] == "not_configured"
    assert "RESEAU_START_MY_DAY" in r.json()["error"]["message"]


# ---- failures: which upstream is down, and why a reply was refused ----

def test_graph8_down_is_upstream_unavailable(monkeypatch):
    def down(*call):
        raise dashboard.Unavailable()

    r = client(down).post("/api/start-my-day", json={})
    assert r.status_code == 503 and r.json()["error"] | {"message": ""} == {
        "kind": "upstream_unavailable", "upstreams": ["graph8"], "message": ""}
    # the Graph8 client: no answer, 429 or 5xx is unavailable; any other status is the workflow's to report
    answers = iter([(None, {"error": "connection"}), (429, {}), (503, {}), (404, {"detail": "no"}), (200, {"ok": 1})])
    monkeypatch.setattr(register_graph8, "g8", lambda *a, **k: next(answers))
    g8 = dashboard.graph8("key")
    for _ in range(3):
        with pytest.raises(dashboard.Unavailable):
            g8("GET", "/x")
    assert [g8("GET", "/x") for _ in range(2)] == [(404, {"detail": "no"}), (200, {"ok": 1})]


def test_graph8_agent_outage_is_upstream_unavailable_not_unverified():
    ctx = context()
    down = "Sorry — I can't respond right now due to a temporary issue on our end. Please try again shortly."
    g8 = FakeGraph8(json.dumps(ctx), [down] * 2)
    r = client(g8).post("/api/start-my-day", json={})
    assert r.status_code == 503 and r.json()["error"]["upstreams"] == ["graph8"]
    assert r.json()["error"]["message"].startswith("Graph8 is unavailable right now")
    assert len(g8.executions()) == 2  # retried once, like any failed reply


def test_red_an_unverified_reply_is_refused_with_its_problems():
    ctx = context()
    bad = good(ctx)
    bad["focus"][0]["activity_ids"] = ["linear:issue:HAR-999"]
    r = client(FakeGraph8(json.dumps(ctx), [json.dumps(bad)] * 2)).post("/api/start-my-day", json={})
    assert r.status_code == 502 and r.json()["error"]["kind"] == "unverified"
    assert "focus[0]: cites 'linear:issue:HAR-999', which the tool did not return for focus" in r.json()["error"]["problems"]


def test_a_failed_run_names_the_upstream_that_is_down():
    r = client(tool_error("github: not connected"), down=lambda: ["github"]).post("/api/start-my-day", json={})
    assert r.status_code == 503 and r.json()["error"]["upstreams"] == ["github"]
    assert r.json()["error"]["message"].startswith("GitHub is unavailable right now")
    r = client(tool_error("unhandled errors in a TaskGroup")).post("/api/start-my-day", json={})
    assert r.status_code == 502 and r.json()["error"]["kind"] == "workflow_failed"
    assert "unhandled errors in a TaskGroup" in r.json()["error"]["message"]


# ---- evidence ----

def test_evidence_resolves_a_cited_record():
    c = client()
    pr = c.get("/api/evidence", params={"activity_id": "github:pr:%s#9" % UI}).json()
    assert (pr["url"], pr["kind"], pr["actor"]["source"]) == ("https://github.com/%s/pull/9" % UI, "pr", "github")
    issue = c.get("/api/evidence", params={"activity_id": "linear:issue:HAR-7"}).json()
    assert (issue["url"], issue["title"]) == ("https://linear.app/acme/issue/HAR-7", "UI Critic Phase 3")


def test_evidence_failures_say_what_happened():
    c = client()
    missing = c.get("/api/evidence", params={"activity_id": "linear:issue:HAR-404"})
    assert missing.status_code == 404 and missing.json()["error"]["kind"] == "not_found"
    invalid = c.get("/api/evidence", params={"activity_id": "not an id"})
    assert invalid.status_code == 400 and invalid.json()["error"]["kind"] == "invalid_activity_id"

    def fails(kind, upstream):
        async def get_evidence(activity_id):
            raise gateway.UpstreamError(gateway.UPSTREAM_UNAVAILABLE, "%s: %s" % (upstream, kind), upstream, kind)
        return client(get_evidence=get_evidence).get("/api/evidence", params={"activity_id": "linear:issue:HAR-7"})

    down = fails("unavailable", "linear")
    assert down.status_code == 503 and down.json()["error"]["upstreams"] == ["linear"]
    assert down.json()["error"]["message"].startswith("Linear is unavailable right now")
    assert fails("unauthorized", "linear").json()["error"]["kind"] == "upstream_unavailable"
    assert fails("out_of_scope", "github").status_code == 403


# ---- security ----

def test_secrets_never_reach_the_browser(secret):
    # a Graph8 error that echoes a key, and a verified briefing sentence that does
    r = client(tool_error("401 Bearer %s rejected" % SECRET)).post("/api/start-my-day", json={})
    assert r.status_code == 502 and SECRET not in r.text and "[REDACTED]" in r.text
    ctx = context()
    leaky = good(ctx)
    leaky["needs_attention"][0]["text"] = "Your PR #20 (%s) has 2 unresolved review threads." % SECRET
    r = client(FakeGraph8(json.dumps(ctx), [json.dumps(leaky)])).post("/api/start-my-day", json={})
    assert r.status_code == 200 and SECRET not in r.text and "[REDACTED]" in r.text
    # the page the browser loads carries no key and no key-shaped string
    page = client()
    for f in sorted(p.name for p in dashboard.STATIC.iterdir()):
        body = page.get("/" + f).text
        assert SECRET not in body and not KEY_SHAPES.search(body), f


def test_only_local_hosts_are_answered():
    assert client().get("/").status_code == 200
    assert client(host="localhost").get("/").status_code == 200
    assert client(host="reseau.attacker.example").get("/").status_code == 400  # DNS rebinding


def test_the_page_is_served():
    c = client()
    page = c.get("/")
    assert page.headers["content-type"].startswith("text/html") and "Start My Day" in page.text
    assert c.get("/app.js").status_code == 200 and c.get("/app.css").status_code == 200
    assert pathlib.Path(dashboard.STATIC / "index.html").read_text() == page.text
