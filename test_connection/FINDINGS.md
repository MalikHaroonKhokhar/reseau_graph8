# MCP connectivity test — GitHub + Linear (2026-09-26)

Client under test: `mcp_probe.py` (stdlib JSON-RPC over streamable HTTP, protocol `2025-06-18`).
Graph8's own MCP client was **not** exercised — no Graph8 endpoint/key available (see "Blocked").

## GitHub — PASS
| step | result |
|---|---|
| endpoint | `https://api.githubcopilot.com/mcp/` |
| auth | `Authorization: Bearer <PAT>`; used local `gh auth token` (scopes: gist, read:org, repo, workflow) |
| transport | streamable HTTP; replies as `text/event-stream`, `Mcp-Session-Id` issued |
| initialize | HTTP 200, 1.0–1.2s, serverInfo `github-mcp-server/remote-7835a23`, protocol 2025-06-18 |
| tools/list | HTTP 200, 1.8–2.4s, **45 tools** |
| tools/call | `get_me` → HTTP 200, ~1.2s, real payload |

Unauthenticated: HTTP 401, `WWW-Authenticate: Bearer error="invalid_request" … resource_metadata=…/oauth-protected-resource/mcp/`.
Scoped variants work and shrink the toolset: `/mcp/readonly` → 27 tools, `/mcp/x/issues` → 10 tools (no `get_me` there).

## Linear — server healthy, direct auth BLOCKED
| step | result |
|---|---|
| endpoint | `https://mcp.linear.app/mcp` (streamable HTTP) |
| unauthenticated | HTTP 401, `WWW-Authenticate: Bearer realm="OAuth", resource_metadata="…/oauth-protected-resource/mcp", scope="read write"` |
| legacy SSE | `https://mcp.linear.app/sse` → **HTTP 404** — SSE transport is gone; an SSE-only client cannot connect |
| auth server | `/authorize`, `/token`, `/register` (dynamic client registration supported), PKCE S256, grants: authorization_code, refresh_token, jwt-bearer |
| static auth | docs: OAuth token **or Linear API key** in `Authorization: Bearer`; read-only via `/mcp/readonly` (401 = route live) or a Read-only API key |
| read-only call | verified out-of-band through this session's Linear OAuth connector: `list_teams` → 1 team. Server-side discovery + read path is healthy. |
| direct bearer call | not run — no OAuth access token on this machine |

Correction to an earlier note in this file: Linear **does** accept a static credential — its docs state the server takes an OAuth token *or a Linear API key* in `Authorization: Bearer <token>`, and `https://mcp.linear.app/mcp/readonly` exists (401 challenge, so the route is live). So "paste an API key" works for Linear; what does **not** work is an SSE-only client (see Transport claim below). Untested here only because no key is on this machine — a bogus key returns 401 as expected.

