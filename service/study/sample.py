"""The built-in sample: four made-up courses, a believable week of work, and one study
material of each kind, so the Study tab can be tried before any school page is read.

Everything here is synthetic (no real person, school or course) and flagged
`sample`, with ids prefixed "sample:", so a real import or "Clear sample" removes
exactly this and nothing else. Due times are computed from `now`, so the sample
always looks like this week.
"""
from __future__ import annotations

from datetime import date, datetime, time as clock, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PY = "sample:py101"
STATS = "sample:stat120"
WRIT = "sample:writ2"
BIO = "sample:bio21"
LAB6 = "sample:py101:lab6"
QUIZ3 = "sample:py101:quiz3"

COURSES = [
    {"id": PY, "name": "Introduction to Programming in Python", "code": "CS 101",
     "grade": "A-", "score": 91.2},
    {"id": STATS, "name": "Elementary Statistics", "code": "STAT 120", "grade": "B+", "score": 88.4},
    {"id": WRIT, "name": "Rhetoric and Inquiry", "code": "WRIT 2", "grade": "A", "score": 94.0},
    {"id": BIO, "name": "Cells and Molecules", "code": "BIO 21", "grade": "B-", "score": 81.5},
]


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo("UTC")


def _assignments(now: float, zone) -> list[dict]:
    today = datetime.fromtimestamp(now, zone).date()

    def at(days: int, hour: int, minute: int = 0) -> float:
        day: date = today + timedelta(days=days)
        return datetime.combine(day, clock(hour, minute), zone).timestamp()

    # The second item due today stays ahead of `now` for most of the day.
    later = max(at(0, 17), (int(now // 3600) + 2) * 3600.0)
    later = min(later, at(0, 23, 30))

    rows = [
        (LAB6, PY, "Lab 6: Dictionaries and word counts", "assignment", at(0, 23, 59), 20.0, None, "upcoming",
         "Write word_count.py. Read a plain-text file, split it into words (lowercase, strip punctuation "
         "with str.strip and string.punctuation), and count each word with a dictionary. Print the ten "
         "most common words with their counts, one per line, most common first; break ties alphabetically. "
         "Use dict.get(word, 0) + 1 or collections.Counter, but explain in a comment which one you chose "
         "and why. Test it on the provided frankenstein_ch1.txt and include your output as a comment at "
         "the bottom. Submit the .py file only."),
        ("sample:writ2:resp4", WRIT, "Reading response 4: Revision as rethinking", "assignment", later, 10.0,
         None, "upcoming",
         "In 300 to 400 words, respond to this week's course-reader essay on revision. Pick one claim the "
         "author makes about how experienced writers revise, quote it, and say whether your own process "
         "fits it. End with one concrete change you will try on Essay 2."),
        ("sample:stat120:ps7", STATS, "Problem Set 7: Sampling distributions", "assignment", at(1, 9), 25.0,
         None, "upcoming",
         "Sections 7.1 to 7.3. Problems 7.4, 7.10, 7.18, 7.22 and 7.31. Show the standard error formula "
         "you used each time, and state whether the Central Limit Theorem applies before using a normal "
         "model. Problem 7.31 needs the class survey data on the course page."),
        ("sample:bio21:disc6", BIO, "Discussion: Should gene editing be used on embryos?", "discussion",
         at(2, 23, 59), 5.0, None, "upcoming",
         "Post a 150-word position with one cited source, then reply to two classmates by Sunday."),
        (QUIZ3, PY, "Quiz 3: Loops and lists", "quiz", at(3, 10), 15.0, None, "upcoming",
         "In class, 25 minutes, closed book. Covers chapters 4 and 5: for and while loops, range(), "
         "break and continue, list indexing and slicing, list methods (append, insert, pop, remove, sort) "
         "and the difference between sort() and sorted(). Expect to trace short programs and predict "
         "their output."),
        ("sample:writ2:read5", WRIT, "Read: Course reader, chapter 5 (counterarguments)", "reading",
         at(4, 9, 30), None, None, "upcoming",
         "Come to class ready to name the strongest objection to your Essay 2 thesis."),
        ("sample:bio21:mid2", BIO, "Midterm 2", "exam", at(6, 14), 100.0, None, "upcoming",
         "Covers enzymes, cellular respiration and photosynthesis (lectures 9 to 16). Bring a pencil and "
         "a basic calculator. A practice exam is posted under Files."),
        ("sample:py101:proj1", PY, "Project 1: Text adventure game", "project", at(9, 23, 59), 50.0, None,
         "upcoming",
         "Build a text adventure with at least five rooms stored in a dictionary of dictionaries. The player "
         "moves with north/south/east/west, can pick up items into a list, and wins by reaching the exit "
         "with the key. Include a README describing your map."),
        ("sample:bio21:lab4", BIO, "Lab report 4: Enzyme kinetics", "assignment", at(-2, 23, 59), 30.0, None,
         "missing",
         "Report the reaction rates you measured at five substrate concentrations, plot rate against "
         "concentration, and estimate Vmax and Km. Late reports lose 10% per day."),
        ("sample:writ2:essay2d", WRIT, "Essay 2: First draft", "assignment", at(-1, 23, 59), 20.0, None,
         "submitted", "A complete draft of 1,200 to 1,500 words for peer review."),
        ("sample:stat120:quiz5", STATS, "Quiz 5: Normal distributions", "quiz", at(-4, 10), 10.0, 9.0,
         "graded", "z-scores, the 68-95-99.7 rule and normal probabilities."),
    ]
    return [{"id": rid, "course_id": course, "title": title, "kind": kind, "due_ts": due, "all_day": False,
             "points": points, "score": score, "status": status, "details": details, "url": None,
             "source": "sample", "sample": True}
            for rid, course, title, kind, due, points, score, status, details in rows]


GUIDE = {
    "summary": "A dictionary maps keys to values, so you can look something up by name instead of by "
               "position. Counting things, like words in a text, is the classic use: each word is a key "
               "and its count is the value. This guide covers what Lab 6 and Quiz 3 lean on.",
    "sections": [
        {"heading": "Making and reading dictionaries",
         "points": ["Create one with braces: ages = {\"Ana\": 19, \"Ben\": 20}, or an empty one with {}.",
                    "Read a value with ages[\"Ana\"]; a missing key raises KeyError.",
                    "ages.get(\"Cy\", 0) returns 0 instead of raising when the key is missing.",
                    "Keys must be immutable (strings, numbers, tuples); values can be anything."]},
        {"heading": "Changing a dictionary",
         "points": ["Assigning to a key adds it or replaces its value: ages[\"Cy\"] = 21.",
                    "del ages[\"Ben\"] removes a key; ages.pop(\"Ben\") removes it and returns the value.",
                    "\"Ana\" in ages checks keys, not values, and is fast even for huge dictionaries."]},
        {"heading": "Looping over a dictionary",
         "points": ["for key in d: loops over keys, in the order they were inserted.",
                    "for key, value in d.items(): gives both at once.",
                    "Do not add or remove keys while looping over the same dictionary; loop over "
                    "list(d) instead."]},
        {"heading": "The counting pattern",
         "points": ["Start with counts = {} and, for each word, write counts[word] = counts.get(word, 0) + 1.",
                    "collections.Counter(words) does the same in one line and adds most_common(n).",
                    "To sort by count, most common first: sorted(counts.items(), key=lambda kv: -kv[1]).",
                    "For alphabetical ties, sort by a pair: key=lambda kv: (-kv[1], kv[0])."]},
    ],
    "key_terms": [
        {"term": "key", "meaning": "The label you look a value up by. Each key appears at most once."},
        {"term": "value", "meaning": "What a key maps to. Values can repeat."},
        {"term": "KeyError", "meaning": "The error raised by d[key] when key is not in the dictionary."},
        {"term": "items()", "meaning": "A view of (key, value) pairs, handy in for loops and sorting."},
        {"term": "Counter", "meaning": "A dictionary subclass from collections built for counting."},
    ],
    "practice": [
        "Write a loop that counts how many times each letter appears in \"mississippi\".",
        "Given scores = {\"Ana\": 88, \"Ben\": 92, \"Cy\": 75}, print the names from highest score to lowest.",
        "Explain why counts[word] += 1 fails on the first occurrence of a word, and two ways to fix it.",
    ],
}

QUIZ = {
    "questions": [
        {"prompt": "What does this print?\n\nnums = [3, 1, 2]\nnums.sort()\nprint(nums)",
         "choices": ["[3, 1, 2]", "[1, 2, 3]", "None", "[3, 2, 1]"], "answer": 1,
         "why": "sort() reorders the list in place, smallest first. The list itself is printed, not the "
                "return value of sort()."},
        {"prompt": "What does this print?\n\nnums = [3, 1, 2]\nresult = nums.sort()\nprint(result)",
         "choices": ["[1, 2, 3]", "[3, 1, 2]", "None", "An error"], "answer": 2,
         "why": "sort() changes the list in place and returns None. Use sorted(nums) when you want a new "
                "sorted list back."},
        {"prompt": "Which values does range(2, 10, 3) produce?",
         "choices": ["2, 5, 8", "2, 5, 8, 11", "3, 6, 9", "2, 4, 6, 8"], "answer": 0,
         "why": "range(start, stop, step) starts at 2 and adds 3 each time, stopping before 10: 2, 5, 8."},
        {"prompt": "letters = ['a', 'b', 'c', 'd', 'e']\nWhat is letters[1:4]?",
         "choices": ["['a', 'b', 'c', 'd']", "['b', 'c', 'd', 'e']", "['b', 'c', 'd']", "['b', 'c']"],
         "answer": 2,
         "why": "A slice includes the start index and stops before the end index, so indexes 1, 2 and 3."},
        {"prompt": "How many times does this loop print?\n\nfor i in range(5):\n    if i == 3:\n        break\n"
                   "    print(i)",
         "choices": ["2", "3", "4", "5"], "answer": 1,
         "why": "It prints 0, 1 and 2. When i reaches 3, break leaves the loop before print runs."},
        {"prompt": "What is in items after this runs?\n\nitems = [10, 20, 30]\nitems.insert(1, 15)\nitems.pop()",
         "choices": ["[10, 15, 20]", "[15, 20, 30]", "[10, 20, 30]", "[10, 15, 20, 30]"], "answer": 0,
         "why": "insert(1, 15) gives [10, 15, 20, 30]; pop() with no index removes the last item, 30."},
    ],
}

LESSON = {
    "steps": [
        {"title": "Why a dictionary?",
         "explain": "To count words you need to remember a number for every different word you have seen. "
                    "A list would make you search for the word each time. A dictionary lets you jump straight "
                    "to a word's count by using the word itself as the key.",
         "example": "counts = {\"the\": 3, \"cat\": 1}\nprint(counts[\"the\"])   # 3",
         "check": None},
        {"title": "Adding one to a count",
         "explain": "The first time you see a word it is not in the dictionary yet, so counts[word] += 1 "
                    "raises KeyError. counts.get(word, 0) returns 0 for a new word, so adding 1 works for "
                    "both new and old words.",
         "example": "counts = {}\nfor word in [\"to\", \"be\", \"or\", \"not\", \"to\", \"be\"]:\n"
                    "    counts[word] = counts.get(word, 0) + 1\nprint(counts)\n"
                    "# {'to': 2, 'be': 2, 'or': 1, 'not': 1}",
         "check": {"prompt": "What does counts.get(\"zebra\", 0) return when \"zebra\" is not a key?",
                   "choices": ["None", "0", "A KeyError", "\"zebra\""], "answer": 1,
                   "why": "The second argument to get() is the default returned for a missing key."}},
        {"title": "Cleaning the words first",
         "explain": "\"The\" and \"the,\" should count as the same word. Lowercase each word and strip "
                    "punctuation from both ends before counting. str.strip(chars) removes any of those "
                    "characters from the start and end only, not the middle.",
         "example": "import string\nraw = \"Hello, world! Hello.\"\n"
                    "words = [w.lower().strip(string.punctuation) for w in raw.split()]\n"
                    "print(words)   # ['hello', 'world', 'hello']",
         "check": {"prompt": "What is \"don't!\".strip(string.punctuation)?",
                   "choices": ["\"dont\"", "\"don't\"", "\"don\"", "\"don't!\""], "answer": 1,
                   "why": "strip only trims the ends, so the apostrophe in the middle stays and the ! goes."}},
        {"title": "Showing the most common words",
         "explain": "To list words from most to least common, sort the (word, count) pairs by count, "
                    "highest first. Sorting by the pair (-count, word) also breaks ties alphabetically, "
                    "which is exactly what Lab 6 asks for.",
         "example": "pairs = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))\n"
                    "for word, n in pairs[:10]:\n    print(word, n)",
         "check": {"prompt": "Why use -kv[1] instead of kv[1] in the sort key?",
                   "choices": ["To sort counts from highest to lowest", "To skip words with a count of 1",
                               "To sort words alphabetically", "To avoid a KeyError"],
                   "answer": 0,
                   "why": "sorted() goes smallest first, so negating the count puts the biggest counts first."}},
    ],
}

CARDS = {
    "cards": [
        {"front": "What does d.get(k, 0) return when k is missing?", "back": "0, the default, instead of raising KeyError."},
        {"front": "list.sort() vs sorted(list)", "back": "sort() changes the list in place and returns None; sorted() returns a new list."},
        {"front": "range(1, 6)", "back": "1, 2, 3, 4, 5 (the stop value is not included)."},
        {"front": "nums[-1]", "back": "The last item of the list."},
        {"front": "nums[2:5]", "back": "Items at indexes 2, 3 and 4: the slice stops before index 5."},
        {"front": "break vs continue", "back": "break leaves the loop; continue skips to the next pass."},
        {"front": "for k, v in d.items():", "back": "Loops over a dictionary's keys and values together."},
        {"front": "\"x\" in d checks…", "back": "Whether \"x\" is a key. Values are not searched."},
        {"front": "list.pop() with no index", "back": "Removes and returns the last item."},
        {"front": "Can a list be a dictionary key?", "back": "No. Keys must be immutable; use a tuple instead."},
    ],
}

MATERIALS = [
    ("sample:guide", "guide", "Dictionaries and counting: study guide", LAB6, GUIDE, 26 * 3600),
    ("sample:quiz", "quiz", "Quiz 3 practice: loops and lists", QUIZ3, QUIZ, 20 * 3600),
    ("sample:lesson", "lesson", "Counting words with a dictionary", LAB6, LESSON, 6 * 3600),
    ("sample:cards", "flashcards", "Python lists and dictionaries", QUIZ3, CARDS, 2 * 3600),
]


def load(store, *, now: float, timezone: str) -> None:
    """Replace any earlier sample with a fresh one dated around `now`. Real rows stay."""
    zone = _zone(timezone)
    store.clear_sample()
    store.upsert_courses([c | {"url": None, "source": "sample", "sample": True} for c in COURSES])
    store.upsert_assignments(_assignments(now, zone))
    for mid, kind, title, assignment, body, age in MATERIALS:
        store.save_material({"id": mid, "kind": kind, "title": title, "course_id": PY,
                             "assignment_id": assignment, "created_at": now - age, "model": "sample",
                             "sample": True, "body": body})
    # One earlier try, so the library shows what a best score looks like.
    store.record_attempt("sample:quiz", 4, 6)
