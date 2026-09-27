import dataclasses
import json
import logging
import re
from datetime import datetime, timezone

import mcp.types as types
import pytest
from mcp.shared.exceptions import MCPError

from reseau import evidence
from reseau.semantic import TOOLS as SEMANTIC_TOOLS
from reseau.evidence import github, linear
from reseau.evidence.records import Actor, Record
from reseau.gateway import DEFAULT_UPSTREAMS, Gateway, Upstream
from tests.mock_upstream import FIXTURES, G8_COMPANY, G8_DEAL, G8_MEETING, G8_TASK, G8_THREAD, MockUpstream
from tests.test_gateway import G8_TOKEN, GH_TOKEN, LIN_TOKEN, run

AT = "2026-09-27T10:00:00+00:00"
PR_ID = "github:pr:octo-dev/reseau_graph8#15"
SHA = "a516b748f6e62cef147c8229afe18b1538ddbb55"
COMMIT_ID = "github:commit:octo-dev/reseau_graph8@" + SHA
COMMENT_ID = "github:review_comment:modelcontextprotocol/python-sdk#3583/4102813786"
ISSUE_ID = "linear:issue:HAR-98"
REVIEW_ID = "github:review:octo-dev/sandbox#13/4779069846"
G8_OWNER = "7c3a9e51-2d84-4b6f-a0e3-91f5d2c8b476"  # the synthetic deal owner and task assignee
CUSTOMER_ID, OPPORTUNITY_ID = "graph8:customer:%d" % G8_COMPANY, "graph8:opportunity:" + G8_DEAL
COMMITMENT_ID, MEETING_ID = "graph8:commitment:" + G8_TASK, "graph8:conversation:meeting/" + G8_MEETING
THREAD_ID = "graph8:conversation:email/" + G8_THREAD
GRAPH8_IDS = [CUSTOMER_ID, OPPORTUNITY_ID, COMMITMENT_ID, MEETING_ID, THREAD_ID]
PEOPLE = {"dev": {"github": "octo-dev", "linear": "00000000-0000-4000-8000-000000000001", "graph8": G8_OWNER}}


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def normalize(activity_id, payload):
    source, kind, fields = evidence.parse(activity_id)
    return evidence.SOURCES[source][kind].normalize(payload, fields, AT)


# ---- normalizers against recorded payloads ----

def test_red_pr_fixture_produces_record():
    assert normalize(PR_ID, fixture("github_pr")) == Record(
        PR_ID, "github", "pr", "15", "https://github.com/octo-dev/reseau_graph8/pull/15",
        "HAR-99: gateway behaviour under concurrency and long-running sessions", Actor("github", "octo-dev"),
        "2026-09-27T04:07:47Z", "2026-09-27T04:20:00Z", AT)


def test_commit_fixture_produces_record():
    rec = normalize("github:commit:octo-dev/reseau_graph8@a516b74", fixture("github_commit"))  # short SHA in
    assert rec == Record(
        COMMIT_ID, "github", "commit", SHA, "https://github.com/octo-dev/reseau_graph8/commit/" + SHA,
        "HAR-99: concurrency, rate-limit and session probes; results in FINDINGS Run 5",
        Actor("github", "octo-dev", "octo-dev"), "2026-09-27T04:05:02Z", "2026-09-27T04:07:19Z", AT)


def test_review_comment_fixture_produces_record():
    rec = normalize(COMMENT_ID, fixture("github_review_comments"))
    assert (rec.activity_id, rec.kind, rec.source_id, rec.actor, rec.created_at) == (
        COMMENT_ID, "review_comment", "4102813786", Actor("github", "reviewer-a"), "2026-09-25T08:47:24Z")
    assert rec.url == "https://github.com/modelcontextprotocol/python-sdk/pull/3583#discussion_r4102813786"
    assert rec.title.startswith("Addressed in [7db42b6]")
    assert normalize(COMMENT_ID.replace("4102813786", "1"), fixture("github_review_comments")) is None


def test_linear_issue_fixture_produces_record():
    assert normalize(ISSUE_ID, fixture("linear_issue")) == Record(
        ISSUE_ID, "linear", "issue", "HAR-98",
        "https://linear.app/acme/issue/HAR-98/gateway-normalized-work-model-with-provenance-identity-mapping-and-get",
        "Gateway: normalized work model with provenance, identity mapping, and get_evidence(activity_id)",
        Actor("linear", "00000000-0000-4000-8000-000000000001", "Dev Person"),
        "2026-09-26T13:26:59.043Z", "2026-09-27T04:23:01.104Z", AT)


