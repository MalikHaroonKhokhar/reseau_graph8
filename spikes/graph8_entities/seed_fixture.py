#!/usr/bin/env python3
"""HAR-107: seed / clean up synthetic Graph8 records so entity_probe.py can reach Green.

Usage: python3 seed_fixture.py seed      # contact+company, deal, deal-linked task, account correspondence
       python3 seed_fixture.py cleanup   # delete everything in seed_state.json; an id leaves the file only once verified gone
       python3 seed_fixture.py selftest  # offline check of the cleanup bookkeeping
WRITES to the live org. Internal writes only: nothing here sends email/SMS/LinkedIn, books a
meeting or sends a calendar invite (those are the only ways to create inbox threads or meetings).
All records are synthetic (reserved .example domain); created ids go to seed_state.json (gitignored).
"""
import datetime, json, os, sys

import entity_probe as p

m = p.m
HERE = p.HERE
STATE = os.path.join(HERE, "seed_state.json")
DOMAIN = "reseau-probe.example"
LINEAR_URL = "https://linear.app/haroon789/issue/HAR-107/spike-map-graph8-business-entities-and-how-engineering-work-links-to"


def session():
    m.load_env(os.path.join(HERE, "..", "..", ".env"))
    url, key = os.environ.get("GRAPH8_API_URL", "https://be.graph8.com/mcp/"), os.environ["GRAPH8_API_KEY"]
    m.rpc(url, key, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": m.PROTOCOL, "capabilities": {}, "clientInfo": {"name": "reseau-seed", "version": "1"}}})
    m.notify(url, key, None, "notifications/initialized")
    p.call(url, key, 2, "g8_current_org", {})
    return url, key


def save(state):
    json.dump(state, open(STATE, "w"), indent=1)


def seed(url, key):
    state = {}
    try:
        state = json.load(open(STATE))
    except (OSError, ValueError):
        pass
    if state:
        sys.exit("seed_state.json exists; run cleanup first")

    c = p.call(url, key, 10, "g8_create_contact", {"work_email": "probe@" + DOMAIN, "first_name": "Reseau",
                                                   "last_name": "Probe", "company_domain": DOMAIN})["result"]
    state["contact_id"] = c.get("contact_id") or c.get("id")
    save(state)
    print("contact    ", state["contact_id"], "keys:", sorted(c))

    _, members = p.rest(key, "GET", "/team-members")
    owner = members["data"]["items"][0]["id"]
    d = p.call(url, key, 11, "g8_create_deal", {"name": "reseau-probe deal", "owner_id": owner,
                                                "contact_ids": [state["contact_id"]]})["result"]
    d = d.get("deal") or d
    state["deal_id"], state["company_id"] = d.get("id"), d.get("company_id")
    save(state)
    print("deal       ", state["deal_id"], "company", state["company_id"], "keys:", sorted(d))

    t = p.call(url, key, 12, "g8_create_task", {
        "title": "reseau-probe commitment", "tags": ["reseau-probe"], "source_url": LINEAR_URL,
        "records": [{"entity_type": "deal", "entity_id": state["deal_id"]}]})["result"]
    t = t.get("task") or t
    state["task_id"] = t.get("id")
    save(state)
    print("task       ", state["task_id"], "keys:", sorted(t))

    s, r = p.rest(key, "POST", "/accounts/%s/correspondence" % state["company_id"], {
        "channel": "call_notes", "direction": "inbound", "subject": "reseau-probe",
        "content": "Synthetic HAR-107 probe record. No customer data.", "external_id": "reseau-probe-1",
        "correspondence_date": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    data = (r or {}).get("data") or {}
    state["correspondence_id"] = data.get("id")
    save(state)
    print("correspond.", s, state["correspondence_id"], "keys:", sorted(data))


# Deleted children before the company: correspondence is addressed through it.
ORDER = ["correspondence_id", "task_id", "deal_id", "contact_id", "company_id"]


def sweep(state, delete, gone, persist):
    """Delete each recorded id, dropping it from state only once `gone` confirms it. Returns ids left."""
    for k in ORDER:
        if state.get(k) is None:
            continue
        if k == "company_id" and any(state.get(c) is not None for c in ORDER[:-1]):
            print("delete %-18s -> SKIPPED, children still present" % k)
            continue
        try:
            print("delete %-18s -> %s" % (k, delete(k, state)))
        except p.ProbeError as e:
            print("delete %-18s -> FAILED %s" % (k, e))
        if gone(k, state):
            state.pop(k)
            persist(state)
        else:
            print("verify %-18s -> STILL PRESENT, kept in seed_state.json" % k)
    return [k for k in ORDER if state.get(k) is not None]


def cleanup(url, key):
    state = json.load(open(STATE))
    mcp_delete = {
        "task_id": lambda s: p.call(url, key, 20, "g8_delete_task", {"task_id": s["task_id"]}),
        "deal_id": lambda s: p.call(url, key, 21, "g8_delete_deal", {"deal_id": s["deal_id"]}),
        "contact_id": lambda s: p.call(url, key, 22, "g8_execute", {"tool_name": "g8_crm_delete_contact",
                                        "arguments": {"contact_id": s["contact_id"], "confirm": True}}).get("ok"),
        "company_id": lambda s: p.call(url, key, 23, "g8_execute", {"tool_name": "g8_crm_delete_company",
                                        "arguments": {"company_id": s["company_id"], "confirm": True}}).get("ok"),
    }

    def delete(k, s):
        if k == "correspondence_id":
            return p.rest(key, "DELETE", "/accounts/%s/correspondence/%s" % (s["company_id"], s[k]))[0]
        return mcp_delete[k](s)

    def gone(k, s):
        """Positive evidence only: a 404 on the record, or absence from a successful 200 listing."""
        if k == "correspondence_id":
            status, body = p.rest(key, "GET", "/accounts/%s/correspondence" % s["company_id"])
            items = ((body or {}).get("data") or {}).get("items") if status == 200 else None
            return status == 404 or (items is not None and all(i.get("id") != s[k] for i in items))
        path = {"task_id": "/tasks/%s", "deal_id": "/deals/%s", "contact_id": "/contacts/%s", "company_id": "/companies/%s"}[k]
        return p.rest(key, "GET", path % s[k])[0] == 404

    left = sweep(state, delete, gone, save)
    if left:
        print("NOT clean; still present: %s (ids kept in seed_state.json, re-run cleanup)" % ", ".join(left))
        return 1
    os.remove(STATE)
    print("clean: every seeded id verified gone; removed seed_state.json")
    return 0


def selftest():
    saved = []
    st = {k: "x" for k in ORDER}
    def delete(k, s):
        if k == "contact_id":
            raise p.ProbeError("g8_execute -> HTTP 500 code=None")
        return 200
    left = sweep(st, delete, lambda k, s: k != "contact_id", lambda s: saved.append(dict(s)))
    assert left == ["contact_id", "company_id"], left           # contact failed, so company must not be deleted
    assert st == {"contact_id": "x", "company_id": "x"}, st      # retry still has both ids
    left = sweep(st, lambda k, s: 200, lambda k, s: True, lambda s: saved.append(dict(s)))
    assert left == [] and st == {} and saved[-1] == {}
    st = {"task_id": "t"}                                        # delete "succeeds" but record still readable
    assert sweep(st, lambda k, s: 200, lambda k, s: False, lambda s: None) == ["task_id"]
    print("selftest ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "selftest":
        sys.exit(selftest())
    if cmd not in ("seed", "cleanup"):
        sys.exit(__doc__)
    url, key = session()
    sys.exit(seed(url, key) if cmd == "seed" else cleanup(url, key))
