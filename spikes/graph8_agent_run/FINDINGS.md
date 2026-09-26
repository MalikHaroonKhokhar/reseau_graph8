# HAR-91: defining and running a Graph8 agent/workflow over registered MCP servers (2026-09-26)

Probe: `agent_run_probe.py` (stdlib, serial, 3 s spacing, explicit User-Agent; reuses `g8`/`redact` from `../mcp_bridge/bridge_probe.py`). `python3 agent_run_probe.py selftest` checks the output validation offline.
Test servers are both public and need no auth: **CoinGecko** `https://mcp.api.coingecko.com/sse` (legacy SSE, 2 tools) and **Microsoft Learn** reached through the stdio bridge (3 tools).

| run | result |
|---|---|
| `python3 agent_run_probe.py red` | nothing is defined; `POST /workflows/<uuid>/execute` → **404** `Workflow not found.` → RED |
| `python3 agent_run_probe.py` (green) | one workflow, 2 MCP tool nodes on 2 servers, run over REST → both `completed` with real results, ~0.5 s run → **GREEN** |
| `python3 agent_run_probe.py agent` (billable, ~12 credits) | green + an `agent` node that summarises the MCP output → it returned `Timer trigger for Azure Functions (programming-language-python)`, ~4.9 s run → **GREEN** |

GREEN requires meaningful content from **both** servers, not just `completed`: a Microsoft Learn hit titled "Timer trigger…" and a CoinGecko hit mentioning `price`. An empty `[]` fails, because that is exactly what mis-mapped arguments produce. In `agent` mode the reply must also name the timer-trigger doc.

Cleanup touches only what the run created. Names carry a per-run prefix (`reseau-probe-har91-<8 hex>-`), and every created id is recorded. Each id is deleted directly and then proven gone: workflows and voice agents must return **404** on `GET` by id. MCP servers have no GET-by-id route, so the id must be absent from a complete `GET /workflows/mcp-servers` listing (`len(servers) == total`). An id that is not proven gone is reported under `leftovers`, and the run exits non-zero. `red` creates nothing and deletes nothing. Other probes' `reseau-probe-*` resources are never touched.

## 1. Definition API: a workflow is the unit, and MCP enters through a `tool` node
`POST /api/v1/workflows` → 201 `{action_id, ...}` (`action_id` is the handle everywhere else). The body is `{name, description, config}`:

```json
{"start_node_id": "trigger_1",
 "nodes": [
  {"node_id": "trigger_1", "name": "trigger_1", "node_type": "trigger", "connections": ["ms_1"],
   "config": {"trigger_type": "tool_call", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}},
  {"node_id": "ms_1", "name": "ms_1", "node_type": "tool", "connections": ["agent_1"],
   "config": {"tool": "mcp", "mcp_server_id": "<uuid>", "mcp_tool_name": "microsoft_docs_search",
              "input_mappings": [{"source_expression": "${trigger.q}", "target_field": "query"}]}},
  {"node_id": "agent_1", "name": "agent_1", "node_type": "agent",
   "config": {"agent_id": "<voice agent uuid>", "instructions": "...",
              "input_mappings": [{"source_expression": "${ms_1.content}", "target_field": "message"}]}}],
 "edges": [{"id": "e0", "source": "trigger_1", "target": "ms_1", "edge_type": "default"},
           {"id": "e1", "source": "ms_1", "target": "agent_1", "edge_type": "default"}]}
```

