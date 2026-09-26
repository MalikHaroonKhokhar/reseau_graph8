import asyncio
import io
import logging
import socket

import pytest

from reseau.gateway import (Gateway, RedactingFilter, Upstream, UpstreamError, build_headers, redact,
                            resolve_credential)
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


def test_redaction_in_log_formatting():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(RedactingFilter({GH_TOKEN}))
    logger = logging.getLogger("test.redact")
    logger.addHandler(handler)
    try:
        logger.warning("header was Bearer %s", GH_TOKEN)
    finally:
        logger.removeHandler(handler)
    assert GH_TOKEN not in buf.getvalue() and "[REDACTED]" in buf.getvalue()
    assert redact("a %s b" % GH_TOKEN, {GH_TOKEN, ""}) == "a [REDACTED] b"


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
    assert out == {"github": (["echo"], "echo:github"), "linear": (["echo"], "echo:linear")}
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
    assert [t.name for t in tools] == ["echo"]
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
