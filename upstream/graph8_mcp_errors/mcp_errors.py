"""MCP connect errors that name the real cause (HAR-87, proposed Graph8 patch).

The MCP client runs inside anyio task groups, so failures reach the caller as
`ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)")`. `str()`
of that group hides the cause.

`test_server` and `list_server_tools` are the bodies of
`POST /api/v1/voice/mcp-servers/{uuid}/test` and `GET .../{uuid}/tools`. The
Graph8 route loads the registration and returns `{"data": <result>}`. Both
handlers send failures through `describe_mcp_error`, so every message is
unwrapped and redacted.

Connect, initialize and tools/list share one deadline, `MCP_CONNECT_TIMEOUT`
(HAR-88). A hung server gets `success: false` naming the phase, instead of
holding the request open until Cloudflare returns 502.

While a connection is open, every log record created in its task tree, including
the MCP SDK's (some go to the root logger), is redacted by the log record
factory installed below. That factory also drops `exc_info`, because the SDK's
tracebacks carry raw server output.
"""
import contextvars
import json
import logging
import re
import tempfile
from typing import Iterable

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

try:
    BaseExceptionGroup
except NameError:  # Python 3.10: anyio installs the backport
    from exceptiongroup import BaseExceptionGroup

logger = logging.getLogger(__name__)

# Graph8's call. Hung requests were measured returning 502 at 15.6-15.9s (test_connection/FINDINGS.md,
# Run 3), so this stays well under that: the SDK can add ~2s terminating a stdio child, plus route overhead.
MCP_CONNECT_TIMEOUT = 10

# httpx appends this to HTTPStatusError messages. It is noise, not diagnosis.
_HTTPX_DOCS_SUFFIX = re.compile(r"\nFor more information check: https://developer\.mozilla\.org/\S*")


def describe_mcp_error(exc: BaseException, redact: Iterable[str] = ()) -> str:
    """Return the exception's message. A group is replaced by its leaves as
    `Type: message`, joined with `; `. Every non-empty value in `redact` is
    masked as `***`.

    A plain exception keeps its existing `str()`, so messages such as
    `[Errno 2] No such file or directory: 'npx'` stay the same.
    """
    if isinstance(exc, BaseExceptionGroup):
        message = "; ".join(_leaves(exc))
    else:
        message = str(exc) or type(exc).__name__
    # Redact the full text before trimming anything, so a multiline secret cannot be cut in half.
    return _HTTPX_DOCS_SUFFIX.sub("", _redact(message, redact))


# ponytail: a secret cut shorter than this by a truncating repr can still show up to 7 chars.
_MIN_FRAGMENT = 8


def _redact(text: str, secrets: Iterable[str]) -> str:
    # Also match the escaped forms: pydantic/repr and JSON turn a newline into a literal \n.
    variants = sorted({v for s in secrets if s for v in (s, repr(s)[1:-1], json.dumps(s)[1:-1])},
                      key=len, reverse=True)
    # Whole values first, longest first, so a value that contains another is fully masked.
    for v in variants:
        text = text.replace(v, "***")
    # Then head and tail fragments: pydantic shows a long value as its first 24 + last 23 chars.
    for v in variants:
        for k in range(len(v) - 1, _MIN_FRAGMENT - 1, -1):
            text = text.replace(v[:k], "***").replace(v[-k:], "***")
    return text


_log_secrets = contextvars.ContextVar("mcp_log_secrets", default=())
_base_record_factory = logging.getLogRecordFactory()


def _redacting_record_factory(*args, **kwargs):
    record = _base_record_factory(*args, **kwargs)
    secrets = _log_secrets.get()
    if secrets:
        try:
            message = record.getMessage()
        except Exception:  # bad %-args: logging would report it at emit time anyway
            message = str(record.msg)
        record.msg, record.args = _redact(message, secrets), ()
        if record.exc_info:
            # Pre-render the traceback redacted. Formatter prints exc_text. Clearing exc_info also stops
            # error trackers from capturing the raw exception and its locals.
            record.exc_text = _redact(logging.Formatter().formatException(record.exc_info), secrets)
            record.exc_info = None
    return record


logging.setLogRecordFactory(_redacting_record_factory)


def _leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        yield f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _fetch_tools(server: dict, errlog, phase: list):
    if server["transport_type"] == "sse":
        client = sse_client(server["connection_url"])
    else:
        # errlog: the child's stderr goes to a temp file, not Graph8's stderr, so it is redacted before anyone sees it.
        client = stdio_client(StdioServerParameters(
            command=server["command"], args=server.get("args") or [], env=server.get("env_vars")), errlog=errlog)
    async with client as (read, write):
        async with ClientSession(read, write) as session:
            phase[0] = "initialize"
            await session.initialize()
            phase[0] = "tools/list"
            return (await session.list_tools()).tools


async def _connect(server: dict, action: str):
    """Return (tools, None) on success or (None, redacted message) on failure."""
    secrets = tuple(v for v in [*(server.get("env_vars") or {}).values(), *(server.get("headers") or {}).values()] if v)
    token = _log_secrets.set(secrets)  # anyio tasks spawned inside the client inherit this
    try:
        # errors="replace": a child can write any bytes, and a decode error must not replace the real failure.
        with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errlog:
            phase = ["connect"]
            try:
                with anyio.move_on_after(MCP_CONNECT_TIMEOUT):
                    return await _fetch_tools(server, errlog, phase), None
                message = f"Timed out after {MCP_CONNECT_TIMEOUT}s during {phase[0]}"
            except Exception as exc:
                message = describe_mcp_error(exc, redact=secrets)
            errlog.seek(0)
            stderr = errlog.read()
        stderr = _redact(stderr, secrets).strip()
        if stderr:
            # ponytail: keeps only the last 2000 chars of stderr. Raise the limit if tracebacks get cut.
            message += "\nstderr: " + stderr[-2000:]
        logger.warning("MCP %s failed for server %s: %s", action, server.get("mcp_server_id"), message)
        return None, message
    finally:
        _log_secrets.reset(token)


async def test_server(server: dict) -> dict:
    tools, error = await _connect(server, "test")
    if error:
        return {"success": False, "message": error, "tools_count": None}
    return {"success": True, "message": "Connection successful", "tools_count": len(tools)}


async def list_server_tools(server: dict) -> dict:
    tools, error = await _connect(server, "tools")
    if error:
        return {"success": False, "message": error, "tools": None}
    return {"success": True, "message": "Connection successful",
            "tools": [t.model_dump(exclude_none=True) for t in tools]}
