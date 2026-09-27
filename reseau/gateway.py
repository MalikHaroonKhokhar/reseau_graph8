"""Upstream side of the Réseau MCP Gateway: hold credentials and sessions, list and call upstream tools.

Proven shape (test_connection/FINDINGS.md, Run 2): Streamable HTTP, protocol 2025-06-18, static
`Authorization: Bearer <token>`. GitHub issues an Mcp-Session-Id, Linear does not; the SDK transport
stores the id only when the server sends one and replays it only then.

Failure isolation: each upstream connects and fails on its own. A dead or unauthenticated upstream is
recorded in health() and raises UpstreamError on use; the others keep working.

Reliability: every request goes through _Reliable, which applies reseau/outbound.py's policy (per-host
concurrency cap, backoff on 429/5xx honouring Retry-After) to the SDK's async transport. Retried: idempotent
HTTP methods, the MCP handshake/listing methods, and the upstream's bootstrap tool; never other tools/call.

Secrets: tokens go into request headers only. Every error message built here names the env var,
never its value. Tool results and upstream errors are scrubbed of every known token before they are
returned, and install_log_redaction() (run by Gateway) scrubs every log record in the process.
"""
import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from reseau import evidence, outbound, semantic

log = logging.getLogger("reseau.gateway")

USER_AGENT = outbound.USER_AGENT  # be.graph8.com's Cloudflare bans library-default UAs; send an explicit one.
RETRY_POLICY = outbound.RetryPolicy()
CONNECT_TIMEOUT = 30.0

# JSON-RPC server-error range codes for gateway-originated errors.
MISSING_CREDENTIAL = -32001
UNAUTHORIZED = -32002
UPSTREAM_UNAVAILABLE = -32003
UNKNOWN_UPSTREAM = -32004
CONTEXT_NOT_ESTABLISHED = -32005
UNKNOWN_TOOL = -32006
TOOL_NOT_ALLOWED = -32007
# -32008, -32009, -32010: reseau/evidence (not found, upstream error, search incomplete)
# -32011: reseau/semantic (unmapped person)
# Graph8's own JSON-RPC code for "Org context not established for this session. Call g8_current_org first".
# Upstream-sent, so it shares the number with UPSTREAM_UNAVAILABLE but never meets it: only matched inside call_tool.
GRAPH8_ORG_GATE = -32003


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str
    token_env: str  # name of the env var holding the bearer token, never the token itself
    # Tool that establishes per-key server context, called before the first tool call and once more on
    # gate_code. Graph8 tracks org context by API key, with no Mcp-Session-Id to resume it (FINDINGS.md).
    bootstrap_tool: str | None = None
    gate_code: int | None = None
    # Prepended to every raw tool name this upstream exposes; None = "<name>_". Raw names collide across
    # upstreams (GitHub and Linear both have list_issues, list_releases: FINDINGS.md Run 2).
    prefix: str | None = None
    # Raw tool names this upstream may list and call; None = pass everything through. A blocked call is
    # rejected before it reaches the upstream. The bootstrap tool is called internally either way.
    allow: frozenset[str] | None = None


