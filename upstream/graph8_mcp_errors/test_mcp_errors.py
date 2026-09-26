"""Run: uv run --python 3.12 --with pytest --with "mcp<2" python -m pytest upstream/graph8_mcp_errors -q"""
import socket
import threading
import time

import anyio
import httpx
import pytest

from mcp_errors import describe_mcp_error


def _http_405():
    request = httpx.Request("GET", "https://learn.microsoft.com/api/mcp")
    response = httpx.Response(405, request=request)
    return httpx.HTTPStatusError(
        "Client error '405 Method Not Allowed' for url 'https://learn.microsoft.com/api/mcp'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/405",
        request=request,
        response=response,
    )


# --- unit ---

def test_group_is_unwrapped_to_leaf():
    exc = ExceptionGroup("unhandled errors in a TaskGroup", [_http_405()])
    assert str(exc) == "unhandled errors in a TaskGroup (1 sub-exception)"  # what /test returns today
    assert describe_mcp_error(exc) == (
        "HTTPStatusError: Client error '405 Method Not Allowed' "
        "for url 'https://learn.microsoft.com/api/mcp'"
    )


def test_nested_groups_list_every_leaf():
    exc = ExceptionGroup("outer", [
        ExceptionGroup("inner", [ValueError("bad json"), TimeoutError()]),
        ConnectionResetError("peer closed"),
    ])
    assert describe_mcp_error(exc) == "ValueError: bad json; TimeoutError; ConnectionResetError: peer closed"


def test_plain_exception_message_unchanged():
    exc = FileNotFoundError(2, "No such file or directory")
    exc.filename = "npx"
    assert describe_mcp_error(exc) == "[Errno 2] No such file or directory: 'npx'"


def test_secrets_are_redacted():
    exc = ExceptionGroup("g", [RuntimeError("auth failed for token lin_api_SECRET at https://gw/g8/cap123/sse")])
    msg = describe_mcp_error(exc, redact=["lin_api_SECRET", "cap123", ""])
    assert "lin_api_SECRET" not in msg and "cap123" not in msg
    assert msg == "RuntimeError: auth failed for token *** at https://gw/g8/***/sse"


# --- integration: the real MCP client, doing what Graph8's /test does ---

async def _graph8_style_test(client_cm):
    from mcp import ClientSession

    async with client_cm as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            return len((await session.list_tools()).tools)


def _run_and_describe(client_cm):
    try:
        anyio.run(_graph8_style_test, client_cm)
    except Exception as exc:
        return exc
    pytest.fail("connection unexpectedly succeeded")


@pytest.fixture(scope="module")
def streamable_only_url():
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("streamable-only")

    @mcp.tool()
    def ping() -> str:
        return "pong"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True


def test_sse_client_against_streamable_only_server(streamable_only_url):
    from mcp.client.sse import sse_client

    exc = _run_and_describe(sse_client(streamable_only_url))
    msg = describe_mcp_error(exc)
    print("sse -> streamable:", repr(str(exc)), "=>", repr(msg))
    assert "TaskGroup" not in msg
    # The streamable endpoint rejects the SSE GET. The status depends on the server (400 here, 405 on learn.microsoft.com).
    assert msg.startswith("HTTPStatusError: Client error '4")


def test_stdio_process_that_is_not_an_mcp_server():
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command="sh", args=["-c", "echo not-json; exit 0"], env={"TOKEN": "s3cret"})
    exc = _run_and_describe(stdio_client(params))
    msg = describe_mcp_error(exc, redact=params.env.values())
    print("stdio sh:", repr(str(exc)), "=>", repr(msg))
    assert "TaskGroup" not in msg and msg
