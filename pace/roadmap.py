"""Strict roadmap markdown format (§6). No LLM: anything ambiguous is an error.

    ## WEB-014: Add CSV export for reports
    - depends: WEB-003, WEB-009

    Free-form description until the next `## ` heading.

A heading without an ID (`## Add CSV export`) is a new item. Text before the
first heading is an error, except blank lines and a single `# Title` line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d{3,}$")
HEADING_RE = re.compile(r"^## (?:(?P<id>[A-Z][A-Z0-9]*-\d+):\s*)?(?P<title>.*?)\s*$")
DEPENDS_RE = re.compile(r"^- depends:\s*(?P<ids>.*?)\s*$")


@dataclass
class Entry:
    title: str
    id: str | None = None        # None: new item, gets an ID on save
    depends: list[str] = field(default_factory=list)
    description: str = ""
    line: int = 0                 # 1-based line of the heading


@dataclass
class ParseError:
    line: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class RoadmapError(ValueError):
    def __init__(self, errors: list[ParseError]):
        self.errors = errors
        super().__init__("\n".join(map(str, errors)))


def format_id(slug: str, n: int) -> str:
    return f"{slug}-{n:03d}"


def parse(text: str, slug: str) -> list[Entry]:
    """Parse roadmap markdown; raises RoadmapError listing every problem found."""
    entries: list[Entry] = []
    errors: list[ParseError] = []
    body: list[str] = []
    seen_title = False

    def close() -> None:
        if entries:
            entries[-1].description = "\n".join(body).strip()
        body.clear()

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if line.startswith("## "):
            close()
            m = HEADING_RE.match(line)
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
            entries.append(Entry(title=title, id=item_id, line=n))
            continue
        if not entries:
            if line.startswith("# ") and not seen_title:
                seen_title = True
            elif line.strip():
                errors.append(ParseError(n, "text before the first `## ` item heading"))
            continue
        m = DEPENDS_RE.match(line)
        if m and not "".join(body).strip() and not entries[-1].depends:
            ids = [d.strip() for d in m.group("ids").split(",") if d.strip()]
            for d in ids:
                if not ID_RE.match(d):
                    errors.append(ParseError(n, f"bad dependency ID {d!r}"))
            entries[-1].depends = ids
            continue
        body.append(line)
    close()

    seen: set[str] = set()
    for e in entries:
        if e.id in seen:
            errors.append(ParseError(e.line, f"duplicate ID {e.id}"))
        if e.id:
            seen.add(e.id)
        if e.id and e.id in e.depends:
            errors.append(ParseError(e.line, f"{e.id} depends on itself"))
    if errors:
        raise RoadmapError(sorted(errors, key=lambda x: x.line))
    return entries


def serialize(entries: list[Entry]) -> str:
    parts = []
    for e in entries:
        head = f"## {e.id}: {e.title}" if e.id else f"## {e.title}"
        lines = [head]
        if e.depends:
            lines.append(f"- depends: {', '.join(e.depends)}")
        if e.description:
            lines += ["", e.description]
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + ("\n" if parts else "")
