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
- **Why a task matters.** `get_business_context(activity_id)` takes a Linear issue or GitHub PR and returns the
  Graph8 customers, opportunities, commitments and conversations linked to it. Each comes with its activity_id and
  a `link_type`:
  - `source_url`: a Graph8 task, linked to a deal or company, was created from the issue or PR;
  - `explicit_reference`: the issue's description names the record's activity_id, e.g. `graph8:opportunity:<id>`;
  - `graph8_link`: Graph8 links it to the record named in `via`, such as the task's deal or the deal's company.

  A PR links through the Linear issues it is attached to. Nothing is inferred from names: with no link, the answer
  is empty, with `reason: "no_link_found"`. Known gap: Linear can't look an issue up by attachment, so a PR is
  matched to the issues whose keys appear in its title, body or branch. A PR attached to an issue by hand, with no
  key in its text, isn't found.
- **GitHub scope is a permission.** `RESEAU_GITHUB_SCOPE` is the complete list of GitHub owners and repos
  the gateway may read: the raw `github_*` tools, `get_evidence` and the semantic tools alike. A call for
  any other repo is refused before it reaches GitHub (`-32012 out_of_scope`), and an org's repos are never
  read or shown until the org is listed. GitHub search, which could reach any repo the token sees, is
  internal to the semantic tools, which add the scope to every query; clients can't call it.
- **Start My Day: a briefing where every sentence is checked.** A Graph8 workflow calls `get_my_day_context`
  on the registered gateway, and an agent writes a Summary, then Focus today, Needs attention and Yesterday as
  sentences, each with the activity_ids it cites. The trigger returns the briefing only if every sentence cites
  at least one ID the tool returned for that section (the summary may cite anything the tool returned), every
  number in the summary is a count the tool returned, and every blocked focus issue's sentence names and cites
  its blocker. A section with no activity must say "Nothing to report." and nothing else, and a section with
  activity can't say it. A reply that fails the check is retried once, then refused.
- **A daily report whose numbers match the source.** A second workflow calls `get_team_summary(date)`, and an
  agent writes a Summary, then Completed, Merged, Commits and Blocked. Every number in a count line must equal
  the tool's count, and the line must cite every activity_id behind it. Each blocked issue gets a line that
  names and cites its blocker. A day with no activity is a report that says "Nothing to report." in every
  section.
- **Ask Réseau: questions answered with their sources, or declined.** A Graph8 agent reads a free-text
  question and picks one of the semantic tools and its arguments: `get_person_activity`, `get_project_context`,
  `get_team_summary`, `get_evidence`, `get_my_day_context` or `get_business_context`. The raw `github_*`,
  `linear_*` and `g8_*` tools are never offered, and a route to one is refused before anything runs. That tool's
  workflow runs, and a second agent answers in one to five sentences. The answer comes back only if every
  sentence cites activity_ids the tool returned and every number in it appears in that output. When the tool
  returned linked Graph8 records, the answer must cite at least one. A question no tool covers (the weather,
  revenue, anyone not in the identity map), or one the output doesn't answer, gets "No evidence found."
  Graph8 can't pick the tool inside a single workflow: a tool node's `mcp_tool_name` isn't interpolated (live,
  2026-09-27), and the agent node can't call MCP tools (HAR-91). So the router is a workflow of its own, and
  each semantic tool has an answer workflow. A question costs two agent runs.
- **Checks catch numbers and citations, not meaning.** A summary sentence can cite real IDs and state real
  counts and still add a judgment ("a strong day"). The prompts forbid judgments, causes and claims about what
  didn't happen, and live runs follow them, but no check enforces it.
- **The dashboard: the three workflows in a browser, every claim one click from its source.** Start My Day
  (one click), the daily report (pick a day) and Ask Réseau (type a question) run their Graph8 workflows and
  show only verified output. Each sentence carries its citations (`ENG-142`, `PR #9`, `SHA abc1234`); clicking
  one, or the sentence, opens its source record through `get_evidence`, with a link out to GitHub or Linear.
  A Graph8 record has no web address, so it shows its activity_id. When Graph8 or an upstream is down, the page
  names it. The browser talks only to the dashboard's own server, which holds every credential.

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
| `RESEAU_DAILY_REPORT` | The daily report workflow's `action_id`, printed by the same `setup`. |
| `RESEAU_ASK` | Ask Réseau's workflows, `route=<action_id>,get_person_activity=<action_id>,...`, printed by the same `setup`. |