# Read-only endpoints (FINDINGS.md: GitHub /mcp/readonly 27 tools, Linear /mcp/readonly 35, no write tools)
# plus a minimal allowlist for the semantic tools (HAR-100, HAR-101, HAR-109); grow it from those tickets.
DEFAULT_UPSTREAMS = (
    Upstream("github", "https://api.githubcopilot.com/mcp/readonly", "GITHUB_MCP_TOKEN",
             allow=frozenset({"get_me", "list_commits", "get_commit", "list_pull_requests", "pull_request_read",
                              "list_issues", "issue_read", "search_pull_requests", "search_repositories"})),
    Upstream("linear", "https://mcp.linear.app/mcp/readonly", "LINEAR_API_KEY",
             allow=frozenset({"list_teams", "list_issues", "get_issue", "list_comments", "list_projects",
                              "get_project"})),
    # ponytail: Graph8 has no known read-only endpoint; the allowlist alone keeps its 126 tools out.
    Upstream("graph8", "https://be.graph8.com/mcp/", "GRAPH8_API_KEY", "g8_current_org", GRAPH8_ORG_GATE,
             prefix="",  # Graph8 already names its tools g8_*
             allow=frozenset({"g8_current_org"})),
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


def allowed(upstream, raw):
    return upstream.allow is None or raw in upstream.allow


def exposed_name(upstream, raw):
    return (upstream.name + "_" if upstream.prefix is None else upstream.prefix) + raw


def merge_tools(listed):
    """[(Upstream, [Tool])] -> (tools renamed to their exposed names, {exposed: (upstream name, raw name)}).
    Pure; the same listing always yields the same names. Raises ValueError if two exposed names are equal."""
    tools, routes = [], {}
    for up, raw_tools in listed:
        for t in raw_tools:
            name = exposed_name(up, t.name)
            if name in routes:
                raise ValueError("tool name collision: %r from %s/%s and %s/%s"
                                 % (name, *routes[name], up.name, t.name))
            routes[name] = (up.name, t.name)
            tools.append(t.model_copy(update={"name": name}))
    return tools, routes


class _Conn:
    """One upstream's live state. last_status is fed by an httpx hook: the SDK maps every HTTP 4xx to a
    generic JSON-RPC error, so the hook is how a 401/403 becomes an `unauthorized` error naming the env var."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.client = None
        self.error = None
        self.last_status = None
        self.context_gen = 0  # bumped by each bootstrap; 0 = never bootstrapped on this connection
        self.bootstrap_lock = None
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
        if self.last_status == 429:  # still throttled after _Reliable's backoff
            return UpstreamError(UPSTREAM_UNAVAILABLE, "%s: upstream rate limited (HTTP 429)" % up.name,
                                 up.name, "rate_limited")
        detail = redact(exc.message, SECRETS) if isinstance(exc, MCPError) else type(exc).__name__
        return UpstreamError(UPSTREAM_UNAVAILABLE, "%s: upstream unavailable (%s)" % (up.name, detail),
                             up.name, "unavailable")


class _SlotStream(httpx2.AsyncByteStream):
    """Response body that frees its concurrency slot when closed, so the cap covers the whole exchange
    (a Streamable HTTP POST can answer with SSE headers first and the tool result much later)."""

    def __init__(self, stream, release):
        self.stream, self.release = stream, release

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            if self.release:
                self.release, release = None, self.release
                release()


class _Reliable(httpx2.AsyncBaseTransport):
    """outbound.Client's policy for the SDK's async httpx transport. outbound.Client itself is sync and
    buffers bodies, which the SDK's streaming (SSE) exchange can't use; the rules are shared, not copied."""

    def __init__(self, slots, retry_tools=frozenset(), inner=None):
        self.slots, self.retry_tools = slots, retry_tools
        self.inner = inner or httpx2.AsyncHTTPTransport()

    def _retryable(self, request):
        if request.method in outbound.IDEMPOTENT_METHODS:
            return True
        try:
            msg = json.loads(request.content)
        except ValueError:
            return False
        if not isinstance(msg, dict):
            return False
        if msg.get("method") == "tools/call":
            return (msg.get("params") or {}).get("name") in self.retry_tools
        return msg.get("method") in outbound.SAFE_MCP_METHODS

    async def handle_async_request(self, request):
        # The standalone GET SSE stream stays open for the whole session; capping it would pin a slot forever.
        slot = None if request.method == "GET" else self.slots.setdefault(
            outbound.host_key(str(request.url)), anyio.Semaphore(outbound.MAX_PER_HOST))
        retry = self._retryable(request)
        attempt = 0
        while True:
            attempt += 1
            if slot:
                await slot.acquire()
            try:
                resp = await self.inner.handle_async_request(request)
            except BaseException:
                if slot:
                    slot.release()
                raise
            if slot:
                # ponytail: a response the caller never closes leaks its slot; the SDK always closes them.
                resp.stream = _SlotStream(resp.stream, slot.release)
            wait = None
            if retry and resp.status_code in outbound.RETRY_STATUSES:
                wait = RETRY_POLICY.delay(attempt, outbound.parse_retry_after(resp.headers.get("retry-after")))
            if wait is None:
                return resp
            await resp.aclose()
            log.warning("retrying %s %s in %.2fs after HTTP %d", request.method, request.url, wait, resp.status_code)
            await anyio.sleep(wait)

    async def aclose(self):
        await self.inner.aclose()


class Gateway:
    """Registry of upstreams. Use as `async with Gateway(upstreams) as gw:`; connections live for the block.

    Each upstream's SDK client lives in its own owner task, so upstreams connect concurrently, close or
    reconnect in any order, and a transport crash ends only that upstream's task."""

    def __init__(self, upstreams=DEFAULT_UPSTREAMS, env=os.environ, identities=None):
        self.env = env
        self.identities = evidence.load_identities(env) if identities is None else evidence.identity_index(identities)
        self.tz = semantic.load_tz(env)
        self.secrets = SECRETS
        install_log_redaction()
        self.conns = {u.name: _Conn(u) for u in upstreams}
        self.slots = {}  # host_key -> semaphore, shared by every upstream on that host
        self.routes = {}  # exposed tool name -> (upstream name, raw name), rebuilt by tools()

    async def __aenter__(self):
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        async with anyio.create_task_group() as init:
            for c in self.conns.values():
                init.start_soon(self._tg.start, self._own, c)
        try:
            await self.tools()  # startup check: a tool-name collision stops the gateway here, not at first tools/list
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc):
        for c in self.conns.values():
            c.stop.set()
        # Owners stop cleanly on their own; don't hand the body's exception to the task group, which would
        # wrap it in an ExceptionGroup. It propagates to the caller unchanged.
        await self._tg.__aexit__(None, None, None)

    async def _own(self, c, *, task_status):
        c.error, c.last_status, c.stop, c.done = None, None, anyio.Event(), anyio.Event()
        c.context_gen, c.bootstrap_lock = 0, anyio.Lock()
        started = False
        try:
            token = resolve_credential(c.upstream, self.env)
            self.secrets.add(token)
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(httpx2.AsyncClient(
                    headers=build_headers(token),
                    timeout=httpx2.Timeout(CONNECT_TIMEOUT, read=300.0),
                    event_hooks={"response": [c.on_response]},
                    transport=_Reliable(self.slots, {c.upstream.bootstrap_tool} - {None}),
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
        up = self._conn(name).upstream
        return [scrub_model(t, self.secrets) for t in await self._use(name, op) if allowed(up, t.name)]

    async def call_tool(self, name, tool, arguments=None):
        c = self._conn(name)
        up = c.upstream
        if not allowed(up, tool):
            raise UpstreamError(TOOL_NOT_ALLOWED, "%s: tool %r is not allowlisted" % (name, tool), name, "tool_not_allowed")
        args = arguments or {}

        async def establish(client, stale_gen):
            """Bootstrap unless another caller already did since stale_gen: concurrent callers share one call."""
            async with c.bootstrap_lock:
                if c.context_gen == stale_gen:
                    await client.call_tool(up.bootstrap_tool, {})
                    c.context_gen += 1

        async def op(client):
            if not up.bootstrap_tool:
                return await client.call_tool(tool, args)
            if c.context_gen == 0:
                await establish(client, 0)
            seen = c.context_gen
            try:
                return await client.call_tool(tool, args)
            except MCPError as exc:
                if exc.code != up.gate_code:
                    raise
            await establish(client, seen)  # context lost mid-session: re-establish once, retry once, never loop
            try:
                return await client.call_tool(tool, args)
            except MCPError as exc:
                if exc.code != up.gate_code:
                    raise
            raise UpstreamError(CONTEXT_NOT_ESTABLISHED, "%s: context still not established after %s; not retrying"
                                % (name, up.bootstrap_tool), name, "context_not_established")

        return scrub_model(await self._use(name, op), self.secrets)

    async def tools(self):
        """The merged tools/list: every reachable upstream's tools under their exposed names. A down upstream
        is left out (health() says why), and so is one that answers tools/list with its own error: one bad
        upstream never takes down the surface or startup. Réseau's own tools come last.
        Raises ValueError on a name collision."""
        listed = []
        for c in self.conns.values():
            try:
                listed.append((c.upstream, await self.list_tools(c.upstream.name)))
            except MCPError as exc:
                if not isinstance(exc, UpstreamError):
                    log.warning("upstream %s tools/list failed: %s", c.upstream.name, exc.message)
        tools, self.routes = merge_tools(listed)
        own = [evidence.TOOL, *semantic.TOOLS]
        for t in own:
            if t.name in self.routes:
                raise ValueError("tool name collision: %r from %s/%s and reseau" % (t.name, *self.routes[t.name]))
        return tools + own

    async def call(self, name, arguments=None):
        """tools/call by exposed name: route to the owning upstream with the raw name. An unknown name
        re-lists once first, so a client whose tool list outlived a gateway restart still routes."""
        if name == evidence.TOOL.name:
            return evidence.as_result(await evidence.get_evidence(
                self.call_tool, (arguments or {}).get("activity_id"), self.identities))
        if name in semantic.HANDLERS:
            return evidence.as_result(await semantic.HANDLERS[name](self, arguments or {}))
        if name not in self.routes:
            await self.tools()  # ponytail: every unknown name re-lists all upstreams; rate-limit if clients spam typos
        if name not in self.routes:
            raise UpstreamError(UNKNOWN_TOOL, "unknown tool %r" % name, None, "unknown_tool")
        upstream, raw = self.routes[name]
        return await self.call_tool(upstream, raw, arguments)


if __name__ == "__main__":
    # Live smoke (manual, needs network + real tokens in env): python -m reseau.gateway
    import asyncio

    async def smoke():
        async with Gateway() as gw:
            for name, tool in (("github", "get_me"), ("linear", "list_teams"), ("graph8", "g8_current_org")):
                try:
                    tools = await gw.list_tools(name)
                    res = await gw.call_tool(name, tool)
                    print(name, len(tools), "tools;", tool, "->", "error" if res.is_error else "ok",
                          redact(str(res.content)[:200], gw.secrets))
                except UpstreamError as e:
                    print(name, "FAILED:", e.message)

    asyncio.run(smoke())
