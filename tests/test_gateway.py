import asyncio
import io
import logging
import socket
import subprocess
import sys

import anyio
import pytest

from mcp.shared.exceptions import MCPError

from reseau import gateway, outbound
from mcp.types import Tool

from reseau.gateway import (SECRETS, Gateway, Upstream, UpstreamError, build_headers, install_log_redaction,
                            merge_tools, redact, resolve_credential, scrub)
from tests.mock_upstream import MockUpstream

GH_TOKEN, LIN_TOKEN = "ghp_secret_abc123", "lin_api_secret_xyz789"


# ---- unit ----

def test_resolve_credential_from_env():
    up = Upstream("github", "http://x", "GITHUB_MCP_TOKEN")
    assert resolve_credential(up, {"GITHUB_MCP_TOKEN": " tok \n"}) == "tok"


@pytest.mark.parametrize("env", [{}, {"GITHUB_MCP_TOKEN": "  "}])
def test_missing_credential_is_structured_and_names_env_var(env):
    up = Upstream("github", "http://x", "GITHUB_MCP_TOKEN")
    with pytest.raises(UpstreamError) as e:
        resolve_credential(up, env)
    assert e.value.code == -32001
    assert e.value.data == {"upstream": "github", "kind": "missing_credential", "env_var": "GITHUB_MCP_TOKEN"}
    assert "GITHUB_MCP_TOKEN" in e.value.message


def test_build_headers():
    h = build_headers("tok")
    assert h["Authorization"] == "Bearer tok"
    assert h["User-Agent"].startswith("reseau-gateway/")


def test_scrub_nested():
    assert scrub({"k " + GH_TOKEN: [GH_TOKEN, 1, None]}, {GH_TOKEN, ""}) == {"k [REDACTED]": ["[REDACTED]", 1, None]}
    assert redact("a %s b" % GH_TOKEN, {GH_TOKEN, ""}) == "a [REDACTED] b"


def test_gateway_installs_log_redaction():
    # Fresh interpreter: the process-wide record factory must come from constructing a Gateway alone.
    code = ("import logging, reseau.gateway as g; g.Gateway([], {}); g.SECRETS.add('tok_x'); "
            "r = logging.getLogger('sdk').makeRecord('sdk', 10, 'f', 1, 'Bearer %s', ('tok_x',), None, extra={'h': 'tok_x'}); "
            "assert (r.getMessage(), r.h) == ('Bearer [REDACTED]', '[REDACTED]'), vars(r)")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_log_redaction_covers_messages_and_tracebacks():
    install_log_redaction()
    SECRETS.add(GH_TOKEN)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)  # a plain handler with no filter: redaction must not depend on setup
    logger = logging.getLogger("test.redact.child")
    logger.addHandler(handler)
    try:
        logger.warning("header was Bearer %s", GH_TOKEN)
        try:
            raise RuntimeError("request failed with Authorization: Bearer " + GH_TOKEN)
        except RuntimeError:
            logger.exception("boom")
        logger.warning("stack", stack_info=True, extra={})
    finally:
        logger.removeHandler(handler)
    out = buf.getvalue()
    records = []
    logger.addHandler(type("H", (logging.Handler,), {"emit": lambda self, r: records.append(r)})())
    try:
        logger.error("x", exc_info=RuntimeError(GH_TOKEN))
    finally:
        logger.handlers.clear()
    assert records[0].exc_info is None  # raw exception not left for a formatter that ignores exc_text
    assert GH_TOKEN not in out
    assert "[REDACTED]" in out and "RuntimeError" in out  # traceback still rendered, just scrubbed


def test_log_redaction_covers_extra_fields():
    install_log_redaction()
    SECRETS.add(GH_TOKEN)

    class Header:  # non-string extra that a formatter would str()
        def __str__(self):
            return "Bearer " + GH_TOKEN

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s auth=%(authorization)s obj=%(obj)s meta=%(meta)s"))
    logger = logging.getLogger("test.redact.extra")
    logger.addHandler(handler)
    try:
        logger.warning("request", extra={"authorization": "Bearer " + GH_TOKEN, "obj": Header(),
                                         "meta": {"headers": {"Authorization": "Bearer " + GH_TOKEN}}})
    finally:
        logger.removeHandler(handler)
    out = buf.getvalue()
    assert GH_TOKEN not in out
    assert out.count("[REDACTED]") == 3