def test_review_fixture_produces_record():
    assert normalize(REVIEW_ID, fixture("github_reviews")) == Record(
        REVIEW_ID, "github", "review", "4779069846",
        "https://github.com/octo-dev/sandbox/pull/13#pullrequestreview-4779069846",
        "The changes in expense.py replace the typing import.", Actor("github", "octo-dev"),
        "2026-07-25T09:51:20Z", "2026-07-25T09:51:20Z", AT)
    assert normalize(REVIEW_ID.replace("4779069846", "1"), fixture("github_reviews")) is None


def test_commit_without_linked_account_is_name_only():
    c = fixture("github_commit")
    c["author"] = None
    assert normalize(COMMIT_ID, c).actor == Actor("github", None, "octo-dev")


# ---- Graph8 normalizers (HAR-108) against synthetic payloads in the live shapes ----

def test_red_graph8_opportunity_fixture_produces_record():
    assert normalize(OPPORTUNITY_ID, fixture("graph8_deal")) == Record(
        OPPORTUNITY_ID, "graph8", "opportunity", G8_DEAL, None, "Example Customer Co - annual plan",
        Actor("graph8", G8_OWNER, "Sam Owner"), "2026-09-21T10:00:00Z", "2026-09-25T16:30:00Z", AT)


def test_graph8_customer_unwraps_the_company_record():
    assert normalize(CUSTOMER_ID, fixture("graph8_company")) == Record(
        CUSTOMER_ID, "graph8", "customer", "4242", None, "Example Customer Co", Actor("graph8", None),
        "2026-09-20T09:00:00Z", None, AT)


def test_graph8_commitment_is_a_task_linked_to_a_deal_or_company():
    assert normalize(COMMITMENT_ID, fixture("graph8_task")) == Record(
        COMMITMENT_ID, "graph8", "commitment", G8_TASK, None, "Ship SSO for Example Customer Co",
        Actor("graph8", G8_OWNER, "Sam Owner"), "2026-09-22T08:00:00Z", "2026-09-24T12:00:00Z", AT)
    unlinked = dict(fixture("graph8_task"), entity_type="team_member", entity_id=G8_OWNER, links=[
        {"entity_type": "contact", "entity_id": "9001"}])
    assert normalize(COMMITMENT_ID, unlinked) is None  # a task, but nothing ties it to a customer
    assert normalize(COMMITMENT_ID, dict(unlinked, company_id=4242)) is not None


def test_graph8_conversation_is_a_meeting_or_an_inbox_thread():
    meeting, thread = normalize(MEETING_ID, fixture("graph8_meeting")), normalize(THREAD_ID, fixture("graph8_thread"))
    assert (meeting.activity_id, meeting.kind, meeting.url, meeting.title, meeting.actor) == (
        MEETING_ID, "conversation", None, "Example Customer Co - SSO requirements",
        Actor("graph8", None, "owner@example.com"))
    assert (thread.activity_id, thread.source_id, thread.title, thread.created_at) == (
        THREAD_ID, G8_THREAD, "Re: SSO timeline", "2026-09-24T09:00:00Z")
    kinds = evidence.SOURCES["graph8"]
    fields = evidence.parse(THREAD_ID)[2]
    assert (kinds["conversation"].tool(fields), kinds["conversation"].args(fields)) == (
        "g8_get_reply", {"reply_id": G8_THREAD, "channel": "email"})


# ---- activity_id ----

@pytest.mark.parametrize("aid", [PR_ID, COMMIT_ID, COMMENT_ID, REVIEW_ID, ISSUE_ID, *GRAPH8_IDS])
def test_activity_id_round_trip(aid):
    assert evidence.format_id(*evidence.parse(aid)) == aid


@pytest.mark.parametrize("aid", [None, 42, ["github:pr:o/r#9"], {"id": 1}, "", "github:pr:o/r", "github:pr:o/r#9x", "gitlab:pr:o/r#1", "github:issue:o/r#1",
                                 "github:commit:" + SHA, "linear:issue:eng-1", "linear:issue:ENG-1 ",
                                 # Graph8: an entity type HAR-107 didn't confirm, and malformed keys
                                 "graph8:deal:" + G8_DEAL, "graph8:contact:9001", "graph8:customer:acme",
                                 "graph8:opportunity:42", "graph8:commitment:" + G8_DEAL.upper(),
                                 "graph8:conversation:" + G8_THREAD, "graph8:conversation:email/a/b"])
