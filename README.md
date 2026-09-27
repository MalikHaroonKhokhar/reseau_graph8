# Réseau

An MCP gateway that gives Graph8 agents access to GitHub, Linear and Graph8's own tools through a single
registration.

Graph8's outbound MCP registration (`POST /api/v1/voice/mcp-servers`) accepts only legacy HTTP+SSE or stdio.
A remote (`sse`) registration has no credential field at all, so Graph8 cannot present a bearer token to it; a
`stdio` registration can carry secrets in `env_vars`, but the read route echoes them to any org-key holder.
GitHub and Linear serve Streamable HTTP and need `Authorization: Bearer`, so Graph8 can't reach them directly
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
  working. A dropped connection, before the answer or partway through it, fails only the call it hit, and reads
  are retried, so it never takes down an upstream's session.
- **Evidence behind every claim.** Réseau's own `get_evidence(activity_id)` resolves `github:pr:owner/repo#9`,
  `github:commit:owner/repo@<sha>`, `github:review_comment:owner/repo#9/<comment id>`,
  `github:review:owner/repo#9/<review id>` or `linear:issue:ENG-142` to a normalized record: canonical URL,
  actor, timestamps and `fetched_at`. A commit ID carries its repo because GitHub can't look up a bare SHA.
- **Graph8 business records are evidence too.** `graph8:customer:<company id>`, `graph8:opportunity:<deal id>`,
  `graph8:commitment:<task id>` and `graph8:conversation:meeting/<id>` or `graph8:conversation:<channel>/<thread id>`
  resolve to the same record shape, with the owner (deal owner, task assignee) as the actor. Graph8 has no record
  URL, so `url` is `null` and the activity_id is the citation. A commitment is a Graph8 task linked to a deal or
  company; any other task is `not_found`. Known gaps: account correspondence has no MCP tool (REST only) and isn't
  covered; meeting and inbox-thread shapes come from Graph8's output schemas, since the org had none to read
  (`spikes/graph8_entities/FINDINGS.md`).
- **Fewer, smarter tools.** `get_person_activity(person, date)` returns one person's commits, PRs opened and
  merged, reviews, and Linear issues moved or completed on a day. `get_my_day_context()` returns the caller's
  `focus` (highest-priority open Linear issues, with the open PRs they wait on), `needs_attention`
  (unresolved review threads on their open PRs) and `yesterday` (commit and repository counts). Both return
  structured facts, not prose, and every fact carries the activity_ids `get_evidence` resolves. Commits come
  from every branch, unmerged feature branches included. Every listing is paged through; anything cut off by
  a safety limit is named in the answer's `incomplete`, never dropped silently.
- **Project and team facts with their evidence.** `get_project_context(project)` returns a configured
  project's open, in-progress and blocked Linear issues, the open PRs in its repositories, and the issues
  completed and PRs merged in the last 7 days. `get_team_summary(date)` counts one day's completed issues,
  merged PRs and commits per team member and in total, and lists the team's blocked issues. Every count comes
  with its activity_ids and always equals their number. An issue is blocked when a Linear blocked-by relation
  points at an open issue; the answer names the blocker and the open PRs it waits on.
- **GitHub scope is a permission.** `RESEAU_GITHUB_SCOPE` is the complete list of GitHub owners and repos
  the gateway may read: the raw `github_*` tools, `get_evidence` and the semantic tools alike. A call for
  any other repo is refused before it reaches GitHub (`-32012 out_of_scope`), and an org's repos are never
  read or shown until the org is listed. GitHub search, which could reach any repo the token sees, is
  internal to the semantic tools, which add the scope to every query; clients can't call it.
