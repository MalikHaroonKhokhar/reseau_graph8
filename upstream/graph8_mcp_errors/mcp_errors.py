"""MCP connect errors that name the real cause (HAR-87, proposed Graph8 patch).

The MCP client runs inside anyio task groups, so failures reach the caller as
`ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)")`. `str()`
of that group hides the cause.

`test_server` and `list_server_tools` are the bodies of
`POST /api/v1/voice/mcp-servers/{uuid}/test` and `GET .../{uuid}/tools`. The
Graph8 route loads the registration and returns `{"data": <result>}`. Both
handlers send failures through `describe_mcp_error`, so every message and log
line is unwrapped and redacted.
"""
import logging
import re
import tempfile
from typing import Iterable

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

try:
    BaseExceptionGroup
except NameError:  # Python 3.10: anyio installs the backport
    from exceptiongroup import BaseExceptionGroup

logger = logging.getLogger(__name__)

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


def _redact(text: str, secrets: Iterable[str]) -> str:
    # Mask longer values first so a value that contains another is fully masked.
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def _leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        yield f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _fetch_tools(server: dict, errlog):
    if server["transport_type"] == "sse":
        client = sse_client(server["connection_url"])
    else:
        # errlog: the child's stderr goes to a temp file, not Graph8's stderr, so it is redacted before anyone sees it.
        client = stdio_client(StdioServerParameters(
            command=server["command"], args=server.get("args") or [], env=server.get("env_vars")), errlog=errlog)
    async with client as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


async def _connect(server: dict, action: str):
    """Return (tools, None) on success or (None, redacted message) on failure."""
    with tempfile.TemporaryFile("w+") as errlog:
        try:
            return await _fetch_tools(server, errlog), None
        except Exception as exc:
            error = exc  # Python unbinds `exc` when the except block ends
            errlog.seek(0)
            stderr = errlog.read()
    secrets = [*(server.get("env_vars") or {}).values(), *(server.get("headers") or {}).values()]
    message = describe_mcp_error(error, redact=secrets)
    stderr = _redact(stderr, secrets).strip()
    if stderr:
        # ponytail: keeps only the last 2000 chars of stderr. Raise the limit if tracebacks get cut.
        message += "\nstderr: " + stderr[-2000:]
    # Log the redacted message only. exc_info would put the raw exception and its secrets back in the log.
    logger.warning("MCP %s failed for server %s: %s", action, server.get("mcp_server_id"), message)
    return None, message


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
