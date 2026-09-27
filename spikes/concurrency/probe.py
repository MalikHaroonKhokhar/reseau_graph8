#!/usr/bin/env python3
"""HAR-99: concurrency and long-session behaviour of the GitHub and Linear MCP upstreams.

Two views per call:
  raw      one Streamable HTTP session per upstream over outbound.http_transport: same UA, headers and endpoint as
           the gateway, but no retry and no per-host cap, so upstream status codes, content types and 429s show as-is.
  gateway  reseau.gateway.Gateway with its real policy (per-host cap, backoff): what an agent turn sees.

Calls are read-only and allowlisted: github get_me, linear list_teams. OAuth refresh is not applicable (static
Linear API key and GitHub PAT) and is not tested.

Usage (from the repo root, tokens in env: set -a; . ./.env; set +a):
  uv run python spikes/concurrency/probe.py load [--levels 1,2,4,8,16,32,64] [github linear]
  uv run python spikes/concurrency/probe.py sustain [--conc 4] [--limit 3000] [github linear]
  uv run python spikes/concurrency/probe.py session          (GitHub: bogus, missing and terminated session ids)
  uv run python spikes/concurrency/probe.py long [--hours 6] [--every 300] [github linear]
  uv run python spikes/concurrency/probe.py selftest
load writes load_results.json, sustain sustain_results.json; long appends one line per tick to longrun_results.jsonl (both gitignored).
No token, Authorization header or raw Mcp-Session-Id is ever written: session ids are recorded as a short hash.
"""
import hashlib
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))
import anyio  # noqa: E402

from reseau import gateway, outbound  # noqa: E402

CALLS = {"github": "get_me", "linear": "list_teams"}
UPSTREAMS = {u.name: u for u in gateway.DEFAULT_UPSTREAMS}
PROTOCOL = "2025-06-18"
KEEP_HEADERS = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "cf-mitigated")


def cap(name):
    return outbound.max_per_host(outbound.host_key(UPSTREAMS[name].url))


def sid_hash(sid):
    return hashlib.sha256(sid.encode()).hexdigest()[:8] if sid else None


class RawSession:
    """Minimal Streamable HTTP client that records every exchange instead of hiding it."""

    def __init__(self, up, token, transport=outbound.http_transport):
        self.up, self.token, self.transport, self.sid, self.n = up, token, transport, None, 0

    def _post(self, payload):
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
                   "mcp-protocol-version": PROTOCOL, **{k.lower(): v for k, v in gateway.build_headers(self.token).items()}}
        if self.sid:
            headers["mcp-session-id"] = self.sid
        t0 = time.monotonic()
        rec = {"t": round(time.time(), 1)}
        try:
            status, h, raw = self.transport("POST", self.up.url, headers, json.dumps(payload).encode(),
                                            outbound.CONNECT_TIMEOUT, outbound.READ_TIMEOUT)
        except Exception as e:  # timeouts, resets: recorded, not raised
            rec.update(status=None, error=type(e).__name__, detail=self._clean(str(e)))
            rec["ms"] = round((time.monotonic() - t0) * 1000)
            return rec, {}
        rec["ms"] = round((time.monotonic() - t0) * 1000)
        ct = h.get("content-type", "").split(";")[0].strip().lower()
        rec.update(status=status, ct=ct, **{k: h[k] for k in KEEP_HEADERS if k in h})
        msg = None
        if 200 <= status < 300 and raw.strip():
            try:
                msg = json.loads(raw) if ct == "application/json" else outbound._parse_sse(raw.decode("utf-8"))
            except ValueError:
                rec["rpc"] = "bad_json"
        if isinstance(msg, dict) and "error" in msg:
            rec["rpc"] = "error %s" % msg["error"].get("code")
            rec["detail"] = self._clean(str(msg["error"].get("message")))
        elif isinstance(msg, dict) and "result" in msg:
            rec["rpc"] = "tool_error" if (msg["result"] or {}).get("isError") else "ok"
        if not 200 <= status < 300:
            rec["detail"] = self._clean(outbound._snippet(raw))  # e.g. Cloudflare "Just a moment..." or a 404 body
        return rec, h

    def _clean(self, text):
        return gateway.redact(text, [self.token, self.sid])

    def initialize(self):
        self.sid = None
        rec, h = self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "reseau-har99-probe", "version": "1"}}})
        self.sid = h.get("mcp-session-id")
        rec["sid"] = sid_hash(self.sid)
        if rec.get("rpc") == "ok":
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return rec

    def call(self, tool):
        self.n += 1
        rec, _ = self._post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": tool, "arguments": {}}})
        rec["sid"] = sid_hash(self.sid)
        return rec


