import json

import pytest

from reseau import outbound, register_graph8

UUID = "11111111-2222-3333-4444-555555555555"
OK_EMPTY = (200, {"servers": [], "total": 0})


def client(replies):
    """outbound.Client whose transport answers DELETE then GET from `replies` (status, JSON body or raw bytes)."""
    it = iter(replies)

    def transport(method, url, headers, body, ct, rt):
        status, data = next(it)
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return status, {"content-type": "application/json"}, raw

    return outbound.Client(transport=transport, sleep=lambda s: None, policy=outbound.RetryPolicy(max_attempts=1))


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(register_graph8.time, "sleep", lambda s: None)


@pytest.mark.parametrize("replies", [
    [(500, {"detail": "boom"}), OK_EMPTY],                        # DELETE failed (the reported repro)
    [(200, {"success": True}), (503, b"<html>")],                 # list unavailable (the reported repro)
    [(200, {"success": True}), (200, {"data": {}})],              # list without servers
    [(200, {"success": True}), (200, {"servers": None})],         # servers not a list
    [(200, {"success": True}), (200, {"servers": ["x"]})],        # malformed entries
    [(200, {"success": True}), (200, {"servers": [{"mcp_server_id": UUID}], "total": 1})],  # still there
])
def test_cleanup_fails_unless_deletion_is_proven(replies):
    assert register_graph8.cleanup(client(replies), "key", UUID) is False


@pytest.mark.parametrize("final", [OK_EMPTY, (200, {"data": {"servers": [{"mcp_server_id": "other"}], "total": 1}})])
def test_cleanup_succeeds_when_record_is_gone(final):
    assert register_graph8.cleanup(client([(200, {"success": True}), final]), "key", UUID) is True
