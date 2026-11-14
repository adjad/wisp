"""Which slides cover an assignment (docs/STUDY_TAB.md "Slides").

Pure apart from reading a store's pages: ``match`` takes either a list of page rows
(the store.search_pages shape) or a store (``sources`` + ``pages``). Page text is
model-made data from the student's own decks; it is only compared here, never run.

A deck matches when the assignment names its lecture or week ("Lecture 7", "Lec 7",
"Weeks 5 and 6", "L7"), and a page matches on keyword overlap with the assignment's
title and details. Same-course pages only, when both sides know their course.
"""
from __future__ import annotations

import re

MAX_DECKS = 3
MAX_SOURCES = 30
DECK_PAGES = 40          # a whole matched deck lists at most this many pages
REVIEW_PAGES = 12
MIN_PAGE_SCORE = 2
NUMBER_SCORE = 10.0

_REF = re.compile(
    r"(?<![a-z])(lectures?|lecs?|weeks?|wks?|l(?=\d))\.?\s*#?\s*(\d{1,3})"
    r"((?:\s*(?:,|and|&|-|–|—|to|through)\s*\d{1,3}(?!\d))*)", re.I)
_TAIL = re.compile(r"(-|–|—|to|through)?\s*(\d{1,3})", re.I)
_WORD = re.compile(r"[a-z][a-z0-9+#']{2,}")
_STOP = set("""
the and for with from that this your you are was were will can not but all any its into
about than then them they their there have has had does did doing out over under more most
each per via due date week weeks lecture lectures lec slide slides quiz quizzes exam exams
midterm final homework assignment assignments project projects reading readings problem
problems set sets part parts question questions points point submit submission page pages
chapter chapters section sections lab labs worksheet unit units review notes note class
course use using used what when where which who how why also one two three four five six
seven eight nine ten first second third new see read write make due today tomorrow
""".split())


def _family(word: str) -> str:
    return "week" if word.lower().startswith("w") else "lecture"


def refs(text) -> set[tuple[str, int]]:
    """{("lecture", 7), ("week", 5)} for every lecture/week number the text names."""
    out: set[tuple[str, int]] = set()
    if not isinstance(text, str):
        return out
    for m in _REF.finditer(text):
        family, last = _family(m.group(1)), int(m.group(2))
        out.add((family, last))
        for sep, num in _TAIL.findall(m.group(3) or ""):
            n = int(num)
            if sep and sep.lower() in ("-", "–", "—", "to", "through") and 0 < n - last <= 10:
                out.update((family, k) for k in range(last + 1, n + 1))
            else:
                out.add((family, n))
            last = n
    return out


def _stem(word: str) -> str:
    word = word.strip("'")
    if len(word) > 4 and word.endswith("es") and word[-3] in "sxz":
        return word[:-2]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text) -> set[str]:
    if not isinstance(text, str):
        return set()
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP and _stem(w) not in _STOP}


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _page_number(row) -> int | None:
    page = row.get("page")
    if isinstance(page, bool) or not isinstance(page, int):
        return None
    return page


def pages_for(store, course_id: str | None = None) -> list[dict]:
    """Every saved page of the course's decks (all decks when course_id is None), each with
    its deck title and course. A store without pages gives []."""
    if store is None or not hasattr(store, "pages"):
        return []
    try:
        sources = store.sources(course_id) if course_id else store.sources()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for src in sources[:MAX_SOURCES]:
        if not isinstance(src, dict) or not src.get("id"):
            continue
        if src.get("pages") == 0 or src.get("deck_status") in ("none", "failed") and not src.get("pages"):
            continue
        try:
            rows = store.pages(src["id"])
        except Exception:  # noqa: BLE001
            continue
        for row in rows or ():
            if isinstance(row, dict):
                out.append({**row, "source_id": row.get("source_id") or src["id"],
                            "deck": row.get("deck") or src.get("title") or "",
                            "course_id": row.get("course_id", src.get("course_id"))})
    return out


def deck_label(deck) -> str | None:
    """"Lecture 7" / "Week 5" / "Weeks 5 and 6" from a deck title, or None."""
    found = sorted(refs(deck if isinstance(deck, str) else ""))
    if not found:
        return None
    family = found[0][0]
    nums = sorted(n for f, n in found if f == family)
    return _numbers_phrase(family, nums)


def _numbers_phrase(family: str, nums: list[int]) -> str:
    word = "Lecture" if family == "lecture" else "Week"
    if len(nums) == 1:
        return f"{word} {nums[0]}"
    return f"{word}s " + _join([str(n) for n in nums])


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _assignment_text(assignment: dict) -> tuple[str, str]:
    title = assignment.get("title") if isinstance(assignment.get("title"), str) else ""
    details = assignment.get("details") if isinstance(assignment.get("details"), str) else ""
    return title, details[:4000]


