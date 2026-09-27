"""Linear normalizers. Payloads are the JSON text of Linear's MCP get_issue (tests/fixtures/linear_issue.json).
get_issue exposes the creator's name and id but no email, so identities map by Linear user id."""
from reseau.evidence.records import Actor, Kind, Record


def issue(i, fields, fetched_at):
    # ponytail: identifiers change when an issue moves team; key by i["uuid"] if moved issues must keep their ID
    return Record("linear:issue:" + i["id"], "linear", "issue", i["id"], i["url"], i["title"],
                  Actor("linear", i.get("createdById"), i.get("createdBy")),
                  i.get("createdAt"), i.get("updatedAt"), fetched_at)


KINDS = {
    "issue": Kind(r"(?P<identifier>[A-Z][A-Z0-9]*-\d+)", "{identifier}", "get_issue",
                  lambda f: {"id": f["identifier"]}, issue),
}
