"""Upstream side of the Réseau MCP Gateway: hold credentials and sessions, list and call upstream tools.

Proven shape (test_connection/FINDINGS.md, Run 2): Streamable HTTP, protocol 2025-06-18, static
`Authorization: Bearer <token>`. GitHub issues an Mcp-Session-Id, Linear does not; the SDK transport
stores the id only when the server sends one and replays it only then.

Failure isolation: each upstream connects and fails on its own. A dead or unauthenticated upstream is
recorded in health() and raises UpstreamError on use; the others keep working.

Secrets: tokens go into request headers only. Every error message built here names the env var,
never its value. Tool results and upstream errors are scrubbed of every known token before they are
returned, and install_log_redaction() (run by Gateway) scrubs every log record in the process.
"""
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

log = logging.getLogger("reseau.gateway")

USER_AGENT = "reseau-gateway/0.1"  # be.graph8.com's Cloudflare bans library-default UAs; send an explicit one.
CONNECT_TIMEOUT = 30.0

# JSON-RPC server-error range codes for gateway-originated errors.
MISSING_CREDENTIAL = -32001
UNAUTHORIZED = -32002
UPSTREAM_UNAVAILABLE = -32003
UNKNOWN_UPSTREAM = -32004


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str
    token_env: str  # name of the env var holding the bearer token, never the token itself


DEFAULT_UPSTREAMS = (
    Upstream("github", "https://api.githubcopilot.com/mcp/", "GITHUB_MCP_TOKEN"),
    Upstream("linear", "https://mcp.linear.app/mcp", "LINEAR_API_KEY"),
)


class UpstreamError(MCPError):
    """Structured MCP error. data = {upstream, kind, env_var?}; the credential value is never included."""

    def __init__(self, code, message, upstream, kind, env_var=None):
        data = {"upstream": upstream, "kind": kind}
        if env_var:
            data["env_var"] = env_var
        super().__init__(code, message, data)


def resolve_credential(upstream, env=os.environ):
    token = (env.get(upstream.token_env) or "").strip()
    if not token:
        raise UpstreamError(MISSING_CREDENTIAL, "%s: credential env var %s is not set" % (upstream.name, upstream.token_env),
                            upstream.name, "missing_credential", upstream.token_env)
    return token


def build_headers(token):
    return {"Authorization": "Bearer " + token, "User-Agent": USER_AGENT}


def redact(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def scrub(value, secrets):
    """redact() over every string in a JSON-shaped value (keys too).
    ponytail: exact-substring match only; base64/URL-encoded echoes of a token (image/blob content) pass."""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {scrub(k, secrets): scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, secrets) for v in value]
    return value


def scrub_model(model, secrets):
    """Sanitize an SDK model (tool result: content, structured content, meta; or a Tool) before it leaves the gateway."""
    return type(model).model_validate(scrub(model.model_dump(by_alias=True, mode="json"), secrets))


def scrub_error(err, secrets):
    return MCPError(err.code, redact(err.message, secrets), scrub(err.data, secrets))


SECRETS = set()  # every token any Gateway has resolved; read by the log redaction below
_log_redaction_installed = False


def _scrub_record(record, secrets):
    record.msg, record.args = redact(record.getMessage(), secrets), None
    if record.exc_info:
        # Pre-format so handlers use exc_text; drop exc_info so no formatter re-renders the raw exception.
        record.exc_text = redact(logging.Formatter().formatException(record.exc_info), secrets)
        record.exc_info = None
    # Everything else on the record, including `extra` fields, stack_info and non-string objects a
    # formatter would str() (e.g. %(authorization)s).
    for key, value in list(vars(record).items()):
        if value is None or isinstance(value, (bool, int, float)):
            continue
        if isinstance(value, (str, dict, list, tuple)):
            clean = scrub(value, secrets)
            changed = clean != (list(value) if isinstance(value, tuple) else value)
        else:
            text = str(value)
            clean = redact(text, secrets)
            changed = clean != text
        if changed:
            setattr(record, key, clean)


def install_log_redaction(secrets=SECRETS):
    """Redact secrets from every log record in the process: message, formatted traceback, stack info and
    `extra` fields. Wraps Logger.makeRecord, the one point that sees a record after `extra` is attached
    (the record factory runs before it), and applies to every logger and handler, present or added later,
    including the SDK's and httpx's. Idempotent.
    ponytail: a Logger subclass that overrides makeRecord bypasses this; wrap it too if one shows up."""
    global _log_redaction_installed
    if _log_redaction_installed:
        return
    original = logging.Logger.makeRecord

    def make_record(self, *args, **kwargs):
        record = original(self, *args, **kwargs)
        if secrets:
            _scrub_record(record, secrets)
        return record

    logging.Logger.makeRecord = make_record
    _log_redaction_installed = True


