"""Shared record types for normalized upstream work objects. Providers (github.py, linear.py, graph8.py) build
these."""
import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Actor:
    source: str
    id: str | None  # the upstream's stable user key: GitHub login, Linear user id
    name: str | None = None
    person: str | None = None  # Réseau person, set only from the identity map
    identity: str = "unmapped"  # "mapped" once the map names a person; never guessed


@dataclass(frozen=True)
class Record:
    activity_id: str  # "<source>:<kind>:<key>", deterministic; see Kind
    source: str
    kind: str
    source_id: str
    url: str | None  # canonical link; None when the source has none (Graph8: cite the activity_id)
    title: str
    actor: Actor
    created_at: str | None
    updated_at: str | None
    fetched_at: str  # when the gateway fetched it; freshness


@dataclass(frozen=True)
class Kind:
    """One record kind of a source. `pattern` parses the activity_id key into fields, `template` formats it
    back. `tool` is the tool name, or fields -> name when the key picks the tool. `args` maps fields to the
    tool's arguments; `normalize(payload, fields, fetched_at)` returns a Record, or None when this payload
    doesn't hold it; `next_page(payload, args)` gives the next page's args, or None, for kinds found by paging."""
    pattern: str
    template: str
    tool: str | Callable[[dict], str]
    args: Callable[[dict], dict]
    normalize: Callable[[dict, dict, str], "Record | None"]
    next_page: Callable[[dict, dict], dict | None] | None = None
    regex: re.Pattern = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "regex", re.compile(self.pattern))


def first_line(text, limit=120):
    line = (text or "").strip().split("\n", 1)[0]
    return line if len(line) <= limit else line[:limit - 1] + "…"
