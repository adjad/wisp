"""Captured school-page text -> courses and assignments (docs/STUDY_TAB.md).

Pure: no I/O, no model, no network. The text is what macOS Accessibility (or an
innerText-style copy) gives for a browser tab: roughly document order, one text
run per line, sometimes several runs on one line, link text and static text
interleaved, navigation chrome mixed in. Every parser here is tolerant: it reads
what it recognises and ignores the rest. Page text is untrusted data and is only
ever matched against patterns, never interpreted.

Ids. A course whose numeric Canvas id is in the URL is ``canvas:<courseId>`` and
its assignments ``canvas:<courseId>:<sha1(title)[:12]>``. Pages that show only a
course NAME (the Dashboard list, the all-courses grades page) key the course as
``canvas:`` + sha1(normalised lower-case name)[:12] and assignments as
``canvas:`` + sha1(normalised name | title)[:12]. Limitation: the same course
can therefore appear twice in the store, once under its numeric id (from a
course page) and once under its name key (from the Dashboard). Every course and
assignment dict carries ``merge_key`` (normalised name, and name|title) so the
store or API can merge them later.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from service.study.course_names import merge_key

SOURCE = "canvas"


@dataclass
class ParsedPage:
    kind: str
    courses: list[dict] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- URLs

_LMS_SUFFIXES = (".instructure.com", ".schoology.com", ".powerschool.com", ".blackboard.com",
                 ".brightspace.com", ".d2l.com", ".edstem.org", ".gradescope.com")
_LMS_EXACT = {"classroom.google.com", "schoology.com", "powerschool.com", "blackboard.com",
              "brightspace.com", "d2l.com", "edstem.org", "gradescope.com"}


def _host(url: str) -> str | None:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    return parts.hostname.lower().rstrip(".")


def _is_canvas_host(host: str) -> bool:
    return host.endswith(".instructure.com") or "canvas" in host.split(".")


def _is_edu_host(host: str) -> bool:
    labels = host.split(".")
    if labels[-1] == "edu":
        return True
    # k12 hosts: <district>.k12.<state>.us
    return len(labels) >= 3 and labels[-1] == "us" and "k12" in labels[:-2]


def is_school_url(url: str) -> bool:
    host = _host(url)
    if not host:
        return False
    if _is_canvas_host(host) or _is_edu_host(host) or host in _LMS_EXACT:
        return True
    if host.endswith(_LMS_SUFFIXES):
        return True
    return any(label.startswith("moodle") for label in host.split("."))


# ---------------------------------------------------------------- text helpers

_WS = re.compile(r"\s+")


def _lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in (text or "").replace("\r", "\n").replace(" ", " ").split("\n"):
        line = _WS.sub(" ", raw).strip()
        if not line:
            continue
        if out and out[-1] == line:
            continue  # Accessibility often repeats a link's text as its own run
        out.append(line)
    return out


# The store's merge_key: one normalisation for the name hash and the store's merging.
_norm = merge_key


def _sha(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


_SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on",
          "or", "the", "to", "via", "vs", "with"}
# Short ordinary words that should not be kept upper-case like a course code.
_SHORT_WORDS = {"art", "arts", "life", "lab", "labs", "data", "web", "law", "film", "math", "home",
                "work", "game", "games", "word", "love", "self", "care", "food", "body", "mind",
                "city", "land", "sea", "war", "wars", "age", "era", "new", "old", "big", "deep",
                "high", "low", "open", "real", "rock", "jazz", "test", "unit", "part", "year",
                "one", "two", "four", "five", "oral", "read", "farm", "fish", "mass", "deaf",
                "play", "ways", "how", "why", "what", "who", "our", "your", "my", "its", "is",
                "are", "you", "we", "me", "us", "it", "not", "all", "any", "east", "west", "film",
                "song", "sex", "race", "sign", "cell", "gene", "star", "sun", "moon", "fire",
                "time", "space", "zen", "yoga", "dance", "drama", "poem", "poet", "myth", "gods",
                "god", "soul", "news", "ad", "ads", "ii", "iii", "iv"}
_ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii"}


def _title_word(word: str, first: bool) -> str:
    core = re.sub(r"[^A-Za-z]", "", word)
    if not core:
        return word
    low = core.lower()
    if low in _ROMAN:
        return word.upper()
    if any(ch.isdigit() for ch in word):
        return word  # HW2, 2-02, CS101
    if low in _SMALL and not first:
        return word.lower()
    if len(core) <= 4 and low not in _SHORT_WORDS and low not in _SMALL:
        return word  # CS, STAT, UNIV
    # Capitalise each hyphen/slash part.
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), word, count=0)


def display_name(name: str) -> str:
    """Title-case an upper-cased Canvas card label; leave mixed-case names alone."""
    name = _WS.sub(" ", name or "").strip()
    letters = [ch for ch in name if ch.isalpha()]
    if not letters or sum(ch.isupper() for ch in letters) < 0.8 * len(letters):
        return name
    words = name.split(" ")
    out = []
    after_colon = True
    for word in words:
        out.append(_title_word(word, after_colon))
        after_colon = word.endswith((":", "-", "–", "—"))
    return " ".join(out)


_CODE = re.compile(r"^([A-Z]{2,5})[\s\-]?(\d{1,3}[A-Z]?)(?:[\s\-]\d{2,3})?\b")


def _code(name: str) -> str | None:
    m = _CODE.match(name or "")
    return f"{m.group(1)} {m.group(2)}" if m else None


def _kind(title: str, label: str | None = None) -> str:
    low = (title or "").lower()
    if re.search(r"\b(exam|midterm|final(?!\s+(?:project|paper|essay|draft|report|presentation|portfolio)))\b", low):
        return "exam"
    if label == "quiz" or re.search(r"\b(quiz|test)\b", low):
        return "quiz"
    if label == "discussion" or re.search(r"\bdiscussion\b", low):
        return "discussion"
    if re.search(r"\b(project|essay|paper)\b", low):
        return "project"
    if re.search(r"\breading\b", low):
        return "reading"
    return "assignment"


# ---------------------------------------------------------------- dates

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
           "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
             r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_WEEKDAY_RE = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?"
_DATE = re.compile(r"(?:\b" + _WEEKDAY_RE + r"\s+)?\b" + _MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b"
                   r"(?:,?\s+(\d{4})\b)?", re.I)
_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?|\b(noon|midnight)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b",
                   re.I)
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _zone(timezone: str) -> ZoneInfo:
    return ZoneInfo(timezone)


def _nearest(month: int, day: int, today: date) -> date | None:
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def _parse_time(text: str) -> tuple[int, int] | None:
    m = _TIME.search(text or "")
    if not m:
        return None
    if m.group(4):
        return (12, 0) if m.group(4).lower() == "noon" else (23, 59)
    if m.group(5):
        return int(m.group(5)), int(m.group(6))
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 12 or minute > 59:
        return None
    if m.group(3).lower() == "p" and hour != 12:
        hour += 12
    if m.group(3).lower() == "a" and hour == 12:
        hour = 0
    return hour, minute


def _parse_day(text: str, today: date) -> tuple[date, re.Match] | None:
    m = _DATE.search(text or "")
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower()[:4].rstrip(".")) or _MONTHS.get(m.group(1).lower()[:3])
    day = int(m.group(2))
    if not month:
        return None
    if m.group(3):
        try:
            return date(int(m.group(3)), month, day), m
        except ValueError:
            return None
    found = _nearest(month, day, today)
    return (found, m) if found else None


def _at(day: date, hm: tuple[int, int] | None, zone: ZoneInfo) -> float:
    hour, minute = hm or (23, 59)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).timestamp()


def parse_due(text: str, *, now: float, timezone: str) -> float | None:
    """Epoch seconds for 'Nov 14 by 11:59pm', 'Mon Nov 16, 2026 11:59pm', 'November 16'."""
    zone = _zone(timezone)
    today = datetime.fromtimestamp(now, zone).date()
    found = _parse_day(text, today)
    if not found:
        return None
    day, m = found
    return _at(day, _parse_time(text[m.end():]), zone)


# ---------------------------------------------------------------- common pieces

_NAV = {"account", "dashboard", "courses", "calendar", "inbox", "history", "help", "groups", "studio",
        "commons", "skip to content", "home", "announcements", "modules", "grades", "people",
        "syllabus", "files", "pages", "assignments", "discussions", "quizzes", "zoom",
        "collaborations", "settings", "recent activity", "view grades", "start a new course",
        "global navigation", "admin", "outcomes", "rubrics", "chat", "conferences", "media gallery",
        "my media", "course navigation", "close", "menu", "search", "logout", "profile",
        "notifications", "portfolios", "course home", "student view"}
_NAV_WORDS = {w for item in _NAV for w in item.split()}

_GRADE_TOKEN = re.compile(r"^(?:(?P<letter1>[A-F][+-]?)\s*\(\s*)?(?P<pct>\d{1,3}(?:\.\d+)?)\s*%\s*\)?"
                          r"(?:\s*\(\s*(?P<letter2>[A-F][+-]?)\s*\))?$|^(?P<none>no grade|n/?a|--|-|ungraded)$",
                          re.I)


def _grade(line: str) -> tuple[bool, float | None, str | None]:
    m = _GRADE_TOKEN.match(line.strip())
    if not m:
        return False, None, None
    if m.group("none"):
        return True, None, None
    return True, float(m.group("pct")), m.group("letter1") or m.group("letter2")


def _is_nav(line: str) -> bool:
    low = line.lower()
    if low in _NAV:
        return True
    words = low.split()
    return len(words) >= 3 and all(w in _NAV_WORDS for w in words)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


class _Builder:
    def __init__(self, url: str, now: float, timezone: str):
        self.url, self.now, self.timezone = url, now, timezone
        self.zone = _zone(timezone)
        self.today = datetime.fromtimestamp(now, self.zone).date()
        self.courses: dict[str, dict] = {}
        self.assignments: dict[str, dict] = {}

    def course(self, name: str | None, *, canvas_id: str | None = None, score=None, grade=None) -> str | None:
        name = display_name(name or "") or None
        if canvas_id:
            cid = f"canvas:{canvas_id}"
            url = f"{_origin(self.url)}/courses/{canvas_id}"
        elif name:
            cid = "canvas:" + _sha(_norm(name))
            url = None
        else:
            return None
        row = self.courses.get(cid)
        if row is None:
            row = self.courses[cid] = {"id": cid, "name": name or "", "code": _code(name or ""),
                                       "grade": None, "score": None, "url": url, "source": SOURCE,
                                       "sample": False, "merge_key": _norm(name or "")}
        if name and not row["name"]:
            row["name"], row["code"], row["merge_key"] = name, _code(name), _norm(name)
        if score is not None:
            row["score"] = score
        if grade:
            row["grade"] = grade
        return cid

    def assignment(self, *, course_id: str | None, course_name: str | None, canvas_id: str | None,
                   title: str, kind: str, due_ts: float | None, points=None, score=None,
                   status: str = "upcoming", all_day: bool = False) -> None:
        title = _WS.sub(" ", title).strip()
        if not title or not course_id:
            return
        if canvas_id:
            aid = f"canvas:{canvas_id}:{_sha(title)}"
        else:
            aid = "canvas:" + _sha(f"{_norm(course_name or '')}|{title}")
        self.assignments[aid] = {
            "id": aid, "course_id": course_id, "title": title[:300], "kind": kind,
            "due_ts": due_ts, "all_day": all_day, "points": points, "score": score,
            "status": status, "details": "", "url": self.url, "source": SOURCE, "sample": False,
            "merge_key": f"{_norm(course_name or '')}|{_norm(title)}",
        }

    def page(self, kind: str) -> ParsedPage:
        return ParsedPage(kind, list(self.courses.values()), list(self.assignments.values()))


def _num(text: str) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


_STATUS_WORDS = {"missing": "missing", "late": "late", "submitted": "submitted", "graded": "graded",
                 "excused": "graded", "complete": "submitted", "completed": "submitted"}


# ---------------------------------------------------------------- 1. dashboard list view

_TYPE_LABEL = re.compile(r"^(?P<course>.+?)\s+(?P<type>ASSIGNMENT|QUIZ|DISCUSSION|ANNOUNCEMENT|"
                         r"CALENDAR EVENT|PAGE|TO DO|PEER REVIEW|NOTE)$", re.I)
_DAY_HEADER = re.compile(r"^(?:(today|tomorrow|yesterday)(?:,\s*(.+))?|(" + "|".join(_WEEKDAYS) + r"),\s*(.+))$",
                         re.I)
_POINTS = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:pts?|points?)\b", re.I)
_DUE_TIME = re.compile(r"\bdue:?\s*(.*)$", re.I)
_PLANNER_NOISE = re.compile(r"^(?:(?:show|hide) \d+ completed items?|load prior dates|jump to today|"
                            r"nothing (?:planned|more) .*|new activity|\+? ?new|.*is (?:not )?marked as "
                            r"(?:done|complete).*|.*\bmark as (?:done|complete).*|feedback|replies|"
                            r"new grades?|new feedback|\d+ new repl(?:y|ies)|all day)$", re.I)


def _day_header(line: str, today: date) -> date | None:
    m = _DAY_HEADER.match(line)
    if m:
        rel = (m.group(1) or "").lower()
        rest = m.group(2) or m.group(4)
        if rest:
            found = _parse_day(rest, today)
            if found:
                return found[0]
        if rel:
            return today + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[rel])
        return None
    # A bare "November 14" (Canvas puts it under "Today").
    found = _DATE.fullmatch(line)
    if found:
        hit = _parse_day(line, today)
        return hit[0] if hit else None
    return None


def _grades_panel(lines: list[str], b: _Builder) -> None:
    for i, line in enumerate(lines):
        if line.lower() not in ("my grades", "grades"):
            continue
        j = i + 1
        while j + 1 < len(lines):
            name, value = lines[j], lines[j + 1]
            if _is_nav(name) and name.lower() != "grades":
                j += 1
                continue
            ok, score, letter = _grade(value)
            if not ok or _grade(name)[0]:
                break
            b.course(name, score=score, grade=letter)
            j += 2


def _parse_dashboard(lines: list[str], b: _Builder) -> None:
    labels = []
    for line in lines:
        m = _TYPE_LABEL.match(line)
        if m and line.isupper():
            labels.append(_norm(m.group("course")))
    known = set(labels)
    _grades_panel(lines, b)
    known |= {c["merge_key"] for c in b.courses.values()}

    current_day: date | None = None
    item: dict | None = None

    def finish():
        nonlocal item
        if item and item.get("title") and item["type"] in ("assignment", "quiz", "discussion", "peer review"):
            label = {"quiz": "quiz", "discussion": "discussion"}.get(item["type"])
            due = None
            if item["day"] is not None:
                due = _at(item["day"], item.get("time"), b.zone)
            cid = b.course(item["course"])
            b.assignment(course_id=cid, course_name=display_name(item["course"]), canvas_id=None,
                         title=item["title"], kind=_kind(item["title"], label), due_ts=due,
                         points=item.get("points"), status=item.get("status", "upcoming"))
        item = None

    want_time = False
    for line in lines:
        header = _day_header(line, b.today)
        if header is not None:
            finish()
            current_day = header
            continue
        m = _TYPE_LABEL.match(line)
        if m and (line.isupper() or _norm(m.group("course")) in known):
            finish()
            item = {"course": m.group("course"), "type": m.group("type").lower(), "day": current_day}
            want_time = False
            continue
        if item is None or _is_nav(line) or _PLANNER_NOISE.match(line):
            continue
        if not item.get("title"):
            if _norm(line) in known or _grade(line)[0]:
                continue
            item["title"] = line
            continue
        low = line.lower()
        if low in _STATUS_WORDS:
            item["status"] = _STATUS_WORDS[low]
            continue
        p = _POINTS.search(line)
        if p and len(line) <= 40:
            item["points"] = float(p.group(1))
        d = _DUE_TIME.search(line)
        if d and len(line) <= 40:
            hm = _parse_time(d.group(1))
            if hm:
                item["time"] = hm
            else:
                want_time = True
            continue
        if want_time:
            hm = _parse_time(line)
            if hm:
                item["time"] = hm
            want_time = False
    finish()


# ---------------------------------------------------------------- 2. all-courses grades

_GRADES_HEADERS = {"course", "grade", "courses i'm taking", "courses i’m taking", "courses i'm observing",
                   "courses i’m observing", "grading period", "all grading periods", "student",
                   "show totals", "print grades"}


def _parse_all_grades(lines: list[str], b: _Builder) -> None:
    for i in range(len(lines) - 1):
        name, value = lines[i], lines[i + 1]
        if name.lower() in _GRADES_HEADERS or _is_nav(name) or _grade(name)[0]:
            continue
        ok, score, letter = _grade(value)
        if ok:
            b.course(name, score=score, grade=letter)


# ---------------------------------------------------------------- 3. one course's grades

_HEADER_CELLS = {"name", "due", "submitted", "status", "score", "out of", "details", "assignment",
                 "submitted date", "sort by", "arrange by", "due date", "title", "module", "assignment group"}
_TOTAL = re.compile(r"^total:?\s*(?P<pct>\d{1,3}(?:\.\d+)?)\s*%\s*(?:\(?\s*(?P<letter>[A-F][+-]?)\s*\)?)?$", re.I)
_SCORE_SR = re.compile(r"score:\s*(-|\d+(?:\.\d+)?)\s*out of\s*(\d+(?:\.\d+)?)", re.I)
_OUT_OF = re.compile(r"^/\s*(\d+(?:\.\d+)?)$")
_SCORE_PAIR = re.compile(r"^(-|\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?:\s*pts?)?$", re.I)
_GRADES_NOISE = re.compile(r"^(?:click to test a different score|instructor is working on grades|"
                           r"assignment details|preview|comments?|this assignment.*|toggle.*|"
                           r"what-if.*|show (?:all )?details|hide (?:all )?details|calculate.*|"
                           r"there are no comments.*|.*has been muted.*|not submitted|unsubmitted|"
                           r"no comments|rubric|show by.*|apply|revert.*|\d+ comments?|"
                           r"course:?.*|grades for .*|print grades|show what-if scores.*|"
                           r"total|weighted.*|assignments are weighted.*|group|weight)$", re.I)


# The part of a Canvas page title before ": " that names the page, not the course
# ("Assignments: Statistics", "Course Modules: Statistics", "Grades for <student>: Statistics").
_TITLE_HEADS = {"assignments", "grades", "syllabus", "modules", "course modules", "course home", "home",
                "announcements", "discussions", "discussion topics", "quizzes", "files", "pages", "people",
                "course syllabus", "course assignments", "course grades"}
# Titles that name a page and no course.
_BARE_TITLES = {"dashboard", "canvas", "canvas lms", "to do", "calendar", "inbox", "user dashboard"}


def _course_from_title(title: str, *, plain: bool = False) -> str | None:
    """The course name a Canvas page title carries. ``plain``: the title may be the bare
    course name (a course's home page is titled just "<Course name>")."""
    t = _WS.sub(" ", title or "").strip()
    for sep in (": ", " - "):
        if sep in t:
            head, tail = t.split(sep, 1)
            low = head.lower().strip()
            if (low in _TITLE_HEADS or low.startswith("grades for ")) and tail.strip():
                return tail.strip()
    low = t.lower()
    if plain and t and len(t) <= 200 and not _is_nav(t) and low not in _BARE_TITLES \
            and not low.startswith("grades for") and low.split(":")[0].strip() not in _TITLE_HEADS:
        return t
    return None


def _code_only(line: str) -> bool:
    """A bare course code ("CS-101-01", "STAT 101-02"): a breadcrumb's short name, not a course name."""
    m = _CODE.match(line or "")
    return bool(m) and not re.sub(r"[\s\-:.,()]+", "", line[m.end():])


def _course_from_text(lines: list[str], crumb: str | None) -> str | None:
    """The course name a course page's text shows: the grades page's "Course" picker, else the
    breadcrumb line just before the page's own crumb (``crumb``: "grades", "assignments")."""
    head = lines[:40]
    for i, line in enumerate(head):
        low = line.lower()
        cand = None
        if low == "course" and i + 1 < len(head):
            cand = head[i + 1]
        elif low.startswith("course:") and len(line) > 7:
            cand = line[7:].strip()
        if cand and not _is_nav(cand) and cand.lower() not in _HEADER_CELLS \
                and cand.lower() not in ("arrange by", "apply") and not _grade(cand)[0]:
            return cand
    # The crumb's first appearance. The breadcrumb comes before the course menu, which names
    # the page again; a lone "Grades" with a menu item after it is the menu itself, where the
    # line before may be a school's own tool, not the course.
    seen = [i for i, line in enumerate(lines[:80]) if i and line.lower() == crumb] if crumb else []
    if seen and seen[0] < len(head):
        i = seen[0]
        prev, nxt = lines[i - 1], (lines[i + 1] if i + 1 < len(lines) else "")
        if ((len(seen) > 1 or not (nxt and _is_nav(nxt))) and not _is_nav(prev) and not _code_only(prev)
                and not _DATE.search(prev) and not _grade(prev)[0] and len(prev) <= 120):
            return prev
    return None


def _course_name(title: str, lines: list[str], *, crumb: str | None = None, plain: bool = False) -> str | None:
    """A course page's course name: its title first, then its text. None when neither says one."""
    return _course_from_title(title, plain=plain) or _course_from_text(lines, crumb)


def _parse_course_grades(lines: list[str], b: _Builder, course_id: str, title: str) -> None:
    cname = _course_name(title, lines, crumb="grades")
    cid = b.course(cname, canvas_id=course_id)
    # Start after the table header when it is there.
    lows = [line.lower() for line in lines]
    start = next((i + 1 for i, low in enumerate(lows) if low == "out of"), None)
    if start is None:
        start = next((i + 1 for i, low in enumerate(lows) if low == "score" or low.startswith("name due")), 0)
    rows: list[dict] = []
    row: dict | None = None
    total_next = False
    for line in lines[start:]:
        low = line.lower()
        if total_next:
            total_next = False
            ok, score, letter = _grade(line)
            if ok:
                b.course(cname, canvas_id=course_id, score=score, grade=letter)
                break
        t = _TOTAL.match(line)
        if t:
            b.course(cname, canvas_id=course_id, score=float(t.group("pct")), grade=t.group("letter"))
            break  # the Total row ends the table; what follows is its points or page chrome
        if low in ("total", "total:"):
            total_next = True
            continue
        if low in _HEADER_CELLS or _is_nav(line) or _GRADES_NOISE.match(line):
            continue
        sr = _SCORE_SR.search(line)
        if sr and row is not None:
            row["score"] = _num(sr.group(1))
            row["points"] = _num(sr.group(2))
            row["has_score"] = True
            continue
        if row is not None:
            if low in _STATUS_WORDS:
                row["status"] = _STATUS_WORDS[low]
                continue
            pair = _SCORE_PAIR.match(line)
            if pair:
                row["score"], row["points"], row["has_score"] = _num(pair.group(1)), _num(pair.group(2)), True
                continue
            out = _OUT_OF.match(line)
            if out:
                row["points"] = _num(out.group(1))
                continue
            if line == "-" or re.fullmatch(r"\d+(?:\.\d+)?", line):
                if not row.get("has_score"):
                    row["score"], row["has_score"] = _num(line), True
                elif row.get("points") is None:
                    row["points"] = _num(line)
                continue
            if re.fullmatch(r"\d{1,3}(?:\.\d+)?\s*%", line):
                row["percent"] = True
                continue
            when = parse_due(line, now=b.now, timezone=b.timezone) if _DATE.search(line) else None
            if when is not None:
                if "due" not in row:
                    row["due"] = when
                else:
                    row["submitted_at"] = when
                continue
            if not row.get("group") and len(row) == 1:
                row["group"] = line
                continue
        row = {"title": line}
        rows.append(row)
    for r in rows:
        if r.get("percent"):
            continue  # an assignment-group total row ("Homework 90%")
        if not r.get("has_score") and "due" not in r and "status" not in r and r.get("points") is None:
            continue  # a stray line, not an assignment row
        status = r.get("status")
        if status is None:
            if r.get("score") is not None:
                status = "graded"
            elif r.get("submitted_at") is not None:
                status = "submitted"
            else:
                status = "upcoming"
        b.assignment(course_id=cid, course_name=cname, canvas_id=course_id, title=r["title"],
                     kind=_kind(r["title"]), due_ts=r.get("due"), points=r.get("points"),
                     score=r.get("score"), status=status)


# ---------------------------------------------------------------- 4. assignments list

_ASSIGN_GROUPS = re.compile(r"^(upcoming|overdue|past|undated|recent|future|later) assignments$", re.I)
_ASSIGN_NOISE = re.compile(r"^(?:assignment|quiz|discussion(?: topic)?|show by (?:date|type)|"
                           r"search for assignment.*|search|closed|no due date|multiple due dates|"
                           r"module:?.*|not available until.*|available until.*|available|"
                           r"\d+(?:\.\d+)?% of total|assignment groups?|weighted|"
                           r"expand|collapse|published|unpublished|\d+ items?|"
                           r"assignments are weighted by group.*|"
                           r"[a-z ]+ \d+(?:\.\d+)?% of total)$", re.I)
_TYPE_LINE = {"quiz": "quiz", "discussion": "discussion", "discussion topic": "discussion"}


def _parse_assignments(lines: list[str], b: _Builder, course_id: str, title: str) -> None:
    cname = _course_name(title, lines, crumb="assignments")
    cid = b.course(cname, canvas_id=course_id)
    items: list[dict] = []
    item: dict | None = None
    section = ""
    pending_type: str | None = None
    for line in lines:
        low = line.lower()
        g = _ASSIGN_GROUPS.match(line)
        if g:
            section = g.group(1).lower()
            item = None
            continue
        if low in _TYPE_LINE:
            if item is not None and not item.get("fields"):
                item["label"] = _TYPE_LINE[low]
            else:
                pending_type = _TYPE_LINE[low]
            continue
        if _is_nav(line) or _ASSIGN_NOISE.match(line):
            continue
        if item is not None:
            if low in _STATUS_WORDS:
                item["status"] = _STATUS_WORDS[low]
                item["fields"] = True
                continue
            pair = _SCORE_PAIR.match(line)
            if pair:
                item["score"], item["points"] = _num(pair.group(1)), _num(pair.group(2))
                item["fields"] = True
                continue
            pts = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:pts?|points?)", line, re.I)
            if pts:
                item["points"] = _num(pts.group(1))
                item["fields"] = True
                continue
            if low in ("due", "due:"):
                item["want_due"] = True
                continue
            due = re.match(r"^due:?\s*(.+)$", line, re.I)
            text = due.group(1) if due else (line if item.get("want_due") or len(line) <= 40 else "")
            when = parse_due(text, now=b.now, timezone=b.timezone) if text and _DATE.search(text) else None
            if when is not None:
                item["due"], item["fields"], item["want_due"] = when, True, False
                continue
        item = {"title": line, "section": section, "label": pending_type}
        pending_type = None
        items.append(item)
    for it in items:
        if not it.get("fields"):
            continue  # a heading or stray text, not an assignment
        status = it.get("status")
        if status is None:
            status = "graded" if it.get("score") is not None else "upcoming"
        b.assignment(course_id=cid, course_name=cname, canvas_id=course_id, title=it["title"],
                     kind=_kind(it["title"], it.get("label")), due_ts=it.get("due"),
                     points=it.get("points"), score=it.get("score"), status=status)


