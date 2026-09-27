#!/usr/bin/env python3
"""Demo data: fictional customers whose Graph8 deals wait on real Réseau work (HAR-108, HAR-109).

Usage: python3 seed_demo.py seed      # 4 customers, their deals, deal-linked tasks, call notes; 1 unlinked task
       python3 seed_demo.py cleanup   # delete everything in demo_state.json; an id leaves it only once verified gone
WRITES to the live org, with only the tool arguments seed_fixture.py already used there. Internal writes only:
nothing is sent (no email/SMS/LinkedIn, meetings or invites), and every contact is on a reserved .example domain.
Each commitment is a task linked to its deal whose source_url is a real Réseau Linear issue or GitHub PR, which is
the link get_business_context (HAR-109) follows. Created ids go to demo_state.json (gitignored).
"""
import datetime, json, os, sys

import entity_probe as p
import seed_fixture as f

STATE = os.path.join(p.HERE, "demo_state.json")
LINEAR = "https://linear.app/haroon789/issue/"
PR = "https://github.com/MalikHaroonKhokhar/reseau_graph8/pull/"
TAG = "reseau-demo"

# (domain, contact first/last, deal, [(task title, source_url)], call note)
CUSTOMERS = [
    ("northwind-logistics.example", "Dana", "Reyes", "Northwind Logistics: Réseau eng-ops pilot",
     [("Northwind: verified daily report for their engineering leads", LINEAR + "HAR-103"),
      ("Northwind: evidence drill-down in the Réseau dashboard", LINEAR + "HAR-106")],
     "Pilot hinges on the daily report: their ops review wants counts that link back to PRs and tickets."),
    ("bluepeak-health.example", "Omar", "Haddad", "Bluepeak Health: gateway security review",
     [("Bluepeak: no upstream tokens in Graph8 read responses", LINEAR + "HAR-111"),
      ("Bluepeak: Linear rate limit must not drop the whole session", LINEAR + "HAR-112")],
     "Security review passed on token handling; the session-drop bug is the last open item before signing."),
    ("quillstone.example", "Mei", "Tanaka", "Quillstone Software: Start My Day rollout",
     [("Quillstone: Start My Day briefing for 40 engineers", LINEAR + "HAR-102"),
      ("Quillstone: Ask Réseau answers with citations", LINEAR + "HAR-104")],
     "Rollout to 40 engineers once Start My Day ships; Ask Réseau is a nice-to-have for phase two."),
    ("harborpine.example", "Luis", "Ortega", "Harbor & Pine Retail: business-context add-on",
     [("Harbor & Pine: show which customer waits on each task", LINEAR + "HAR-109"),
      ("Harbor & Pine: Graph8 records as cited evidence", PR + "22")],
     "They buy the add-on if engineering tasks show the customer and deal behind them, with sources."),
]
UNLINKED = "Réseau: rehearse the hackathon demo"  # a task with no deal or company: not a commitment


def save(state):
    json.dump(state, open(STATE, "w"), indent=1)


