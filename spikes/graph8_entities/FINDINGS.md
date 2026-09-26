# HAR-107 — Graph8 business entities: mapping, linkage, and where reads happen (2026-09-26)

Method: serial, read-only MCP calls to `https://be.graph8.com/mcp/` with the **org-scoped** `g8_live_…` key, 2–3 s apart, explicit User-Agent. Field lists come from each tool's published `outputSchema` (120 of the 126 visible tools ship one), cross-checked against `@graph8/sdk@0.245.0` types and live responses where the org had data. No values were recorded, only keys and types.

> **Status: PROVISIONAL. Green is not demonstrated.** Only *customer* has been verified against a live record. The opportunity, conversation and commitment mappings come from published schemas plus empty live responses. Treat them as provisional until the probe returns a qualifying record for each one.

Re-run:
- `python3 entity_probe.py` prints SAMPLE / UNQUALIFIED / EMPTY / ERROR per entity and writes `shapes.json`.
- `--discover` also writes `discovery.json`: the full tool catalog, the entity output-field lists, the term-search hits and the key scopes. This is the retained evidence behind every "not found" below.
- `--selftest` runs the offline checks (scrubbing and the commitment rule).

Output files and stdout carry keys, types, tool names and error codes only. Errors print `tool -> HTTP status code=<JSON-RPC code>` and never the server's message text.

## Probe result today (Red)

| entity | tool | live result | qualifies when |
|---|---|---|---|
| customer | `g8_search_companies` | **SAMPLE** (total 250) | any row |
| opportunity | `g8_get_deals` | EMPTY, `total=0` | any row |
| conversation | `g8_list_inbox`, `g8_list_meetings(scope=all, timeframe=all)` | EMPTY, `total=0` both | any row from either tool |
| commitment | `g8_get_tasks` (scans 100) | EMPTY, `total=0` | task tied to a deal or company: `entity_type ∈ {deal, company}` with an `entity_id`, or `company_id` set, or a `links[]` entry of that type with a non-empty target ID. An unlinked task reports UNQUALIFIED and does not pass. |

