#!/usr/bin/env python3
"""Probe which MCP registration shape Graph8 can actually connect to.

For each case: register a throwaway server, read it back from the list route,
POST /test, delete. Serial, explicit UA. Leftover `reseau-probe-*` servers are
swept at the end and the final list is printed.

Usage: python3 bridge_probe.py [case ...]      (default: all cases)
Cases: a_sse  b_import  b_net  ctl_sleep  ctl_missing  b_bridge
Reads GRAPH8_API_KEY from .env next to this file. Writes bridge_results.json.
"""
import json, os, sys, time, urllib.error, urllib.request

from mcp_probe import HERE, load_env

BASE = "https://be.graph8.com"
UA = "reseau-bridge-probe/1"
PREFIX = "reseau-probe-"
CANARY = "canary-not-a-secret"  # checks env_vars stay write-only
BRIDGE = open(os.path.join(HERE, "stdio_bridge.py")).read()

NET_ORACLE = """import time, urllib.request as u
try: u.urlopen(u.Request('https://learn.microsoft.com/api/mcp', headers={'User-Agent': 'x'}), timeout=15)
except u.HTTPError: pass
time.sleep(100)"""

# hang (Cloudflare 502 after ~100s) = the line before sleep succeeded; fast TaskGroup error = it raised
CASES = {
    "a_sse": {"transport_type": "sse", "connection_url": "https://mcp.api.coingecko.com/sse"},
    "b_import": {"transport_type": "stdio", "command": "python3",
                 "args": ["-c", "import mcp.client.streamable_http, time; time.sleep(100)"]},
    "b_net": {"transport_type": "stdio", "command": "python3", "args": ["-c", NET_ORACLE]},
    # oracle calibration: what a hang and a failed import look like on today's gateway
    "ctl_sleep": {"transport_type": "stdio", "command": "python3", "args": ["-c", "import time; time.sleep(100)"]},
    "ctl_missing": {"transport_type": "stdio", "command": "python3",
                    "args": ["-c", "import no_such_module_reseau, time; time.sleep(100)"]},
    "b_bridge": {"transport_type": "stdio", "command": "python3", "args": ["-c", BRIDGE],
                 "env_vars": {"UPSTREAM_URL": "https://learn.microsoft.com/api/mcp", "RESEAU_CANARY": CANARY}},
}


def g8(method, path, body=None, timeout=150):
    time.sleep(3)  # be.graph8.com 429-challenges bursts
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ["GRAPH8_API_KEY"],
                                          "Content-Type": "application/json", "User-Agent": UA})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, text = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        status, text = None, repr(e)
    ms = int((time.time() - t0) * 1000)
    if "<html" in text[:200].lower():
        text = "<html body, %d bytes>" % len(text)
    print("   %s %s -> %s in %dms  %s" % (method, path, status, ms, text[:400]))
    return status, text, ms


def data(text):
    try:
        j = json.loads(text)
        return j.get("data", j) if isinstance(j, dict) else j
    except ValueError:
        return {}


def run(name):
    print("\n== " + name)
    out = {"case": name}
    status, text, _ = g8("POST", "/api/v1/voice/mcp-servers", dict(name=PREFIX + name, **CASES[name]))
    out["create"] = {"status": status, "body": text}
    uuid = data(text).get("mcp_server_id") if status in (200, 201) else None
    if not uuid:
        return out
    _, listing, _ = g8("GET", "/api/v1/workflows/mcp-servers")
    url = CASES[name].get("connection_url")
    out["list_echoes_connection_url"] = bool(url) and url in listing
    out["list_echoes_env_value"] = CANARY in listing
    out["listed"] = next((x for x in data(listing).get("servers", []) if x.get("mcp_server_id") == uuid), None)
    if out["listed"]:
        out["listed"]["args"] = "<%d args omitted>" % len(out["listed"].get("args") or [])
    status, text, ms = g8("POST", "/api/v1/voice/mcp-servers/%s/test" % uuid)
    out["test"] = {"status": status, "body": text, "ms": ms}
    status, text, _ = g8("DELETE", "/api/v1/voice/mcp-servers/" + uuid)
    out["delete"] = {"status": status, "body": text}
    return out


def sweep():
    _, text, _ = g8("GET", "/api/v1/workflows/mcp-servers")
    for s in data(text).get("servers", []):
        if str(s.get("name", "")).startswith(PREFIX):
            g8("DELETE", "/api/v1/voice/mcp-servers/" + str(s.get("mcp_server_id")))
    return g8("GET", "/api/v1/workflows/mcp-servers")[1]


def main():
    load_env()
    names = sys.argv[1:] or list(CASES)
    results = []
    try:
        for n in names:
            results.append(run(n))
    finally:
        final = sweep()
        json.dump({"results": results, "final_list": final},
                  open(os.path.join(HERE, "bridge_results.json"), "w"), indent=2)
    print("\n== final list: " + final)
    green = [r["case"] for r in results
             if data(r.get("test", {}).get("body", "{}")).get("success") is True
             and data(r["test"]["body"]).get("tools_count") is not None]
    print("== green (success + tools_count): %s" % (green or "none"))
    return 0 if green else 1


if __name__ == "__main__":
    sys.exit(main())
