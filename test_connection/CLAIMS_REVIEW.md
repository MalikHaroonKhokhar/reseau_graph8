# Independent review of the pitch claims — 2026-09-27

This review reran live probes against Graph8 and the providers. It distinguishes observed failures from claims about unobserved internal implementation. Sanitized results are in `claims_review_evidence.json`; no provider credentials or full business records are included.

## Verified live

- Graph8 rejected `streamable_http`, `streamable-http`, `streamablehttp`, `http`, and `streamable` with HTTP 422 and `Input should be 'sse' or 'stdio'`.
- The live schema is available at **https://be.graph8.com/api/v1/openapi.json**. `McpServerCreateRequest` has the eight stated properties and the SSE/stdio enum. It has no explicit remote headers, bearer-token, or OAuth configuration property. The previously attempted `/openapi.json` was the wrong path.
- CoinGecko exposes the same two tools (`execute`, `search_docs`) over `/sse` and `/mcp`, verified directly with the matching SDK clients and no credentials. Graph8 registration as `sse` succeeded against `/sse` with 2 tools and failed against `/mcp`. This establishes the tested client's legacy-SSE behavior, without relying on provider authentication.
- Additional `headers`, `auth`, `oauth_client_id`, and `bearer_token` canaries were accepted on create but absent from both create and list responses. The published schema omits them. This proves they are not exposed as supported registration properties; response omission alone cannot prove internal storage or wire behavior. The canary registration's test returned `success: true` but `tools_count: null`, so that particular test does not prove successful tool discovery.
- GitHub and Linear's primary MCP endpoints returned 401 with Bearer challenges without credentials. Authenticated Streamable HTTP discovery succeeded. Linear `/sse` returned 404 in this run, despite its documentation advertising a deprecated fallback.
- Full discovery returned GitHub 45 + Linear 59 + Graph8 126 = **230 tools**. The current gateway exposed **14**, including distinct `github_list_issues` and `linear_list_issues`. The raw GitHub/Linear intersections were `list_issues` and `list_releases`; `list_releases` is not in the gateway's allowlist.
- Graph8 execution history confirms workflow `2c21f971-9ceb-4433-bc15-63fa3fa9cc4e`, execution `6f0fef68-7e13-4c40-996c-32b770ac0cd7`, completed. Its GitHub tool output contains PR #14 and its Linear tool output contains HAR-90. Both identify `reseau-gateway` and report `is_error: false`. The workflow definition now returns 404, so its original registration UUID mapping cannot be independently reread from the definition.
- The later five-node workflow's execution `68f46b6c-e6e4-4638-88f7-277afa34ae4d` also completed, including both provider tool nodes, the merge, and the agent summary.
- **73 tests passed** in this review.

## Claims that must change

1. **“The stdio escape hatch is shut” is false.** A fresh registration running the repository's stdlib Python bridge on Graph8 successfully discovered 3 Microsoft Learn tools through Streamable HTTP. This is an alternative bridge architecture, not native remote registration. Do not confuse missing preinstalled bridge executables with inability to run a bridge.
2. **“Only python3 and sh” is false.** A temporary MCP runtime-report process directly checked executable availability: `python3`, `sh`, and `bash` exist; `npx`, `uvx`, `node`, and `mcp-proxy` were not found on PATH. `mcp_proxy` was not importable. This is not an exhaustive inventory of the host.
3. **“No outbound OAuth; too fast for any token exchange” is not proved.** Atlassian, Notion, and Asana registrations failed in 1.27s, 0.66s, and 0.75s respectively. That proves unsuccessful authentication/connectivity for these registrations. Timing and generic TaskGroup failures cannot reveal whether discovery, a token request, or another OAuth step was attempted. “All under 1.1s” is also false.
4. **“Graph8 never holds credentials for arbitrary MCP servers” is false.** `env_vars` is an explicit credential-bearing channel for arbitrary stdio processes. Neither a few provider-session 500 responses nor absence of an MCP `connection_id` proves the complete internal OAuth capability. The session-token docs explicitly warn that provider configuration keys can differ by environment.
5. **“No credential field at all” needs the qualifier “no explicit remote HTTP credential field.”** The schema includes `env_vars`; URL credentials are also possible, as Réseau demonstrates. Public read responses remain a credential-exposure concern.
6. **“230 reduced to 14 read-only tools” is a valid end-to-end comparison, but not solely an allowlist reduction.** The configured read-only endpoints reduce the baseline first; then the allowlist selects 14. Graph8 itself has no verified read-only endpoint here; its one exposed tool is restricted by the gateway allowlist.

## Current versus historical gateway verification

The saved HAR-96 registration transcript records `/test` success with 14 tools and cleanup. The prior claims results also record a successful 14-tool test. During this independent review, the existing registration list was empty, so no currently registered public gateway could be retested. Fresh local gateway discovery returned 14 and historical workflow output was retrieved directly from Graph8. Do not describe this as a newly repeated public-gateway `/test`.

All temporary registrations created by this review were deleted and their absence verified. No existing workflow or registration was modified, and no workflow execution was triggered.

## Defensible pitch

Graph8's tested outbound MCP registration supports legacy SSE and stdio, with no explicit remote HTTP authentication configuration. Its SSE client fails against the tested Streamable HTTP endpoints. Réseau provides a verified bridge: authenticated legacy SSE toward Graph8, authenticated Streamable HTTP toward GitHub and Linear, and a reduced tool surface with per-source prefixes. A Python stdio bridge is also technically possible; Réseau's public gateway keeps provider credentials outside Graph8's registration records. Live execution history confirms a workflow retrieved PR #14 and HAR-90 through Réseau.

## Official references

- [Connect an MCP server](https://docs.graph8.com/developers/api-reference/operations/create_mcp_server_voice_mcp_servers_post/)
- [Test a saved MCP server](https://docs.graph8.com/developers/api-reference/operations/test_mcp_server_voice_mcp_servers__mcp_server_id__test_post/)
- [Hosted connection session tokens](https://docs.graph8.com/developers/api-reference/operations/create_connection_session_token_integrations_connections_session_token_post/)
- [Linear MCP](https://linear.app/docs/mcp)
- [GitHub MCP server manifest](https://raw.githubusercontent.com/github/github-mcp-server/main/server.json)