## Run

```sh
set -a; . ./.env; set +a
uv run python -m reseau.front --port 8080        # binds 127.0.0.1
```

Réseau runs locally only; there is no deployed instance. Graph8 calls the gateway from its own servers, so
it needs a public tunnel while it runs workflows. In a second shell:

```sh
set -a; . ./.env; set +a
uv run python -m reseau.tunnel --port 8080       # Ctrl-C when you're done
```

It opens a localhost.run tunnel and keeps the `reseau-gateway` registration pointed at it. The free tunnel
moves to a new `*.lhr.life` host without warning, and a dropped connection ends it. The command reopens the
tunnel, moves the same registration to each new host (so every workflow keeps working), and retries Graph8's
`/test` until it passes. With no registration yet, it creates one.

## Register with Graph8

```sh
uv run python -m reseau.register_graph8 https://<public-gateway-host>          # create, /test, delete
uv run python -m reseau.register_graph8 https://<public-gateway-host> --keep   # leave it registered
```

The script counts the gateway's tools itself, registers `https://<host>/g8/<token>/sse` as an `sse` server,
and checks that Graph8's `/test` returns `success: true` with the same `tools_count`. Without `--keep`, it
then deletes the record and verifies that it's gone.

## Start My Day, the daily report and Ask Réseau

With the gateway and `reseau.tunnel` running:

```sh
uv run python -m reseau.workflows setup                    # the voice agent and every workflow not set up yet; prints the env lines
uv run python -m reseau.workflows update                   # after changing a prompt: Graph8 keeps its own copy
uv run python -m reseau.workflows start-my-day             # the verified briefing (~12 credits a run)
uv run python -m reseau.workflows daily-report 2026-09-26  # the verified team report for that day (~12 credits)
uv run python -m reseau.workflows ask "Why does HAR-104 matter?"  # a verified answer (~24 credits; ~12 to decline)
uv run python -m reseau.workflows verify <execution_id>    # check any run, e.g. one started in Graph8 (free)
```

A run started from Graph8's dashboard shows the agent's raw reply, which no check has seen. `verify` puts
that execution through the same checks as the commands above and exits non-zero if it fails.

