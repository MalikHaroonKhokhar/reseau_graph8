# HAR-107 — Graph8 business entities: mapping, linkage, and where reads happen (2026-09-26)

Method:
- Discovery: serial, read-only MCP calls to `https://be.graph8.com/mcp/` with the **org-scoped** `g8_live_…` key, 2–3 s apart, with an explicit User-Agent. Field lists came from each tool's `outputSchema` (120 of the 126 visible tools ship one) and from the `@graph8/sdk@0.245.0` contract (`g8.api.operations()`: 3247 operations, each with a side-effect tier and a scope).
- Validation: seeded synthetic records (`seed_fixture.py`), ran the probe against them, then deleted them.

Only keys and types are committed. No values are committed.

> **Status: Green demonstrated on 2026-09-26** for all four entities, against synthetic seeded records that were then deleted.
> - Conversation passed through **account correspondence**.
> - **Inbox threads and meetings are still unverified with real rows.** The only ways to create them send real email, SMS or LinkedIn messages, calendar invites or bookings (SDK tiers `external`/`run`), so they were not seeded. Conclusions about those two surfaces stay provisional.

Re-run:
- `python3 entity_probe.py` prints SAMPLE / UNQUALIFIED / EMPTY / ERROR per entity and writes `shapes.json`.
  - It is read-only, and exits 0 only when every entity has a qualifying sample.
  - Against the org as it is today (no deals, tasks or conversations), it is Red.
- `--discover` also writes `discovery.json`: the full tool catalog, the entity output-field lists, the term-search hits and the key scopes. This is the retained evidence behind every "not found" below.
- `--selftest` runs the offline checks (scrubbing and the commitment rule).
- `python3 seed_fixture.py seed` then `cleanup` **writes to the live org**. It makes internal writes only; nothing is sent. It creates a contact on `reseau-probe.example`, which also creates the company, then a deal, a deal-linked task with `source_url` = the HAR-107 Linear URL, and a correspondence record. Created IDs go to `seed_state.json` (gitignored). Cleanup removes an ID from that file only after it is **verified gone**: a REST `GET` on the record returns 404 (confirmed for tasks, deals, contacts and companies), or the ID is absent from a 200 correspondence listing. The company is deleted last, and only once no child records remain. Any failure keeps the IDs and exits 1, so cleanup can be re-run. `seed_fixture.py selftest` checks this bookkeeping offline.

Output files and stdout carry keys, types, tool names and error codes only. Errors print `tool -> HTTP status code=<JSON-RPC code>` and never the server's message text.

## Probe results

**Green run (seeded, 2026-09-26):**
```
customer     g8_search_companies    SAMPLE (total=251, scanned=25, qualifying=25)
opportunity  g8_get_deals           SAMPLE (total=1, scanned=1, qualifying=1)
conversation g8_list_inbox          EMPTY (total=0, scanned=0, qualifying=0)
conversation g8_list_meetings       EMPTY (total=0, scanned=0, qualifying=0)
conversation REST correspondence    SAMPLE (accounts checked=1, rows=1)
commitment   g8_get_tasks           SAMPLE (total=1, scanned=1, qualifying=1)
linkage      g8_get_tasks           1 of 1 scanned tasks are commitments with a Linear/GitHub source_url
exit=0
```

**Cleanup:**
- All five deletes succeeded: correspondence 204; task, deal, contact and company 200.
- Verified afterwards:
  - companies total back to 250; deals 0; tasks 0;
  - `GET /contacts/{id}` → 404;
  - correspondence on the account `total: 0`.
- `shapes.json` is the Green run's shapes. It holds types only, and a grep for the seeded values returns 0 hits.

| entity | source | qualifies when |
|---|---|---|
| customer | `g8_search_companies` | any row |
| opportunity | `g8_get_deals` | any row |
| conversation | `g8_list_inbox`, `g8_list_meetings(scope=all, timeframe=all)`, or REST `GET /accounts/{company_id}/correspondence` for the companies of the sampled deals | any row from any source |
| commitment | `g8_get_tasks` (scans 100) | task tied to a deal or company: `entity_type ∈ {deal, company}` with an `entity_id`, or `company_id` set, or a `links[]` entry of that type with a non-empty `entity_id`. An unlinked task reports UNQUALIFIED and does not pass. |

Key-scope evidence: `g8_connection_describe_current_key` (`discovery.json` → `key`) shows the key has `deals:read`, `tasks:read`, `inbox:read`, `meetings:read`, `notes:read`, `companies:read` and `transcripts:read`, and reaches 3139/3139 operations. An empty inbox or meetings page therefore isn't explained by a missing key *scope*. It could still come from a role rule: `meetings:see_all` is a user permission, not a key scope, and an org key has no user behind it.

