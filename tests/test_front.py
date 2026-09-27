import dataclasses
import logging
import socket

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.sse import sse_client

from reseau import front
from reseau.gateway import DEFAULT_UPSTREAMS, Gateway, Upstream
from tests.mock_upstream import MockUpstream
from tests.test_gateway import G8_TOKEN, GH_TOKEN, LIN_TOKEN, assert_no_secret, run

TOK, TOK2 = "g8tok_" + "a" * 32, "g8tok_" + "b" * 32


@pytest.fixture
def mocks():
    with MockUpstream(GH_TOKEN, stateless=False) as gh, MockUpstream(LIN_TOKEN, stateless=True) as lin:
        yield gh, lin


async def serving(mocks, body, tokens=(TOK,), identities=None, env=None):
    """Gateway over the mocks (GitHub, Linear, and optionally Graph8 as shipped), fronted by front.app on a loopback
    port; body(base_url) runs against it."""
    gh, lin, *g8 = mocks
    ups = [Upstream("github", gh.url, "GITHUB_MCP_TOKEN"), Upstream("linear", lin.url, "LINEAR_API_KEY")] + [
        dataclasses.replace({u.name: u for u in DEFAULT_UPSTREAMS}["graph8"], url=m.url) for m in g8]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = "http://127.0.0.1:%d" % sock.getsockname()[1]
    tokens_env = {"GITHUB_MCP_TOKEN": GH_TOKEN, "LINEAR_API_KEY": LIN_TOKEN, "GRAPH8_API_KEY": G8_TOKEN}
    async with Gateway(ups, {**tokens_env, **(env or {})}, identities) as gw:
        srv = front.uvicorn.Server(front.uvicorn.Config(front.app(gw, list(tokens)), log_level="warning",
                                                        access_log=False, lifespan="off"))
        async with anyio.create_task_group() as tg:
            tg.start_soon(srv.serve, [sock])
            while not srv.started:
                await anyio.sleep(0.01)
            try:
                return await body(base, gw)
            finally:
                srv.should_exit = True


async def list_and_call(url):
    async with Client(sse_client(url), mode="legacy") as c:
        tools = (await c.list_tools()).tools
        res = await c.call_tool("github_echo", {"text": "hi"})
        return [t.name for t in tools], res.content[0].text


def test_green_sse_lists_and_calls_gateway_tools(mocks, caplog):
    caplog.set_level(logging.DEBUG)

    async def body(base, gw):
        return await list_and_call(base + "/g8/%s/sse" % TOK), [t.name for t in await gw.tools()]

    (names, text), expected = run(serving(mocks, body))
    assert names == expected and "github_echo" in names and "linear_list_issues" in names
    assert text == "echo:hi"
    assert TOK not in caplog.text  # the endpoint event (path with token) is logged at DEBUG by the SDK
    assert_no_secret(caplog.text)


def test_red_unauthenticated_is_rejected_everywhere(mocks, caplog):
    async def body(base, gw):
        async with httpx2.AsyncClient() as http:
            statuses = [(await http.request(m, base + p)).status_code for m, p in (
                ("GET", "/sse"), ("GET", "/g8/sse"), ("GET", "/g8//sse"), ("GET", "/g8/%s/sse" % ("x" * 38)),
                ("GET", "/g8/%s/sse" % TOK[:-1]), ("POST", "/g8/wrong/messages/?session_id=" + "0" * 32))]
        with pytest.raises(Exception):
            await list_and_call(base + "/g8/%s/sse" % TOK2)
        return statuses

    assert run(serving(mocks, body)) == [404] * 6
    assert TOK not in caplog.text


def test_health_paths_answer_ok_and_nothing_else(mocks):
    async def body(base, gw):
        async with httpx2.AsyncClient() as http:
            return [((r := await http.get(base + p)).status_code, r.text) for p in ("/", "/ping", "/ping/", "/pong")]

    assert run(serving(mocks, body)) == [(200, "ok"), (200, "ok"), (404, "Not Found"), (404, "Not Found")]


def test_rotation_accepts_every_listed_token(mocks):
    async def body(base, gw):
        return [(await list_and_call(base + "/g8/%s/sse" % t))[1] for t in (TOK, TOK2)]

    assert run(serving(mocks, body, tokens=(TOK, TOK2))) == ["echo:hi", "echo:hi"]


def test_session_cannot_be_used_without_token(mocks):
    """The POST endpoint handed out in the endpoint event carries the token; stripping it gets a 404."""
    async def body(base, gw):
        async with httpx2.AsyncClient() as http:
            async with http.stream("GET", base + "/g8/%s/sse" % TOK) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        endpoint = line[5:].strip()
                        break
            msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
            bare = endpoint.replace("/g8/%s" % TOK, "")
            return endpoint, (await http.post(base + bare, json=msg)).status_code

    endpoint, status = run(serving(mocks, body))
    assert endpoint.startswith("/g8/%s/messages/?session_id=" % TOK)
    assert status == 404


@pytest.mark.parametrize("value", [None, "", " , ", "short", TOK + ",short", "a/" + "b" * 30])
def test_load_tokens_refuses_missing_or_weak(value):
    with pytest.raises(SystemExit) as e:
        front.load_tokens({} if value is None else {front.TOKEN_ENV: value})
    assert TOK not in str(e.value)


def test_load_tokens_splits():
    assert front.load_tokens({front.TOKEN_ENV: " %s , %s " % (TOK, TOK2)}) == [TOK, TOK2]
    assert front.valid(TOK2, [TOK, TOK2]) and not front.valid(TOK[:-1], [TOK, TOK2])