Traps. None of these are in the SDK types; they were found with the validator and live runs:
- **The SDK's `WorkflowConfig` is wrong.** It has a top-level `connections`, but the server requires `edges` (each with an `id`), requires a `name` on every node, and the **executor walks each node's own `connections: [next_id]`**. An edge that is not mirrored there never runs (the validator warns about this).
- **MCP tool arguments come only from `input_mappings`.** `tool_config` in any shape (`{...}`, `{arguments}`, `{input}`, `{params}`) is **silently dropped**. The tool runs with no arguments and reports `completed` with empty results. Literal values work too (`"source_expression": "typescript"`).
- **An agent node's `instructions` are not interpolated.** `${ms_1.content}` reached the model verbatim. Upstream data has to arrive as the user turn, through `input_mappings` → `target_field: "message"` (`input` and `context` are ignored).
- Tool node output: `{mcp_server, tool, content: <text>, is_error}`. When a tool fails, the node reports the same swallowed `unhandled errors in a TaskGroup (1 sub-exception)` as `/test` (seen with CoinGecko's `execute` tool).
- Useful free helpers: `GET /workflows/node-types/schema` returns 67 node types with config and output schemas plus authoring rules. `POST /workflows/validate` names every structural error. `POST /workflows/plan` returns the per-node cost and `external_effect` (MCP `tool` nodes: free, no external effect; `agent` nodes: ~12 credits, `llm:g8_t1`).
- The `agent` node needs a **voice agent** (`POST /voice/agents`; `persona.*_level` must be 0–1). Its reply carries that agent's persona (every reply opened with "Hi, thanks for connecting!"), so Réseau wants a dedicated agent with a neutral persona.

## 2. Run trigger API
`POST /api/v1/workflows/{action_id}/execute` with `{"input_data": {...}}` → 200 `{"success": true, "execution_id": "<uuid>", "status": "pending"}`. It is asynchronous. The run records `trigger_type: "manual"` and `triggered_by: "api-key@graph8.com"`. The trigger node's own type (`tool_call`) only declares the input schema. `tool_call` is also what lets an agent call the workflow as a skill (see §5).

## 3. Output retrieval: polling
`GET /api/v1/workflows/executions/{execution_id}` → `status` (`pending|running|completed|failed|stopped|paused`), `error_message`, `duration_ms`, `tokens_*`, and `output_data.node_results.<node_id> = {status, error, output, duration_ms}`, plus `failed_at_node` on failure. The first poll after about 3 s was already terminal in every run (tools only ~0.5 s; with the agent ~4.9 s). No streaming or webhook route for executions was found. `GET /workflows/{action_id}/runs/recent` and `GET /workflows/executions` list past runs.

## 4. Key type
**An org-scoped key is enough for all of it**: register servers, create and validate workflows, execute, poll, and create and delete voice agents. No `/profile/*` route is involved, so the 403 on org keys doesn't matter here. The SDK labels create, update and execute as tier `external` (`workflows:run`) because a workflow *can* send email. What a given workflow actually does shows in `/plan`.

## 5. Multiple MCP servers per agent: **yes in a workflow; for an LLM-selected toolset it is unresolved**
- **Workflow: yes, proven.** One run called the `…ms` (stdio) and `…cg` (sse) servers, and both returned real data. Each `tool` node is pinned to one `mcp_server_id` + `mcp_tool_name`, so the calls are deterministic, not chosen by the model.
- **Workflow `agent` node picking MCP tools itself: not demonstrated.** Its `tools` field ("Tool IDs to enable") had no effect with raw server uuids, `mcp:<uuid>`, the MCP tool name, `{type: mcp, mcp_server_id}`, a workflow `action_id`, or `knowledge_search`. Every variant listed the same fixed voice toolset (`search_knowledge`, `request_human_transfer`, `collect_visitor_info`), with `tools_used: null`. That shows only that none of these configurations worked on this node type. It does not show that Graph8 agents can't do it.
- **Agent + workflows as skills: attach works, use not shown.** `POST /voice/agents/{id}/skills {"action_ids": [...]}` attaches a `tool_call`-triggered workflow (201; it appears in `GET .../skills`). The agent *node* still didn't see it. The surface that probably uses skills, `POST /voice/chat-agent/messages`, needs a CRM `contact_id`, meaning a website-visitor chat. Not run.
- `POST /agent/runs` / `/agent/chat` drive Graph8's built-in copilots (`agent_id` ∈ onboarding, campaign_run, …). They have no tool or MCP configuration and were not run.

**Still open (acceptance question):** whether any Graph8 agent surface lets the model choose among tools from more than one registered MCP server. The untested candidates are skills on the chat agent (needs a CRM `contact_id`) and any agent `tools` format not tried here.

**For Réseau:** build Start My Day and the daily report as workflows: MCP `tool` nodes collect data from the gateway (any number, any servers), then one `agent` node writes the summary from `input_mappings → message`. The dashboard calls `execute` and polls. Free-form Ask Réseau, where the model picks the gateway's tools itself, has no proven path yet. The candidates are a single gateway server fronting GitHub, Linear and Graph8 (so "multiple servers" never comes up), plus skills on the chat agent, which is untested.

## Other observations
- `GET /voice/mcp-servers/{uuid}/tools` now returns 200 with the live tool list (it returned 502 in the earlier spike). `GET .../tools/cached` returns 404 for a uuid that exists.
- Voice agents are listed by `agent_name` at the top level of `GET /voice/agents`, and deleted by `agent_id` uuid. Workflows are deleted by `action_id`.