# ---------------------------------------------------------------- 5. To Do / Coming Up / agenda

_TODO_HEADERS = re.compile(r"^(?:to do|coming up|upcoming|recent feedback|view calendar|"
                           r"show all|nothing for now|agenda)$", re.I)


def _parse_todo(lines: list[str], b: _Builder, course_id: str | None, title: str,
               home: bool = False) -> None:
    # A course's home page is titled with the bare course name; other pages are not.
    cname = _course_name(title, lines, plain=home) if course_id else _course_from_title(title)
    default_cid = b.course(cname, canvas_id=course_id) if course_id else None
    for i, line in enumerate(lines):
        if not _DATE.search(line):
            continue
        due = parse_due(line, now=b.now, timezone=b.timezone)
        if due is None:
            continue
        # The title is the nearest preceding non-meta line; a course name may sit between.
        points = None
        p = _POINTS.search(line)
        if p:
            points = float(p.group(1))
        back = [l for l in lines[max(0, i - 3):i]]
        candidates = []
        for prev in reversed(back):
            if (_DATE.search(prev) or _is_nav(prev) or _TODO_HEADERS.match(prev)
                    or re.fullmatch(r"\d+(?:\.\d+)?\s*(?:pts?|points?)", prev, re.I)):
                if candidates:
                    break
                p2 = _POINTS.search(prev)
                if p2 and points is None:
                    points = float(p2.group(1))
                continue
            candidates.append(prev)
            if len(candidates) == 2:
                break
        if not candidates:
            continue
        candidates.reverse()
        item_title = candidates[-1]
        course_name = cname
        if len(candidates) == 2 and not course_id:
            # "Turn in Lab 5" / "CS 101" (title, then course) on a dashboard To Do.
            item_title, course_name = candidates[0], candidates[1]
        item_title = re.sub(r"^turn in\s+", "", item_title, flags=re.I)
        if len(item_title) < 2 or len(item_title) > 200:
            continue
        cid = default_cid if course_id else b.course(course_name)
        if cid is None:
            continue
        b.assignment(course_id=cid, course_name=course_name if not course_id else cname,
                     canvas_id=course_id, title=item_title, kind=_kind(item_title), due_ts=due,
                     points=points)