class _Conn:
    """One upstream's live state. last_status is fed by an httpx hook: the SDK maps every HTTP 4xx to a
    generic JSON-RPC error, so the hook is how a 401/403 becomes an `unauthorized` error naming the env var."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.client = None
        self.error = None
        self.last_status = None
        self.stop = self.done = None

    async def on_response(self, response):
        self.last_status = response.status_code

    def classify(self, exc):
        """Map an exception to a structured error. A JSON-RPC error the upstream itself sent (tool failure,
        bad params) passes through unchanged; only transport-level failures become UpstreamError."""
        up = self.upstream
        if isinstance(exc, UpstreamError):
            return exc
        if isinstance(exc, MCPError) and (self.last_status or 200) < 400:
            return exc
        if self.last_status in (401, 403):
            return UpstreamError(UNAUTHORIZED, "%s: upstream rejected credential from %s (HTTP %d)"
                                 % (up.name, up.token_env, self.last_status), up.name, "unauthorized", up.token_env)
        detail = redact(exc.message, SECRETS) if isinstance(exc, MCPError) else type(exc).__name__
        return UpstreamError(UPSTREAM_UNAVAILABLE, "%s: upstream unavailable (%s)" % (up.name, detail),
                             up.name, "unavailable")


class Gateway:
    """Registry of upstreams. Use as `async with Gateway(upstreams) as gw:`; connections live for the block.

    Each upstream's SDK client lives in its own owner task, so upstreams connect concurrently, close or
    reconnect in any order, and a transport crash ends only that upstream's task."""

    def __init__(self, upstreams=DEFAULT_UPSTREAMS, env=os.environ):
        self.env = env
        self.secrets = SECRETS
        install_log_redaction()
        self.conns = {u.name: _Conn(u) for u in upstreams}

    async def __aenter__(self):
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        async with anyio.create_task_group() as init:
            for c in self.conns.values():
                init.start_soon(self._tg.start, self._own, c)
        return self

    async def __aexit__(self, *exc):
        for c in self.conns.values():
            c.stop.set()
        # Owners stop cleanly on their own; don't hand the body's exception to the task group, which would
        # wrap it in an ExceptionGroup. It propagates to the caller unchanged.
        await self._tg.__aexit__(None, None, None)

    async def _own(self, c, *, task_status):
        c.error, c.last_status, c.stop, c.done = None, None, anyio.Event(), anyio.Event()
        started = False
        try:
            token = resolve_credential(c.upstream, self.env)
            self.secrets.add(token)
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(httpx2.AsyncClient(
                    headers=build_headers(token),
                    timeout=httpx2.Timeout(CONNECT_TIMEOUT, read=300.0),
                    event_hooks={"response": [c.on_response]},
                ))
                c.client = await stack.enter_async_context(
                    Client(streamable_http_client(c.upstream.url, http_client=http), mode="legacy"))
                started = True
                task_status.started()
                await c.stop.wait()
        except Exception as exc:
            err = c.classify(exc)
            if not isinstance(err, UpstreamError):
                err = UpstreamError(UPSTREAM_UNAVAILABLE, "%s: initialize failed (%s)" % (c.upstream.name, redact(err.message, self.secrets)),
                                    c.upstream.name, "unavailable")
            c.error = c.error or err
            log.warning("upstream %s down: %s", c.upstream.name, redact(c.error.message, self.secrets))
        finally:
            c.client = None
            c.done.set()
            if not started:
                task_status.started()

    async def _close(self, c):
        c.stop.set()
        await c.done.wait()

    async def reconnect(self, name):
        c = self._conn(name)
        await self._close(c)
        await self._tg.start(self._own, c)

    def health(self):
        return {n: {"ok": c.client is not None and c.error is None,
                    "error": None if c.error is None else {"code": c.error.code, "message": c.error.message, **c.error.data}}
                for n, c in self.conns.items()}

    def _conn(self, name):
        if name not in self.conns:
            raise UpstreamError(UNKNOWN_UPSTREAM, "unknown upstream %r" % name, name, "unknown_upstream")
        return self.conns[name]

    async def _use(self, name, op):
        """Run op on the upstream. Health: a transport failure (unauthorized/unavailable) is recorded in
        c.error; the next call that reaches the upstream (success, or an upstream-sent JSON-RPC error)
        clears it. Unauthorized also closes the session, so only reconnect() recovers from it."""
        c = self._conn(name)
        if c.client is None:
            raise c.error or UpstreamError(UPSTREAM_UNAVAILABLE, "%s: not connected" % name, name, "unavailable")
        c.last_status = None  # ponytail: shared per upstream, concurrent calls may misattribute a 401; per-request status if that bites
        try:
            result = await op(c.client)
        except Exception as exc:
            err = c.classify(exc)
            if not isinstance(err, UpstreamError):
                c.error = None  # upstream answered with its own error: reachable
                raise scrub_error(err, self.secrets) from None
            c.error = err
            if err.data["kind"] == "unauthorized":
                await self._close(c)
            raise err from None
        c.error = None
        return result

    async def list_tools(self, name):
        async def op(client):
            tools, cursor = [], None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools += page.tools
                cursor = page.next_cursor
                if not cursor:
                    return tools
        return [scrub_model(t, self.secrets) for t in await self._use(name, op)]

    async def call_tool(self, name, tool, arguments=None):
        return scrub_model(await self._use(name, lambda client: client.call_tool(tool, arguments or {})), self.secrets)


if __name__ == "__main__":
    # Live smoke (manual, needs network + real tokens in env): python -m reseau.gateway
    import asyncio

    async def smoke():
        async with Gateway() as gw:
            for name, tool in (("github", "get_me"), ("linear", "list_teams")):
                try:
                    tools = await gw.list_tools(name)
                    res = await gw.call_tool(name, tool)
                    print(name, len(tools), "tools;", tool, "->", "error" if res.is_error else "ok",
                          redact(str(res.content)[:200], gw.secrets))
                except UpstreamError as e:
                    print(name, "FAILED:", e.message)

    asyncio.run(smoke())
