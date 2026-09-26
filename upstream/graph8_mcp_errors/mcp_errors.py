"""Readable error messages for MCP connection failures (HAR-87, proposed Graph8 patch).

The MCP client runs inside anyio task groups, so failures reach the caller as
`ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)")`, and
`str()` of that group hides the real cause. `describe_mcp_error` lists every
leaf instead.

Wiring on the Graph8 side, in both `POST /{uuid}/test` and `GET /{uuid}/tools`:

    except Exception as exc:
        message = describe_mcp_error(exc, redact=[*server.env_vars.values(), *headers.values()])
        logger.warning("MCP connect failed for %s: %s", server.mcp_server_id, message)

Log the returned message, not `exc` or its traceback, so the redaction also covers the logs.
"""
from typing import Iterable

try:
    BaseExceptionGroup
except NameError:  # Python 3.10: anyio installs the backport
    from exceptiongroup import BaseExceptionGroup


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
    # Mask longer values first so a value that contains another is fully masked.
    for secret in sorted({s for s in redact if s}, key=len, reverse=True):
        message = message.replace(secret, "***")
    return message


def _leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _leaves(sub)
    else:
        # httpx adds a second "For more information check: <mdn url>" line. Drop it.
        text = str(exc).split("\n", 1)[0]
        yield f"{type(exc).__name__}: {text}" if text else type(exc).__name__
