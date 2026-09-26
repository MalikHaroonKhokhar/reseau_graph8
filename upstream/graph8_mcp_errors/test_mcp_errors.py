"""Run: uv run --python 3.12 --with pytest --with "mcp<2" --with uvicorn python -m pytest upstream/graph8_mcp_errors -q"""
import contextlib
import logging
import socket
import sys
import threading
import time

import anyio
import httpx
import pytest

import mcp_errors  # module import: pytest would otherwise collect mcp_errors.test_server as a test
from mcp_errors import describe_mcp_error

CANARY = "canary-first-line\ncanary-second-line"


def _http_405():
    request = httpx.Request("GET", "https://learn.microsoft.com/api/mcp")
    response = httpx.Response(405, request=request)
    return httpx.HTTPStatusError(
        "Client error '405 Method Not Allowed' for url 'https://learn.microsoft.com/api/mcp'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/405",
        request=request,
        response=response,
    )


def _assert_no_canary(text):
    assert "canary-first-line" not in text and "canary-second-line" not in text


# --- describe_mcp_error ---

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
    assert msg == "RuntimeError: auth failed for token *** at https://gw/g8/***/sse"


def test_multiline_secret_is_fully_redacted():
    exc = ExceptionGroup("g", [RuntimeError(f"child printed {CANARY} then exited")])
    msg = describe_mcp_error(exc, redact=[CANARY])
    _assert_no_canary(msg)
    assert msg == "RuntimeError: child printed *** then exited"


def test_multiline_diagnostics_are_kept():
    exc = ExceptionGroup("g", [RuntimeError("Transport failed\nHTTP 405; content-type: application/json")])
    assert describe_mcp_error(exc) == "RuntimeError: Transport failed\nHTTP 405; content-type: application/json"


# --- endpoint handlers, mocked client (the ticket's red test) ---

@pytest.fixture
def failing_sse(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_sse_client(url):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError(f"upstream said {CANARY}"), _http_405()])
        yield

    monkeypatch.setattr(mcp_errors, "sse_client", fake_sse_client)
    return {"mcp_server_id": "uuid-1", "transport_type": "sse", "connection_url": "https://learn.microsoft.com/api/mcp",
            "env_vars": {"TOKEN": CANARY}, "headers": {"Authorization": "Bearer hdr-secret"}}


@pytest.mark.parametrize("handler, empty_field", [("test_server", "tools_count"), ("list_server_tools", "tools")])
def test_handler_reports_leaves_and_logs_without_secrets(failing_sse, caplog, handler, empty_field):
    caplog.set_level(logging.DEBUG)
    result = anyio.run(getattr(mcp_errors, handler), failing_sse)

    assert result == {
        "success": False,
        "message": "RuntimeError: upstream said ***; HTTPStatusError: Client error '405 Method Not Allowed' "
                   "for url 'https://learn.microsoft.com/api/mcp'",
        empty_field: None,
    }
    assert "uuid-1" in caplog.text and "405 Method Not Allowed" in caplog.text
    _assert_no_canary(caplog.text)
    assert all(r.exc_info is None for r in caplog.records)


# --- endpoint handlers, real MCP client ---

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


def test_sse_registration_of_streamable_only_server(streamable_only_url, caplog):
    server = {"mcp_server_id": "uuid-2", "transport_type": "sse", "connection_url": streamable_only_url}
    result = anyio.run(mcp_errors.test_server, server)
    print("sse -> streamable:", result)

    assert result["success"] is False and result["tools_count"] is None
    # The streamable endpoint rejects the SSE GET. The status depends on the server (400 here, 405 on learn.microsoft.com).
    assert result["message"].startswith("HTTPStatusError: Client error '4")
    assert result["message"] in caplog.text


@pytest.mark.parametrize("handler", ["test_server", "list_server_tools"])
def test_stdio_failure_redacts_child_stderr(caplog, capfd, handler):
    caplog.set_level(logging.DEBUG, logger="mcp_errors")
    server = {"mcp_server_id": "uuid-3", "transport_type": "stdio", "command": "sh",
              "args": ["-c", 'echo "boom: token=$TOKEN" >&2; exit 3'], "env_vars": {"TOKEN": CANARY}}
    result = anyio.run(getattr(mcp_errors, handler), server)
    out, err = capfd.readouterr()  # fd-level: catches what the child process writes, which caplog cannot see

    assert result["success"] is False
    assert result["message"] == "McpError: Connection closed\nstderr: boom: token=***"
    for text in (result["message"], caplog.text, out, err):
        _assert_no_canary(text)