What the empty pages do and don't show:
- **They don't mean the records are absent.** An empty page is consistent with an empty org, and also with a filter the key can't see past.
- **Missing scopes are ruled out as a cause.** `g8_connection_describe_current_key` (in `discovery.json` → `key`) shows the key carries `deals:read`, `tasks:read`, `inbox:read`, `meetings:read`, `notes:read`, `companies:read` and `transcripts:read`, and reaches 3139 of 3139 operations. So a missing key *scope* doesn't explain an empty page.
- **A role rule is not ruled out.** Row-level visibility such as meetings `scope=all` needing `meetings:see_all` is a user permission, not a key scope (it's absent from the scope vocabulary). An org key has no user behind it, so an empty meetings page may mean "not visible" rather than "none".

**Path to Green:** seed one deal, one task linked to that deal with `source_url` = a Linear issue URL, and one meeting or inbox thread. Then re-run. Seeding is a write, so it needs sign-off. It is outside this read-only spike.

## Entity → tool → ID / owner / time / URL

Customer fields are verified live. All other rows come from `outputSchema` (see `discovery.json` → `entity_output_fields`) and are **provisional**. `ResourceDetail` on `g8_get_deal` declares only `id` and passes other fields through, so the deal detail shape is unknown until a deal exists.

| Réseau entity | Graph8 object | list / get tools | ID | owner | timestamps | evidence URL |
|---|---|---|---|---|---|---|
| **Customer** | Company (`MashupCompany`) | `g8_search_companies` (filters: `name`, `domain`, `industry`, `lifecycle_stage`) · `g8_get_contact_company` · hidden `g8_crm_get_company` | `id` **int** | `owner_id` (only populated on the `lifecycle_stage` branch; null on a plain search) | **none** on the list shape | none |
| **Opportunity** | Deal | `g8_get_deals` (filters: `owner_*`, `outcome`, `stage_id`, `pipeline_id`, `search`, `stale_before`…) · `g8_get_deal(deal_id)` · `g8_get_company_deals(company_id)` · `g8_get_contact_deals` | `id` **UUID str** | `owner_id`, `owner_email`, `owner_name` | `created_at`, `updated_at`, `last_activity_at`, `close_date` | none |
| **Conversation** | Two objects: inbox thread (email/SMS/LinkedIn) and meeting (calendar + transcript). Deal/company notes are a third, weaker source. | threads: `g8_list_inbox` → `g8_get_reply(reply_id, channel)` · meetings: `g8_list_meetings` → `g8_get_meeting(meeting_id)` · notes: `g8_list_notes(company_id \| deal_id \| contact_id)` | thread `id` str (+ `channel`, which is needed to fetch it); meeting `id` str, `transcript_id`; note `id` str | thread `assignees`; meeting `organizer_email`, `user_email`; note `created_by` | `created_at`, `updated_at`; meeting `start_time`/`end_time` | `meeting_url` is the **conference link**, not a Graph8 record page |
| **Commitment** | **None found in inspected surfaces** (provisional). Nearest is **Task** | `g8_get_tasks` (filters: `status`, `priority`, `assignee_id`, `search` on title only) · `g8_get_task(task_id)` | `id` **UUID str** | `assignee_id`, `assignee_name`, `created_by`, `executor_type` (human/agent) | `created_at`, `updated_at`, `due_date`, `reminder_at` | `source_url`: the originating external URL, not a Graph8 page |

How each record ties back to the account:
- deal → `company_id` (int), `contact_id`, `contacts[]`, `pipeline_id`, `stage_id`/`stage_name`, `status`, `amount`/`currency`
- task → typed `entity_type`/`entity_id`/`entity_label` plus `company_id`, `contact_id`, `links[]` (untyped dicts), and meeting provenance `source_meeting_id`, `source_transcript_id`
- meeting → `matched_account` (dict), `participant_links`; detail adds `action_items`, `meeting_tasks`, `key_topics`, `custom_fields`
- thread → `contact` (dict)
- note → `entity_type`/`entity_id`, `read_only`

### Commitment: terminology (provisional)
**No commitment object was found in the surfaces inspected.** That is not proof Graph8 lacks one. Surfaces inspected:
- the full MCP catalog: all **586** tools, names and descriptions, from `g8_tool_search("g8", activate=false)`, stored in `discovery.json` → `catalog`;
- the output schemas of the 126 visible tools;
- `@graph8/sdk@0.245.0` types.

Terms `commit`, `promis`, `obligation` → the only hits are unrelated uses: "buying-committee", "commit SHA", "rest still commit" (`term_hits_in_catalog_names_and_descriptions`). `action item` → only `g8_get_meeting`.

Not inspected: REST routes that have no MCP tool, and the app UI. So Réseau should *treat* commitment as derived, and revisit if Graph8 confirms otherwise. Two candidate sources:
1. **Task** (primary). A task can be typed-linked to `deal`, `company`, `contact`, `lead`, and others via `records`. It carries `due_date`, an assignee, `status`, and `source_url`. It can come from a meeting (`source_meeting_id`). "We promised customer X feature Y by date Z" maps onto a task linked to the deal, with a due date.
2. **Meeting `action_items` / `meeting_tasks`**. These are extracted from transcripts and returned as untyped dicts. Their shape is unverifiable today (no meetings in the org), and they are not independently addressable, so treat them as a way to discover a commitment, not as its record.

Réseau should define **commitment = Graph8 task linked to a deal or company**. Answers should say "task", not "commitment", when citing the Graph8 source.

### Evidence URLs: none found
No output schema across the 126 visible tools contains a record URL. This is a schema check; a pass-through field on a populated record could still carry one. The only URL-like fields are `meeting_url` (video call), `participant_links`, and `task.source_url`/`links` (external). The SDK mentions `app.graph8.com` only for `/settings`. `get_evidence` therefore has to cite Graph8 records by `(type, id)`, e.g. `graph8:deal/<uuid>`. A deep link into `app.graph8.com` would be a guessed route: unverified, and it needs a logged-in human to confirm. Don't ship one until someone checks it in the UI.

## Work ↔ business linkage

Checked for an existing link first. All of this is in `discovery.json`:
- **No Linear/GitHub issue-or-PR integration was found in the inspected surfaces.**
  - Across all 586 catalog names and descriptions: `linear`, `jira`, `issue track`, `pull request` → 0 hits.
  - `github` → only `g8_connect_repo`, which connects a repo "for GTM automation" (tech-stack recording) and says nothing about issues or PRs.
  - `g8_list_crm_syncs` covers HubSpot, Salesforce, Pipedrive, Zoho and SugarCRM only.
  - Not inspected: REST-only routes, and the in-app integrations page. `integrations:read` exists as a scope, so confirm there before ruling it out.
- `GET /tasks/records/{entity_type}/{entity_id}` (listed under the key's `tasks:read` operations) looks like the deal → tasks reverse lookup. **No MCP tool exposes it.** Via MCP, finding the tasks for a deal means paging `g8_get_tasks`.
- Custom fields (`g8_list_fields`, `g8_create_fields`) exist for **contacts and companies only**. Deals have no custom-field slot, and `g8_update_deal` takes only `description` as free text.
- The linkage scan in the probe found 0 of 0 tasks with a Linear/GitHub `source_url`, so today nothing is linked.

| candidate | accuracy | failure modes | verdict |
|---|---|---|---|
| **Graph8 task with `source_url` = Linear issue / PR URL, linked via `records` to the deal/company** | Exact when present (string/ID match, no inference) | `source_url` is set on **create only** (`g8_update_task` has no `source_url`), so a wrong link means delete and recreate. No server-side filter on `source_url`: the reverse lookup pages `g8_get_tasks` (≤100/page) and matches client-side. Linear URLs vary (`/issue/HAR-107` vs `/issue/HAR-107/slug`; key changes if the issue moves teams), so normalize to the issue key and match on that. Missing link means no evidence. | **Recommended.** It is both the commitment record and the join, it lives where sales works, and it is typed to the deal/company. |
| Explicit reference on the Linear issue (description token like `graph8:deal/<uuid>`, attachment, or label) | Exact when present | Linear attachments need a URL, and no Graph8 record URL was found. Labels would need one per deal (label sprawl, UUIDs unreadable). A description token is free text that editors can mangle. Engineers, not the account owner, have to maintain it. | Secondary: good for the forward lookup (issue → deal), and cheap to parse. Use it as a fallback when no task exists. |
| Réseau-maintained mapping | Exact | A third store to keep in sync. Invisible to both sales and engineering. Drifts silently. | Only if Graph8 writes are disallowed. |
| Name matching (company name ↔ issue text) | **Weak.** Substring/fuzzy on names that collide ("Acme" vs "Acme Labs"), abbreviations, and customers never named in tickets. Expect frequent false positives *and* misses; not measurable here without labelled data. | Silent wrong answers, the worst failure for a "why does this matter" answer | **Never as evidence.** At most a "possible match, unconfirmed" suggestion that asks a human to create the task link. |

Rule for answers: when no explicit link exists, Réseau says "no business link recorded", not a guess.

## Where the reads happen: (a) gateway vs (b) Graph8 agent natively

| | (a) gateway calls Graph8 MCP and normalizes | (b) Graph8 agent uses `g8_*` directly |
|---|---|---|
| `get_evidence` shape | Uniform across Linear, GitHub, and Graph8 | Graph8 records come back in raw tool shapes. Linear/GitHub go through the gateway, so there are two shapes |
| The join (task `source_url` ↔ issue key) | Deterministic code: normalize, page, match | Left to the LLM, which has to page tasks and string-match URLs inside a turn. That is error-prone and burns tokens |
| Cloudflare 429 / UA 1010 | One place to serialize and set the UA | Graph8's own agent calling its own server; presumably fine, but out of our control |
| Org-context gate | Gateway must bootstrap `g8_current_org` per key (HAR-92). Observed: after one call the context stuck to the key, and later sessions called tools without re-bootstrapping. Still call it: it's cheap and the gate is documented | Native; no bootstrap |
| Credentials | Gateway holds a Graph8 key (org-scoped is enough, below) | None extra |
| Permissions | Key-scoped (whole org) | Per signed-in user |
| PII | Gateway can scrub/trim before evidence leaves | Full records in the agent context |

**Recommendation: (a).** The linkage is the product, and it has to be deterministic, which means code in the gateway, not model judgment in (b). Uniform `get_evidence` is also only possible there. The costs are the HAR-92 bootstrap (one call) and holding a Graph8 key. Leave (b) available for the agent's free-form follow-ups ("show me the deal notes"). It doesn't conflict: the agent can still call `g8_*` natively after the gateway has given it the link.

## Key type
**Provisional: the org-scoped key has the scopes, but reading real records is proven only for companies.**
- Every tool used here answered 200 with the org key.
- `g8_connection_describe_current_key` reports `key_mode: live`, all read scopes for these entities, and 3139/3139 operations reachable (`discovery.json` → `key`).
- So a personal key isn't needed at the *scope* level.

Known limits:
- `/api/v1/profile/*` REST routes still need a personal key (Run 2), but none of them are on this path.
- **Meetings.** `g8_list_meetings` defaults to `scope='my'`. An org key has no person behind it, so the probe uses `scope='all'`, which needs `meetings:see_all`. That is **not a key scope**: it's missing from the key's scope vocabulary, so it's a role permission. It returned 200 (empty) here. **Unverifiable until a meeting exists.** A silent empty result is the risk.
- **Transcripts.** `g8_get_meeting` redacts transcripts (`transcript_redacted=true`) without `meetings:see_transcripts`. The key does have `transcripts:read`. Same caveat.
- `g8_search_companies(lifecycle_stage=…)` needs `deals:read`. A 403 there means the scope is missing, not that the stage is empty.
- `g8_current_org` with this key: `count: 0, current: null`, "single-org API key… already bound to one organization and cannot switch".

## Open items
1. **Blocks closing HAR-107.** Seed one deal, one task (`source_url` = a Linear issue URL, linked to the deal), and one meeting or inbox thread. Then re-run the probe to confirm Green, fill `shapes.json`, and verify the `links[]` shape that `is_commitment` assumes. This is a write, so it needs sign-off.
2. Check the in-app integrations page for a Linear/GitHub issue integration before treating "none found" as final.
3. Confirm the `app.graph8.com` record route in the UI before any deep link ships.
4. Verify `meetings:see_all` / `see_transcripts` on the org key once a meeting exists.