# ---------------------------------------------------------------- dispatch

_COURSE_PATH = re.compile(r"^/courses/(\d+)(/.*)?$")


def parse_page(url: str, title: str, text: str, *, now: float, timezone: str) -> ParsedPage:
    host = _host(url)
    if not host or not (_is_canvas_host(host) or _is_edu_host(host)):
        return ParsedPage("other", [], [])
    sure = _is_canvas_host(host)
    path = (urlsplit(url).path or "/").rstrip("/") or "/"
    title = _WS.sub(" ", title or "").strip()
    lines = _lines(text)
    joined = "\n".join(lines).lower()
    b = _Builder(url, now, timezone)
    m = _COURSE_PATH.match(path)
    kind = "other"
    if path == "/grades":
        kind = "grades"
        _parse_all_grades(lines, b)
    elif m and (m.group(2) == "/grades" or title.lower().startswith("grades for")):
        kind = "course_grades"
        _parse_course_grades(lines, b, m.group(1), title)
    elif m and m.group(2) == "/assignments":
        kind = "assignments"
        _parse_assignments(lines, b, m.group(1), title)
    elif path in ("/", "/dashboard") and (title.lower().startswith("dashboard")
                                           or _TYPE_LABEL.search(joined.upper()) or "my grades" in joined):
        kind = "dashboard"
        _parse_dashboard(lines, b)
        if not b.assignments and ("to do" in joined or "coming up" in joined):
            _parse_todo(lines, b, None, title)
    elif path.startswith("/calendar") or (m and m.group(2) in (None, "", "/home")) or \
            "coming up" in joined or "\nto do\n" in f"\n{joined}\n":
        kind = "calendar"
        _parse_todo(lines, b, m.group(1) if m else None, title,
                    home=bool(m and m.group(2) in (None, "", "/home")))
    if kind == "other":
        return ParsedPage("other", [], [])
    page = b.page(kind)
    if not sure and not page.courses and not page.assignments:
        return ParsedPage("other", [], [])
    return page
