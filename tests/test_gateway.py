import asyncio
import io
import logging
import socket
import subprocess
import sys

import pytest

from mcp.shared.exceptions import MCPError

from reseau.gateway import (SECRETS, Gateway, Upstream, UpstreamError, build_headers, install_log_redaction,
                            redact, resolve_credential, scrub)
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
    code = ("import logging, reseau.gateway as g; f = logging.getLogRecordFactory(); g.Gateway([], {}); "
            "g.SECRETS.add('tok_x'); r = logging.getLogger('sdk').makeRecord('sdk', 10, 'f', 1, 'Bearer %s', ('tok_x',), None); "
            "assert logging.getLogRecordFactory() is not f and r.getMessage() == 'Bearer [REDACTED]', r.getMessage()")
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


# ---- integration (loopback mocks, no network) ----

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
    names = ["echo", "echo_struct", "rpc_fail"]
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
