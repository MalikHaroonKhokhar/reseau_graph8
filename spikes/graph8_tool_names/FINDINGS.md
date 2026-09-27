# HAR-94: Graph8 accepts the gateway's provider-prefixed tool names (2026-09-27)

Probe: `names_probe.py` (stdlib, serial, reuses `agent_run_probe`'s request, workflow and cleanup helpers). `python3 names_probe.py selftest` checks the verdict logic and compiles the inline server offline.

It registers one throwaway stdio MCP server (inline stdlib script) whose tools carry the gateway's exposed names plus length and character probes. It reads back `GET /voice/mcp-servers/{id}/tools`, then runs one workflow with an MCP `tool` node per name. Tool nodes only: free, no external effect. Every tool replies `called:<its name>`, so a completed node whose output matches proves Graph8 called the tool by that exact name.

## Result: all 11 names listed unchanged and called by exact name

| name | length | listed unchanged | called |
|---|---|---|---|
| `github_list_issues`, `linear_list_issues` | 18 | yes | yes |
| `github_list_releases`, `linear_list_releases` | 20 | yes | yes |
| `github_add_reply_to_pull_request_comment` (longest real exposed name) | 40 | yes | yes |
| `n64_…`, `n65_…`, `n128_…`, `n129_…` | 64, 65, 128, 129 | yes | yes |
| `dot.name`, `dash-name` | 8, 9 | yes | yes |

`/test`: `success: true, tools_count: 11`. `/workflows/validate`: valid, no warnings. Execution `completed` in 662 ms.

- **Graph8 does not add its own prefix or rename registered tools.** The gateway's exposed names are what Graph8 uses, so `github_*` / `linear_*` (Graph8 keeps `g8_*`) needs no further mapping.
- **No length or character limit up to 129 characters**, `.` and `-` included. The gateway's longest name today is 40 characters.
- Scope: this covers the proven consumption path, workflow `tool` nodes pinned to `mcp_tool_name`. No path where a Graph8 LLM chooses MCP tools itself has been shown yet (HAR-91). If one ships, the LLM API's rule applies, typically `^[a-zA-Z0-9_-]{1,64}$`, and every gateway name meets it.

Cleanup: the workflow was deleted and returned 404; the server was deleted and the final `GET /workflows/mcp-servers` returned `{"servers":[],"total":0}`.
