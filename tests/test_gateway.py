import asyncio
import io
import json
import logging
import socket
import subprocess
import sys

import anyio
import httpx2
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
    names = ["echo", "slow", "echo_struct", "list_issues", "list_releases", "create_issue", "rpc_fail"]
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


def test_429_on_tool_call_is_not_retried_off_a_read_only_upstream_and_is_structured(caplog):
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


@pytest.mark.parametrize("caps, peak", [({}, outbound.MAX_PER_HOST), ({"127.0.0.1": 3}, 3)])
def test_per_host_concurrency_cap(monkeypatch, caps, peak):
    monkeypatch.setattr(outbound, "HOST_CAPS", caps)  # the mock's host: unknown by default, then capped at 3

    async def go():
        with MockUpstream(G8_TOKEN, stateless=True) as g8:
            async with Gateway(graph8(g8, bootstrap=False), G8_ENV) as gw:
                async with anyio.create_task_group() as tg:
                    for i in range(6):
                        tg.start_soon(gw.call_tool, "graph8", "slow", {"text": str(i)})
                return g8.peak

    assert run(go()) == peak


class Replies(httpx2.AsyncBaseTransport):
    """An inner transport answering with the given (status, headers), one per request; an exception is raised,
    a Response returned as is."""

    def __init__(self, *replies):
        self.replies, self.sent = list(replies), 0

    async def handle_async_request(self, request):
        self.sent += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        status, headers = reply
        return httpx2.Response(status, headers=headers, stream=httpx2.ByteStream(b"{}"))  # streamed, like a real one


@pytest.mark.parametrize("read_only, replies, status, sent, waits", [
    (True, [(429, {"retry-after": "2"}), (200, {})], 200, 2, [2.0]),  # refused before it ran: wait, repeat
    (True, [(503, {}), (200, {})], 503, 1, []),  # may have run: never repeated
    (False, [(429, {"retry-after": "2"}), (200, {})], 429, 1, []),  # an upstream with write tools: never repeated
])
def test_tools_call_is_retried_on_429_only_and_only_on_a_read_only_upstream(monkeypatch, read_only, replies,
                                                                            status, sent, waits):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gateway.anyio, "sleep", sleep)
    inner = Replies(*replies)
    request = httpx2.Request("POST", "https://api.githubcopilot.com/mcp/readonly", content=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_issues"}}).encode())

    async def go():
        resp = await gateway._Reliable({}, read_only=read_only, inner=inner).handle_async_request(request)
        await resp.aclose()
        return resp.status_code

    assert (run(go()), inner.sent, slept) == (status, sent, waits)


def test_429_on_tool_call_is_retried_on_a_read_only_upstream():
    async def go():
        with MockUpstream(GH_TOKEN, stateless=False) as gh:
            up = Upstream("github", gh.url, "GITHUB_MCP_TOKEN", read_only=True)
            async with Gateway([up], {"GITHUB_MCP_TOKEN": GH_TOKEN}) as gw:
                gh.fail_status, gh.fail_times = 429, 1  # Cloudflare-style HTML 429, no Retry-After
                res = await gw.call_tool("github", "echo", {"text": "a"})
                return res.content[0].text, gh.fail_status, gw.health()

    text, fail_status, health = run(go())
    assert (text, fail_status) == ("echo:a", None)  # the 429 was spent, then the retry answered
    assert health["github"]["ok"]



# ---- a network error fails one request, never the session ----

DISCONNECTED = httpx2.RemoteProtocolError("Server disconnected without sending a response.")
RESET = httpx2.ReadError("peer closed connection")


class CutShort(httpx2.AsyncByteStream):
    """A body that breaks after its first few bytes, before the answer."""

    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk[:8]
            raise RESET

    async def aclose(self):
        await self.stream.aclose()


@pytest.fixture
def drops(monkeypatch):
    """The real transport, except that the next `n` tools/call POSTs lose their connection: before any response,
    or with `mid` set, partway through the answer's event stream."""
    left = {"n": 0, "mid": False}
    real = httpx2.AsyncHTTPTransport

    class Dropping(real):
        async def handle_async_request(self, request):
            if not (request.method == "POST" and b'"tools/call"' in request.content and left["n"]):
                return await super().handle_async_request(request)
            left["n"] -= 1
            if not left["mid"]:
                raise DISCONNECTED
            resp = await super().handle_async_request(request)
            assert resp.headers["content-type"].startswith("text/event-stream")  # a streamed answer, like GitHub's
            resp.stream = CutShort(resp.stream)
            return resp

    monkeypatch.setattr(gateway.httpx2, "AsyncHTTPTransport", Dropping)
    return left


TIMED_OUT = httpx2.ReadTimeout("timed out")
ANSWER = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'


def cut_answer():
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=CutShort(httpx2.ByteStream(ANSWER)))


