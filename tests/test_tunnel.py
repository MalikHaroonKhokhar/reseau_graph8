"""reseau.tunnel: following localhost.run's hosts onto the Graph8 registration, over a scripted Graph8."""
import pytest

from reseau import tunnel

LOG = ["** your connection id is 1f2e, please mention it if you send us a message **",
       "7f5b471ff6665b.lhr.life tunneled with tls termination, https://7f5b471ff6665b.lhr.life",
       "\x1b[7m  \x1b[0m\x1b[49m  \x1b[0m",  # the QR code
       "7f5b471ff6665b.lhr.life tunneled with tls termination, https://7f5b471ff6665b.lhr.life",
       "061d232cb1c235.lhr.life tunneled with tls termination, https://061d232cb1c235.lhr.life"]


class FakeGraph8:
    """One registration list; /test fails `failing` times first."""

    def __init__(self, servers, failing=0):
        self.servers, self.failing, self.calls = servers, failing, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return 200, {"servers": self.servers}
        if path.endswith("/test"):
            self.failing -= 1
            return 200, {"success": self.failing < 0}
        if method == "POST":
            self.servers.append(dict(body, mcp_server_id="srv-new"))
            return 201, {"mcp_server_id": "srv-new"}
        self.servers[0]["connection_url"] = body["connection_url"]
        return 200, {}

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m in ("POST", "PUT") and not p.endswith("/test")]


def reg(url):
    return {"name": "reseau-gateway", "mcp_server_id": "srv-1", "connection_url": url}


def test_each_new_host_once():
    assert list(tunnel.hosts(LOG)) == ["https://7f5b471ff6665b.lhr.life", "https://061d232cb1c235.lhr.life"]


def test_point_re_points_the_same_registration():
    g8 = FakeGraph8([reg("https://old.lhr.life/g8/t/sse")])
    assert tunnel.point(g8, "https://new.lhr.life/g8/t/sse")
    assert g8.writes() == [("PUT", "/api/v1/voice/mcp-servers/srv-1")]  # same id: every workflow keeps working
    assert g8.servers[0]["connection_url"] == "https://new.lhr.life/g8/t/sse"
    g8.calls.clear()
    assert tunnel.point(g8, "https://new.lhr.life/g8/t/sse") and g8.writes() == []  # already there: only /test


def test_point_registers_when_there_is_none():
    g8 = FakeGraph8([])
    assert tunnel.point(g8, "https://new.lhr.life/g8/t/sse")
    assert g8.writes() == [("POST", "/api/v1/voice/mcp-servers")]


def test_point_refuses_to_guess_between_registrations():
    with pytest.raises(SystemExit, match="2 registrations"):
        tunnel.point(FakeGraph8([reg("a"), reg("b")]), "https://new.lhr.life/g8/t/sse")


def test_follow_retries_until_graph8_reaches_the_gateway():
    g8 = FakeGraph8([reg("https://old.lhr.life/g8/t/sse")], failing=2)
    tunnel.follow(g8, "t", LOG, pause=0)
    tests = [p for m, p, _ in g8.calls if p.endswith("/test")]
    assert len(tests) == 2 + 1 + 1  # two failures and a pass on the first host, one pass on the second
    assert g8.servers[0]["connection_url"] == "https://061d232cb1c235.lhr.life/g8/t/sse"
