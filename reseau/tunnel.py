"""Keep Graph8 able to reach the local gateway for a whole session, a demo included: run the localhost.run
tunnel, restart it when it drops, and point the reseau-gateway registration at every public host it gets.

localhost.run's free tunnel moves to a new *.lhr.life host without warning (live on 2026-09-27, within the hour),
and a dropped connection ends ssh. Graph8 keeps calling the registered connection_url, so each new host is PUT on
the same registration, whose mcp_server_id (and so every workflow built on it) stays the same, and confirmed with
Graph8's /test. With no registration yet, one is created; workflows made before that need
`python -m reseau.workflows update`.

    uv run python -m reseau.front --port 8080     # the gateway, in one shell
    uv run python -m reseau.tunnel --port 8080    # this, in another; Ctrl-C to stop

Needs GRAPH8_API_KEY and RESEAU_GATEWAY_TOKEN. While registered, Graph8 shows connection_url, and so the gateway
token, to the whole org (README, Security).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from functools import partial

from reseau import front, gateway, outbound, register_graph8
from reseau.register_graph8 import say

HOST = re.compile(r"https://[a-z0-9-]+\.lhr\.life")
RESTART = 5  # seconds before a dropped tunnel is reopened
TRIES = 5  # attempts at pointing the registration at a new host, 10 s apart


def ssh(port):
    return ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-o", "ExitOnForwardFailure=yes",
            "-R", "80:localhost:%d" % port, "nokey@localhost.run"]


def hosts(lines):
    """The tunnel's output -> each public host it announces, once per change."""
    last = None
    for line in lines:
        m = HOST.search(line)
        if m and m.group() != last:
            last = m.group()
            yield last


def point(g8, url):
    """Make the reseau-gateway registration's connection_url `url`, creating the registration if there is none,
    and check it with /test -> True when Graph8 reaches the gateway there."""
    _, listing = g8("GET", "/api/v1/workflows/mcp-servers")
    found = [s for s in (listing or {}).get("servers") or [] if s.get("name") == register_graph8.NAME]
    if len(found) > 1:
        raise SystemExit("%d registrations named %r: delete the extras" % (len(found), register_graph8.NAME))
    body = {"name": register_graph8.NAME, "transport_type": "sse", "connection_url": url}
    if not found:
        _, rec = g8("POST", "/api/v1/voice/mcp-servers", body)
        server = isinstance(rec, dict) and rec.get("mcp_server_id")
        if not server:
            say("registration failed:", rec)
            return False
    else:
        server = found[0]["mcp_server_id"]
        if found[0].get("connection_url") != url:
            g8("PUT", "/api/v1/voice/mcp-servers/" + server, body)
    _, tested = g8("POST", "/api/v1/voice/mcp-servers/%s/test" % server)
    ok = isinstance(tested, dict) and tested.get("success") is True
    say(time.strftime("%H:%M:%S"), register_graph8.NAME, server, "->", url.split("/g8/")[0],
        "OK" if ok else "FAILED: %s" % json.dumps(tested)[:300])
    return ok


def follow(g8, token, lines, tries=TRIES, pause=10):
    """Point the registration at each host the tunnel announces, retrying until Graph8's /test passes."""
    for host in hosts(lines):
        for _ in range(tries):
            try:
                if point(g8, "%s/g8/%s/sse" % (host, token)):
                    break
            except (OSError, ValueError) as e:  # one failed Graph8 call must not end the tunnel
                say("pointing the registration failed:", e)
            time.sleep(pause)


def main(argv=sys.argv[1:]):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=8080, help="the local gateway's port")
    p.add_argument("--url", help="a fixed public gateway host (e.g. Render's): point the registration at it once, "
                                 "no tunnel")
    a = p.parse_args(argv)
    key = gateway.resolve_credential(gateway.Upstream("graph8", register_graph8.BASE, "GRAPH8_API_KEY"))
    token = front.load_tokens(os.environ)[0]
    gateway.SECRETS.update({key, token})
    g8 = partial(register_graph8.g8, outbound.Client(), key)
    if a.url:
        return 0 if point(g8, "%s/g8/%s/sse" % (a.url.rstrip("/"), token)) else 1
    while True:
        proc = subprocess.Popen(ssh(a.port), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace")
        try:
            follow(g8, token, proc.stdout)
        finally:
            proc.kill()
        say(time.strftime("%H:%M:%S"), "tunnel closed (ssh exit %s); reopening in %d s" % (proc.wait(), RESTART))
        time.sleep(RESTART)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