def test_invalid_activity_id_is_structured(aid):
    with pytest.raises(MCPError) as e:
        evidence.parse(aid)
    assert (e.value.code, e.value.data) == (-32602, {"kind": "invalid_activity_id", "activity_id": aid})


# ---- identities ----

def test_identity_mapped_across_sources_case_insensitively():
    index = evidence.identity_index({"dev": {"github": "Octo-Dev", "linear": "00000000-0000-4000-8000-000000000001"}})
    for aid, name in ((PR_ID, "github_pr"), (ISSUE_ID, "linear_issue")):
        actor = evidence.resolve(normalize(aid, fixture(name)), index).actor
        assert (actor.person, actor.identity) == ("dev", "mapped")


def test_red_missing_mapping_is_unmapped_not_guessed():
    # "octo-dev" matches the person's name exactly; only the explicit map may link them
    index = evidence.identity_index({"octo-dev": {"linear": "someone-else"}})
    actor = evidence.resolve(normalize(PR_ID, fixture("github_pr")), index).actor
    assert (actor.person, actor.identity) == (None, "unmapped")


def test_identity_claimed_by_two_people_is_rejected():
    with pytest.raises(ValueError, match="mapped to both"):
        evidence.identity_index({"a": {"github": "octo-dev"}, "b": {"github": "OCTO-DEV"}})


def test_identities_load_from_env_file(tmp_path):
    path = tmp_path / "people.json"
    path.write_text(json.dumps(PEOPLE))
    assert evidence.load_identities({"RESEAU_IDENTITIES": str(path)})[("github", "octo-dev")] == "dev"
    assert evidence.load_identities({}) == {}


# ---- get_evidence over a fake call_tool ----

def fake(*pages):
    calls = []

    async def call_tool(source, tool, args):
        calls.append(args)
        text, is_error = pages[len(calls) - 1]
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)
    return call_tool, calls


def test_review_comment_found_on_a_later_page():
    page1 = fixture("github_review_comments")
    page2 = {"review_threads": [{"comments": [dict(page1["review_threads"][0]["comments"][0],
                                                  html_url="https://github.com/modelcontextprotocol/python-sdk/pull/3583#discussion_r7")]}],
             "pageInfo": {"hasNextPage": False}}
    call, calls = fake((json.dumps(page1), False), (json.dumps(page2), False))
    now = datetime(2026, 9, 27, 10, tzinfo=timezone.utc)
    rec = run(evidence.get_evidence(call, COMMENT_ID.replace("4102813786", "7"), {}, now))
    assert rec.source_id == "7" and rec.fetched_at == AT
    assert calls[1]["after"] == page1["pageInfo"]["endCursor"] and "after" not in calls[0]


def test_review_found_on_the_next_numbered_page(monkeypatch):
    monkeypatch.setattr(github, "PER_PAGE", 2)  # the 2-review fixture is then a full page
    call, calls = fake((json.dumps(fixture("github_reviews")), False), ('[{"id": 7, "state": "APPROVED", '
        '"html_url": "https://github.com/octo-dev/sandbox/pull/13#pullrequestreview-7", "submitted_at": null}]', False))
    rec = run(evidence.get_evidence(call, REVIEW_ID.replace("4779069846", "7"), {}))
    assert (rec.activity_id, rec.title) == (REVIEW_ID.replace("4779069846", "7"), "APPROVED")
    assert [c.get("page") for c in calls] == [None, 2]


def test_review_comment_missing_after_last_page_is_not_found():
    call, calls = fake((json.dumps(fixture("github_review_comments")), False), ('{"review_threads":[],"pageInfo":{}}', False))
    with pytest.raises(MCPError) as e:
        run(evidence.get_evidence(call, COMMENT_ID.replace("4102813786", "7"), {}))
    assert (e.value.code, e.value.data["kind"], len(calls)) == (-32008, "not_found", 2)


