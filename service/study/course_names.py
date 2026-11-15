"""Course-name normalisation shared by the Canvas parser and the Study store.

``merge_key`` is the stored key (and the parser's name hash input): lower-case, punctuation
to spaces, single spaces. ``course_key`` is what two rows are compared by when deciding they
are one course: the Dashboard says "Writing and Rhetoric: Voices and Public Argument"
where the course's own pages say "WRIT 1 Writing and Rhetoric: Voices & Public Argument
Section 04" with the code "WRIT-1-04". Term suffixes ("(Fall 2026)", "F26") are left alone:
they also tell two runs of one course apart.
"""
from __future__ import annotations

import re
from typing import NamedTuple

_WS = re.compile(r"\s+")
# A code field ("WRIT-1-04", "CS 101", "STAT 101-02") once normalised: subject, number, section.
_CODE_FIELD = re.compile(r"([a-z]{2,6}) ?(\d{1,4}[a-z]?)(?: (\d{1,3}[a-z]?))?")
# A course code leading a name ("WRIT 1 ", "CS 101: ", "WRIT-1 - ", "WRIT-1-04 ").
_LEAD = re.compile(r"([a-z]{2,6}) ?(\d{1,4}[a-z]?)(?: (\d{1,3}))? (?=\S)")
_SECTION_TAIL = re.compile(r" (?:section|sec) (\d{1,3}[a-z]?)$")
_BARE_TAIL = re.compile(r" (\d{1,3}[a-z]?)$")


def merge_key(name) -> str:
    return _WS.sub(" ", re.sub(r"[^0-9a-z]+", " ", str(name or "").lower())).strip()


class CourseKey(NamedTuple):
    keys: frozenset      # the full key, plus the key without code and section when that is non-trivial
    course: str | None   # subject + number ("writ 1"), from the code field or the name
    section: str | None  # "04"
    clean: bool          # the name carries no course code or section marker


def _trivial(core: str) -> bool:
    return len(core.split()) < 2 and len(core) < 8


def course_key(name, code=None) -> CourseKey:
    full = merge_key(str(name or "").replace("&", " and "))
    if not full:
        return CourseKey(frozenset(), None, None, True)
    course = section = None
    field = merge_key(code)
    m = _CODE_FIELD.fullmatch(field)
    if m:
        course, section = f"{m.group(1)} {m.group(2)}", m.group(3)
    core = full
    if field and not m and core.startswith(field + " "):
        core = core[len(field) + 1:]   # a code of another shape that the name starts with
    else:
        lead = _LEAD.match(core)
        if lead and (course is None or f"{lead.group(1)} {lead.group(2)}" == course) \
                and not (lead.group(3) and section and lead.group(3) != section):
            course = course or f"{lead.group(1)} {lead.group(2)}"
            section = section or lead.group(3)
            core = core[lead.end():]
    tail = _SECTION_TAIL.search(core)
    if tail and (section is None or tail.group(1) == section):
        section = section or tail.group(1)
        core = core[:tail.start()]
    elif section:
        bare = _BARE_TAIL.search(core)
        if bare and bare.group(1) == section:
            core = core[:bare.start()]
    keys = {full}
    if core != full and not _trivial(core):
        keys.add(core)
    return CourseKey(frozenset(keys), course, section, core == full)


def same_course(a: CourseKey, b: CourseKey) -> bool:
    """Whether two names (with their codes) are one course: a shared key, and no codes naming a
    different subject+number or a different section."""
    if not a.keys & b.keys:
        return False
    if a.course and b.course and a.course != b.course:
        return False
    return not (a.section and b.section and a.section != b.section)


def clean_name(names):
    """The display name of merged rows: the first name without a code or section marker, else the
    first name. ``names`` are (name, code) pairs in priority order."""
    present = [(n, c) for n, c in names if isinstance(n, str) and n.strip()]
    for n, c in present:
        if course_key(n, c).clean:
            return n
    return present[0][0] if present else None
