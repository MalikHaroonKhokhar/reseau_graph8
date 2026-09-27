# Réseau

An MCP gateway that gives Graph8 agents access to GitHub, Linear and Graph8's own tools through a single
registration.

Graph8's outbound MCP registration (`POST /api/v1/voice/mcp-servers`) accepts only legacy HTTP+SSE or stdio and
carries no header or auth field, so it cannot present a bearer token to an upstream. GitHub and Linear serve
Streamable HTTP and need `Authorization: Bearer`, so Graph8 can't reach them directly
(`test_connection/FINDINGS.md`). Graph8's own inbound MCP server is OAuth over Streamable HTTP and is
unaffected — that is the endpoint Réseau consumes as an upstream. Réseau sits in between:

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
uv run python -m reseau.front --port 8080        # binds 127.0.0.1
```

Réseau runs locally only; there is no deployed instance. Graph8 calls the gateway from its own servers, so
open a tunnel while Graph8 needs it, and close it when you're done:
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
- **Known gap (accepted): Graph8 shows the token to everyone in the org.** Its read route
  (`GET /api/v1/workflows/mcp-servers`) returns `connection_url` in plaintext while a registration exists,
  and nothing on Réseau's side can hide it. HAR-96 accepts this; the criterion is "no *upstream* credentials
  in Graph8 read responses". A possible Graph8-side fix is sketched in `upstream/graph8_mcp_read_redaction/`
  (not planned). To keep the exposure small:
  - Upstream tokens never reach Graph8. The gateway token only unlocks read-only, allowlisted tools.
  - A leaked token is useless while the tunnel is down, and the tunnel only runs while Graph8 needs it.
  - Use a fresh `RESEAU_GATEWAY_TOKEN` each session, and let `register_graph8` delete the record (no `--keep`)
    unless an agent needs it.
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
| `upstream/` | Fixes proposed to Graph8, handed off as tickets (tests: `uv run pytest upstream/<name>`) |