LONG_TOKEN = "tok_" + "0123456789abcdef" * 4  # over 50 chars, so pydantic truncates it in its error


@pytest.mark.parametrize("secret", [CANARY, LONG_TOKEN], ids=["multiline", "truncated"])
def test_sdk_parse_error_logs_are_redacted(caplog, capfd, secret):
    caplog.set_level(logging.DEBUG)
    server = {"mcp_server_id": "uuid-4", "transport_type": "stdio", "command": "sh",
              "args": ["-c", 'printf "%s\\n" "$TOKEN"; exit 0'], "env_vars": {"TOKEN": secret}}
    result = anyio.run(mcp_errors.test_server, server)
    out, err = capfd.readouterr()

    assert result["success"] is False
    assert "Failed to parse JSONRPC message from server" in caplog.text  # the SDK path under test ran
    assert all(r.exc_info is None for r in caplog.records)
    for text in (result["message"], caplog.text, out, err):
        _assert_no_canary(text)
        assert "0123456789" not in text


def test_non_utf8_stderr_still_reports_failure():
    server = {"transport_type": "stdio", "command": "sh", "args": ["-c", "printf '\\377boom\\n' >&2; exit 1"]}
    result = anyio.run(mcp_errors.test_server, server)
    assert result == {"success": False, "message": "McpError: Connection closed\nstderr: \ufffdboom", "tools_count": None}


def test_redaction_is_scoped_to_the_connection(caplog):
    caplog.set_level(logging.INFO)
    logging.getLogger("elsewhere").info("tok_0123456789abcdef is fine here")
    assert "tok_0123456789abcdef" in caplog.text


STDIO_SERVER = """
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("ok")

@mcp.tool()
def ping() -> str:
    return "pong"

mcp.run()
"""


@pytest.mark.parametrize("handler", ["test_server", "list_server_tools"])
def test_working_server_still_succeeds(handler):
    server = {"transport_type": "stdio", "command": sys.executable, "args": ["-c", STDIO_SERVER]}
    result = anyio.run(getattr(mcp_errors, handler), server)
    assert result["success"] is True and result["message"] == "Connection successful"
    assert result.get("tools_count", 1) == 1 and [t["name"] for t in result.get("tools", [{"name": "ping"}])] == ["ping"]


# --- HAR-88: a hung server fails within the timeout and names the phase ---

# Answers initialize, then never answers tools/list.
HANGS_ON_LIST = """
import json, sys, time
req = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {
    "protocolVersion": req["params"]["protocolVersion"], "capabilities": {"tools": {}},
    "serverInfo": {"name": "hang", "version": "0"}}}), flush=True)
time.sleep(100)
"""


@pytest.fixture(scope="module")
def silent_tcp_url():
    """Accepts connections and never sends a byte."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    conns = []
    threading.Thread(target=lambda: conns.extend(iter(lambda: sock.accept()[0], None)), daemon=True).start()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}/sse"
    sock.close()


@pytest.mark.parametrize("handler", ["test_server", "list_server_tools"])
@pytest.mark.parametrize("phase", ["connect", "initialize", "tools/list"])
def test_hung_server_times_out_with_phase(monkeypatch, silent_tcp_url, handler, phase):
    monkeypatch.setattr(mcp_errors, "MCP_CONNECT_TIMEOUT", 1)
    server = {
        "connect": {"transport_type": "sse", "connection_url": silent_tcp_url},
        # The ticket's reproduction.
        "initialize": {"transport_type": "stdio", "command": sys.executable, "args": ["-c", "import time; time.sleep(100)"]},
        "tools/list": {"transport_type": "stdio", "command": sys.executable, "args": ["-c", HANGS_ON_LIST]},
    }[phase]
    start = time.monotonic()
    result = anyio.run(getattr(mcp_errors, handler), server)
    elapsed = time.monotonic() - start

    assert elapsed < 1 + 3  # timeout plus the SDK's 2s child-termination grace
    assert result["success"] is False
    assert result["message"] == f"Timed out after 1s during {phase}"