@pytest.mark.parametrize("method, read_only, replies, sent, waits, status", [
    ("tools/call", True, [DISCONNECTED, (200, {})], 2, 1, 200),  # a read repeats harmlessly
    ("tools/call", True, [cut_answer(), (200, {})], 2, 1, 200),  # partway through its answer, too
    ("tools/call", True, [DISCONNECTED] * outbound.MAX_ATTEMPTS, outbound.MAX_ATTEMPTS, 3, "502 RemoteProtocolError"),
    ("tools/call", True, [TIMED_OUT], 1, 0, "502 ReadTimeout"),  # already waited out the read timeout: not again
    ("tools/call", False, [DISCONNECTED], 1, 0, "502 RemoteProtocolError"),  # may have run: write tools upstream
    ("tools/call", False, [cut_answer()], 1, 0, "502 ReadError"),
    ("initialize", False, [httpx2.ConnectError("refused"), (200, {})], 2, 1, 200),  # the handshake always is
])
def test_network_error_is_retried_where_the_request_is_or_answered_as_a_502(monkeypatch, method, read_only,
                                                                            replies, sent, waits, status):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(gateway.anyio, "sleep", sleep)
    inner, slots = Replies(*replies), {}
    request = httpx2.Request("POST", "https://api.githubcopilot.com/mcp/readonly", content=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"name": "list_issues"}}).encode())

    async def go():
        resp = await gateway._Reliable(slots, read_only=read_only, inner=inner).handle_async_request(request)
        body = await resp.aread()
        await resp.aclose()
        return resp.status_code, json.loads(body)

    got, body = run(go())
    assert (got, inner.sent, len(slept)) == (int(str(status)[:3]), sent, waits)
    if got == 502:  # a JSON-RPC error the SDK hands to this one request; not -32003, Graph8's org gate
        assert body["error"]["code"] == -32603
        assert body["error"]["message"].startswith(status.split()[1] + ": ")
    [slot] = slots.values()
    assert slot.value == outbound.HOST_CAPS["api.githubcopilot.com"]  # every attempt gave its slot back


class Held(httpx2.AsyncByteStream):
    """A server that streams a notification, then its answer split across chunks, then holds the stream open."""

    closed = False

    async def __aiter__(self):
        yield b'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\r\n\r\n'
        yield b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,'
        yield b'"result":{"content":[]}}\r'
        yield b'\n\r\n'
        while True:
            await anyio.sleep(3600)
            yield b": ping\n\n"

    async def aclose(self):
        self.closed = True


