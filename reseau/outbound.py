"""Shared outbound HTTP client. Every Réseau upstream call (GitHub, Linear, Graph8) goes through Client.

Why (spikes/mcp_bridge/FINDINGS.md, "Cloudflare throttling and UA banning"): be.graph8.com answers bursts with
HTTP 429 plus an HTML "Just a moment..." page, and bans library-default user-agents (403, error 1010). So every
request gets an explicit User-Agent, a content-type check before parsing, backoff on 429/5xx, connect and read
timeouts, and a per-host concurrency cap. GitHub and Linear were measured in HAR-99 (test_connection/FINDINGS.md
Run 5): neither challenges with HTML, and their caps (HOST_CAPS) are set from those numbers.

RETRY SCOPE: only idempotent reads (GET/HEAD/OPTIONS) and the MCP handshake (SAFE_MCP_METHODS) are retried by
default. tools/call and any other call with side effects is NOT retried here; business-level retry is out of scope.
Pass retry=True only when you know the call is safe to repeat. (The gateway's async transport also retries
tools/call on 429 for read-only upstreams; see gateway._Reliable.)
"""
import http.client
import json
import logging
import random
import socket
import threading
import time
import urllib.parse
from collections import namedtuple
from email.utils import parsedate_to_datetime

log = logging.getLogger("reseau.http")

USER_AGENT = "reseau-gateway/0.1"
MAX_ATTEMPTS = 4
BACKOFF_BASE = 0.5       # seconds; full jitter over base * 2**(attempt-1)
BACKOFF_CAP = 8.0
RETRY_AFTER_CAP = 30.0   # server asks for longer than this -> give up instead of stalling the agent turn
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
MAX_PER_HOST = 2  # hosts not in HOST_CAPS
# Concurrent requests per host, measured in HAR-99 (test_connection/FINDINGS.md Run 5). GitHub drew 429s at ~17
# req/s; 8 x ~0.85 s is ~9 req/s. Linear limits a request budget, not concurrency, so 4 only cuts queueing.
# Graph8's edge challenges ~6 parallel calls.
HOST_CAPS = {"api.githubcopilot.com": 8, "mcp.linear.app": 4, "be.graph8.com": 2}

RETRY_STATUSES = {429, 500, 502, 503, 504}
IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS"}
SAFE_MCP_METHODS = {"initialize", "notifications/initialized", "ping", "tools/list"}

Response = namedtuple("Response", "status headers data")  # headers: lower-cased keys; data: parsed JSON or None


class HttpError(Exception):
    """kind: rate_limited | http | unexpected_content_type | bad_json | timeout | connection"""

    def __init__(self, kind, url, status=None, content_type=None, snippet="", attempts=1):
        self.kind, self.url, self.status, self.content_type = kind, url, status, content_type
        self.snippet, self.attempts = snippet, attempts
        super().__init__(str(self))

    def __str__(self):
        return "%s: %s status=%s content_type=%s attempts=%d %r" % (
            self.kind, self.url, self.status, self.content_type, self.attempts, self.snippet)


class RetryPolicy:
    """Decides whether and how long to wait. Knows nothing about transport."""

    def __init__(self, max_attempts=MAX_ATTEMPTS, base=BACKOFF_BASE, cap=BACKOFF_CAP,
                 retry_after_cap=RETRY_AFTER_CAP, rand=random.random):
        self.max_attempts, self.base, self.cap = max_attempts, base, cap
        self.retry_after_cap, self.rand = retry_after_cap, rand

    def delay(self, attempt, retry_after=None):
        """Seconds to wait before the next attempt, or None to give up."""
        if attempt >= self.max_attempts:
            return None
        if retry_after is not None:
            return retry_after if retry_after <= self.retry_after_cap else None
        return self.rand() * min(self.cap, self.base * 2 ** (attempt - 1))


def http_transport(method, url, headers, body, connect_timeout, read_timeout):
    """One raw exchange -> (status, lower-cased headers, body bytes). http.client adds no default User-Agent."""
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = cls(u.hostname, u.port, timeout=connect_timeout)
    try:
        conn.connect()
        conn.sock.settimeout(read_timeout)
        conn.request(method, (u.path or "/") + ("?" + u.query if u.query else ""), body=body, headers=headers)
        r = conn.getresponse()
        h = {k.lower(): v for k, v in r.getheaders()}
        sse = h.get("content-type", "").split(";")[0].strip().lower() == "text/event-stream"
        return r.status, h, read_sse_response(r, read_timeout) if sse else r.read()
    finally:
        conn.close()


