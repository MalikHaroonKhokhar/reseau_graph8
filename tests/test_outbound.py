"""HAR-90: python3 -m unittest discover -s tests -t .   (stdlib only; no real backoff sleeps)"""
import io
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from reseau import outbound
from reseau.outbound import Client, HttpError, RetryPolicy, host_key, read_sse_response

URL = "https://be.graph8.com/mcp/"
CF_429 = b'<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>cf challenge</body></html>'
CF_1010 = b'<!DOCTYPE html><title>Access denied | be.graph8.com used Cloudflare</title>Error code 1010 browser_signature_banned'
HTML = {"content-type": "text/html; charset=UTF-8"}
JSON = {"content-type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
OK = {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "graph8"}}}


class Fake:
    """Scripted transport: each item is (status, headers, body) or an exception to raise."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def __call__(self, method, url, headers, body, connect_timeout, read_timeout):
        self.calls.append({"method": method, "headers": headers, "timeouts": (connect_timeout, read_timeout)})
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


def client(fake, **kw):
    sleeps = []
    return Client(transport=fake, sleep=sleeps.append, policy=RetryPolicy(rand=lambda: 1.0), **kw), sleeps


class OutboundTest(unittest.TestCase):
    def test_429_html_challenge_is_retried_then_succeeds(self):
        fake = Fake((429, HTML, CF_429), (200, JSON, json.dumps(OK).encode()))
        c, sleeps = client(fake)
        self.assertEqual(c.rpc(URL, INIT).data, OK)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(sleeps, [0.5])  # base * 2**0 at full jitter ceiling

    def test_explicit_user_agent_and_timeouts(self):
        fake = Fake((200, JSON, b"{}"))
        c, _ = client(fake)
        c.request("GET", URL)
        ua = fake.calls[0]["headers"]["user-agent"]
        self.assertEqual(ua, outbound.USER_AGENT)
        self.assertNotRegex(ua.lower(), r"python|urllib|requests|httpx|aiohttp")
        self.assertEqual(fake.calls[0]["timeouts"], (outbound.CONNECT_TIMEOUT, outbound.READ_TIMEOUT))

    def test_caller_headers_cannot_override_or_duplicate_user_agent(self):
        fake = Fake((200, JSON, b"{}"))
        c, _ = client(fake)
        c.request("GET", URL, headers={"user-agent": "Python-urllib/3.9", "User-Agent": "curl/8", "X-Trace": "1"})
        sent = fake.calls[0]["headers"]
        self.assertEqual([v for k, v in sent.items() if k.lower() == "user-agent"], [outbound.USER_AGENT])
        self.assertEqual(sent["x-trace"], "1")

    def test_equivalent_host_urls_share_one_concurrency_slot(self):
        c = Client()
        keys = {host_key(u) for u in ("https://be.graph8.com/mcp/", "https://BE.GRAPH8.COM/x",
                                      "https://be.graph8.com:443/", "https://be.graph8.com./")}
        self.assertEqual(keys, {("be.graph8.com", 443)})
        self.assertIs(c._slot(host_key("https://be.graph8.com/")), c._slot(host_key("HTTPS://Be.Graph8.com:443/")))
        self.assertNotEqual(host_key("http://be.graph8.com/"), host_key("https://be.graph8.com/"))

    def test_sse_read_stops_at_response_without_draining_stream(self):
        result = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        fp = io.BytesIO(b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n' + result + b": ping\n\n" * 100)
        raw = read_sse_response(fp, budget=60)
        self.assertTrue(raw.endswith(result))
        self.assertEqual(fp.read(), b": ping\n\n" * 100)  # heartbeats after the answer are never waited on

    def test_sse_heartbeats_without_response_hit_overall_deadline(self):
        class Heartbeats:
            def readline(self):
                return b": ping\n"
        ticks = iter(range(1000))
        with self.assertRaises(socket.timeout):
            read_sse_response(Heartbeats(), budget=5, clock=lambda: next(ticks))

    def test_403_1010_is_structured_and_not_retried(self):
        fake = Fake((403, HTML, CF_1010))
        c, sleeps = client(fake)
        with self.assertRaises(HttpError) as cm:
            c.request("GET", URL)
        e = cm.exception
        self.assertEqual((e.kind, e.status, e.content_type, e.attempts), ("http", 403, "text/html", 1))
        self.assertIn("1010", e.snippet)
        self.assertEqual((len(fake.calls), sleeps), (1, []))

    def test_retry_after_seconds_honoured(self):
        c, sleeps = client(Fake((429, {**HTML, "retry-after": "7"}, CF_429), (200, JSON, b"{}")))
        c.request("GET", URL)
        self.assertEqual(sleeps, [7.0])

    def test_retry_after_http_date_uses_injected_clock(self):
        fake = Fake((503, {"retry-after": "Sat, 26 Sep 2026 12:00:05 GMT"}, b""), (200, JSON, b"{}"))
        c, sleeps = client(fake, clock=lambda: 1790424000.0)  # 2026-09-26 12:00:00 UTC
        c.request("GET", URL)
        self.assertEqual(sleeps, [5.0])

    def test_retry_after_beyond_cap_gives_up(self):
        c, sleeps = client(Fake((429, {**HTML, "retry-after": "3600"}, CF_429)))
        with self.assertRaises(HttpError) as cm:
            c.request("GET", URL)
        self.assertEqual((cm.exception.kind, sleeps), ("rate_limited", []))

    def test_attempt_cap_then_structured_rate_limit(self):
        fake = Fake((429, HTML, CF_429))
        c, sleeps = client(fake)
        with self.assertRaises(HttpError) as cm:
            c.request("GET", URL)
        self.assertEqual((cm.exception.kind, cm.exception.attempts), ("rate_limited", outbound.MAX_ATTEMPTS))
        self.assertEqual(len(fake.calls), outbound.MAX_ATTEMPTS)
        self.assertEqual(sleeps, [0.5, 1.0, 2.0])  # exponential

    def test_jitter_is_bounded_by_backoff(self):
        p = RetryPolicy(rand=lambda: 0.25)
        self.assertEqual([p.delay(n) for n in (1, 2, 3, 4)], [0.125, 0.25, 0.5, None])
        self.assertEqual(RetryPolicy(rand=lambda: 1.0, max_attempts=99).delay(20), outbound.BACKOFF_CAP)

    def test_timeout_is_structured(self):
        c, sleeps = client(Fake(socket.timeout("timed out")))
        with self.assertRaises(HttpError) as cm:
            c.rpc(URL, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}})
        self.assertEqual((cm.exception.kind, cm.exception.attempts, sleeps), ("timeout", 1, []))

    def test_tools_call_is_not_retried(self):
        fake = Fake((429, HTML, CF_429))
        c, _ = client(fake)
        with self.assertRaises(HttpError) as cm:
            c.rpc(URL, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "x"}})
        self.assertEqual((cm.exception.kind, len(fake.calls)), ("rate_limited", 1))

    def test_html_200_and_broken_json_never_raise_parse_errors(self):
        for resp, kind in [((200, HTML, CF_429), "unexpected_content_type"), ((200, JSON, b"{nope"), "bad_json")]:
            c, _ = client(Fake(resp))
            with self.assertRaises(HttpError) as cm:
                c.request("GET", URL)
            self.assertEqual(cm.exception.kind, kind)

    def test_sse_and_empty_bodies(self):
        sse = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\r\n\r\n'
        c, _ = client(Fake((200, {"content-type": "text/event-stream"}, sse)))
        self.assertEqual(c.rpc(URL, INIT).data["result"], {"ok": True})
        c, _ = client(Fake((202, {}, b"")))
        self.assertIsNone(c.rpc(URL, {"jsonrpc": "2.0", "method": "notifications/initialized"}).data)

    def test_cap_is_looked_up_per_host(self):
        caps = {u: outbound.max_per_host(host_key(u)) for u in (
            "https://api.githubcopilot.com/mcp/readonly", "https://API.GitHubCopilot.com.:443/x",
            "https://mcp.linear.app/mcp/readonly", "https://be.graph8.com/mcp/", "https://example.com/")}
        self.assertEqual(list(caps.values()), [8, 8, 4, 2, outbound.MAX_PER_HOST])
        self.assertEqual(Client()._slot(host_key("https://api.githubcopilot.com/"))._value, 8)
        self.assertEqual(Client(max_per_host=3)._slot(host_key("https://api.githubcopilot.com/"))._value, 3)

    def test_concurrency_capped_per_host(self):
        cond, gate, state = threading.Condition(), threading.Event(), {"now": 0, "max": 0, "total": 0}

        def transport(*_):
            with cond:
                state["now"] += 1
                state["total"] += 1
                state["max"] = max(state["max"], state["now"])
                cond.notify_all()
            gate.wait(5)
            with cond:
                state["now"] -= 1
            return 200, JSON, b"{}"

        c = Client(transport=transport, max_per_host=2)
        threads = [threading.Thread(target=c.request, args=("GET", URL)) for _ in range(5)]
        for t in threads:
            t.start()
        with cond:
            self.assertTrue(cond.wait_for(lambda: state["now"] == 2, 5))
            self.assertEqual(state["total"], 2)  # the other three are parked on the host slot
        gate.set()
        for t in threads:
            t.join(5)
        self.assertEqual((state["max"], state["total"]), (2, 5))


class MockServerTest(unittest.TestCase):
    def test_429_html_twice_then_jsonrpc_over_real_socket(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                seen.append(self.headers.get_all("User-Agent"))
                status, ctype, body = ((429, "text/html", CF_429) if len(seen) <= 2
                                       else (200, "application/json", json.dumps(OK).encode()))
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        sleeps = []
        c = Client(sleep=sleeps.append)
        with self.assertLogs("reseau.http", "INFO") as logs:
            resp = c.rpc("http://127.0.0.1:%d/mcp/" % srv.server_port, INIT)
        self.assertEqual(resp.data, OK)
        attempts = [m for m in logs.output if "(attempt " in m]
        self.assertEqual(len(attempts), 3, logs.output)
        self.assertEqual((len(sleeps), seen), (2, [[outbound.USER_AGENT]] * 3))

    def test_sse_result_returned_while_stream_stays_open(self):
        done = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: " + json.dumps(OK).encode() + b"\n\n")
                self.wfile.flush()
                done.wait(10)  # keep the stream open until the client has returned

            def log_message(self, *_):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(done.set)
        resp = Client(read_timeout=5).rpc("http://127.0.0.1:%d/mcp/" % srv.server_port, INIT)
        self.assertFalse(done.is_set())  # returned before the server closed the stream
        self.assertEqual(resp.data, OK)


if __name__ == "__main__":
    unittest.main()
