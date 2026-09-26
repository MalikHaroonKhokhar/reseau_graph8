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
    # Mask longer values first so a value that contains another is fully masked.
    for secret in sorted({s for s in redact if s}, key=len, reverse=True):
        message = message.replace(secret, "***")
    return _HTTPX_DOCS_SUFFIX.sub("", message)


def _leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        yield f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def _fetch_tools(server: dict):
    if server["transport_type"] == "sse":
        client = sse_client(server["connection_url"])
    else:
        client = stdio_client(StdioServerParameters(
            command=server["command"], args=server.get("args") or [], env=server.get("env_vars")))
    async with client as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


def _failure(server: dict, action: str, exc: Exception) -> str:
    secrets = [*(server.get("env_vars") or {}).values(), *(server.get("headers") or {}).values()]
    message = describe_mcp_error(exc, redact=secrets)
    # Log the redacted message only. exc_info would put the raw exception and its secrets back in the log.
    logger.warning("MCP %s failed for server %s: %s", action, server.get("mcp_server_id"), message)
    return message


async def test_server(server: dict) -> dict:
    try:
        tools = await _fetch_tools(server)
    except Exception as exc:
        return {"success": False, "message": _failure(server, "test", exc), "tools_count": None}
    return {"success": True, "message": "Connection successful", "tools_count": len(tools)}


async def list_server_tools(server: dict) -> dict:
    try:
        tools = await _fetch_tools(server)
    except Exception as exc:
        return {"success": False, "message": _failure(server, "tools", exc), "tools": None}
    return {"success": True, "message": "Connection successful",
            "tools": [t.model_dump(exclude_none=True) for t in tools]}