def seed(url, key):
    if os.path.exists(STATE):
        sys.exit("demo_state.json exists; run cleanup first")
    state = []  # [{"kind", "id", "company_id"?}] in creation order; cleanup walks it backwards
    _, members = p.rest(key, "GET", "/team-members")
    owner = members["data"]["items"][0]["id"]
    rid = 100
    for domain, first, last, deal, tasks, note in CUSTOMERS:
        rid += 1
        c = p.call(url, key, rid, "g8_create_contact", {"work_email": "%s@%s" % (first.lower(), domain),
                                                        "first_name": first, "last_name": last,
                                                        "company_domain": domain})["result"]
        state.append({"kind": "contact", "id": c.get("contact_id") or c.get("id")})
        save(state)
        rid += 1
        d = p.call(url, key, rid, "g8_create_deal", {"name": deal, "owner_id": owner,
                                                     "contact_ids": [state[-1]["id"]]})["result"]
        d = d.get("deal") or d
        state.insert(0, {"kind": "company", "id": d.get("company_id")})  # deleted last, once its children are gone
        state.append({"kind": "deal", "id": d.get("id")})
        save(state)
        print("customer %-28s company %s deal %s" % (domain, d.get("company_id"), d.get("id")))
        for title, source_url in tasks:
            rid += 1
            t = p.call(url, key, rid, "g8_create_task", {"title": title, "tags": [TAG], "source_url": source_url,
                                                         "records": [{"entity_type": "deal", "entity_id": d["id"]}]})
            t = t["result"].get("task") or t["result"]
            state.append({"kind": "task", "id": t.get("id")})
            save(state)
            print("  commitment %s <- %s" % (t.get("id"), source_url))
        s, r = p.rest(key, "POST", "/accounts/%s/correspondence" % d["company_id"], {
            "channel": "call_notes", "direction": "inbound", "subject": "Call notes: %s" % deal.split(":")[0],
            "content": note + " (Synthetic demo record.)", "external_id": "%s-%s" % (TAG, domain),
            "correspondence_date": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        state.append({"kind": "correspondence", "id": ((r or {}).get("data") or {}).get("id"),
                      "company_id": d["company_id"]})
        save(state)
        print("  call notes %s (HTTP %s)" % (state[-1]["id"], s))
    rid += 1
    t = p.call(url, key, rid, "g8_create_task", {"title": UNLINKED, "tags": [TAG]})["result"]
    t = t.get("task") or t
    state.append({"kind": "task", "id": t.get("id")})
    save(state)
    print("unlinked task %s (not a commitment)" % t.get("id"))


def cleanup(url, key):
    state = json.load(open(STATE))
    rest_path = {"task": "/tasks/%s", "deal": "/deals/%s", "contact": "/contacts/%s", "company": "/companies/%s"}

    def delete(r):
        if r["kind"] == "correspondence":
            return p.rest(key, "DELETE", "/accounts/%s/correspondence/%s" % (r["company_id"], r["id"]))[0]
        if r["kind"] in ("task", "deal"):
            return p.call(url, key, 200, "g8_delete_%s" % r["kind"], {"%s_id" % r["kind"]: r["id"]})
        return p.call(url, key, 201, "g8_execute", {"tool_name": "g8_crm_delete_%s" % r["kind"],
                                                    "arguments": {"%s_id" % r["kind"]: r["id"], "confirm": True}})

    def gone(r):
        """Positive evidence only, as in seed_fixture.py: a 404, or absence from a 200 listing."""
        if r["kind"] == "correspondence":
            status, body = p.rest(key, "GET", "/accounts/%s/correspondence" % r["company_id"])
            items = ((body or {}).get("data") or {}).get("items") if status == 200 else None
            return status == 404 or (items is not None and all(i.get("id") != r["id"] for i in items))
        return p.rest(key, "GET", rest_path[r["kind"]] % r["id"])[0] == 404

    for r in list(reversed(state)):
        if r["id"] is None:
            state.remove(r)
            continue
        if r["kind"] == "company" and any(x["kind"] != "company" for x in state):
            print("delete company %s -> SKIPPED, children still present" % r["id"])
            continue
        try:
            print("delete %-14s %s -> %s" % (r["kind"], r["id"], delete(r)))
        except p.ProbeError as e:
            print("delete %-14s %s -> FAILED %s" % (r["kind"], r["id"], e))
        if gone(r):
            state.remove(r)
            save(state)
        else:
            print("verify %-14s %s -> STILL PRESENT, kept in demo_state.json" % (r["kind"], r["id"]))
    if state:
        print("NOT clean; %d records left in demo_state.json, re-run cleanup" % len(state))
        return 1
    os.remove(STATE)
    print("clean: every demo record verified gone")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("seed", "cleanup"):
        sys.exit(__doc__)
    url, key = f.session()
    sys.exit(seed(url, key) if cmd == "seed" else cleanup(url, key))
