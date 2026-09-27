"""Run: uv run pytest upstream/graph8_mcp_read_redaction -q"""
import json

import pytest

from read_redaction import MASK, apply_update, public_server

TOKEN = "canary-gw-token-0123456789abcdef"
SSE = {"mcp_server_id": "u1", "name": "reseau-gateway", "transport_type": "sse", "enabled": True,
       "connection_url": "https://gw.example.com/g8/%s/sse" % TOKEN}
STDIO = {"mcp_server_id": "u2", "name": "bridge", "transport_type": "stdio", "command": "python3",
         "args": ["-c", "TOKEN = '%s'" % TOKEN], "env_vars": {"UPSTREAM_URL": "https://x", "UPSTREAM_TOKEN": TOKEN}}


@pytest.mark.parametrize("record", [SSE, STDIO, {**SSE, "headers": {"Authorization": "Bearer " + TOKEN}}])
def test_read_response_contains_no_secret(record):
    listing = json.dumps({"servers": [public_server(record)], "total": 1})
    assert TOKEN not in listing


def test_shape_is_kept_for_the_ui():
    s, b = public_server(SSE), public_server(STDIO)
    assert s["connection_url"] == "https://gw.example.com/" + MASK
    assert (s["name"], s["transport_type"], s["enabled"]) == ("reseau-gateway", "sse", True)
    assert b["command"] == "python3" and b["args"] == [MASK, MASK]
    assert b["env_vars"] == {"UPSTREAM_URL": MASK, "UPSTREAM_TOKEN": MASK}
    assert STDIO["env_vars"]["UPSTREAM_TOKEN"] == TOKEN  # input not mutated


@pytest.mark.parametrize("url, shown", [
    ("https://mcp.api.coingecko.com/sse", "https://mcp.api.coingecko.com/" + MASK),
    ("https://host.example", "https://host.example"),
    ("https://host.example/", "https://host.example/"),
    ("http://user:pw@host.example:8443/", "http://host.example:8443/" + MASK),
    ("https://host.example/?key=" + TOKEN, "https://host.example/" + MASK),
    ("https://host.example/#" + TOKEN, "https://host.example/" + MASK),
])
def test_connection_url_masking(url, shown):
    assert public_server({"connection_url": url})["connection_url"] == shown


def test_round_trip_of_masked_record_keeps_secrets():
    for stored in (SSE, STDIO):
        assert apply_update(stored, public_server(stored)) == stored


def test_update_changes_only_what_the_client_set():
    out = apply_update(STDIO, {"env_vars": {"UPSTREAM_URL": "https://new", "UPSTREAM_TOKEN": MASK, "EXTRA": "e"},
                               "name": "renamed"})
    assert out["env_vars"] == {"UPSTREAM_URL": "https://new", "UPSTREAM_TOKEN": TOKEN, "EXTRA": "e"}
    assert out["name"] == "renamed" and out["args"] == STDIO["args"]
    assert apply_update(SSE, {"connection_url": "https://gw.example.com/g8/new/sse"})["connection_url"].endswith("/new/sse")
    assert apply_update(STDIO, {"args": ["-c", "print(1)"]})["args"] == ["-c", "print(1)"]


def test_mask_for_a_key_that_was_never_set_is_stored_literally():
    # No stored value to keep: the client really sent "***". Graph8 may prefer to reject this with 422.
    assert apply_update(STDIO, {"env_vars": {"NEW": MASK}})["env_vars"] == {"NEW": MASK}