def summarize(recs):
    ms = sorted(r["ms"] for r in recs)
    count = lambda key: {str(k): sum(1 for r in recs if r.get(key) == k) for k in sorted({r.get(key) for r in recs}, key=str)}
    return {"n": len(recs), "status": count("status"), "ct": count("ct"), "rpc": count("rpc"),
            "p50_ms": ms[len(ms) // 2], "p95_ms": ms[min(len(ms) - 1, int(len(ms) * 0.95))], "max_ms": ms[-1],
            "retry_after": sorted({r["retry-after"] for r in recs if "retry-after" in r}),
            "details": sorted({r["detail"] for r in recs if r.get("detail")})[:3]}


def throttled(summary):
    return any(s not in ("200", "202") for s in summary["status"]) or any(k != "ok" for k in summary["rpc"])


def raw_burst(session, tool, n):
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(lambda _: session.call(tool), range(n)))


async def gateway_burst(gw, name, tool, n):
    recs = []

    async def one():
        t0 = time.monotonic()
        try:
            res = await gw.call_tool(name, tool)
            rec = {"rpc": "tool_error" if res.is_error else "ok"}
        except gateway.MCPError as e:
            rec = {"rpc": "error %s" % e.code, "detail": gateway.redact(e.message, gw.secrets)}
        rec["ms"] = round((time.monotonic() - t0) * 1000)
        recs.append(rec)

    async with anyio.create_task_group() as tg:
        for _ in range(n):
            tg.start_soon(one)
    return recs


def tokens(names):
    out = {}
    for n in names:
        out[n] = (os.environ.get(UPSTREAMS[n].token_env) or "").strip()
        if not out[n]:
            sys.exit("%s is not set" % UPSTREAMS[n].token_env)
    return out


