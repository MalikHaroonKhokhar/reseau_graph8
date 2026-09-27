"""Local Streamable HTTP MCP servers for integration tests: bearer-token gated, stateful (issues
Mcp-Session-Id, like GitHub) or stateless (no session id, like Linear). Loopback only, no network."""
import json
import pathlib
import socket
import threading
import time

import anyio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# IDs of the synthetic Graph8 fixtures
G8_COMPANY, G8_MEETING, G8_THREAD = 4242, "mtg_01J9EXAMPLE", "thr_01J9EXAMPLE"
G8_DEAL, G8_TASK = "5f1c2b7e-8a44-4c1e-9d0b-2e6f3a9c7d10", "0b9e4d2a-3c71-4f58-8e16-7a2d5c4b9f31"


def scripted_result(script, name, args):
    """script = {tool name: fn(arguments) -> JSON value}. A raised exception becomes an isError result with its
    message, the way the real upstreams report tool failures."""
    try:
        text, is_error = json.dumps(script[name](args or {})), False
    except Exception as e:
        text, is_error = str(e), True
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


def scripted_app(script, stateless):
    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(name=n, input_schema={"type": "object"}) for n in script])

    async def call_tool(ctx, params):
        return scripted_result(script, params.name, params.arguments)

    return Server("mock-scripted", on_list_tools=list_tools, on_call_tool=call_tool).streamable_http_app(
        stateless_http=stateless)


class MockUpstream:
    def __init__(self, token, stateless, org_gate=False, evidence=False, script=None):
        self.token = token
        self.org_ready = not org_gate  # Graph8-style gate: tools fail -32003 until g8_current_org is called
        self.org_calls = 0
        self.org_stuck = False  # gate keeps failing even after g8_current_org
        self.fail_status = None  # set to e.g. 503 to fail requests after auth
        self.fail_times = None  # with fail_status: fail only the next N requests (None = every request)
        self.in_flight = self.peak = 0  # concurrent `slow` tool executions
        self.created = 0
        self.seen = []  # (method, headers dict) of every request that passed auth
        server = MCPServer("mock-" + ("stateless" if stateless else "stateful"))

        def gate_check():
            if not self.org_ready or self.org_stuck:
                raise MCPError(-32003, "Org context not established for this session. Call g8_current_org first")

        def g8_current_org() -> str:
            self.org_calls += 1
            self.org_ready = True
            return "org"

        if org_gate:
            server.tool()(g8_current_org)

        @server.tool()
        def echo(text: str) -> str:
            gate_check()
            return "echo:" + text

        @server.tool()
        async def slow(text: str) -> str:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await anyio.sleep(0.2)
            finally:
                self.in_flight -= 1
            return "slow:" + text

        @server.tool()
        def echo_struct(text: str) -> dict[str, str]:
            return {"text": text}

        # Same raw names as GitHub and Linear both expose (FINDINGS.md Run 2); the reply says which mock answered.
        @server.tool()
        def list_issues() -> str:
            return "list_issues@" + server.name

        @server.tool()
        def list_releases() -> str:
            return "list_releases@" + server.name

        @server.tool()
        def create_issue(title: str) -> str:  # write tool; created counts calls that got through
            self.created += 1
            return "created:" + title

        @server.tool()
        def rpc_fail(text: str) -> str:
            raise MCPError(-32000, "upstream failed on " + text, {"input": text})

        def fixture(name):
            return (FIXTURES / (name + ".json")).read_text()

        if evidence and org_gate:  # Graph8 read tools: gated like the rest, and a missing record answered the way the
            # live server does (HAR-108 probe): an "Error: ..." text result with isError unset
            @server.tool()
            def g8_crm_get_company(company_id: int) -> str:
                gate_check()
                return fixture("graph8_company") if company_id == G8_COMPANY else "Error: g8_crm_get_company: Company not found"

            @server.tool()
            def g8_get_deal(deal_id: str) -> str:
                gate_check()
                return fixture("graph8_deal") if deal_id == G8_DEAL else "Error: Deal: Deal not found"

            @server.tool()
            def g8_get_task(task_id: str) -> str:
                gate_check()
                return fixture("graph8_task") if task_id == G8_TASK else "Task %s: Error: Task lookup: Task not found" % task_id

            @server.tool()
            def g8_get_meeting(meeting_id: str) -> str:
                gate_check()
                return fixture("graph8_meeting") if meeting_id == G8_MEETING else "Error: API error (400): Invalid meeting ID: " + meeting_id

            @server.tool()
            def g8_get_reply(reply_id: str, channel: str = "email") -> str:
                gate_check()
                return fixture("graph8_thread") if (reply_id, channel) == (G8_THREAD, "email") else \
                    "Error: Inbox thread: Email %s not found" % reply_id

            @server.tool()
            def g8_create_deal(name: str) -> str:  # write tool, never allowlisted; created counts calls that got through
                self.created += 1
                return "created:" + name

        elif evidence:  # GitHub and Linear read tools answering with the recorded fixtures, or their live not-found text
            @server.tool()
            def pull_request_read(method: str, owner: str, repo: str, pullNumber: int, perPage: int | None = None,
                                  after: str | None = None) -> str:
                if (method, owner, repo, pullNumber) == ("get", "octo-dev", "reseau_graph8", 15):
                    return fixture("github_pr")
                if (method, owner, repo, pullNumber) == ("get_review_comments", "modelcontextprotocol", "python-sdk", 3583):
                    return fixture("github_review_comments") if after is None else '{"review_threads":[],"pageInfo":{}}'
                raise ToolError("failed to get pull request: GET https://api.github.com/repos/%s/%s/pulls/%d: "
                                 "404 Not Found []" % (owner, repo, pullNumber))

            @server.tool()
            def get_commit(owner: str, repo: str, sha: str, detail: str = "stats") -> str:
                if (owner, repo) == ("octo-dev", "reseau_graph8") and "a516b748f6e62cef147c8229afe18b1538ddbb55".startswith(sha):
                    return fixture("github_commit")
                raise ToolError("failed to get commit: %s: No commit found for SHA: %s" % (sha, sha))

            @server.tool()
            def get_issue(id: str) -> str:
                if id == "HAR-98":
                    return fixture("linear_issue")
                raise ToolError('{"error":"invalid_request","message":"Could not find referenced Issue.","status":400}')

        # script replaces every tool above with scripted_result's canned answers
        app = scripted_app(script, stateless) if script else server.streamable_http_app(stateless_http=stateless)

        async def gate(scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                if headers.get("authorization") != "Bearer " + self.token:
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"www-authenticate", b'Bearer error="invalid_token"')]})
                    await send({"type": "http.response.body", "body": b"unauthorized"})
                    return
                if self.fail_status:
                    status = self.fail_status
                    if self.fail_times is not None:
                        self.fail_times -= 1
                        if self.fail_times <= 0:
                            self.fail_status = self.fail_times = None
                    # Cloudflare-style: 429 answered with an HTML challenge page, not JSON
                    await send({"type": "http.response.start", "status": status,
                                "headers": [(b"content-type", b"text/html; charset=UTF-8")]})
                    await send({"type": "http.response.body", "body": b"<!DOCTYPE html><title>Just a moment...</title>"})
                    return
                self.seen.append((scope["method"], headers))
            await app(scope, receive, send)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.url = "http://127.0.0.1:%d/mcp" % sock.getsockname()[1]
        self._sock = sock
        self._server = uvicorn.Server(uvicorn.Config(gate, log_level="warning", lifespan="on"))

    def __enter__(self):
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True)
        self._thread.start()
        while not self._server.started:
            time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(5)