# ---- integration (loopback mocks, no network) ----

@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(gateway, "RETRY_POLICY", outbound.RetryPolicy(base=0.01, cap=0.02))


@pytest.fixture
def mocks():
    with MockUpstream(GH_TOKEN, stateless=False) as gh, MockUpstream(LIN_TOKEN, stateless=True) as lin:
        yield gh, lin


def upstreams(gh, lin):
    return [Upstream("github", gh.url, "GITHUB_MCP_TOKEN"), Upstream("linear", lin.url, "LINEAR_API_KEY")]


def run(coro):
    return asyncio.run(coro)


def assert_no_secret(*texts):
    for t in texts:
        assert GH_TOKEN not in t and LIN_TOKEN not in t


def test_green_both_upstreams_list_and_call(mocks, caplog):
    gh, lin = mocks
    env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}

    async def go():
        async with Gateway(upstreams(gh, lin), env) as gw:
            out = {}
            for name in ("github", "linear"):
                tools = await gw.list_tools(name)
                res = await gw.call_tool(name, "echo", {"text": name})
                out[name] = ([t.name for t in tools], res.content[0].text)
            return gw.health(), out

    caplog.set_level(logging.DEBUG)
    health, out = run(go())
    assert health == {"github": {"ok": True, "error": None}, "linear": {"ok": True, "error": None}}
    names = ["echo", "slow", "echo_struct", "list_issues", "list_releases", "rpc_fail"]
    assert out == {"github": (names, "echo:github"), "linear": (names, "echo:linear")}
    # session id carried only when issued: stateful mock gets it after initialize, stateless never does
    assert any("mcp-session-id" in h for _, h in gh.seen)
    assert not any("mcp-session-id" in h for _, h in lin.seen)
    assert_no_secret(caplog.text)


def test_red_missing_credential_isolated(mocks, caplog):
    gh, lin = mocks

    async def go():
        async with Gateway(upstreams(gh, lin), {"LINEAR_API_KEY": LIN_TOKEN}) as gw:
            with pytest.raises(UpstreamError) as e:
                await gw.call_tool("github", "echo", {"text": "x"})
            res = await gw.call_tool("linear", "echo", {"text": "ok"})
            return gw.health(), e.value, res

    health, err, res = run(go())
    assert err.data == {"upstream": "github", "kind": "missing_credential", "env_var": "GITHUB_MCP_TOKEN"}
    assert health["github"]["ok"] is False
    assert health["github"]["error"]["kind"] == "missing_credential"
    assert health["github"]["error"]["env_var"] == "GITHUB_MCP_TOKEN"
    assert health["linear"]["ok"] and res.content[0].text == "echo:ok"
    assert gh.seen == []  # never sent an unauthenticated request


def test_red_wrong_credential_gets_401_as_structured_error(mocks, caplog):
    gh, lin = mocks
    bad = "ghp_wrong_but_secret_000"

    async def go():
        async with Gateway(upstreams(gh, lin), {"GITHUB_MCP_TOKEN": bad, "LINEAR_API_KEY": LIN_TOKEN}) as gw:
            with pytest.raises(UpstreamError) as e:
                await gw.list_tools("github")
            tools = await gw.list_tools("linear")
            return gw.health(), e.value, tools

    caplog.set_level(logging.DEBUG)
    health, err, tools = run(go())
    assert err.code == -32002
    assert err.data == {"upstream": "github", "kind": "unauthorized", "env_var": "GITHUB_MCP_TOKEN"}
    assert "401" in err.message and bad not in err.message
    assert health["github"]["error"]["kind"] == "unauthorized"
    assert "echo" in [t.name for t in tools]
    assert bad not in caplog.text and bad not in str(health)


