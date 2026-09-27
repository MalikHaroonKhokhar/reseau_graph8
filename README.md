# Réseau

An MCP gateway that gives Graph8 agents access to GitHub, Linear and Graph8's own tools through a single
registration.

Graph8's MCP client can only register servers as legacy HTTP+SSE or stdio, with no header or auth field.
GitHub and Linear serve Streamable HTTP and need `Authorization: Bearer`, so Graph8 can't reach them directly
(`test_connection/FINDINGS.md`). Réseau sits in between:

```
Graph8 agent ──legacy SSE──▶ Réseau gateway ──Streamable HTTP + Bearer──▶ GitHub  /mcp/readonly
             /g8/<token>/sse   (holds all          ├─────────────────────▶ Linear  /mcp/readonly
                                upstream tokens)   └─────────────────────▶ Graph8  /mcp/
```

- **One tool surface.** Tools are prefixed per upstream (`github_list_issues`, `linear_list_issues`); Graph8's
  tools keep their own `g8_` names. Each upstream is limited to an allowlist of read-only tools.
- **Upstream credentials never leave the gateway.** Graph8 only ever sees a gateway token.
- **Failures stay isolated.** A dead or unauthorized upstream is left out of the tool list; the others keep
  working.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp .env.example .env   # then fill it in
```

| Variable | What it is |
|---|---|
| `GITHUB_MCP_TOKEN` | GitHub PAT (read scopes are enough) |
| `LINEAR_API_KEY` | Linear API key (`lin_api_…`) |
| `GRAPH8_API_KEY` | Graph8 org API key (`g8_live_…`). Used as an upstream and by the registration script. |
| `RESEAU_GATEWAY_TOKEN` | Secret Graph8 uses to reach the gateway. Generate it with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Comma-separate several to rotate. |

## Run

```sh
set -a; . ./.env; set +a
uv run python -m reseau.front --host 0.0.0.0 --port 8080
```

The gateway must be reachable from the public internet. For a quick test, use a tunnel:
`ssh -R 80:localhost:8080 nokey@localhost.run`.

## Register with Graph8

```sh
uv run python -m reseau.register_graph8 https://<public-gateway-host>          # create, /test, delete
uv run python -m reseau.register_graph8 https://<public-gateway-host> --keep   # leave it registered
```

The script counts the gateway's tools itself, registers `https://<host>/g8/<token>/sse` as an `sse` server,
and checks that Graph8's `/test` returns `success: true` with the same `tools_count`. Without `--keep`, it
then deletes the record and verifies that it's gone.

## Security

- **The token is in the URL.** A Graph8 registration can only carry `connection_url`, so the gateway token
  goes in the path. It's checked on the SSE stream and on every message POST, compared in constant time, and
  a wrong token gets a plain 404.
- **Graph8 shows the URL to everyone in the org.** Its read route (`GET /api/v1/workflows/mcp-servers`)
  returns `connection_url` in plaintext, so anyone with an org key can read the token. The token only
  unlocks the gateway's read-only, allowlisted tools. Rotate it when a registration is removed:
  put the new token first in `RESEAU_GATEWAY_TOKEN`, re-register, then drop the old one.
- **Tokens stay out of logs and output.** Every upstream token and gateway token is redacted from all log
  records, tool results and errors, and uvicorn's access log is off.

## Tests

```sh
uv run pytest
```

The tests run against local mock MCP servers on loopback and need no network.

## Layout

| Path | Contents |
|---|---|
| `reseau/gateway.py` | Upstream side: credentials, sessions, retries, tool prefixing, allowlists, redaction |
| `reseau/front.py` | Graph8-facing side: legacy SSE server and token auth |
| `reseau/outbound.py` | Shared HTTP policy: explicit User-Agent, backoff on 429/5xx, per-host concurrency cap |
| `reseau/register_graph8.py` | Graph8 registration live check |
| `test_connection/`, `spikes/` | Findings from probing Graph8, GitHub and Linear that the design is based on |
| `upstream/` | Fixes proposed to Graph8, to be handed off as tickets |