**Pitfall found:**
- Some tools report failure as a normal result string with `isError` unset. For example, `g8_get_contact_detail` on a deleted contact returns `{"result": "Error: Contact detail: Contact not found"}`.
- A client that trusts `isError` will treat that as data. The probe's `call()` rejects `"Error…"` results.
- The gateway needs the same guard.

## Entity → tool → ID / owner / time / URL

Verified against live records: customer, opportunity (list and `g8_get_deal` detail), commitment (list and `g8_get_task`), and account correspondence. Inbox thread and meeting rows come from `outputSchema` only (`discovery.json` → `entity_output_fields`) and are **provisional**.

Verified detail worth knowing:
- `g8_get_deal` returns the list fields plus `description` and `revision` (for `expected_revision` on updates).
- `g8_get_company_deals` returns a **different, thinner shape**: `deal_id` rather than `id`, and no amount or company. Normalize on `deal_id`/`id`.

| Réseau entity | Graph8 object | list / get tools | ID | owner | timestamps | evidence URL |
|---|---|---|---|---|---|---|
| **Customer** | Company (`MashupCompany`) | `g8_search_companies` (filters: `name`, `domain`, `industry`, `lifecycle_stage`) · `g8_get_contact_company` · hidden `g8_crm_get_company` | `id` **int** | `owner_id` (only populated on the `lifecycle_stage` branch; null on a plain search) | **none** on the list shape | none |
| **Opportunity** | Deal | `g8_get_deals` (filters: `owner_*`, `outcome`, `stage_id`, `pipeline_id`, `search`, `stale_before`…) · `g8_get_deal(deal_id)` · `g8_get_company_deals(company_id)` · `g8_get_contact_deals` | `id` **UUID str** | `owner_id`, `owner_email`, `owner_name` | `created_at`, `updated_at`, `last_activity_at`, `close_date` | none |
| **Conversation** | Three objects: **account correspondence** (email/LinkedIn/call notes logged on a company; *verified*), inbox thread (email/SMS/LinkedIn; provisional) and meeting (calendar + transcript; provisional). Deal/company notes are a fourth, weaker source. | correspondence: **REST/SDK only, no MCP tool**: `GET /accounts/{company_id}/correspondence` (`companies:read`; `account_id` is the company ID) · threads: `g8_list_inbox` → `g8_get_reply(reply_id, channel)` · meetings: `g8_list_meetings` → `g8_get_meeting(meeting_id)` · notes: `g8_list_notes(company_id \| deal_id \| contact_id)` | correspondence `id` UUID + `account_id`, caller-set `external_id`, `stakeholder_id`; thread `id` str (+ `channel`, needed to fetch it); meeting `id` str, `transcript_id`; note `id` str | correspondence: none (`stakeholder_id` is the account-side person); thread `assignees`; meeting `organizer_email`, `user_email`; note `created_by` | correspondence `correspondence_date` (when it happened), `created_at`; others `created_at`, `updated_at`; meeting `start_time`/`end_time` | none; `meeting_url` is the **conference link** |
| **Commitment** | **None found in inspected surfaces.** Nearest is **Task** (verified) | `g8_get_tasks` (filters: `status`, `priority`, `assignee_id`, `search` on title only) · `g8_get_task(task_id)` | `id` **UUID str** | `assignee_id`, `assignee_name`, `created_by`, `executor_type` (human/agent) | `created_at`, `updated_at`, `due_date`, `reminder_at` | `source_url`: the originating external URL, not a Graph8 page |

How each record ties back to the account:
- deal → `company_id` (int), `contact_id`, `contacts[]`, `pipeline_id`, `stage_id`/`stage_name`, `status`, `amount`/`currency`
- task → typed `entity_type`/`entity_id`/`entity_label`, and `links[]`, verified as `{entity_type, entity_id, entity_label, canonical_entity_id, available}`. Also `company_id`, `contact_id`, and meeting provenance `source_meeting_id`, `source_transcript_id`.
  - **A task linked to a deal does not inherit the deal's company.** `company_id` came back null. To get deal → company, Réseau has to read the deal.
  - `source_url` round-trips verbatim.