The dashboard calls `reseau.workflows.start_my_day(g8, action_id)` and gets back
`{"execution_id", "date", "me", "sections": {"summary" | "focus" | "needs_attention" | "yesterday": [{"text", "activity_ids"}]}, "incomplete"}`.
`reseau.workflows.daily_report(g8, action_id, date)` returns
`{"execution_id", "team", "date", "sections": {"summary" | "completed" | "merged" | "commits" | "blocked": [{"text", "activity_ids"}]}, "unmapped", "incomplete"}`.
Either raises a `WorkflowError` listing what failed. `unmapped` (team members who aren't counted) and
`incomplete` (anything that may be missing) come straight from the tool. The verifiers are in
`reseau/verify.py`: `citations`, `counts`, `numbers` and `blockers`.

`reseau.workflows.ask(g8, reseau.workflows.ask_ids(RESEAU_ASK), question)` returns
`{"question", "tool", "arguments", "execution_ids": {"route", "answer"}, "sections": {"answer": [{"text", "activity_ids"}]}, "incomplete"}`.
A decline is `[{"text": "No evidence found.", "activity_ids": []}]`, with `tool` `null` when no tool fit. The
router is told today's date and the names in `RESEAU_IDENTITIES` and `RESEAU_PROJECTS`, read from the
environment of the process that calls `ask`. A route or answer that fails its check is retried once, then
refused with a `WorkflowError`.

## Dashboard

With the gateway, the tunnel and the workflows set up (above), in a third shell:

```sh
set -a; . ./.env; set +a
uv run python -m reseau.dashboard --port 8081    # open http://127.0.0.1:8081
```

It needs the same environment as `reseau.workflows` (`GRAPH8_API_KEY`, `RESEAU_START_MY_DAY`,
`RESEAU_DAILY_REPORT`, `RESEAU_ASK`) and the upstream tokens, since it resolves evidence through its own
`Gateway`. Each click is a billable run, like the commands above. To work on the page without Graph8 or
credits, serve it over the test fixtures instead: `uv run python -m tests.fixture_dashboard` (port 8082).

Stack: the repo had no frontend, so the page is plain HTML, CSS and an ES module in `reseau/static/`, with no
build step and no JS dependencies, served by Starlette (already installed with `mcp`). The server
(`reseau/dashboard.py`) is a thin JSON API over `reseau.workflows` and `get_evidence`; its docstring lists the
endpoints and error kinds. It is single-user, with no login (HAR-106 leaves auth out).

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
- **The dashboard is local only.** It binds 127.0.0.1, answers only a `127.0.0.1` or `localhost` Host header
  (so a DNS-rebinding page can't reach it) and starts runs only on a JSON POST (so another site's form can't).
  Keep it off the tunnel. Credentials stay on its server, and every response is redacted against them. It logs
  workflow errors, which quote customer and deal data, at debug level only, and the page logs nothing.
- **Tokens stay out of logs and output.** Every upstream token and gateway token is redacted from all log
  records, tool results and errors, and uvicorn's access log is off.

## Tests

```sh
uv run pytest
```

The tests run against local mock MCP servers on loopback and need no network.

The dashboard's browser tests (`tests/test_dashboard_ui.py`) drive the installed Google Chrome through
Playwright. Without Chrome, run `uv run playwright install chromium` once. Their E2E test runs against
`tests/fixture_dashboard.py`: the real server, workflow triggers and verifiers, with a scripted Graph8.

Live check of both workflows. Graph8 runs each real workflow against the gateway served over the test
fixtures, 3 times plus once on an empty day, and verifies every reply without retrying. It is billable
(~12 credits a run, 8 runs by default), and it deletes everything it creates:
`uv run python -m tests.live_workflows https://<public host> [--workflow daily-report]`, with a tunnel open
to port 8080.

Live check of Ask Réseau: 7 fixed questions about the data behind the running gateway (the Réseau team and
the Graph8 demo customers of `spikes/graph8_entities/seed_demo.py`), each asked once without retrying. Each
answer must pass its check and cite what the question needs (the blocker, a Graph8 record, the unlinked
issue), or decline. With `RESEAU_ASK` set, it pushes this code's prompts onto those workflows. Otherwise it
creates them and deletes them afterwards, unless `--keep`. It is billable (~150 credits):
`uv run python -m tests.live_ask [--keep]`, with `reseau.front` and `reseau.tunnel` running.

## Layout

| Path | Contents |
|---|---|
| `reseau/gateway.py` | Upstream side: credentials, sessions, retries, tool prefixing, allowlists, redaction |
| `reseau/front.py` | Graph8-facing side: legacy SSE server and token auth |
| `reseau/evidence/` | Normalized records with provenance, activity_ids, identity mapping, `get_evidence`; one normalizer module per source (GitHub, Linear, Graph8) |
| `reseau/semantic.py` | `get_person_activity`, `get_my_day_context`, `get_project_context`, `get_team_summary` and `get_business_context`: upstream fetching, then pure aggregation over records. The docstring records how "me", dates, issue↔PR links, unresolved threads, projects, blocked issues, team membership and work↔business links are resolved. |
| `reseau/outbound.py` | Shared HTTP policy: explicit User-Agent, backoff on 429/5xx, per-host concurrency cap |
| `reseau/workflows.py` | Graph8 workflows: Start My Day, the daily report and Ask Réseau; definitions, prompts and triggers |
| `reseau/dashboard.py`, `reseau/static/` | The dashboard: its JSON API and server, and the page (HTML, CSS, JS) |
| `reseau/verify.py` | The verifiers every workflow reply passes: citations, counts, blockers |
| `reseau/register_graph8.py` | Graph8 registration live check |
| `reseau/tunnel.py` | The localhost.run tunnel, kept open and followed by the Graph8 registration |
| `test_connection/`, `spikes/` | Findings from probing Graph8, GitHub and Linear that the design is based on |
| `upstream/` | Fixes proposed to Graph8, handed off as tickets (tests: `uv run pytest upstream/<name>`) |
