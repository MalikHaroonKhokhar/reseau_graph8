"""Graph8 normalizers (HAR-108) over the business entities HAR-107 mapped (spikes/graph8_entities/FINDINGS.md).

customer     = company       graph8:customer:<company id>              g8_crm_get_company (hidden, but callable)
opportunity  = deal          graph8:opportunity:<deal uuid>            g8_get_deal
commitment   = task linked to a deal or company
                             graph8:commitment:<task uuid>             g8_get_task
conversation = meeting       graph8:conversation:meeting/<meeting id>  g8_get_meeting
             = inbox thread  graph8:conversation:<channel>/<thread id> g8_get_reply (channel: email, sms, ...)

Graph8 has no record URL (HAR-107 found none, and a guessed app.graph8.com route is not shipped), so url is None
and the activity_id is the citation. The actor is the record's owner: the deal owner, the task assignee, the
meeting's Graph8 user. Companies and threads carry no single owner. Payloads are the tools' JSON text;
g8_crm_get_company wraps its record in {"data": ...}. Company, deal and task shapes were verified on live
records; meeting and thread shapes come from outputSchema only (the org had none), so they are provisional.
Account correspondence, HAR-107's one verified conversation source, has no MCP tool (REST only): not here.
"""
from reseau.evidence.records import Actor, Kind, Record

UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
BUSINESS = {"deal", "company"}


def _record(kind, key, p, actor, title, fetched_at):
    return Record("graph8:%s:%s" % (kind, key), "graph8", kind, str(p["id"]), None, title or "", actor,
                  p.get("created_at"), p.get("updated_at"), fetched_at)


def customer(p, fields, fetched_at):
    c = p["data"] if isinstance(p.get("data"), dict) else p
    return _record("customer", c["id"], c, Actor("graph8", None), c.get("name") or c.get("domain"), fetched_at)


def opportunity(d, fields, fetched_at):
    return _record("opportunity", d["id"], d, Actor("graph8", d.get("owner_id"), d.get("owner_name")), d.get("name"),
                   fetched_at)


def is_commitment(t):
    """A task is a commitment only when it is tied to a deal or company (HAR-107's rule)."""
    return bool(t.get("entity_type") in BUSINESS and t.get("entity_id") or t.get("company_id") is not None
                or any(link.get("entity_type") in BUSINESS and link.get("entity_id") for link in t.get("links") or []))


def commitment(t, fields, fetched_at):
    if not is_commitment(t):
        return None  # a task, but no commitment: nothing ties it to a customer
    return _record("commitment", t["id"], t, Actor("graph8", t.get("assignee_id"), t.get("assignee_name")),
                   t.get("title"), fetched_at)


def conversation(c, fields, fetched_at):
    channel = fields["channel"]
    actor = Actor("graph8", None, c.get("user_email")) if channel == "meeting" else Actor("graph8", None)
    return _record("conversation", "%s/%s" % (channel, c["id"]), c, actor, c.get("subject"), fetched_at)


def _conversation_args(f):
    return {"meeting_id": f["id"]} if f["channel"] == "meeting" else {"reply_id": f["id"], "channel": f["channel"]}


KINDS = {
    "customer": Kind(r"(?P<company_id>\d+)", "{company_id}", "g8_crm_get_company",
                     lambda f: {"company_id": int(f["company_id"])}, customer),
    "opportunity": Kind("(?P<deal_id>%s)" % UUID, "{deal_id}", "g8_get_deal", lambda f: {"deal_id": f["deal_id"]},
                        opportunity),
    "commitment": Kind("(?P<task_id>%s)" % UUID, "{task_id}", "g8_get_task", lambda f: {"task_id": f["task_id"]},
                       commitment),
    "conversation": Kind(r"(?P<channel>[a-z]+)/(?P<id>[^\s/]+)", "{channel}/{id}",
                         lambda f: "g8_get_meeting" if f["channel"] == "meeting" else "g8_get_reply",
                         _conversation_args, conversation),
}