- **Start My Day: a briefing where every sentence is checked.** A Graph8 workflow calls `get_my_day_context`
  on the registered gateway, and an agent writes Focus today, Needs attention and Yesterday as sentences, each
  with the activity_ids it cites. The trigger returns the briefing only if every sentence cites at least one
  ID the tool returned for that section. A section with no activity must say "Nothing to report." and nothing
  else, and a section with activity can't say it. A reply that fails the check is retried once, then refused.

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
| `RESEAU_IDENTITIES` | Optional. Path to a JSON map of people to their upstream IDs, e.g. `{"ana": {"github": "ana-gh", "linear": "<Linear user id>", "graph8": "<Graph8 user id>"}}`. Actors not in the map are reported as `unmapped`, never guessed. `get_person_activity` only accepts people in this map. |
| `RESEAU_GITHUB_SCOPE` | Comma-separated GitHub owners (users or orgs) and `owner/repo` entries: everything the gateway may read on GitHub, e.g. `ana-gh,acme/app`. An owner entry covers only repos that account owns, not the orgs it belongs to. Unset means no GitHub repo is read. |
| `RESEAU_PROJECTS` | Optional. Path to a JSON map of projects to their Linear project and GitHub repos, e.g. `{"app": {"linear": "App launch", "repos": ["acme/app"]}}`. Every repo must be inside `RESEAU_GITHUB_SCOPE`. `get_project_context` only accepts projects in this map. |
| `RESEAU_TEAM` | Optional. The Linear team (name, key or ID) that `get_team_summary` covers. Members not in `RESEAU_IDENTITIES` are listed as unmapped and not counted. |
| `RESEAU_TIMEZONE` | Optional. IANA timezone (e.g. `Asia/Karachi`) that sets where a day starts for `get_person_activity`'s and `get_team_summary`'s date and for "yesterday". Default `UTC`. |
| `RESEAU_GATEWAY_TOKEN` | Secret Graph8 uses to reach the gateway. Generate it with `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Comma-separate several to rotate. |
| `RESEAU_START_MY_DAY` | The Start My Day workflow's `action_id`, printed by `python -m reseau.workflows setup`. |

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

## Start My Day

```sh
uv run python -m reseau.register_graph8 https://<public-gateway-host> --keep   # once
uv run python -m reseau.workflows setup          # creates the voice agent and workflow, prints RESEAU_START_MY_DAY=...
uv run python -m reseau.workflows start-my-day   # runs it and prints the verified briefing (~12 credits a run)
```

The dashboard calls `reseau.workflows.start_my_day(g8, action_id)` and gets back
`{"execution_id", "date", "sections": {"focus" | "needs_attention" | "yesterday": [{"text", "activity_ids"}]}, "incomplete"}`,
or a `WorkflowError` listing what failed. `incomplete` is the tool's own list of anything that may be
missing. `workflows.check(briefing, sources)` is the citation verifier; the daily report and Ask Réseau use it
as well.

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

Live check of Start My Day. Graph8 runs the real workflow against the gateway served over the test fixtures,
3 times plus once on an empty day. It is billable, and it deletes everything it creates:
`uv run python -m tests.live_start_my_day https://<public host>`, with a tunnel open to port 8080.

## Layout

| Path | Contents |
|---|---|
| `reseau/gateway.py` | Upstream side: credentials, sessions, retries, tool prefixing, allowlists, redaction |
| `reseau/front.py` | Graph8-facing side: legacy SSE server and token auth |
| `reseau/evidence/` | Normalized records with provenance, activity_ids, identity mapping, `get_evidence`; one normalizer module per source (GitHub, Linear, Graph8) |
| `reseau/semantic.py` | `get_person_activity`, `get_my_day_context`, `get_project_context` and `get_team_summary`: upstream fetching, then pure aggregation over records. The docstring records how "me", dates, issue↔PR links, unresolved threads, projects, blocked issues and team membership are resolved. |
| `reseau/outbound.py` | Shared HTTP policy: explicit User-Agent, backoff on 429/5xx, per-host concurrency cap |
| `reseau/workflows.py` | Graph8 workflows: the Start My Day definition, prompt and trigger, and the citation verifier they share |
| `reseau/register_graph8.py` | Graph8 registration live check |
| `test_connection/`, `spikes/` | Findings from probing Graph8, GitHub and Linear that the design is based on |
| `upstream/` | Fixes proposed to Graph8, handed off as tickets (tests: `uv run pytest upstream/<name>`) |