## Both servers together
Single process, both clients in one run: GitHub completed all three steps while Linear failed auth — no cross-contamination, failures stay per-server.
No tool-name collisions between GitHub's 45 names and Linear's ~80. **(Wrong — corrected in Run 2: raw Linear names collide with GitHub on `list_issues` and `list_releases`. The earlier check compared GitHub against Claude's namespaced `mcp__…_Linear__*` names, which cannot collide by construction.)** But a single agent request holding both = ~125 tool definitions; expect payload bloat and tool-selection errors. Mitigation: GitHub's scoped endpoints (`/mcp/readonly`, `/mcp/x/<toolset>`) and trimming Linear's toolset.

## Errors observed
- GitHub, no token: `401 bad request: missing required Authorization header`.
- Linear, no token: `401` + OAuth challenge (above).
- Linear `/sse`: `404` (transport removed).
- No timeouts, no protocol-negotiation failures, no malformed responses in any successful run.

## Blocked / not yet tested
1. Graph8's own MCP client and a single Graph8 agent request holding both servers — needs `GRAPH8_API_URL`, `GRAPH8_API_KEY`, and the server-registration format.
2. Linear over a raw bearer token from this client.
3. Behaviour under concurrency, long sessions, and OAuth token refresh.

## Transport claim — "Graph8 accepts SSE/stdio; modern servers like Linear expose Streamable HTTP"

**Both halves confirmed.** Evidence for the Graph8 half, from `@graph8/sdk@0.245.0` (`npm i @graph8/sdk`, types generated from the API contract), `dist/index.d.ts:15965`:

```ts
interface McpServerCreateRequest {
    args?: Array<string> | null;
    /** Required when `transport_type` is `stdio`. */
    command?: string | null;
    /** Required when `transport_type` is `sse`. */
    connection_url?: string | null;
    /** Environment for the server process. WRITE-ONLY: values set here are never echoed back by the read routes. */
    env_vars?: Record<string, string> | null;
    name: string;
    /** `sse` for a remote server reached over HTTP, `stdio` for a local process. */
    transport_type: "sse" | "stdio";
}
```

`transport_type` is a closed two-value union in both create and update (`:15988`), and the string "streamable" appears **nowhere** in the package (0 hits across `dist/` and README). So the contract Graph8 validates against has no Streamable HTTP option.

Caveat worth one test: the field is *named* `sse`, which does not prove the backend client speaks legacy HTTP+SSE on the wire — several implementations label the field `sse` while using an SDK client that negotiates Streamable HTTP. Registering Linear as `{transport_type: "sse", connection_url: "https://mcp.linear.app/mcp"}` and checking whether tools appear settles it in one call. Needs a Graph8 API key (app.graph8.com/settings → MCP & API → API).

The SDK's own MCP routes (`/profile/mcp/tools`, `/profile/mcp/status`, `/profile/mcp/sessions`, `/mcp/connection`) are the *inbound* side — Graph8 as an MCP server for clients like Claude. Outbound registration CRUD is not exposed in this SDK, only its request types.

Spec status (modelcontextprotocol.io, versions 2025-06-18 **and** the current 2025-11-25): "The protocol currently defines two standard transport mechanisms: stdio and Streamable HTTP." Streamable HTTP "replaces the HTTP+SSE transport from protocol version 2024-11-05"; HTTP+SSE is referred to only as *the deprecated transport*, kept alive through a backwards-compatibility fallback. So "SSE" is not a current transport — it is the 2024-11-05 legacy.

Measured 2026-09-26, unauthenticated POST `initialize` and GET (`Accept: text/event-stream`):

| server | `/mcp` | `/sse` |
|---|---|---|
| Linear | 401 (live) | **404** |
| Sentry | 401 (live) | 404 POST / **410 Gone** |
| Notion | 401 | 401 (route still there) |
| Atlassian (`/v1`) | 401 | 401 |
| Asana | 401 | 401 |
| PayPal | 401 | 401 |
| GitHub | 401 → 200 with PAT | no legacy SSE endpoint; `api.githubcopilot.com/sse` = 404, and `/mcp/sse` just routes to the Streamable-HTTP server (200, `text/event-stream` POST reply) |

Every server ships Streamable HTTP. Legacy SSE lingers on some, but Linear and Sentry have already dropped it. Linear's own docs call Streamable HTTP "the primary transport" and `/sse` "a deprecated fallback" — and that fallback now 404s, so the doc's WSL `--transport sse-only` recipe is stale.

**Consequence for Graph8** — both registration paths were tested against the live API and both are closed today:

1. **`sse` → a Streamable HTTP server: fails.** Proven with a public no-auth server, so transport is the only variable (`/test` → `success: false`, `tools_count: null`).
2. **`stdio` → `npx -y mcp-remote …`: fails.** `/test` → `"[Errno 2] No such file or directory: 'npx'"`. There is no Node in the runtime that spawns the subprocess, so the standard bridge cannot run.

### What the stdio runtime actually has
`Errno 2` vs any other error is a clean existence oracle for the spawn host. Registered a throwaway stdio server per command, called `/test`, deleted it:

| command | `/test` message | verdict |
|---|---|---|
| `npx` | `[Errno 2] No such file or directory: 'npx'` | **absent** — no Node |
| `uvx` | `[Errno 2] No such file or directory: 'uvx'` | **absent** |
| `python3` | `unhandled errors in a TaskGroup` (process ran, handshake failed as designed) | **present** |
| `sh` | `unhandled errors in a TaskGroup` | **present** |

So the spawn host is a Python image with no Node and no uv. `mcp-remote` (Node) cannot run there. Two follow-ups on the Python route:

* `python3 -m mcp_proxy <url>` → `TaskGroup` error, fast. The `mcp-proxy` package is **not installed**, so there is no ready-made Python bridge either.
* `python3 -c "import mcp, time; time.sleep(100)"` → the call **hung until Cloudflare returned 502**. A failed import would have exited immediately with the fast `TaskGroup` error instead, so the Python **MCP SDK appears to be present in the image**. If so, adding Streamable HTTP is close to a one-line change in Graph8's client: the same SDK ships `streamablehttp_client` alongside `sse_client`.

That hang also exposes a second bug: nothing times out the connect attempt server-side, so a slow or wedged MCP server turns into a **Cloudflare 502** (the same signature `GET /{uuid}/tools` produced). A registration test should fail fast with a reason, not ride the gateway timeout.

Which leaves, in order of cost: add Streamable HTTP to the client (one transport, what the spec and every vendor now target, and the only option if that runtime cannot spawn subprocesses at all); or install Node in the image so `mcp-remote` works, which also needs a header/auth field added to the registration contract before Linear or GitHub can authenticate.

---

# Run 2 — real credentials (2026-09-26)

## Linear — PASS with a static API key
`lin_api_…` sent as `Authorization: Bearer` against `https://mcp.linear.app/mcp`: initialize 200 (1.8s), **59 tools**, `list_teams` → team "Personal". Confirms empirically what Run 1 got wrong: **no OAuth flow needed, a plain Linear API key works.** Linear returns **no `Mcp-Session-Id`** — stateless server, so nothing to carry between calls.

## GitHub — PASS on the refreshed PAT (first token was dead)
**Resolved:** the refreshed 40-char `ghp_…` PAT passes — initialize 200, 45 tools, `get_me` → real profile. The note below is about the first, dead token.

The original `ghp_…` token in `.env` failed at the MCP server (`401 unauthorized: AuthenticateToken authentication failed`) **and** at the plain REST API (`GET https://api.github.com/user` → 401). So it is the credential, not MCP: expired, revoked, or mis-copied. Regenerate it, or use the `gh` CLI token, which passes: initialize 200, **45 tools**, `get_me` → real profile.

## Graph8 as an MCP *server* — PASS
`GRAPH8_API_URL` in `.env` is `https://be.graph8.com/mcp/`, which is Graph8's own MCP endpoint (not the REST base — that is `https://be.graph8.com`). With the `g8_live_…` key as bearer:

| step | result |
|---|---|
| initialize | HTTP 200, ~0.9s, replies `text/event-stream` — i.e. Graph8 **serves** Streamable HTTP |
| session | **no `Mcp-Session-Id` header** |
| tools/list | HTTP 200, 0.6–1.1s, **126 tools** (`g8_*`) |
| tools/call | blocked by a session gate: `-32003 "Org context not established for this session. Call g8_current_org first"` |

The gate plus the missing session header is the thing to watch: the server tracks "session" by something other than the MCP session id (presumably the API key), so any client must call `g8_current_org` before its first real tool call, and no client can *resume* that context via the standard header.

## Combined — 3 servers, one client run
230 tools total (45 GitHub + 59 Linear + 126 Graph8). Per-server failures stayed isolated. **Name collisions: `list_issues` and `list_releases` (GitHub ↔ Linear).** Any client that flattens tools into one namespace will mis-route those two; Graph8 must prefix per server (as it already does for its own `g8_` tools).

## Graph8 REST — MCP surfaces
| route | result |
|---|---|
| `GET /api/v1/workflows/mcp-servers` | 200 `{"servers":[],"total":0}` — **no external MCP servers registered yet** |
| `GET /api/v1/profile/mcp/tools` | 200, toolbox groups (`crm` "222 tools", `campaigns`, …) |
| `GET /api/v1/profile/mcp/status` | **403** — "An organisation-scoped key has no person behind it … Use a personal API key." The key in `.env` is org-scoped. |

## Outbound registration surface
`POST /api/v1/voice/mcp-servers` is the route (GET on the collection → 405 Method Not Allowed; `/api/v1/mcp/servers`, `/api/v1/mcp-servers`, `/api/v1/agents/mcp-servers` → 404). The SDK exposes the request *types* but no client method, so callers hand-roll the HTTP.

### Registration, measured end to end
Created two throwaway servers in the live org and deleted them again (final state verified: `{"servers":[],"total":0}`).

| call | result |
|---|---|
| `POST /api/v1/voice/mcp-servers` `{}` | 422, requires `name` + `transport_type` |
| `POST` create (`transport_type: "sse"`) | **201**, record has `id` (int), `mcp_server_id` (uuid), `enabled`, **`cached_tools: null`**, **`last_connected_at: null`** — registration alone connects to nothing |
| `GET /api/v1/voice/mcp-servers` and `/{id}` | 405 — the route is write-only; reads come from `GET /api/v1/workflows/mcp-servers` |
| `POST /api/v1/voice/mcp-servers/{uuid}/test` | exists, 200 with a result body |
| `GET /api/v1/voice/mcp-servers/{uuid}/tools` | **502 `origin_bad_gateway`** — route exists, origin fell over (probably hanging on the same connect) |
| `POST .../{uuid}/refresh` | 404 |
| `DELETE /api/v1/voice/mcp-servers/{uuid}` | 200 `{"success": true}` — uuid, not the int id |

### The decisive test
Registered `https://learn.microsoft.com/api/mcp` as `transport_type: "sse"`. That server is **public, needs no auth, and serves Streamable HTTP only** (verified directly: initialize 200, `Mcp-Session-Id` issued; GET on `/api/mcp/sse` → 405). So auth is removed as a variable and transport is the only thing under test. Graph8's answer:

```json
POST /api/v1/voice/mcp-servers/{uuid}/test
{"data": {"success": false, "message": "unhandled errors in a TaskGroup (1 sub-exception)", "tools_count": null}}
```

**Graph8's `sse` client cannot connect to a Streamable HTTP server.** The `sse` label is not cosmetic — it is the legacy HTTP+SSE transport, so every modern remote server is unreachable through it, Linear included. `tools_count: null` means no discovery happened.

Secondary finding: the failure message is an unhandled `TaskGroup` sub-exception, i.e. the real cause is swallowed before it reaches the API. Anyone debugging a failed registration gets no actionable signal — worth fixing regardless of the transport work.

**Contract gap, and it compounds the transport one:** `McpServerCreateRequest` has no header/auth field. A `transport_type: "sse"` registration carries only `connection_url`; `env_vars` is documented as "environment for the server process", i.e. stdio. So a remote server that authenticates with `Authorization: Bearer` — Linear and GitHub both do — cannot be given a credential at all through this shape. Even if Graph8's client spoke Streamable HTTP, remote Linear would still fail on auth. stdio + `mcp-remote` is the only path that can pass a token today.

## Cloudflare throttling and UA banning — client-side hazards
Two separate bot-protection behaviours on `be.graph8.com`, both of which hit an integration rather than a browser:

1. **Bursts get challenged.** ~6 parallel route probes were all answered with **HTTP 429 plus a Cloudflare "Just a moment…" HTML interstitial** instead of JSON-RPC — including on `/mcp/` itself. Serial requests a few seconds apart are fine. A client that assumes a JSON body on non-200 throws a parse error here instead of retrying, and tool fan-out inside one agent turn is exactly the pattern that trips it.
2. **Default library user-agents are banned outright.** Python's `urllib` default UA returns **HTTP 403, Cloudflare error 1010 `browser_signature_banned`** ("blocked access based on your browser's signature") on every route, including ones that had just answered 200 to `curl`. Setting any ordinary `User-Agent` clears it. Note that this means an MCP/HTTP client library's out-of-the-box defaults can be blanket-banned by your own edge — worth checking before blaming an integration.

---

# Verdict for Réseau

> **Superseded for the MCP-client half by Run 3 below**: two registration shapes now return `success: true` with tools. Direct registration of Linear/GitHub is still closed; a bridge is open.

**Graph8 as an MCP server: usable now.** 126 tools over Streamable HTTP, an org-scoped key authenticates, org context persists per key across calls, ~0.6–1.8s per call. Caveats: call `g8_current_org` before the first tool call, no `Mcp-Session-Id` so context cannot be resumed the standard way, `/profile/*` routes need a *personal* key, and the edge will 429-challenge bursts and 403-ban default library user-agents.

**Graph8 as an MCP client: not usable for Linear or GitHub today.** Not a credential problem — all three servers authenticate fine from a plain client:

| server | auth | tools | read-only call |
|---|---|---|---|
| GitHub (`api.githubcopilot.com/mcp/`) | refreshed `ghp_` PAT | 45 | `get_me` ✓ |
| Linear (`mcp.linear.app/mcp`) | `lin_api_` key | 59 | `list_teams` ✓ |
| Graph8 (`be.graph8.com/mcp/`) | `g8_live_` key | 126 | gated, then arg-level error ✓ |

Three independent blockers, each verified against the live API:
1. **Transport.** `transport_type` is `"sse" | "stdio"` only, and the `sse` client genuinely cannot reach a Streamable HTTP server (proven with a public no-auth server, so auth was not a factor). Linear has no SSE endpoint at all (`/sse` → 404), and GitHub never had one.
2. **No way to pass credentials to a remote server.** `McpServerCreateRequest` has no header/auth field; `env_vars` only reaches a spawned process. Linear and GitHub both need `Authorization: Bearer`.
3. **No bridge available.** `stdio` works in principle but the runtime has no `npx` and no `uvx`, and `mcp-proxy` is not installed.

**Smallest path to unblocked:** add `streamablehttp_client` as a third `transport_type` (the SDK is likely already in the image) **and** a `headers` map on the registration contract. Either one alone still leaves Linear and GitHub unreachable.

**Also fix while in there:** the swallowed `TaskGroup` exception (every failure looks identical), the missing connect timeout (hangs become 502s), and per-server tool-name prefixing — GitHub and Linear both expose `list_issues` and `list_releases`, and 230 tools in one agent request needs scoping anyway.

All test registrations created during this work were deleted; final state verified `{"servers": [], "total": 0}`.

---

# Run 3 — bridge spike: which registration shape produces tools (2026-09-26)

Probe: `bridge_probe.py` (re-runnable: `python3 bridge_probe.py [a_sse b_import b_net ctl_sleep ctl_missing b_bridge]`). Each case registers a throwaway `reseau-probe-*` server, reads it back from the list route, calls `/test`, deletes it, and sweeps leftovers at the end. Serial, 3 s apart, UA `reseau-bridge-probe/1`. Bridge payload: `stdio_bridge.py`. Raw results go to `bridge_results.json` (gitignored).

## /test responses, verbatim

| case | registration | `/test` response | time |
|---|---|---|---|
| **A** `a_sse` | `sse` → `https://mcp.api.coingecko.com/sse` (public, no auth, legacy HTTP+SSE: GET returns `event: endpoint`) | `200 {"data":{"success":true,"message":"Connection successful","tools_count":2},"pagination":null}` | 2.1 s |
| `ctl_missing` | `python3 -c "import no_such_module_reseau, time; time.sleep(100)"` | `200 {"data":{"success":false,"message":"unhandled errors in a TaskGroup (1 sub-exception)","tools_count":null},"pagination":null}` | 0.5 s |
| `ctl_sleep` | `python3 -c "import time; time.sleep(100)"` | `502 error code: 502` | 15.9 s |
| B `b_import` | `python3 -c "import mcp.client.streamable_http, time; time.sleep(100)"` | `502 error code: 502` | 15.6 s |
| B `b_net` | `python3 -c` urlopen `https://learn.microsoft.com/api/mcp` (HTTPError tolerated), then sleep | `502 error code: 502` | 15.7 s |
| **B** `b_bridge` | `python3 -c <stdio_bridge.py>`, `env_vars: {UPSTREAM_URL: https://learn.microsoft.com/api/mcp}` | `200 {"data":{"success":true,"message":"Connection successful","tools_count":3},"pagination":null}` (run twice, 1.3 s / 2.4 s) | 1.3 s |

**Oracle calibration.** The controls pin both states: a failed import returns a fast `200` `TaskGroup` error; a live process returns `502` at about 15.8 s. The gateway cutoff is now about **16 s, not the ~100 s** measured earlier, so hangs are cheaper to probe than they used to be. By the controls:
- **`mcp.client.streamable_http` is present** on the spawn host (`b_import` hangs like `ctl_sleep`). The version is unknown, and the name differs across versions (`streamablehttp_client` in 1.x; `streamable_http_client` in 1.30+ and 2.x, where the `headers=` kwarg is gone). That is why the bridge does not use the SDK.
- **The spawn host has outbound HTTPS** (`b_net` hangs), and `b_bridge` proves it outright: 3 tools is exactly `learn.microsoft.com`'s toolset.

**Graph8's `sse` client works against a real legacy-SSE server.** Together with Run 1's failure against Streamable HTTP, this confirms it is the 2024-11-05 transport and nothing newer.

## The bridge (`stdio_bridge.py`)
A ~45-line **stdlib-only** stdio ↔ Streamable HTTP pass-through: each stdin JSON-RPC line is POSTed upstream with `Mcp-Session-Id` carried, and JSON or SSE replies are written to stdout. It needs only `python3`, which is proven present, so it does not depend on the image's SDK version. It is passed inline as `args: ["-c", <source>]`. Verified locally with mcp 1.9, 1.30 and 2.2 stdio clients, including authenticated upstreams (token via `UPSTREAM_TOKEN`): GitHub 45 tools and `get_me` OK; Linear 59 tools and `list_teams` OK. Limits: one request at a time; server→client requests mid-stream are dropped.

## Credential exposure — `env_vars` is NOT write-only
The SDK types document `env_vars` as "WRITE-ONLY: values set here are never echoed back by the read routes". **False on the live API:** `GET /api/v1/workflows/mcp-servers` returned `"env_vars": {"UPSTREAM_URL": "...", "RESEAU_CANARY": "canary-not-a-secret"}` in plaintext. The same route also echoes `connection_url` and the full `args`. So **every field a registration can carry is readable by any holder of an org key**. No channel exists today that hides a secret from readers.

Consequences:
- B with the raw GitHub PAT or Linear key in `env_vars` stores those tokens readable org-wide. Do not do this.
- A with a secret in the URL path has the same exposure, but only for whatever that secret unlocks.
- Report to Graph8: this is a doc/behaviour mismatch on a field that is explicitly meant for secrets.

## Recommendation: **A — Réseau gateway over legacy SSE**, with B as the proven fallback

1. **Path.** The Réseau gateway serves legacy HTTP+SSE (2024-11-05) publicly and is registered as `transport_type: "sse"`. This is native to Graph8 (2 s, no subprocess, no code shipped in `args`), and it is the same client that just returned `success: true`. The gateway speaks Streamable HTTP upstream to GitHub and Linear.
2. **Graph8 → gateway auth: a capability secret in the URL path**, e.g. `https://<gateway>/g8/<secret>/sse`. The header field does not exist, and the path is the only thing Graph8 sends. Because `connection_url` is readable org-wide, the secret must be:
   - per Graph8 org, random (≥128 bits), revocable and rotatable from the gateway side;
   - scoped to the gateway's tool surface only, never the upstream credential itself;
   - validated in constant time, stripped from gateway logs and access logs, and required on the POST message endpoint too (the `endpoint` event must carry it or a session-bound token).
3. **Upstream tokens (GitHub PAT, Linear key) live only in the gateway.** They never reach Graph8, so the plaintext-echo bug exposes a revocable gateway key and not a GitHub/Linear credential.
4. **Fallback B:** if a public gateway endpoint is not available, the stdlib bridge works today. Point it at the gateway (`UPSTREAM_URL` plus a gateway-scoped `UPSTREAM_TOKEN`), not at GitHub or Linear directly, for the same reason as point 3.
5. **Risk to track:** legacy SSE is deprecated in the MCP spec, and several vendors have already turned it off (Linear, Sentry, DeepWiki and Cloudflare docs return 404/410). Graph8 still depends on it. The gateway absorbs this: when Graph8 ships Streamable HTTP plus a headers field, only the gateway's front transport changes.

Also still open for Graph8: the swallowed `TaskGroup` cause, and per-server tool prefixing (`list_issues` and `list_releases` collide).

Cleanup: every `reseau-probe-*` registration was deleted; the final `GET /api/v1/workflows/mcp-servers` returned `{"servers":[],"total":0}` (both runs).

---

# Run 4 — read-only endpoints and the allowlist (HAR-95, 2026-09-27)

## Linear `/mcp/readonly` — PASS with a static API key
`https://mcp.linear.app/mcp/readonly` with the `lin_api_…` key as `Authorization: Bearer`: initialize 200, tools/list → **35 tools** (vs **59** on `/mcp`). Every tool is `get_*`, `list_*`, `search_documentation` or `extract_images`. None of them write. The 24 dropped tools are the writers: `save_*` (issue, comment, document, project, milestone, release, release note, status update, labels), `create_*`, `delete_*`, `retire_*`/`restore_*` labels, `share_issue`/`unshare_issue`, `mark_notification`, `prepare_attachment_upload`. `list_teams` → OK.

GitHub `/mcp/readonly` re-listed: 27 tools, as before. It includes `run_secret_scanning`, which the allowlist keeps out.

## Gateway default surface
The default config uses read-only endpoints for GitHub and Linear. Graph8 has no known read-only variant. Each provider also has an allowlist, seeded from the semantic-tool tickets (HAR-100/101/109):

| upstream | endpoint | upstream tools | exposed |
|---|---|---|---|
| GitHub | `/mcp/readonly` | 27 | 7: `get_me`, `list_commits`, `get_commit`, `list_pull_requests`, `pull_request_read`, `list_issues`, `issue_read` |
| Linear | `/mcp/readonly` | 35 | 6: `list_teams`, `list_issues`, `get_issue`, `list_comments`, `list_projects`, `get_project` |
| Graph8 | `/mcp/` | 126 | 1: `g8_current_org` |

Live smoke (`python -m reseau.gateway`): all three connect and return 7/6/1 tools. `get_me`, `list_teams` and `g8_current_org` all return OK. **230 → 14 tools** per agent request. A call to a tool not on the allowlist gets `-32007 tool_not_allowed` and is never sent upstream.