def test_an_answer_is_read_up_to_its_event_even_if_the_stream_stays_open():
    held = Held()
    request = httpx2.Request("POST", "https://api.githubcopilot.com/mcp/readonly", content=b"{}")

    async def go():
        with anyio.fail_after(5):  # stops at the answer, not at the end of a stream that never ends
            return await gateway.read_answer(request, httpx2.Response(
                200, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"}, stream=held))

    resp = run(go())
    assert held.closed and resp.headers["mcp-session-id"] == "s1"
    assert resp.content.endswith(b'"result":{"content":[]}}\r\n\r\n')  # the notification before it is kept
    assert resp.content.startswith(b'data: {"jsonrpc":"2.0","method":"notifications/message"')


def test_a_crashed_session_names_its_cause():
    err = gateway._Conn(Upstream("github", "http://x", "T")).classify(
        ExceptionGroup("unhandled errors in a TaskGroup", [httpx2.ConnectError("refused")]))
    assert err.message == "github: upstream unavailable (ConnectError)"


@pytest.mark.parametrize("mid", [False, True], ids=["before_response", "mid_stream"])
@pytest.mark.parametrize("read_only", [True, False])
def test_red_a_dropped_connection_fails_one_call_not_the_session(drops, read_only, mid):
    async def go():
        with MockUpstream(GH_TOKEN, stateless=False) as gh:
            up = Upstream("github", gh.url, "GITHUB_MCP_TOKEN", read_only=read_only)
            async with Gateway([up], {"GITHUB_MCP_TOKEN": GH_TOKEN}) as gw:
                drops["n"], drops["mid"] = 1, mid
                try:
                    first = (await gw.call_tool("github", "echo", {"text": "a"})).content[0].text
                except MCPError as e:
                    first = e
                return first, (await gw.call_tool("github", "echo", {"text": "b"})).content[0].text, gw.health()

    first, second, health = run(go())
    if read_only:  # a read repeats harmlessly: retried, and nobody sees the drop
        assert first == "echo:a"
    else:  # the tool may have run: not repeated, and the error says what happened
        assert isinstance(first, UpstreamError) and first.data["kind"] == "unavailable"
        assert ("ReadError: peer closed" if mid else "RemoteProtocolError: Server disconnected") in first.message
    assert (second, health["github"]["ok"]) == ("echo:b", True)  # the session survived


def test_a_dropped_graph8_call_is_not_mistaken_for_its_org_gate(drops):
    async def go():
        with MockUpstream(G8_TOKEN, stateless=True, org_gate=True) as g8:
            async with Gateway(graph8(g8), G8_ENV) as gw:
                await gw.call_tool("graph8", "echo", {"text": "a"})  # bootstraps the org
                drops["n"] = 1
                with pytest.raises(UpstreamError) as e:
                    await gw.call_tool("graph8", "echo", {"text": "b"})
                return e.value, g8.org_calls

    err, org_calls = run(go())
    assert (err.data["kind"], org_calls) == ("unavailable", 1)  # not re-bootstrapped, and echo not repeated

@pytest.mark.parametrize("mid", [False, True], ids=["before_response", "mid_stream"])
def test_concurrent_reads_all_survive_a_dropped_connection(drops, mid):
    async def go():
        with MockUpstream(GH_TOKEN, stateless=False) as gh:
            up = Upstream("github", gh.url, "GITHUB_MCP_TOKEN", read_only=True)
            async with Gateway([up], {"GITHUB_MCP_TOKEN": GH_TOKEN}) as gw:
                drops["n"], drops["mid"] = 2, mid
                out = {}

                async def call(i):
                    out[i] = (await gw.call_tool("github", "slow", {"text": str(i)})).content[0].text

                async with anyio.create_task_group() as tg:
                    for i in range(6):
                        tg.start_soon(call, i)
                return out, drops["n"]

    out, left = run(go())
    assert (out, left) == ({i: "slow:%d" % i for i in range(6)}, 0)


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
    assert len(names) == len(set(names)) == 20  # 7 per upstream + get_evidence and the 5 semantic tools
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


# ---- tool allowlist (HAR-95) ----

def test_red_allowlist_hides_and_blocks_tool_before_upstream(mocks):
    _, lin = mocks
    up = Upstream("linear", lin.url, "LINEAR_API_KEY", allow=frozenset({"list_issues"}), internal=frozenset({"echo"}))

    async def go():
        async with Gateway([up], {"LINEAR_API_KEY": LIN_TOKEN}) as gw:
            names = [t.name for t in await gw.tools()]
            with pytest.raises(UpstreamError) as raw:
                await gw.call_tool("linear", "create_issue", {"title": "x"})
            with pytest.raises(UpstreamError) as exposed:
                await gw.call("linear_create_issue", {"title": "x"})
            with pytest.raises(UpstreamError) as internal_by_client:
                await gw.call("linear_echo", {"text": "x"})
            internal = (await gw.call_tool("linear", "echo", {"text": "x"})).content[0].text
            ok = (await gw.call("linear_list_issues")).content[0].text
            return names, raw.value, exposed.value, internal_by_client.value, internal, ok, gw.health()

    names, raw, exposed, internal_by_client, internal, ok, health = run(go())
    assert names == ["linear_list_issues", "get_evidence", "get_person_activity", "get_my_day_context",
                     "get_project_context", "get_team_summary", "get_business_context"]
    assert (raw.code, raw.data["kind"]) == (-32007, "tool_not_allowed")
    assert exposed.data["kind"] == "unknown_tool"
    # an internal tool is callable by Réseau's own tools only: not listed, not routed for a client
    assert (internal_by_client.data["kind"], internal) == ("unknown_tool", "echo:x")
    assert ok == "list_issues@mock-stateless"
    assert lin.created == 0  # the blocked call never reached the upstream
    assert health["linear"]["ok"]  # a blocked call is not an upstream failure


def test_default_config_is_read_only_and_allowlisted():
    from reseau.gateway import DEFAULT_UPSTREAMS
    ups = {u.name: u for u in DEFAULT_UPSTREAMS}
    assert ups["github"].url.endswith("/mcp/readonly") and ups["linear"].url.endswith("/mcp/readonly")
    assert all(u.allow is not None for u in DEFAULT_UPSTREAMS)
    assert not ups["github"].allow & {"search_pull_requests", "search_repositories", "list_branches"}
    assert "list_users" in ups["linear"].internal and "list_users" not in ups["linear"].allow  # emails
    assert ups["github"].repo_scoped and not ups["linear"].repo_scoped
    # 429 retry for tools/call: the /readonly endpoints only; Graph8's endpoint also serves write tools
    assert ups["github"].read_only and ups["linear"].read_only and not ups["graph8"].read_only
    assert ups["graph8"].bootstrap_tool in ups["graph8"].allow