def test_unreachable_upstream_isolated(mocks):
    gh, lin = mocks
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = "http://127.0.0.1:%d/mcp" % s.getsockname()[1]
    s.close()  # nothing listening -> connection refused

    async def go():
        ups = [Upstream("github", dead, "GITHUB_MCP_TOKEN"), Upstream("linear", lin.url, "LINEAR_API_KEY")]
        async with Gateway(ups, {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}) as gw:
            with pytest.raises(UpstreamError) as e:
                await gw.list_tools("github")
            return e.value, await gw.call_tool("linear", "echo", {"text": "still up"})

    err, res = run(go())
    assert err.data["kind"] == "unavailable"
    assert res.content[0].text == "echo:still up"


def test_upstream_tool_error_passes_through(mocks):
    gh, lin = mocks

    async def go():
        async with Gateway(upstreams(gh, lin), {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}) as gw:
            res = await gw.call_tool("linear", "no_such_tool", {})
            return res, gw.health()

    res, health = run(go())
    assert res.is_error  # the upstream's own error result, not a gateway failure
    assert health["linear"]["ok"]


def test_unknown_upstream():
    async def go():
        async with Gateway([], {}) as gw:
            await gw.list_tools("nope")

    with pytest.raises(UpstreamError) as e:
        run(go())
    assert e.value.data["kind"] == "unknown_upstream"


def test_revoked_mid_session_then_reconnect(mocks):
    gh, lin = mocks
    env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}

    async def go():
        async with Gateway(upstreams(gh, lin), env) as gw:
            gh.token = "rotated"  # server stops accepting the configured token
            with pytest.raises(UpstreamError) as e:
                await gw.call_tool("github", "echo", {"text": "x"})
            after_revoke = gw.health()
            linear_ok = (await gw.call_tool("linear", "echo", {"text": "y"})).content[0].text
            env["GITHUB_MCP_TOKEN"] = "rotated"
            await gw.reconnect("github")  # first-entered upstream, closed out of LIFO order
            return e.value, after_revoke, linear_ok, gw.health(), (await gw.list_tools("github"))[0].name

    err, after_revoke, linear_ok, healed, tool = run(go())
    assert err.data["kind"] == "unauthorized"
    assert after_revoke["github"]["ok"] is False and after_revoke["linear"]["ok"]
    assert linear_ok == "echo:y"
    assert healed["github"] == {"ok": True, "error": None} and tool == "echo"


def test_credentials_scrubbed_from_results_and_errors(mocks):
    gh, lin = mocks
    env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}
    leak = "%s and %s" % (GH_TOKEN, LIN_TOKEN)  # upstream echoing its own and another upstream's credential

    async def go():
        async with Gateway(upstreams(gh, lin), env) as gw:
            text = await gw.call_tool("linear", "echo", {"text": leak})
            struct = await gw.call_tool("github", "echo_struct", {"text": leak})
            with pytest.raises(MCPError) as e:
                await gw.call_tool("linear", "rpc_fail", {"text": leak})
            return text, struct, e.value, gw.health()

    text, struct, err, health = run(go())
    assert text.content[0].text == "echo:[REDACTED] and [REDACTED]"
    assert struct.structured_content == {"text": "[REDACTED] and [REDACTED]"}
    assert_no_secret(struct.model_dump_json())
    assert err.code == -32000 and err.data == {"input": "[REDACTED] and [REDACTED]"}
    assert_no_secret(err.message, str(err.data))
    assert health["linear"] == {"ok": True, "error": None}  # an upstream-sent error is not an outage


def test_health_tracks_availability_failure_and_recovery(mocks):
    gh, lin = mocks
    env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}

    async def go():
        async with Gateway(upstreams(gh, lin), env) as gw:
            gh.fail_status = 503
            with pytest.raises(UpstreamError) as e:
                await gw.list_tools("github")
            during = gw.health()
            gh.fail_status = None
            await gw.list_tools("github")  # next successful call clears the failure
            return e.value, during, gw.health()

    err, during, after = run(go())
    assert err.data["kind"] == "unavailable"
    assert during["github"]["ok"] is False and during["github"]["error"]["kind"] == "unavailable"
    assert during["linear"] == {"ok": True, "error": None}
    assert after["github"] == {"ok": True, "error": None}


# ---- Graph8 org-context gate (HAR-92) ----

G8_TOKEN = "g8_live_secret_q1w2e3"
G8_ENV = {"GRAPH8_API_KEY": G8_TOKEN}


