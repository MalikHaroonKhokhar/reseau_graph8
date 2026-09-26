"""Local Streamable HTTP MCP servers for integration tests: bearer-token gated, stateful (issues
Mcp-Session-Id, like GitHub) or stateless (no session id, like Linear). Loopback only, no network."""
import socket
import threading
import time

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError


class MockUpstream:
    def __init__(self, token, stateless, org_gate=False):
        self.token = token
        self.org_ready = not org_gate  # Graph8-style gate: tools fail -32003 until g8_current_org is called
        self.org_calls = 0
        self.org_stuck = False  # gate keeps failing even after g8_current_org
        self.fail_status = None  # set to e.g. 503 to fail every request after auth
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
        def echo_struct(text: str) -> dict[str, str]:
            return {"text": text}

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
                    await send({"type": "http.response.start", "status": self.fail_status, "headers": []})
                    await send({"type": "http.response.body", "body": b"unavailable"})
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