def match(assignment: dict, pages_or_store, *, limit: int = MAX_DECKS) -> list[dict]:
    """[{"source_id", "deck", "course_id", "pages": [int], "score", "label", "whole", "hits"}],
    best first. ``whole``: the assignment names this deck's lecture/week, so all its pages
    count; ``hits`` are the pages that also matched on words (the review pages)."""
    if not isinstance(assignment, dict):
        return []
    course_id = assignment.get("course_id") if isinstance(assignment.get("course_id"), str) else None
    if isinstance(pages_or_store, list):
        pages = [p for p in pages_or_store if isinstance(p, dict)]
    else:
        pages = pages_for(pages_or_store, course_id)
    if not pages:
        return []
    title, details = _assignment_text(assignment)
    wanted = refs(title) | refs(details)
    keys = words(title) | words(details)
    decks: dict[str, dict] = {}
    for row in pages:
        sid, number = row.get("source_id"), _page_number(row)
        if not isinstance(sid, str) or number is None:
            continue
        if course_id and row.get("course_id") and row.get("course_id") != course_id:
            continue
        deck = decks.setdefault(sid, {"source_id": sid, "deck": row.get("deck") or "",
                                      "course_id": row.get("course_id"), "all": [], "page_hits": {},
                                      "named": False})
        deck["all"].append(number)
        page_title = row.get("title") if isinstance(row.get("title"), str) else ""
        strong = words(page_title) | words(" ".join(_strings(row.get("topics"))))
        weak = words(" ".join(_strings(row.get("points")) + _strings(row.get("figure"))))
        score = sum(2 if k in strong else 1 if k in weak else 0 for k in keys)
        if wanted and refs(page_title) & wanted:
            score += NUMBER_SCORE / 2
        if score >= MIN_PAGE_SCORE:
            deck["page_hits"][number] = score
    out = []
    for deck in decks.values():
        named = bool(wanted and refs(deck["deck"]) & wanted)
        hits = sorted(set(deck["page_hits"]))
        if not named and not hits:
            continue
        score = (NUMBER_SCORE if named else 0.0) + sum(deck["page_hits"].values())
        pages_out = sorted(set(deck["all"]))[:DECK_PAGES] if named else hits[:DECK_PAGES]
        out.append({"source_id": deck["source_id"], "deck": deck["deck"], "course_id": deck["course_id"],
                    "pages": pages_out, "score": round(score, 2), "label": deck_label(deck["deck"]),
                    "whole": named, "hits": hits})
    out.sort(key=lambda d: (-d["score"], d["deck"].casefold(), d["source_id"]))
    return out[:limit]


def slides(matches: list[dict]) -> list[dict]:
    """The today object's "slides": [{"source_id", "deck", "pages"}]."""
    return [{"source_id": m["source_id"], "deck": m.get("deck") or "", "pages": list(m.get("pages") or [])}
            for m in matches or () if isinstance(m, dict) and m.get("source_id")]


def review_pages(matches: list[dict], cap: int = REVIEW_PAGES) -> list[dict]:
    """Up to ``cap`` [{"source_id", "page"}]: word-matched pages first, then whole decks in order."""
    out: list[dict] = []
    seen = set()
    for key in ("hits", "pages"):
        for m in matches or ():
            for page in m.get(key) or ():
                ref = (m["source_id"], page)
                if ref not in seen and len(out) < cap:
                    seen.add(ref)
                    out.append({"source_id": m["source_id"], "page": page})
    return out


def _short_deck(m: dict) -> str:
    if m.get("label"):
        return m["label"]
    deck = (m.get("deck") or "").strip() or "your slides"
    deck = re.sub(r"\.(pdf|pptx?|key)$", "", deck, flags=re.I)
    return deck if len(deck) <= 40 else deck[:39].rstrip() + "…"


def _page_span(pages: list[int]) -> str:
    pages = sorted(set(pages))
    if len(pages) > 1 and pages[-1] - pages[0] == len(pages) - 1:
        return f"{pages[0]}–{pages[-1]}"
    if len(pages) > 4:
        return ", ".join(str(p) for p in pages[:3]) + f" and {len(pages) - 3} more"
    return _join([str(p) for p in pages])


def deck_phrase(matches: list[dict]) -> str | None:
    """"Lectures 7 and 8" / "Week 5" / "Slides 4–9 of Lecture 7" / "Lecture 7 and Pointers review"."""
    usable = [m for m in matches or () if isinstance(m, dict) and m.get("pages")]
    if not usable:
        return None
    labels = [refs(m.get("deck") or "") for m in usable]
    whole = all(m.get("whole") for m in usable)
    if whole and all(labels) and len({f for lab in labels for f, _ in lab}) == 1:
        family = next(iter(labels[0]))[0]
        nums = sorted({n for lab in labels for _, n in lab})
        return _numbers_phrase(family, nums)
    if len(usable) == 1 and not usable[0].get("whole"):
        m = usable[0]
        word = "Slide" if len(set(m["pages"])) == 1 else "Slides"
        return f"{word} {_page_span(m['pages'])} of {_short_deck(m)}"
    return _join([_short_deck(m) for m in usable])


def covers_text(assignment: dict, matches: list[dict]) -> str | None:
    """"Lectures 7 and 8 cover it." / "Slides 4–9 of Lecture 7 cover it." / None."""
    phrase = deck_phrase(matches)
    if not phrase:
        return None
    plural = phrase.startswith(("Slides ", "Lectures ", "Weeks ")) or " and " in phrase
    return f"{phrase} {'cover' if plural else 'covers'} it."


def from_phrase(matches: list[dict]) -> str | None:
    """The phrase mid-sentence: "weeks 5 and 6", "lecture 7", "slides 4–9 of Lecture 7"."""
    phrase = deck_phrase(matches)
    if not phrase:
        return None
    if phrase.startswith(("Lecture", "Week", "Slide")):
        return phrase[0].lower() + phrase[1:]
    return phrase
