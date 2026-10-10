import pytest

from pace.roadmap import Entry, Group, Roadmap, RoadmapError, parse, serialize

DOC = """Roadmap-wide framing: tiers are suggestions.

## WEB-001: First thing
- depends: WEB-002, WEB-010

Some context.

- a bullet
- depends: not a depends line, it's in the body

## Brand new item

# Tier 2 — Realism
Why this tier exists.

## WEB-002: Second
- kind: debt
- parked
Description right under the metadata.

### A sub-heading inside the description

# v0.4
## WEB-003: Third
"""


def test_parse():
    rm = parse(DOC, "WEB")
    assert rm.preamble == "Roadmap-wide framing: tiers are suggestions."
    assert [(g.title, g.description) for g in rm.groups] == [
        ("Tier 2 — Realism", "Why this tier exists."), ("v0.4", "")]
    es = rm.entries
    assert [(e.id, e.title, e.group) for e in es] == [
        ("WEB-001", "First thing", None), (None, "Brand new item", None),
        ("WEB-002", "Second", "Tier 2 — Realism"), ("WEB-003", "Third", "v0.4")]
    assert es[0].depends == ["WEB-002", "WEB-010"]
    assert es[0].description.startswith("Some context.")
    assert es[0].description.endswith("in the body")
    assert es[1].description == "" and es[1].line == 11
    assert (es[2].kind, es[2].parked) == ("debt", True)
    assert es[2].description.startswith("Description right under the metadata.")
    assert "### A sub-heading" in es[2].description


def test_round_trip():
    rm = parse(DOC, "WEB")
    again = parse(serialize(rm), "WEB")
    key = lambda r: (r.preamble, [(g.title, g.description) for g in r.groups],  # noqa: E731
                     [(e.id, e.title, e.depends, e.description, e.kind, e.parked, e.group)
                      for e in r.entries])
    assert key(again) == key(rm)


def test_serialize_new_and_empty():
    assert serialize(Roadmap()) == ""
    assert serialize(Roadmap(entries=[Entry("X")])) == "## X\n"
    assert serialize(Roadmap(groups=[Group("G")], entries=[Entry("X", group="G")])) == \
        "# G\n\n## X\n"


@pytest.mark.parametrize("text,line,msg", [
    ("## WEB-001: a\n## WEB-001: b", 2, "duplicate ID WEB-001"),
    ("## APP-001: a", 1, "doesn't belong"),
    ("## WEB-1: a", 1, "at least 3 digits"),
    ("## WEB-001:", 1, "no title"),
    ("## WEB-001: a\n- depends: web-2", 2, "bad dependency"),
    ("## WEB-001: a\n- depends: WEB-001", 1, "depends on itself"),
    ("## web-001: a", 1, "malformed ID"),
    ("# G\n# G", 2, "duplicate group"),
    ("# ", 1, "group heading has no title"),
    ("## WEB-001: a\n- kind: Big Thing", 2, "one lowercase word"),
    ("## WEB-001: a\n- depends: WEB-002\n- depends: WEB-003", 3, "more than one"),
])
def test_errors(text, line, msg):
    with pytest.raises(RoadmapError) as ei:
        parse(text, "WEB")
    assert any(e.line == line and msg in e.message for e in ei.value.errors), ei.value.errors


def test_all_errors_reported():
    with pytest.raises(RoadmapError) as ei:
        parse("preamble is fine\n## WEB-001: a\n## WEB-001: b\n## WEB-9: c", "WEB")
    assert [e.line for e in ei.value.errors] == [3, 4]


@pytest.mark.parametrize("origin,want", [
    ("owner/repo", "git@github.com:owner/repo.git"),
    ("https://github.com/owner/repo.git/", "git@github.com:owner/repo.git"),
    ("ssh://git@github.com/owner/repo", "git@github.com:owner/repo.git"),
    ("git@github-nkms:owner/repo.git", "git@github-nkms:owner/repo.git"),
    ("/srv/git/repo.git", "/srv/git/repo.git"),
])
def test_normalize_origin(origin, want):
    from pace.git import normalize_origin
    assert normalize_origin(origin) == want