def test_page_limit_with_pages_left_is_incomplete_not_not_found(monkeypatch):
    monkeypatch.setattr(evidence, "MAX_PAGES", 2)
    page = json.dumps(fixture("github_review_comments"))  # hasNextPage: true, target not on it
    call, calls = fake((page, False), (page, False))
    with pytest.raises(MCPError) as e:
        run(evidence.get_evidence(call, COMMENT_ID.replace("4102813786", "7"), {}))
    assert (e.value.code, e.value.data["kind"], len(calls)) == (-32010, "search_incomplete", 2)


def test_other_upstream_error_is_not_reported_as_not_found():
    call, _ = fake(("failed to get pull request: 502 Bad Gateway", True))
    with pytest.raises(MCPError) as e:
        run(evidence.get_evidence(call, PR_ID, {}))
    assert (e.value.code, e.value.data["kind"]) == (-32009, "upstream_error")


# ---- fixtures ----

def test_fixtures_contain_no_secrets_or_real_emails():
    secret = re.compile(r"ghp_|gho_|github_pat_|lin_api_|lin_oauth_|g8_live_|Bearer |[\w.+-]+@(?!example\.com)[\w-]+\.\w+")
    for path in FIXTURES.glob("*.json"):
        assert not secret.search(path.read_text()), path.name


# ---- integration: gateway + mock upstreams ----

@pytest.fixture
def gateway_up():
    with MockUpstream(GH_TOKEN, stateless=False, evidence=True) as gh, \
            MockUpstream(LIN_TOKEN, stateless=True, evidence=True) as lin:
        yield [Upstream("github", gh.url, "GITHUB_MCP_TOKEN"), Upstream("linear", lin.url, "LINEAR_API_KEY")]


ENV = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}


def test_green_get_evidence_resolves_all_four_kinds(gateway_up):
    async def go():
        async with Gateway(gateway_up, ENV, identities=PEOPLE) as gw:
            names = [t.name for t in await gw.tools()]
            return names, {aid: (await gw.call("get_evidence", {"activity_id": aid})).structured_content
                           for aid in (PR_ID, "github:commit:octo-dev/reseau_graph8@a516b74", COMMENT_ID, ISSUE_ID)}

    names, out = run(go())
    assert "get_evidence" in names
    recs = list(out.values())
    assert [r["activity_id"] for r in recs] == [PR_ID, COMMIT_ID, COMMENT_ID, ISSUE_ID]
    for r in recs:
        assert r["url"].startswith("https://") and r["source"] in ("github", "linear")
        assert datetime.fromisoformat(r["fetched_at"]).tzinfo is not None
    assert [(r["actor"]["person"], r["actor"]["identity"]) for r in recs] == [
        ("dev", "mapped"), ("dev", "mapped"), (None, "unmapped"), ("dev", "mapped")]


def test_red_github_scope_blocks_raw_tools_and_evidence_before_the_upstream(gateway_up):
    gh = dataclasses.replace(gateway_up[0], repo_scoped=True)
    env = dict(ENV, RESEAU_GITHUB_SCOPE="octo-dev")
    out_of_scope = {"method": "get", "owner": "modelcontextprotocol", "repo": "python-sdk", "pullNumber": 3583}

    async def go():
        async with Gateway([gh, gateway_up[1]], env, identities=PEOPLE) as gw:
            ok = (await gw.call("get_evidence", {"activity_id": PR_ID})).structured_content
            errors = []
            for call in (gw.call("github_pull_request_read", out_of_scope),
                         gw.call("get_evidence", {"activity_id": COMMENT_ID}),
                         gw.call("github_get_commit", {"owner": "OCTO-DEV-2", "repo": "x", "sha": "abc1234"})):
                with pytest.raises(MCPError) as e:
                    await call
                errors.append(e.value)
            linear = (await gw.call("get_evidence", {"activity_id": ISSUE_ID})).structured_content  # not GitHub
            return ok, errors, linear

    ok, errors, linear = run(go())
    assert ok["activity_id"] == PR_ID and linear["activity_id"] == ISSUE_ID
    assert [(e.code, e.data["kind"]) for e in errors] == [(-32012, "out_of_scope")] * 3
    assert "modelcontextprotocol/python-sdk is outside RESEAU_GITHUB_SCOPE" in errors[0].message


@pytest.mark.parametrize("aid", ["github:pr:octo-dev/reseau_graph8#9", "github:commit:octo-dev/reseau_graph8@deadbeef",
                                 "github:review_comment:octo-dev/reseau_graph8#9/1", "linear:issue:ENG-142"])