- correspondence → `account_id` (= company ID), `channel`, `direction`, `stakeholder_id`, `source`, `indexed_in_rag`
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
No output schema across the 126 visible tools contains a record URL. The populated deal detail, task and correspondence records had no URL field either (verified). The only URL-like fields are `meeting_url` (video call), `participant_links`, and `task.source_url`/`links` (external). The SDK mentions `app.graph8.com` only for `/settings`. `get_evidence` therefore has to cite Graph8 records by `(type, id)`, e.g. `graph8:deal/<uuid>`. A deep link into `app.graph8.com` would be a guessed route: unverified, and it needs a logged-in human to confirm. Don't ship one until someone checks it in the UI.

## Work ↔ business linkage

Checked for an existing link first. All of this is in `discovery.json`:
- **No Linear/GitHub issue-or-PR integration was found in the inspected surfaces.**
  - Across all 586 catalog names and descriptions: `linear`, `jira`, `issue track`, `pull request` → 0 hits.
  - `github` → only `g8_connect_repo`, which connects a repo "for GTM automation" (tech-stack recording) and says nothing about issues or PRs.
  - `g8_list_crm_syncs` covers HubSpot, Salesforce, Pipedrive, Zoho and SugarCRM only.
  - Not inspected: REST-only routes, and the in-app integrations page. `integrations:read` exists as a scope, so confirm there before ruling it out.
- `GET /tasks/records/{entity_type}/{entity_id}` looked like a deal → tasks reverse lookup. **It isn't.** Called on the seeded deal, it returns the record descriptor (`entity_type, entity_id, entity_label, canonical_entity_id, available`), not tasks. Finding the tasks for a deal means paging `g8_get_tasks` and filtering on `links[]`.
- Custom fields (`g8_list_fields`, `g8_create_fields`) exist for **contacts and companies only**. Deals have no custom-field slot, and `g8_update_deal` takes only `description` as free text.
- The linkage scan is verified end to end on the seeded task: created with `source_url` = the HAR-107 Linear URL and `records=[{deal}]`, then read back with both intact, and matched (1 of 1). The real org has no such tasks today, so nothing is linked yet.
- Correspondence has a caller-set `external_id`, but Graph8 uses it to dedupe imports (message IDs). Don't overload it with issue keys.

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
| Account correspondence | Reachable over REST (`GET /accounts/{company_id}/correspondence`) | **Not reachable**: no `g8_*` tool reads it |
| Tool errors as results | One place to guard `"Error…"` results that have `isError` unset | Each agent turn has to notice them |

**Recommendation: (a).** The linkage is the product, and it has to be deterministic, which means code in the gateway, not model judgment in (b). Uniform `get_evidence` is also only possible there. The costs are the HAR-92 bootstrap (one call) and holding a Graph8 key. Leave (b) available for the agent's free-form follow-ups ("show me the deal notes"). It doesn't conflict: the agent can still call `g8_*` natively after the gateway has given it the link.

## Key type
**The org-scoped key is enough, verified for companies, deals, tasks and account correspondence.** The key read, created and deleted each of them. Inbox and meetings remain provisional (see below).
- Every tool used here answered 200 with the org key.
- `g8_connection_describe_current_key` reports `key_mode: live`, all read scopes for these entities, and 3139/3139 operations reachable (`discovery.json` → `key`).
- So a personal key isn't needed.

Known limits:
- `/api/v1/profile/*` REST routes still need a personal key (Run 2), but none of them are on this path.
- **Meetings.** `g8_list_meetings` defaults to `scope='my'`. An org key has no person behind it, so the probe uses `scope='all'`, which needs `meetings:see_all`. That is **not a key scope**: it's missing from the key's scope vocabulary, so it's a role permission. It returned 200 (empty) here. **Unverifiable until a meeting exists.** A silent empty result is the risk.
- **Transcripts.** `g8_get_meeting` redacts transcripts (`transcript_redacted=true`) without `meetings:see_transcripts`. The key does have `transcripts:read`. Same caveat.
- `g8_search_companies(lifecycle_stage=…)` needs `deals:read`. A 403 there means the scope is missing, not that the stage is empty.
- `g8_current_org` with this key: `count: 0, current: null`, "single-org API key… already bound to one organization and cannot switch".

## Open items
1. Verify inbox threads and meetings with real rows once the org has any; seeding them would send real messages or invites. Until then, conversation evidence rests on account correspondence.
2. Check the in-app integrations page for a Linear/GitHub issue integration before treating "none found" as final.
3. Confirm the `app.graph8.com` record route in the UI before any deep link ships.
4. Verify `meetings:see_all` / `see_transcripts` on the org key once a meeting exists (same event as item 1).
