"""The verifiers every workflow's output goes through before anyone reads it: Start My Day (HAR-102), the daily
report (HAR-103) and Ask Réseau (HAR-104).

A reply is sections of sentences, {section: [{"text": one sentence, "activity_ids": [...]}]}, checked against the
tool output it was written from. Each verifier returns its problems; none means verified.
- citations: every sentence cites activity_ids the tool returned for its section.
- counts: every number a counted sentence states is the tool's count, and it cites everything behind it.
- numbers: every number a free-form sentence (a summary) states is some count in the tool output.
- blockers: every blocked issue the tool returned has a sentence that names what blocks it.
"""
import json
import re

from mcp.shared.exceptions import MCPError

from reseau import evidence

NOTHING = "Nothing to report."
SENTENCE_BREAK = re.compile(r"[.!?]\s+(?=[A-Z0-9])")
NUMBER = re.compile(r"\b\d+\b")  # not the 8 in reseau_graph8 or a SHA's digits
IDENTIFIER = re.compile(r"#\d+|\b[A-Za-z]+-\d+\b|\b\d{4}-\d\d-\d\d\b")  # PR #9, ENG-142, a date: names, not counts


def is_activity_id(value):
    try:
        evidence.parse(value)
        return True
    except MCPError:
        return False


def activity_ids(value):
    """Every activity_id anywhere in a tool's JSON output: activity_id, activity_ids, blocked_by, via..."""
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return {i for v in value for i in activity_ids(v)}
    return {value} if is_activity_id(value) else set()


def is_sentence(s):
    return (isinstance(s, dict) and isinstance(s.get("text"), str) and s["text"].strip()
            and isinstance(s.get("activity_ids"), list) and all(isinstance(i, str) for i in s["activity_ids"]))


def sentences(reply, section):
    """(where, sentence) for a section's well-formed sentences; citations() reports the rest."""
    found = reply.get(section) if isinstance(reply, dict) else None
    return [("%s[%d]" % (section, k), s) for k, s in enumerate(found if isinstance(found, list) else [])
            if is_sentence(s)]


def citations(reply, sources):
    """sources = {section: the tool output that section may cite}. Every sentence cites at least one
    activity_id, and only ones found in its section's source. The one uncited sentence is NOTHING, and only for
    a section whose source holds no activity_id; a section with activity can't be empty or NOTHING."""
    if not isinstance(reply, dict):
        return ["not a JSON object of sections"]
    problems = ["%s: not a known section" % s for s in reply if s not in sources]
    for section, source in sources.items():
        allowed, found = activity_ids(source), reply.get(section)
        if not isinstance(found, list) or not found:
            problems.append("%s: no sentences" % section)
            continue
        for k, s in enumerate(found):
            where = "%s[%d]" % (section, k)
            if not is_sentence(s):
                problems.append("%s: not a {text, activity_ids} sentence" % where)
                continue
            text, cited = s["text"], s["activity_ids"]
            if text == NOTHING and not cited:
                if allowed:
                    problems.append("%s: says %r, but the tool returned %d activity_id(s)" % (where, NOTHING, len(allowed)))
            elif not cited:
                problems.append("%s: no citation: %r" % (where, text))
            else:
                problems += ["%s: cites %r, which the tool did not return for %s" % (where, i, section)
                             for i in cited if i not in allowed]
                if SENTENCE_BREAK.search(text.strip()):
                    problems.append("%s: more than one sentence: %r" % (where, text))
    return problems


def stated(text):
    """The numbers a sentence states, in digits; identifiers aside."""
    return [int(n) for n in NUMBER.findall(IDENTIFIER.sub(" ", text))]


def counted(value):
    """Every count in a tool's JSON output: its integers, and the length of each of its lists."""
    if isinstance(value, dict):
        return {n for v in value.values() for n in counted(v)}
    if isinstance(value, list):
        return {len(value)} | {n for v in value for n in counted(v)}
    return {value} if isinstance(value, int) and not isinstance(value, bool) else set()


def numbers(reply, section, allowed):
    """Every number a section's sentences state is one of allowed (counted(tool output)): a summary may say
    "8 issues" only if the tool returned a count of 8."""
    return ["%s: says %d, which is no count the tool returned" % (where, n)
            for where, s in sentences(reply, section) for n in stated(s["text"]) if n not in allowed]


def counts(reply, tallies):
    """tallies = {section: the tool's {"count", "activity_ids"}}. Every sentence of a counted section states the
    count in digits, every other number in it (identifiers aside) is that count too, and it cites every
    activity_id behind the count. NOTHING, for a count of 0, is citations()' to check."""
    problems = []
    for section, tally in tallies.items():
        for where, s in sentences(reply, section):
            text, cited = s["text"], s["activity_ids"]
            if text == NOTHING and not cited:
                continue
            said = stated(text)
            if not said:
                problems.append("%s: states no count: %r" % (where, text))
            problems += ["%s: says %d, but the tool counted %d" % (where, n, tally["count"])
                         for n in said if n != tally["count"]]
            missing = [i for i in tally["activity_ids"] if i not in cited]
            if missing:
                problems.append("%s: doesn't cite %d of the %d activity_ids behind the count: %s"
                                % (where, len(missing), tally["count"], missing))
    return problems


def blockers(reply, section, blocked):
    """blocked = the tool's blocked issues, [{"activity_id", "blocked_by": [activity_id]}]. Each one has a
    sentence in section that cites it, and cites and names (ENG-7 for linear:issue:ENG-7) each of its blockers."""
    problems = []
    for issue in blocked:
        mine = [s for _, s in sentences(reply, section) if issue["activity_id"] in s["activity_ids"]]
        if not mine:
            problems.append("%s: no sentence for blocked %s" % (section, issue["activity_id"]))
        for blocker in issue["blocked_by"] if mine else ():
            name = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(blocker.rsplit(":", 1)[1]))
            if not any(blocker in s["activity_ids"] and name.search(s["text"]) for s in mine):
                problems.append("%s: %s's sentence doesn't name and cite its blocker %s"
                                % (section, issue["activity_id"], blocker))
    return problems


def parse(reply):
    """The agent's reply -> {section: sentences}, or None if it holds no JSON object. Tolerates text around the
    object (a persona greeting, code fences). An empty section, or "nothing to report" in any casing, becomes
    [NOTHING], which citations() then holds to the tool output."""
    text = reply if isinstance(reply, str) else ""
    try:
        parsed = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    for section, found in parsed.items():
        if found == [] or (isinstance(found, list) and len(found) == 1 and isinstance(found[0], dict)
                           and str(found[0].get("text")).strip().rstrip(".").casefold() == "nothing to report"
                           and not found[0].get("activity_ids")):
            parsed[section] = [{"text": NOTHING, "activity_ids": []}]
    return parsed