def test_red_unknown_id_is_structured_not_found(gateway_up, aid):
    async def go():
        async with Gateway(gateway_up, ENV, identities={}) as gw:
            with pytest.raises(MCPError) as e:
                await gw.call("get_evidence", {"activity_id": aid})
            return e.value, gw.health()

    err, health = run(go())
    assert (err.code, err.data) == (-32008, {"kind": "not_found", "activity_id": aid})
    assert all(h["ok"] for h in health.values())  # a missing record is not an upstream failure


# ---- integration: gateway + mock Graph8 (HAR-108) ----

def test_green_graph8_evidence_through_the_org_gate(caplog):
    """The shipped Graph8 upstream (allowlist, hidden company getter, error-text guard) against a mock that fails
    every tool with -32003 until g8_current_org is called."""
    caplog.set_level(logging.DEBUG)
    shipped = {u.name: u for u in DEFAULT_UPSTREAMS}["graph8"]

    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True, evidence=True) as mock:
            async with Gateway([dataclasses.replace(shipped, url=mock.url)], {"GRAPH8_API_KEY": G8_TOKEN},
                               identities=PEOPLE) as gw:
                names = {t.name for t in await gw.tools()}
                found = {aid: (await gw.call("get_evidence", {"activity_id": aid})).structured_content
                         for aid in GRAPH8_IDS}
                missing = []
                for aid in ("graph8:customer:1", "graph8:opportunity:" + G8_TASK, "graph8:commitment:" + G8_DEAL,
                            "graph8:conversation:meeting/nope", "graph8:conversation:email/nope"):
                    with pytest.raises(MCPError) as e:
                        await gw.call("get_evidence", {"activity_id": aid})
                    missing.append((e.value.code, e.value.data["kind"]))
                raw_missing = await gw.call("g8_get_deal", {"deal_id": G8_TASK})
                refused = []
                for name, args in (("g8_crm_get_company", {"company_id": G8_COMPANY}), ("g8_create_deal", {"name": "x"})):
                    with pytest.raises(MCPError) as e:
                        await gw.call(name, args)
                    refused.append(e.value.data["kind"])
                return names, found, missing, raw_missing, refused, mock.org_calls, mock.created

    names, found, missing, raw_missing, refused, org_calls, created = run(go())
    assert names - {"get_evidence", *(t.name for t in SEMANTIC_TOOLS)} == {
        "g8_current_org", "g8_get_deal", "g8_get_task", "g8_get_meeting", "g8_get_reply"}  # what the mock serves
    customer = found[CUSTOMER_ID]
    assert (customer["source"], customer["kind"], customer["url"], customer["title"]) == (
        "graph8", "customer", None, "Example Customer Co")
    assert datetime.fromisoformat(customer["fetched_at"]).tzinfo is not None
    assert [r["activity_id"] for r in found.values()] == GRAPH8_IDS
    assert found[OPPORTUNITY_ID]["actor"] == {"source": "graph8", "id": G8_OWNER, "name": "Sam Owner",
                                              "person": "dev", "identity": "mapped"}
    assert missing == [(-32008, "not_found")] * 5
    assert raw_missing.is_error  # Graph8's "Error: ..." text with isError unset reaches clients as an error
    assert refused == ["unknown_tool", "unknown_tool"] and created == 0  # hidden getter and writes: not routable
    assert org_calls == 1
    assert G8_TOKEN not in caplog.text
    # Record bodies stay out of INFO and above; only the MCP SDK's own DEBUG traces carry message bodies
    assert not [r for r in caplog.records if r.levelno >= logging.INFO and "Example Customer" in r.getMessage()]


def test_graph8_allowlist_is_read_only():
    """Every tool the gateway may call on Graph8 is one HAR-107 captured with readOnlyHint (discovery.json), or the
    hidden company getter, which has no annotation to check."""
    shipped = {u.name: u for u in DEFAULT_UPSTREAMS}["graph8"]
    captured = json.loads((FIXTURES.parent.parent / "spikes/graph8_entities/discovery.json").read_text())
    read_only = {name for name, hint in captured["visible_tools"] if hint}
    assert shipped.allow <= read_only
    assert shipped.internal == {"g8_crm_get_company"}
