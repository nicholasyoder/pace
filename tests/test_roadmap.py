import pytest

from pace.roadmap import Entry, RoadmapError, parse, serialize

DOC = """# Roadmap

## WEB-001: First thing
- depends: WEB-002, WEB-010

Some context.

- a bullet
- depends: not a depends line, it's in the body

## Brand new item

## WEB-002: Second
Description right under the heading.
"""


def test_parse():
    es = parse(DOC, "WEB")
    assert [(e.id, e.title) for e in es] == [
        ("WEB-001", "First thing"), (None, "Brand new item"), ("WEB-002", "Second")]
    assert es[0].depends == ["WEB-002", "WEB-010"]
    assert es[0].description.startswith("Some context.")
    assert es[0].description.endswith("in the body")
    assert es[1].description == "" and es[1].line == 11
    assert es[2].description == "Description right under the heading."


def test_round_trip():
    es = parse(DOC, "WEB")
    again = parse(serialize(es), "WEB")
    assert [(e.id, e.title, e.depends, e.description) for e in again] == \
           [(e.id, e.title, e.depends, e.description) for e in es]


def test_serialize_new_and_empty():
    assert serialize([]) == ""
    assert serialize([Entry("X")]) == "## X\n"


@pytest.mark.parametrize("text,line,msg", [
    ("stray text\n## WEB-001: a", 1, "before the first"),
    ("## WEB-001: a\n## WEB-001: b", 2, "duplicate ID WEB-001"),
    ("## APP-001: a", 1, "doesn't belong"),
    ("## WEB-1: a", 1, "at least 3 digits"),
    ("## WEB-001:", 1, "no title"),
    ("## WEB-001: a\n- depends: web-2", 2, "bad dependency"),
    ("## WEB-001: a\n- depends: WEB-001", 1, "depends on itself"),
    ("## web-001: a", 1, "malformed ID"),
])
def test_errors(text, line, msg):
    with pytest.raises(RoadmapError) as ei:
        parse(text, "WEB")
    assert any(e.line == line and msg in e.message for e in ei.value.errors), ei.value.errors


def test_all_errors_reported():
    with pytest.raises(RoadmapError) as ei:
        parse("junk\n## WEB-001: a\n## WEB-001: b\n## WEB-9: c", "WEB")
    assert [e.line for e in ei.value.errors] == [1, 3, 4]
