"""Local Streamable HTTP MCP servers for integration tests: bearer-token gated, stateful (issues
Mcp-Session-Id, like GitHub) or stateless (no session id, like Linear). Loopback only, no network."""
import socket
import threading
import time

import anyio
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError


class MockUpstream:
    def __init__(self, token, stateless, org_gate=False):
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

        app = server.streamable_http_app(stateless_http=stateless)

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
