"""Normalized work model with provenance (HAR-98): activity_ids, identity mapping, get_evidence.

activity_id = "<source>:<kind>:<key>", e.g. github:pr:o/r#9, github:commit:o/r@<sha>,
github:review_comment:o/r#9/<comment id>, github:review:o/r#9/<review id>, linear:issue:ENG-142,
graph8:customer:<id>, graph8:opportunity:<uuid>, graph8:commitment:<uuid>, graph8:conversation:<channel>/<id>
(graph8.py). A commit carries its repo because get_commit can't resolve a bare SHA. A new source is one more
module in SOURCES.

Identities come only from an explicit map (RESEAU_IDENTITIES = path to JSON:
{"<person>": {"github": "<login>", "linear": "<Linear user id>", "graph8": "<Graph8 user id>"}}). No match ->
identity "unmapped"; nothing is inferred from names or emails (Linear's MCP exposes no email to match on).
"""
import dataclasses
import json
import os
import re
from datetime import datetime, timezone

import mcp.types as types
from mcp.shared.exceptions import MCPError

from reseau.evidence import github, graph8, linear

SOURCES = {"github": github.KINDS, "linear": linear.KINDS, "graph8": graph8.KINDS}
IDENTITIES_ENV = "RESEAU_IDENTITIES"
MAX_PAGES = 10  # review comments are found by paging; past 10 x 100 threads the search reports search_incomplete

# Continues gateway.py's JSON-RPC code list.
INVALID_ACTIVITY_ID = -32602  # JSON-RPC "invalid params"
EVIDENCE_NOT_FOUND = -32008
EVIDENCE_UPSTREAM_ERROR = -32009
EVIDENCE_SEARCH_INCOMPLETE = -32010
# The phrases the live upstreams use for a missing object (captured in HAR-98's and HAR-108's probes). Graph8:
# "Deal not found", "Task <id>: Error: Task lookup: Task not found", "Email <id> not found", "Invalid meeting ID".
UPSTREAM_NOT_FOUND = re.compile(r"404 Not Found|No commit found|Could not resolve to a|Could not find referenced"
                                r"|(?:Company|Deal|Task) not found|Inbox thread: .* not found|Invalid meeting ID")

TOOL = types.Tool(
    name="get_evidence",
    description="Resolve an activity_id (e.g. github:pr:owner/repo#9, github:commit:owner/repo@<sha>, "
                "github:review_comment:owner/repo#9/<comment id>, github:review:owner/repo#9/<review id>, "
                "linear:issue:ENG-142, graph8:customer:<id>, graph8:opportunity:<id>, graph8:commitment:<id>, "
                "graph8:conversation:<channel>/<id>) to its source record: canonical URL (null for Graph8, which "
                "has none), actor or owner (with Réseau person, or identity 'unmapped'), timestamps and fetched_at.",
    input_schema={"type": "object", "properties": {"activity_id": {"type": "string"}}, "required": ["activity_id"]},
)


def _error(code, kind, message, activity_id):
    return MCPError(code, message, {"kind": kind, "activity_id": activity_id})


def parse(activity_id):
    """activity_id -> (source, kind, fields). Raises MCPError(invalid_activity_id)."""
    source, _, rest = (activity_id if isinstance(activity_id, str) else "").partition(":")
    kind_name, _, key = rest.partition(":")
    kind = SOURCES.get(source, {}).get(kind_name)
    m = kind and kind.regex.fullmatch(key)
    if not m:
        raise _error(INVALID_ACTIVITY_ID, "invalid_activity_id", "not a valid activity_id: %r" % activity_id, activity_id)
    return source, kind_name, m.groupdict()


def format_id(source, kind, fields):
    return "%s:%s:%s" % (source, kind, SOURCES[source][kind].template.format(**fields))


def load_identities(env=os.environ):
    path = env.get(IDENTITIES_ENV)
    return identity_index(json.load(open(path)) if path else {})


def identity_index(people):
    """{"person": {"github": login, ...}} -> {(source, id casefolded): person}. One identity, one person."""
    index = {}
    for person, ids in people.items():
        for source, uid in ids.items():
            key = (source, str(uid).casefold())
            if index.get(key, person) != person:
                raise ValueError("identity %s:%s is mapped to both %r and %r" % (source, uid, index[key], person))
            index[key] = person
    return index


def resolve_actor(a, index):
    person = a.id and index.get((a.source, a.id.casefold()))
    return dataclasses.replace(a, person=person, identity="mapped") if person else a


def resolve(record, index):
    return dataclasses.replace(record, actor=resolve_actor(record.actor, index))


async def fetch_json(call_tool, source, tool, args, activity_id=None):
    """One upstream tool call -> its JSON payload, or None when the upstream says the object doesn't exist.
    Any other failure raises upstream_error."""
    result = await call_tool(source, tool, args)
    text = "".join(c.text for c in result.content if c.type == "text")
    if result.is_error:
        if UPSTREAM_NOT_FOUND.search(text):
            return None
        raise _error(EVIDENCE_UPSTREAM_ERROR, "upstream_error", "%s: %s" % (source, text[:500]), activity_id)
    try:
        return json.loads(text)
    except ValueError:
        raise _error(EVIDENCE_UPSTREAM_ERROR, "upstream_error", "%s: %s returned non-JSON" % (source, tool),
                     activity_id) from None


async def get_evidence(call_tool, activity_id, index, now=None):
    """Fetch and normalize one record. call_tool = Gateway.call_tool (allowlist and redaction apply)."""
    source, kind_name, fields = parse(activity_id)
    kind = SOURCES[source][kind_name]
    tool = kind.tool(fields) if callable(kind.tool) else kind.tool
    fetched_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    args = kind.args(fields)
    for _ in range(MAX_PAGES):
        payload = await fetch_json(call_tool, source, tool, args, activity_id)
        if payload is None:
            break
        record = kind.normalize(payload, fields, fetched_at)
        if record:
            return resolve(record, index)
        args = kind.next_page and kind.next_page(payload, args)
        if not args:
            break
    else:  # pages remain unsearched: the record may exist, so never call it not_found
        raise _error(EVIDENCE_SEARCH_INCOMPLETE, "search_incomplete",
                     "%s: searched %d pages without finding it; more remain" % (activity_id, MAX_PAGES), activity_id)
    raise _error(EVIDENCE_NOT_FOUND, "not_found", "no evidence found for %s" % activity_id, activity_id)


def as_result(obj):
    """Any result dataclass -> tool result, as JSON text and as structured content."""
    data = dataclasses.asdict(obj)
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))], structured_content=data)
