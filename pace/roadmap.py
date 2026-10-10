"""Strict roadmap markdown format (§6). No LLM: anything ambiguous is an error.

    Optional preamble: roadmap-wide context, sent to Claude with every item.

    ## WEB-002: An ungrouped item (ungrouped items come first)

    # v0.4 — Reporting
    Optional group description: context for every item in the group.

    ## WEB-014: Add CSV export for reports
    - depends: WEB-003, WEB-009
    - kind: debt
    - parked

    Free-form description until the next `## ` or `# ` heading. Headings
    inside a description must be `###` or deeper.

A heading without an ID (`## Add CSV export`) is a new item. Metadata lines
(`- depends:`, `- kind:`, `- parked`) come straight after the item heading,
before any description text. Groups (tiers, releases, milestones) are ordered
like items: document order is priority.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d{3,}$")
HEADING_RE = re.compile(r"^## (?:(?P<id>[A-Z][A-Z0-9]*-\d+):\s*)?(?P<title>.*?)\s*$")
DEPENDS_RE = re.compile(r"^- depends:\s*(?P<ids>.*?)\s*$")
KIND_RE = re.compile(r"^- kind:\s*(?P<kind>.*?)\s*$")
KIND_OK = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass
class Group:
    title: str
    description: str = ""
    line: int = 0


@dataclass
class Entry:
    title: str
    id: str | None = None        # None: new item, gets an ID on save
    depends: list[str] = field(default_factory=list)
    description: str = ""
    kind: str = ""
    parked: bool = False
    group: str | None = None     # group title; None: ungrouped
    line: int = 0                # 1-based line of the heading


@dataclass
class Roadmap:
    preamble: str = ""
    groups: list[Group] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)


@dataclass
class ParseError:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}" if self.line else self.message


class RoadmapError(ValueError):
    def __init__(self, errors: list[ParseError]):
        self.errors = errors
        super().__init__("\n".join(map(str, errors)))


def format_id(slug: str, n: int) -> str:
    return f"{slug}-{n:03d}"


def parse(text: str, slug: str) -> Roadmap:
    """Parse roadmap markdown; raises RoadmapError listing every problem found."""
    rm = Roadmap()
    errors: list[ParseError] = []
    body: list[str] = []
    current: Group | Entry | None = None  # what `body` lines belong to; None: preamble
    meta_open = False                     # still in the item's metadata lines

    def close() -> None:
        text = "\n".join(body).strip()
        if current is None:
            rm.preamble = text
        else:
            current.description = text
        body.clear()

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if line.startswith("# ") or line == "#":
            close()
            title = line[1:].strip()
            if not title:
                errors.append(ParseError(n, "group heading has no title"))
            elif any(g.title == title for g in rm.groups):
                errors.append(ParseError(n, f"duplicate group {title!r}"))
            current = Group(title, line=n)
            rm.groups.append(current)
            meta_open = False
            continue
        if line.startswith("## ") or line == "##":
            close()
            m = HEADING_RE.match(line + " ")
            assert m  # the pattern accepts any `## ` line
            item_id, title = m.group("id"), m.group("title")
            if item_id is None and re.match(r"^[A-Za-z0-9]+-\d+:", title):
                errors.append(ParseError(n, f"malformed ID in heading {title.split(':')[0]!r}"))
            elif item_id and not item_id.startswith(f"{slug}-"):
                errors.append(ParseError(n, f"ID {item_id} doesn't belong to project {slug}"))
            elif item_id and not ID_RE.match(item_id):
                errors.append(ParseError(n, f"ID {item_id} needs at least 3 digits"))
            if not title:
                errors.append(ParseError(n, "heading has no title"))
            current = Entry(title=title, id=item_id, line=n,
                            group=rm.groups[-1].title if rm.groups else None)
            rm.entries.append(current)
            meta_open = True
            continue
        if isinstance(current, Entry) and meta_open:
            if m := DEPENDS_RE.match(line):
                if current.depends:
                    errors.append(ParseError(n, "more than one `- depends:` line"))
                ids = [d.strip() for d in m.group("ids").split(",") if d.strip()]
                for d in ids:
                    if not ID_RE.match(d):
                        errors.append(ParseError(n, f"bad dependency ID {d!r}"))
                current.depends = ids
                continue
            if m := KIND_RE.match(line):
                if not KIND_OK.match(m.group("kind")):
                    errors.append(ParseError(n, f"kind must be one lowercase word, not "
                                                f"{m.group('kind')!r}"))
                current.kind = m.group("kind")
                continue
            if line == "- parked":
                current.parked = True
                continue
            if line.strip():
                meta_open = False
        body.append(line)
    close()

    seen: set[str] = set()
    for e in rm.entries:
        if e.id in seen:
            errors.append(ParseError(e.line, f"duplicate ID {e.id}"))
        if e.id:
            seen.add(e.id)
        if e.id and e.id in e.depends:
            errors.append(ParseError(e.line, f"{e.id} depends on itself"))
    if errors:
        raise RoadmapError(sorted(errors, key=lambda x: x.line))
    return rm


def _entry(e: Entry) -> str:
    lines = [f"## {e.id}: {e.title}" if e.id else f"## {e.title}"]
    if e.depends:
        lines.append(f"- depends: {', '.join(e.depends)}")
    if e.kind:
        lines.append(f"- kind: {e.kind}")
    if e.parked:
        lines.append("- parked")
    if e.description:
        lines += ["", e.description]
    return "\n".join(lines)


def serialize(rm: Roadmap) -> str:
    parts = []
    if rm.preamble:
        parts.append(rm.preamble)
    parts += [_entry(e) for e in rm.entries if e.group is None]
    for g in rm.groups:
        parts.append(f"# {g.title}" + (f"\n{g.description}" if g.description else ""))
        parts += [_entry(e) for e in rm.entries if e.group == g.title]
    return "\n\n".join(parts) + ("\n" if parts else "")