def graph8(mock, bootstrap=True):
    extra = ("g8_current_org", -32003) if bootstrap else ()
    return [Upstream("graph8", mock.url, "GRAPH8_API_KEY", *extra)]


def test_default_graph8_upstream_bootstraps_org():
    from reseau.gateway import DEFAULT_UPSTREAMS
    g8 = {u.name: u for u in DEFAULT_UPSTREAMS}["graph8"]
    assert (g8.token_env, g8.bootstrap_tool, g8.gate_code) == ("GRAPH8_API_KEY", "g8_current_org", -32003)


def test_red_org_gate_without_bootstrap_fails():
    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            async with Gateway(graph8(g8, bootstrap=False), G8_ENV) as gw:
                with pytest.raises(MCPError) as e:
                    await gw.call_tool("graph8", "echo", {"text": "x"})
                return e.value

    assert run(go()).code == -32003


def test_green_first_call_bootstraps_once(caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            async with Gateway(graph8(g8), G8_ENV) as gw:
                a = await gw.call_tool("graph8", "echo", {"text": "a"})
                b = await gw.call_tool("graph8", "echo", {"text": "b"})
                return a.content[0].text, b.content[0].text, g8.org_calls

    assert run(go()) == ("echo:a", "echo:b", 1)
    assert G8_TOKEN not in caplog.text


def test_gate_mid_session_reestablishes_once_and_retries():
    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            async with Gateway(graph8(g8), G8_ENV) as gw:
                await gw.call_tool("graph8", "echo", {"text": "a"})
                g8.org_ready = False  # server dropped the org context
                res = await gw.call_tool("graph8", "echo", {"text": "b"})
                return res.content[0].text, g8.org_calls

    assert run(go()) == ("echo:b", 2)


def test_persistent_gate_is_structured_error_without_loop(caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            async with Gateway(graph8(g8), G8_ENV) as gw:
                await gw.call_tool("graph8", "echo", {"text": "a"})
                g8.org_calls, g8.org_stuck = 0, True
                with pytest.raises(UpstreamError) as e:
                    await gw.call_tool("graph8", "echo", {"text": "b"})
                return e.value, g8.org_calls

    err, calls = run(go())
    assert err.code == -32005
    assert err.data == {"upstream": "graph8", "kind": "context_not_established"}
    assert calls == 1  # exactly one re-establish, then give up
    assert G8_TOKEN not in err.message and G8_TOKEN not in caplog.text


def test_concurrent_first_calls_bootstrap_once():
    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            async with Gateway(graph8(g8), G8_ENV) as gw:
                out = []

                async def call(i):
                    out.append((await gw.call_tool("graph8", "echo", {"text": str(i)})).content[0].text)

                async with anyio.create_task_group() as tg:
                    for i in range(5):
                        tg.start_soon(call, i)
                return sorted(out), g8.org_calls

    assert run(go()) == (["echo:%d" % i for i in range(5)], 1)


# ---- shared HTTP reliability (outbound.py policy) through the gateway ----

def test_429_html_on_connect_and_bootstrap_is_retried():
    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            g8.fail_status, g8.fail_times = 429, 2  # burst during initialize
            async with Gateway(graph8(g8), G8_ENV) as gw:
                g8.fail_status, g8.fail_times = 429, 1  # next request is the g8_current_org bootstrap
                res = await gw.call_tool("graph8", "echo", {"text": "a"})
                return res.content[0].text, g8.org_calls, gw.health()

    text, org_calls, health = run(go())
    assert (text, org_calls) == ("echo:a", 1)
    assert health["graph8"] == {"ok": True, "error": None}


def test_429_on_tool_call_is_not_retried_and_is_structured(caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            async with Gateway(graph8(g8), G8_ENV) as gw:
                await gw.call_tool("graph8", "echo", {"text": "a"})
                g8.fail_status, g8.fail_times = 429, 2
                with pytest.raises(UpstreamError) as e:
                    await gw.call_tool("graph8", "echo", {"text": "side effect"})
                left = g8.fail_times  # 1 left: the tools/call was sent exactly once
                g8.fail_status = g8.fail_times = None
                res = await gw.call_tool("graph8", "echo", {"text": "b"})
                return e.value, left, res.content[0].text

    err, left, text = run(go())
    assert err.data == {"upstream": "graph8", "kind": "rate_limited"}
    assert left == 1
    assert text == "echo:b"
    assert G8_TOKEN not in caplog.text


def test_per_host_concurrency_cap():
    async def go():
        with MockUpstream(G8_TOKEN, stateless=True) as g8:
            async with Gateway(graph8(g8, bootstrap=False), G8_ENV) as gw:
                async with anyio.create_task_group() as tg:
                    for i in range(5):
                        tg.start_soon(gw.call_tool, "graph8", "slow", {"text": str(i)})
                return g8.peak

    assert run(go()) == outbound.MAX_PER_HOST


# ---- tool-name namespacing (HAR-94) ----

GH, LIN = Upstream("github", "http://gh", "T1"), Upstream("linear", "http://lin", "T2")


def fake(*names):
    return [Tool(name=n, description="does " + n, input_schema={"type": "object"}) for n in names]


def test_red_colliding_raw_names_get_distinct_routed_names():
    tools, routes = merge_tools([(GH, fake("list_issues", "list_releases")), (LIN, fake("list_issues", "list_releases"))])
    assert [t.name for t in tools] == ["github_list_issues", "github_list_releases",
                                       "linear_list_issues", "linear_list_releases"]
    assert routes["github_list_issues"] == ("github", "list_issues")
    assert routes["linear_list_issues"] == ("linear", "list_issues")
    assert tools[0].description == "does list_issues"


def test_mapping_is_deterministic():
    listed = [(GH, fake("a", "b")), (LIN, fake("a"))]
    assert merge_tools(listed)[1] == merge_tools(listed)[1]


def test_graph8_keeps_its_own_g8_prefix():
    from reseau.gateway import DEFAULT_UPSTREAMS, exposed_name
    g8 = {u.name: u for u in DEFAULT_UPSTREAMS}["graph8"]
    assert exposed_name(g8, "g8_current_org") == "g8_current_org"


def test_collision_after_prefixing_fails_loudly():
    crafted = Upstream("gh2", "http://x", "T3", prefix="github_")
    with pytest.raises(ValueError, match="github_list_issues"):
        merge_tools([(GH, fake("list_issues")), (crafted, fake("list_issues"))])


def test_merged_surface_routes_each_name_to_its_upstream(mocks):
    gh, lin = mocks
    env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}

    async def go():
        async with Gateway(upstreams(gh, lin), env) as gw:
            names = [t.name for t in await gw.tools()]
            out = {n: (await gw.call(n)).content[0].text
                   for n in ("github_list_issues", "linear_list_issues", "github_list_releases", "linear_list_releases")}
            with pytest.raises(UpstreamError) as e:
                await gw.call("gitlab_list_issues")
            return names, out, e.value

    names, out, err = run(go())
    assert len(names) == len(set(names)) == 12
    # the mocks only know raw names, so each answer proves the raw name went upstream unchanged
    assert out == {"github_list_issues": "list_issues@mock-stateful", "linear_list_issues": "list_issues@mock-stateless",
                   "github_list_releases": "list_releases@mock-stateful",
                   "linear_list_releases": "list_releases@mock-stateless"}
    assert (err.code, err.data["kind"]) == (-32006, "unknown_tool")


def test_call_before_list_routes_after_restart(mocks):
    gh, lin = mocks
    env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}

    async def go():
        async with Gateway(upstreams(gh, lin), env) as gw:  # fresh gateway, client still holds old names
            return (await gw.call("linear_echo", {"text": "hi"})).content[0].text

    assert run(go()) == "echo:hi"


def test_collision_fails_startup_and_closes_connections(mocks):
    gh, lin = mocks
    env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN}
    clash = [Upstream("github", gh.url, "GITHUB_MCP_TOKEN"), Upstream("linear", lin.url, "LINEAR_API_KEY", prefix="github_")]
    gw = Gateway(clash, env)

    async def go():
        with pytest.raises(ValueError, match="tool name collision: 'github_"):
            async with gw:
                pytest.fail("gateway started despite a tool-name collision")

    run(go())
    assert all(c.client is None and c.done.is_set() for c in gw.conns.values())
