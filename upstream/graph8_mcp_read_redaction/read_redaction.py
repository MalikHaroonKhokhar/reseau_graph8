"""Stop Graph8's read routes from returning MCP registration secrets (HAR-96 follow-up, proposed Graph8 patch).

Measured (spikes/mcp_bridge/FINDINGS.md, Run 3; test_connection/har96_live_check.txt):
`GET /api/v1/workflows/mcp-servers` returns `env_vars` values, full `args` and full `connection_url` in
plaintext to any org-key holder. The SDK contract documents `env_vars` as "WRITE-ONLY: values set here are
never echoed back by the read routes". Since a registration has no headers field, these are the only
places a credential can go, so every MCP credential in an org is currently readable org-wide.

`public_server(record)` is the read-route serializer: every field that can carry a secret is masked, and
the structure stays the same so the UI can still show that a value is set:
  env_vars / headers  values -> "***", keys kept
  args                each arg -> "***", count kept (args can carry inline source, as Réseau's bridge does)
  connection_url      scheme://host[:port] kept; userinfo, path, query and fragment -> "/***"

`apply_update(stored, patch)` is the update-route merge: a client that read a masked record and PUTs it back
unchanged must not overwrite the real secret with "***". Any field (or env/header value) still equal to its
masked form keeps the stored value.
"""
import copy
from urllib.parse import urlsplit

MASK = "***"


def _mask_url(url):
    if not url:
        return url
    u = urlsplit(url)
    host = u.hostname or ""
    if u.port:
        host += ":%d" % u.port
    secret_part = u.username or u.password or u.path.strip("/") or u.query or u.fragment
    return "%s://%s%s" % (u.scheme, host, "/" + MASK if secret_part else u.path)


def public_server(record):
    """Copy of a registration record that is safe to return from a read route."""
    out = copy.deepcopy(record)
    for field in ("env_vars", "headers"):
        if out.get(field):
            out[field] = {k: MASK for k in out[field]}
    if out.get("args"):
        out["args"] = [MASK] * len(out["args"])
    if out.get("connection_url"):
        out["connection_url"] = _mask_url(out["connection_url"])
    return out


def apply_update(stored, patch):
    """Merge an update request into the stored record. Masked values mean "unchanged"."""
    out = copy.deepcopy(stored)
    for field, value in patch.items():
        if field in ("env_vars", "headers") and isinstance(value, dict):
            old = stored.get(field) or {}
            out[field] = {k: old[k] if v == MASK and k in old else v for k, v in value.items()}
        elif field == "args" and isinstance(value, list) and stored.get("args") and value == [MASK] * len(stored["args"]):
            continue
        elif field == "connection_url" and stored.get("connection_url") and value == _mask_url(stored["connection_url"]):
            continue
        else:
            out[field] = value
    return out