def read_sse_response(fp, budget, clock=time.monotonic):
    """Read an SSE body only up to the first JSON-RPC response event: servers may keep the stream open (heartbeats)
    after answering. budget caps the whole read, since each heartbeat resets the socket read timeout."""
    deadline, buf, data = clock() + budget, [], []
    while True:
        line = fp.readline()
        if not line:
            return b"".join(buf)
        buf.append(line)
        s = line.rstrip(b"\r\n")
        if s.startswith(b"data:"):
            data.append(s[6:] if s.startswith(b"data: ") else s[5:])
        elif not s and data:
            try:
                msg = json.loads(b"\n".join(data))
            except ValueError:
                msg = None  # left for _parse to report as bad_json
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return b"".join(buf)
            data = []
        if clock() > deadline:
            raise socket.timeout("no JSON-RPC response on SSE stream within %ss" % budget)


def host_key(url):
    """Semaphore key: be.graph8.com, BE.GRAPH8.COM. and be.graph8.com:443 are one host."""
    u = urllib.parse.urlsplit(url)
    return (u.hostname or "").rstrip("."), u.port or (443 if u.scheme.lower() == "https" else 80)


def max_per_host(key):
    """Concurrency cap for a host_key: HOST_CAPS, else MAX_PER_HOST."""
    return HOST_CAPS.get(key[0], MAX_PER_HOST)


def parse_retry_after(value, clock=time.time):
    """Retry-After header (delta-seconds or HTTP-date) -> seconds to wait, or None if absent/unparseable."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - clock())
    except (TypeError, ValueError):
        return None


def _snippet(raw):
    return " ".join(raw[:400].decode("utf-8", "replace").split())[:200]


def _parse_sse(text):
    """Streamable HTTP SSE reply -> the JSON-RPC response message (last event if none carries result/error)."""
    msgs = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(l[6:] if l.startswith("data: ") else l[5:] for l in block.split("\n") if l.startswith("data:"))
        if data:
            msgs.append(json.loads(data))
    return next((m for m in msgs if isinstance(m, dict) and ("result" in m or "error" in m)), msgs[-1] if msgs else None)


class Client:
    def __init__(self, transport=http_transport, policy=None, sleep=time.sleep, clock=time.time,
                 max_per_host=None, connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT):
        self.transport, self.policy = transport, policy or RetryPolicy()
        self.sleep, self.clock, self.max_per_host = sleep, clock, max_per_host
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self._lock, self._slots = threading.Lock(), {}

    def _slot(self, host):
        with self._lock:
            return self._slots.setdefault(host, threading.BoundedSemaphore(self.max_per_host or max_per_host(host)))

    def _retry_after(self, value):
        return parse_retry_after(value, self.clock)

    def request(self, method, url, body=None, headers=None, retry=None):
        """-> Response. Raises HttpError only; non-JSON bodies never surface as parse exceptions."""
        method = method.upper()
        retry = method in IDEMPOTENT_METHODS if retry is None else retry
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        headers["user-agent"] = USER_AGENT  # enforced: caller headers can never reintroduce a banned library default
        headers.setdefault("accept", "application/json, text/event-stream")
        slot = self._slot(host_key(url))
        attempt = 0
        while True:
            attempt += 1
            retry_after = None
            try:
                with slot:
                    status, rh, raw = self.transport(method, url, headers, body, self.connect_timeout, self.read_timeout)
            except (socket.timeout, TimeoutError) as e:
                log.info("%s %s -> timeout (attempt %d)", method, url, attempt)
                err = HttpError("timeout", url, snippet=str(e))
            except (OSError, http.client.HTTPException) as e:
                log.info("%s %s -> %s (attempt %d)", method, url, type(e).__name__, attempt)
                err = HttpError("connection", url, snippet=str(e))
            else:
                log.info("%s %s -> %s (attempt %d)", method, url, status, attempt)
                ct = rh.get("content-type", "").split(";")[0].strip().lower()
                if 200 <= status < 300:
                    return Response(status, rh, self._parse(url, status, ct, raw))
                err = HttpError("rate_limited" if status == 429 else "http", url, status, ct, _snippet(raw))
                if status not in RETRY_STATUSES:
                    err.attempts = attempt
                    raise err
                retry_after = self._retry_after(rh.get("retry-after"))
            wait = self.policy.delay(attempt, retry_after) if retry else None
            if wait is None:
                err.attempts = attempt
                raise err
            log.warning("retrying %s %s in %.2fs after %s", method, url, wait, err.kind)
            self.sleep(wait)

    def _parse(self, url, status, ct, raw):
        if not raw.strip():
            return None  # 202/204, e.g. notifications
        try:
            if ct == "application/json":
                return json.loads(raw)
            if ct == "text/event-stream":
                return _parse_sse(raw.decode("utf-8"))
        except ValueError:
            raise HttpError("bad_json", url, status, ct, _snippet(raw)) from None
        raise HttpError("unexpected_content_type", url, status, ct, _snippet(raw))

    def rpc(self, url, payload, headers=None):
        """MCP JSON-RPC POST. Only the handshake / listing methods are retried; tools/call never is."""
        return self.request("POST", url, json.dumps(payload).encode(),
                            {"Content-Type": "application/json", **(headers or {})},
                            retry=payload.get("method") in SAFE_MCP_METHODS)