async def load(names, levels, pause=5.0):
    toks, results = tokens(names), {}
    for name in names:
        s, tool = RawSession(UPSTREAMS[name], toks[name]), CALLS[name]
        init = s.initialize()
        results[name] = {"initialize": init, "raw": {}, "gateway": {}}
        print(name, "initialize", init["status"], init.get("rpc"), "sid" if init["sid"] else "no sid")
        for n in levels:
            summ = summarize(await anyio.to_thread.run_sync(raw_burst, s, tool, n))
            results[name]["raw"][n] = summ
            print(name, "raw N=%d" % n, summ["status"], summ["rpc"], "p50", summ["p50_ms"], "max", summ["max_ms"])
            await anyio.sleep(pause)
            if throttled(summ):
                break  # threshold found; don't push the key further into a ban
    async with gateway.Gateway([UPSTREAMS[n] for n in names]) as gw:
        for name in names:
            for n in levels:
                summ = summarize(await gateway_burst(gw, name, CALLS[name], n))
                results[name]["gateway"][n] = summ
                print(name, "gateway N=%d cap=%d" % (n, cap(name)), summ["rpc"], "p50", summ["p50_ms"], "max", summ["max_ms"])
                await anyio.sleep(pause)
    results["_meta"] = {"levels": levels, "max_per_host": {n: cap(n) for n in names}, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    with open(os.path.join(HERE, "load_results.json"), "w") as f:
        json.dump(results, f, indent=1)


def session_check(token, transport=outbound.http_transport):
    """GitHub session semantics without waiting hours: bogus / missing / terminated Mcp-Session-Id."""
    s = RawSession(UPSTREAMS["github"], token, transport)
    out = {"initialize": s.initialize(), "valid": s.call(CALLS["github"])}
    real = s.sid
    for label, sid in (("bogus", "00000000-dead-beef-0000-000000000000"), ("missing", None)):
        s.sid = sid
        out[label] = s.call(CALLS["github"])
    s.sid = real
    h = {"user-agent": outbound.USER_AGENT, "mcp-protocol-version": PROTOCOL, "mcp-session-id": real,
         **{k.lower(): v for k, v in gateway.build_headers(token).items()}}
    out["delete_status"] = transport("DELETE", s.up.url, h, None, outbound.CONNECT_TIMEOUT, outbound.READ_TIMEOUT)[0]
    out["after_delete"] = s.call(CALLS["github"])
    return out


def sustain(names, conc, limit, poll=5.0):
    """Calls at fixed concurrency until the first non-ok reply (or `limit` calls), then polls until ok again.
    Measures what a single burst can't: a rolling-window budget and how long its lockout lasts."""
    toks, results = tokens(names), {}
    for name in names:
        s = RawSession(UPSTREAMS[name], toks[name])
        s.initialize()
        recs, t0, stop = [], time.monotonic(), False

        def one(_):
            nonlocal stop
            if stop:
                return
            r = s.call(CALLS[name])
            recs.append(r)
            stop = stop or r.get("rpc") != "ok"

        with ThreadPoolExecutor(conc) as pool:
            list(pool.map(one, range(limit)))
        fail = next((r for r in recs if r.get("rpc") != "ok"), None)
        res = {"conc": conc, "calls": len(recs), "ok_before_fail": sum(1 for r in recs if r.get("rpc") == "ok"),
               "secs": round(time.monotonic() - t0, 1), "first_fail": fail}
        if fail:
            t1, polls = time.monotonic(), []
            while True:
                time.sleep(poll)
                r = s.call(CALLS[name])
                polls.append(r)
                if r.get("rpc") == "ok":
                    break
            res.update(recovered_after_s=round(time.monotonic() - t1, 1), polls=summarize(polls))
        results[name] = res
        print(name, json.dumps({k: v for k, v in res.items() if k != "polls"}))
    with open(os.path.join(HERE, "sustain_results.json"), "w") as f:
        json.dump(results, f, indent=1)


async def long_run(names, hours, every):
    toks = tokens(names)
    sessions = {n: RawSession(UPSTREAMS[n], toks[n]) for n in names}
    out = open(os.path.join(HERE, "longrun_results.jsonl"), "a")
    start = time.monotonic()

    def write(rec):
        out.write(json.dumps(rec) + "\n")
        out.flush()

    for n, s in sessions.items():
        write({"upstream": n, "view": "raw", "op": "initialize", "age_s": 0, **s.initialize()})
    born = {n: time.monotonic() for n in names}
    async with gateway.Gateway([UPSTREAMS[n] for n in names]) as gw:
        while time.monotonic() - start < hours * 3600:
            for n, s in sessions.items():
                rec = await anyio.to_thread.run_sync(s.call, CALLS[n])
                write({"upstream": n, "view": "raw", "op": "call", "age_s": round(time.monotonic() - born[n]), **rec})
                if rec.get("rpc") != "ok":
                    # The failure shape is recorded above; start a fresh session so later ticks keep measuring.
                    write({"upstream": n, "view": "raw", "op": "reinitialize", "age_s": 0, **s.initialize()})
                    born[n] = time.monotonic()
                g = (await gateway_burst(gw, n, CALLS[n], 1))[0]
                write({"upstream": n, "view": "gateway", "op": "call", "t": round(time.time(), 1),
                       "age_s": round(time.monotonic() - start), **g, "health": gw.health()[n]["ok"]})
                if not gw.health()[n]["ok"]:
                    await gw.reconnect(n)
            await anyio.sleep(every)
    out.close()


def selftest():
    """Fake upstream: session ids, a 429 challenge page, an expired-session 404. Checks records carry no secrets."""
    token, sid = "ghp_SECRET_TOKEN", "SESSION-ID-SECRET"
    replies = iter([
        (200, {"content-type": "application/json", "mcp-session-id": sid}, b'{"jsonrpc":"2.0","id":0,"result":{}}'),
        (202, {}, b""),
        (200, {"content-type": "text/event-stream"}, b'data: {"jsonrpc":"2.0","id":1,"result":{"content":[]}}\n\n'),
        (429, {"content-type": "text/html", "retry-after": "7"}, b"<title>Just a moment...</title>"),
        (404, {"content-type": "application/json"}, ('{"error":"session %s not found"}' % sid).encode()),
    ])
    s = RawSession(UPSTREAMS["github"], token, transport=lambda *a: next(replies))
    init, ok, limited, expired = s.initialize(), s.call("get_me"), s.call("get_me"), s.call("get_me")
    assert init["sid"] == sid_hash(sid) and ok["rpc"] == "ok" and ok["ct"] == "text/event-stream"
    assert limited["status"] == 429 and limited["retry-after"] == "7" and "Just a moment" in limited["detail"]
    assert expired["status"] == 404 and "[REDACTED]" in expired["detail"]
    dump = json.dumps([init, ok, limited, expired])
    assert token not in dump and sid not in dump
    summ = summarize([ok, limited, expired])
    assert throttled(summ) and summ["status"] == {"200": 1, "404": 1, "429": 1}
    assert not throttled(summarize([ok]))
    print("selftest ok")


def main(argv):
    mode, args = (argv[0] if argv else "selftest"), argv[1:]
    opts, names = {}, []
    while args:
        a = args.pop(0)
        if a.startswith("--"):
            opts[a[2:]] = args.pop(0)
        else:
            names.append(a)
    names = names or list(CALLS)
    if mode == "selftest":
        return selftest()
    if mode == "load":
        levels = [int(x) for x in opts.get("levels", "1,2,4,8,16,32,64").split(",")]
        return anyio.run(load, names, levels)
    if mode == "session":
        out = session_check(tokens(["github"])["github"])
        print(json.dumps(out, indent=1))
        with open(os.path.join(HERE, "session_results.json"), "w") as f:
            json.dump(out, f, indent=1)
        return
    if mode == "sustain":
        return sustain(names, int(opts.get("conc", 4)), int(opts.get("limit", 3000)))
    if mode == "long":
        return anyio.run(long_run, names, float(opts.get("hours", 6)), float(opts.get("every", 300)))
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
