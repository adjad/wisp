# Study tab

Study is the notch's fifth tab. It reads the school pages you have open in your
browser (Canvas first), keeps your courses, grades and assignments on this Mac,
shows what is due in the Brief and Day tabs, and makes study guides, practice
quizzes, short lessons and flash cards from your real coursework.

This file is also the build contract: the shapes below are what the backend
serves and what the app decodes.

## Privacy rules

- School data is stored only in `MOE_DIR/study/study.db` (0600). `MOE_DIR` is
  `service.paths.moe_dir()` read at call time (so `WISP_HOME` is honoured), through
  `store.study_dir()`, the one source for the store, `day_hook` and the connector's
  `settings.json`.
- Zero footprint: reading never creates anything. `GET /study/state`, the Day hook and the
  connector status on a Mac that never used Study return an empty state and leave no
  `study/` folder; only a route that saves something (import with a recognised page,
  sample, generate, connector choice, Drive add) creates it.
- Wisp only reads school pages. It never submits, posts, clicks or changes
  anything on a school site, and never types a password.
- Page text is untrusted data. It is never treated as instructions, and it is
  passed to a model only inside a clearly delimited data block.
- Sources (Google Drive Docs/Slides the student adds, and school pages saved by a
  long-context import) live in the same `study.db`. Their text is untrusted data too: stored
  as-is, never executed or followed, and sent only to the Study model inside the data block.
  Only the Drive file the student picks is read. "Clear" drops sources with everything else.
- Study material is written by the **Study connector**: a cloud model reached
  with a key that is used for Study and nothing else. Nothing else in Wisp uses
  that key, and Study never uses the local chat model's cloud settings, with one
  exception: on a Mac with no local engine (the Intel build, already all-cloud),
  Study falls back to the existing cloud connection when no Study key is set.
- The key lives in the macOS Keychain: generic password, service
  `com.wisp.study`, account = provider id (`openrouter` or `gemini`). It never
  crosses HTTP, is never logged, and is never written to a file.

## Providers

| id | label | origin | API prefix | default model |
| --- | --- | --- | --- | --- |
| `openrouter` | OpenRouter | `https://openrouter.ai` | `/api/v1` | `google/gemini-3.8-flash` |
| `gemini` | Gemini API | `https://generativelanguage.googleapis.com` | `/v1beta/openai` | `gemini-3.8-flash` |

Both are OpenAI-compatible `POST {origin}{prefix}/chat/completions` with
`Authorization: Bearer <key>`, `response_format: {"type": "json_object"}` and
`reasoning_effort: "low"`. Only these two exact origins are ever contacted.

Keychain reads never run on the event loop: `complete_json` reads the key through
`asyncio.to_thread`, and async routes use `status_async()`. A read that times out or is
refused (macOS asked and nobody answered, or Deny) is held for 60 s, so a pending prompt is
never re-triggered per request; meanwhile calls fail `missing` with "Wisp could not read the
Study key from your Keychain. If macOS asked, choose Always Allow, or save the key again."
Choosing a connector (`set_choice`, which the app calls after saving a key) drops the hold.

Intel fallback: only when `remote_policy.remote_only_profile()` is set, Study asks
`remote_default_target("general")` (a role in `generation_roles` on main and on the Intel
branch; else the profile's first role) and uses it only when its origin and prefix are one of
the two rows above. Its key comes from the endpoint's own `api_key()`
(`provider_credentials.resolve_keychain`), in a thread. Any exception means `source: "none"`.

### Cost bounds

| bound | value |
| --- | --- |
| POSTs per `complete_json` call | 3: the first, one retry (bad JSON, 5xx, timeout), one 400 retry without `reasoning_effort` / `response_format` |
| POSTs per `/study/generate` | 3 in all (`generate.MAX_POSTS`), across `make`'s own retry and the connector's |
| time per `/study/generate` | 100 s, then `unreachable` (502) |
| pages per import read by the model | 3 (`api.MODEL_PAGES`); the rest are reported `recognized: false` |
| POSTs per import | 6 (`api.IMPORT_POSTS`) across those pages |
| model time per import | 75 s; pages left after it are reported `recognized: false` |
| stop reading pages | after the first `missing`, `rejected`, `busy` or `unreachable` (a `bad_reply` page does not stop the next) |

The app's own request timeout is 120 s, so the server always stops first.

### Long context

`connector.long_context(model_id)` is true for a Gemini model: bare `gemini-…` (Gemini API)
or `google/gemini-…` (OpenRouter). The state's `connector.long_context` is that test applied
to `active_model_id` (false when not ready). Only then do these bounds replace the ones above;
every other model keeps the table above exactly (same limits, same sequential flow, no page
sources saved).

| bound | long context | ordinary |
| --- | --- | --- |
| pages per import read by the model | 12 (`api.LONG_MODEL_PAGES` = `MAX_PAGES`), 4 at a time (`LONG_CONCURRENCY`); results keep the request's order | 3, one after another |
| POSTs per import | 24 (`api.LONG_IMPORT_POSTS`), one budget shared by the concurrent reads | 6 |
| model time per import | 100 s (`api.LONG_IMPORT_MODEL_S`) | 75 s |
| page text read per page | 200k chars (`generate.LONG_EXTRACT_CHARS`; the app sends at most 150k) | 24k (`EXTRACT_CHARS`) |
| stop reading | after a `missing` / `rejected` / `busy` / `unreachable` or the deadline, no new page starts (up to 4 already running finish or fail) | as above |
| data block per material | 600k chars (`generate.LONG_CONTEXT_CHARS`) | 12k (`CONTEXT_CHARS`) |
| whole-course assignment text | 100k chars (`api.WHOLE_COURSE_CONTEXT`) | 6k (`CONTEXT_MAX`) |
| POSTs / time per `/study/generate` | unchanged: 3 POSTs, 100 s | 3 POSTs, 100 s |

Worst-case cost: one long-context import is at most 24 paid requests carrying up to ~200k
chars of page text each; one long-context material is at most 3 requests carrying up to
~600k chars (about 150k tokens) each. A source is stored up to 400k chars (`store.SOURCE_CHARS`).
Source text is cleaned with at most 8 tag-stripping passes (`generate.SOURCE_PASSES`); text
still changing after that loses every bracket look-alike, so a hostile file of nested fake
tags cannot hold a worker. Prompt building with sources runs on a worker thread.

## Backend (`service/study/`)

| file | owns |
| --- | --- |
| `store.py` | sqlite store: courses, assignments, materials, attempts, meta, sources |
| `canvas_parse.py` | pure: captured page text -> courses and assignments |
| `plan.py` | pure: what is due, study-block suggestions |
| `day_hook.py` | `attach(result, now=, timezone=)` for `/assistant/today` |
| `connector.py` | Study connector: choice, key read, JSON completion |
| `generate.py` | prompts + strict validation for the four material kinds, and model-assisted page extraction |
| `sample.py` | built-in sample courses, assignments and one material of each kind |
| `api.py` | FastAPI router, prefix `/study`; exports `Refusal`, `_answer`, `_state` for `drive_api.py` |
| `drive.py`, `drive_api.py` | Google Drive list/export through the existing read-only Google sign-in; router `/study/drive` |

### Python interfaces (exact)

```python
# store.py
class StudyStore:
    def __init__(self, path: Path): ...                       # .path
    def upsert_courses(self, courses: list[dict]) -> int      # by id
    def upsert_assignments(self, items: list[dict]) -> int    # by id
    def import_rows(self, courses: list[dict], assignments: list[dict]) -> tuple[int, int]
    #   one import: courses converge by merge_key (normalised name, also without a leading course
    #   code), the numeric Canvas id winning; assignments by (course, normalised title)
    def courses(self) -> list[dict]
    def assignments(self) -> list[dict]                       # every row, due_ts ascending, nulls last
    def save_material(self, material: dict) -> dict           # assigns id + created_at when missing
    def material(self, material_id: str) -> dict | None       # full, with body
    def materials(self) -> list[dict]                         # summaries, newest first
    def delete_material(self, material_id: str) -> bool
    def record_attempt(self, material_id: str, correct: int, total: int) -> float | None  # best score 0..1
    def meta(self, key: str, default=None); def set_meta(self, key: str, value) -> None
    def has_sample(self) -> bool
    def save_source(self, source: dict) -> dict               # {"kind","url","title","text","course_id"} ->
    #   the summary (no text); assigns id ("src_" + 12 hex) and created_at. The same (kind, url) again
    #   replaces text/title/course, refreshes created_at and keeps the id. Text cut to SOURCE_CHARS.
    def sources(self, course_id: str | None = None) -> list[dict]   # summaries, newest first; None = all
    def source(self, source_id: str) -> dict | None           # the summary plus "text"
    def delete_source(self, source_id: str) -> bool
    def clear_sample(self) -> None                            # only sample rows and their materials (sources stay)
    def clear(self) -> None                                   # everything, sources included
SOURCE_CHARS = 400_000
def study_dir() -> Path; def study_path() -> Path             # MOE_DIR/study, .../study.db
def study_exists() -> bool                                    # never creates anything
def study_store() -> StudyStore   # lazy singleton at study_path(); creates the file (savers only)

# canvas_parse.py  (no I/O, no model)
@dataclass
class ParsedPage: kind: str; courses: list[dict]; assignments: list[dict]
#   kind: "grades" | "course_grades" | "assignments" | "dashboard" | "calendar" | "other"
def parse_page(url: str, title: str, text: str, *, now: float, timezone: str) -> ParsedPage
def is_school_url(url: str) -> bool

# plan.py  (pure)
def day_summary(assignments: list[dict], *, now: float, timezone: str) -> dict   # the "study" object below
def study_blocks(assignments: list[dict], busy: list[tuple[float, float]], *, now: float, timezone: str,
                 day: str) -> list[dict]                                          # Day `blocks` entries
def deadlines(assignments: list[dict], *, day: str, timezone: str) -> list[dict]  # Day `deadlines` entries

# day_hook.py
def attach(result: dict, *, now: float, timezone: str) -> None   # never raises

# connector.py
class StudyConnectorError(Exception): code: str; message: str
#   codes: "missing" (no key, or the Keychain did not hand it over), "rejected" (401/403),
#          "busy" (429), "unreachable", "bad_reply"
def status() -> dict                       # the "connector" object below; never returns the key (blocking)
def long_context(model_id: str | None) -> bool   # "gemini-…" or "google/gemini-…"
async def status_async() -> dict           # status() on a worker thread, for async routes
def set_choice(provider: str, model_id: str) -> dict
def post_budget(posts: int)                # context manager: caps POSTs of everything inside
async def complete_json(system: str, user: str, *, max_tokens: int = 6000, key_reader=None, transport=None) -> dict

# generate.py
KINDS = ("guide", "quiz", "lesson", "flashcards")
CONTEXT_CHARS = 12_000; LONG_CONTEXT_CHARS = 600_000; EXTRACT_CHARS = 24_000; LONG_EXTRACT_CHARS = 200_000
async def make(kind: str, *, course: dict | None, assignment: dict | None, topic: str | None,
               count: int | None, context: str = "", complete=None,
               sources: list[dict] | None = None, long_context: bool = False) -> dict   # {"title": str, "body": {...}}
#   sources: rows with "title" and "text"; they share the data block, capped at LONG_CONTEXT_CHARS
#   when long_context else CONTEXT_CHARS. No sources and no long_context: the prompt is unchanged.
def validate(kind: str, body: dict) -> dict        # raises ValueError; returns the cleaned body
async def extract_school_data(url: str, title: str, text: str, *, now: float, timezone: str,
                              complete=None, long_context: bool = False) -> ParsedPage
```

### Stored rows

```jsonc
// course
{"id": "canvas:12345", "name": "Introduction to Programming", "code": "CS 101",
 "grade": "B+", "score": 87.5, "url": "https://…/courses/12345", "source": "canvas", "sample": false}
// assignment
{"id": "canvas:12345:678", "course_id": "canvas:12345", "title": "Lab 5: Lists",
 "kind": "assignment",            // assignment | quiz | exam | project | discussion | reading
 "due_ts": 1794700740.0,          // epoch seconds, or null
 "all_day": false,                // true when the page gave only a date: due_ts is 23:59 local
 "points": 20.0, "score": null,   // score in points, or null
 "status": "upcoming",            // upcoming | submitted | graded | missing | late
 "details": "",                   // description text when the page had one (<= 4000 chars)
 "url": "https://…", "source": "canvas", "sample": false}
```

```jsonc
// source (table `sources`; unique on (kind, url))
{"id": "src_0a1b2c3d4e5f", "course_id": "canvas:12345",   // or null: not in a course
 "kind": "drive",                 // drive (a Google Doc or Slides file) | page (a school page)
 "title": "Unit 3 notes", "url": "https://docs.google.com/document/d/…",
 "chars": 48210,                  // length of the saved text (<= 400k)
 "created_at": 1794650000.0}      // summaries carry these keys; store.source() adds "text"
```

A merged course carries its sources (`import_rows` moves them to the surviving id). A
study.db from before sources opens fine (`CREATE TABLE IF NOT EXISTS`).

Ids are stable across imports so a re-read updates rows. When a page gives no
numeric ids, the id is `"<source>:" + sha1(course name | title)[:12]`.

Merging: a field a page does not show (null, missing or blank) never blanks a saved one.
Parsers fill `status: "upcoming"` when a page shows none, so an incoming `"upcoming"` is
"no information" whenever the stored status is anything else (submitted, graded, missing,
late), except when the incoming `due_ts` is later than the stored one (the deadline was
extended). The same rule holds when duplicate rows are folded into one.

### HTTP API (`/study`)

All mutating routes are native-only (`application/json`, no `Origin`). The guard answers
FastAPI's shape, not the Study one: 403 `{"detail": "Change this from Wisp's notch."}` with
an `Origin`, 415 `{"detail": "Content-Type must be application/json."}` otherwise.

`GET /study/state`

```jsonc
{
  "connector": {
    "provider": "openrouter", "model_id": "google/gemini-3.8-flash",   // the Study choice
    "ready": true,
    "source": "study",            // study | cloud (Intel fallback) | none
    "active_model_id": "google/gemini-3.8-flash",   // what a call would use now (the cloud
                                  //   profile's on the fallback), or null when not ready
    "long_context": true,         // active_model_id is a Gemini model (see Long context); false when not ready
    "providers": [{"id": "openrouter", "label": "OpenRouter", "default_model": "google/gemini-3.8-flash",
                   "models": [{"id": "google/gemini-3.8-flash", "label": "Gemini 3.8 Flash"}],
                   "key_url": "https://openrouter.ai/keys", "key_hint": "sk-or-…"}]
  },
  "sample": false,                // true while the built-in sample is loaded
  "last_import": 1794650000.0,    // or null
  "courses": [{"id", "name", "code", "grade", "score", "url", "due_count": 2, "active": true, "sample"}],
  //   active: something due this week, or a grade. Sorted: due first, then active, then the rest
  //   (orientation modules and the like); by name inside each group
  "assignments": [{"id", "course_id", "course_name", "title", "kind", "due_ts", "all_day",
                   "points", "score", "status", "url", "sample"}],
  //   assignments: not yet due, or missing/late within the last 14 days; due_ts ascending, nulls last
  "materials": [{"id", "kind", "title", "course_id", "assignment_id", "created_at",
                 "count": 8, "best_score": 0.75, "sample": false}]
  //   count: questions / cards / steps / sections
  "sources": [{"id", "course_id", "kind", "title", "url", "chars", "created_at"}]
  //   every source, newest first (no text). An older service sends no "sources": decode as [].
}
```

| route | body | returns |
| --- | --- | --- |
| `POST /study/import` | `{"pages": [{"url", "title", "text"}], "timezone"?}` (<= 12 pages, text <= 150k chars each, body <= 6 MB) | `{"courses": n, "assignments": n, "pages": [{"url", "kind", "recognized": bool}], "state": <state>}` |
| `POST /study/connector` | `{"provider", "model_id"}` | `<state>.connector` |
| `POST /study/generate` | `{"kind", "course_id"?, "assignment_id"?, "topic"?, "count"?, "source_ids"?: [str] (<= 20), "whole_course"?: bool}` | full material |
| `DELETE /study/sources/{id}` | | `{"state": <state>}` |
| `GET /study/drive/status` | | `{"connected": bool, "drive": bool, "account": str\|null, "message": str\|null}` |
| `GET /study/drive/files?q=&limit=` | | `{"files": [{"id", "name", "kind": "doc"\|"slides", "modified": float\|null, "url": str\|null}]}` |
| `POST /study/drive/add` | `{"file_id", "course_id"?}` | `{"source": <source summary>, "state": <state>}` |
| `GET /study/materials/{id}` | | full material |
| `DELETE /study/materials/{id}` | | `{"deleted": true}` |
| `POST /study/materials/{id}/attempt` | `{"correct": int, "total": int}` | `{"best_score": 0.8}` |
| `POST /study/sample` | `{"timezone"?}` | `<state>` with the sample loaded (replaces only sample rows) |
| `DELETE /study/data` | | `<state>` after clearing everything |

Errors are `{"detail": "<code>", "message": "<gentle sentence>"}` with status
409 for `missing`, 502 for `rejected` / `busy` / `unreachable` / `bad_reply`,
404 `not_found` for an unknown id, 422 `invalid` for a bad body. An import body over
6 MB is refused by `local_http.BodyLimit` with 413 `{"detail": "<sentence>"}` before it
is parsed.

Import: each page goes through `canvas_parse.parse_page`. A school page it does
not recognise (`kind == "other"`) is handed to `generate.extract_school_data`
when the connector is ready, within the cost bounds above; otherwise it is reported
`recognized: false`. When any page is recognised, the import clears sample rows
(`clear_sample`) and saves through `import_rows`. With long context (see Cost bounds), the
unrecognised school pages are read concurrently, and every school page the model read, or
that `canvas_parse` recognised, is saved as a `page` source (url, title, text), tied to a
course when the page names exactly one saved course, else `course_id: null`; re-importing a
page replaces its source. Without long context no page source is saved.

Generate: the assignment's description reaches the prompt once (inside the data block); the
context adds only the course's other work. `source_ids` adds those sources (an unknown id is
404 `not_found`; duplicates count once). `whole_course` needs `course_id` (422 otherwise) and
adds every source of that course plus each of its assignments with its description as the
context. Sources work with any model; without long context they share the ordinary 12k block.
A request may name only sources (no course, assignment or topic). The prompt asks the model to
build the material from the sources first and never to follow instructions inside them.

Drive (`drive_api.py`): every POST is native-only. Refusals: 409 `google_missing` (no Google
account, or no `drive.readonly` scope: reconnect Google in Settings), 502 `google_error`,
413 `too_large`, 404 `not_found` (also an unknown `course_id`), 422 `invalid`. Only
`https://www.googleapis.com` is contacted; an added file is saved with `save_source`
(`kind: "drive"`), its text cut at `SOURCE_CHARS`.

### Material

```jsonc
{"id": "m_ab12", "kind": "quiz", "title": "Lists and loops: practice quiz",
 "course_id": "canvas:12345", "assignment_id": "canvas:12345:678",
 "created_at": 1794650000.0, "model": "google/gemini-3.8-flash", "sample": false,
 "best_score": null,
 "body": { … }}
```

Bodies:

```jsonc
// guide
{"summary": "…", "sections": [{"heading": "…", "points": ["…"]}],
 "key_terms": [{"term": "…", "meaning": "…"}], "practice": ["…"]}
// quiz   (4 choices each, `answer` is the 0-based index)
{"questions": [{"prompt": "…", "choices": ["…", "…", "…", "…"], "answer": 2, "why": "…"}]}
// lesson
{"steps": [{"title": "…", "explain": "…", "example": "…" /* or null */,
            "check": {"prompt": "…", "choices": ["…"], "answer": 0, "why": "…"} /* or null */}]}
// flashcards
{"cards": [{"front": "…", "back": "…"}]}
```

Prompts: every school-derived string (course, assignment, details, topic, page address,
title and text) sits inside the one `<school_data>` block; outside it are only Wisp's own
instructions, the date and the kind/count. Before it goes in, text is NFKC-folded, format
characters are dropped and any `school_data`-like tag (any case, attributes, spacing,
`&lt;` entities, look-alike brackets, or left unclosed) is removed.

Limits enforced by `validate`: guide 2–8 sections; quiz 3–15 questions with 2–5
choices; lesson 2–8 steps; flashcards 4–30 cards; every string trimmed and
capped (prompt 600, choice 300, explain 2000).

### Brief and Day (`/assistant/today`)

`day_hook.attach` adds, without touching the saved plan:

```jsonc
"study": {
  "due_today": [{"id", "title", "course_name", "due_ts", "all_day", "kind", "status"}],
  "due_tomorrow_count": 1,
  "due_week_count": 4,
  "missing_count": 0,
  "next_test": {"id", "title", "course_name", "due_ts"},   // next quiz/exam within 7 days, or null
  "line": "2 due today: Lab 5 (CS 101), Essay draft",        // "" when nothing is due today or tomorrow
  "sample": false,
  // Open work due from local today 00:00 through the end of local day today+7, soonest
  // first, at most 8 (today's items are in both due_today and upcoming).
  "upcoming": [{"id", "title", "course_name", "course_label", "due_ts", "all_day", "kind", "status"}],
  "upcoming_more": 0                                          // how many the cap of 8 left out
}
```

`course_label` is the course's short code ("CS 101"), or `""` when it has none; the app then
names the course by `course_name`, truncated. An older service sends neither `upcoming` nor
`upcoming_more`; the app decodes them as `[]` and `0`.

What the tabs show (display only: school rows never reach a prompt, log, notification,
widget or export, and never show grades or points):

- **Brief** (today only): one SCHOOL row whose headline is `line` (or "Nothing due today or
  tomorrow" when `line` is empty but `upcoming` is not), then up to 3 of `upcoming`, one line
  each (title, course label, short due time such as "Today 11:59 PM", "Tomorrow 10:00 AM",
  "Mon, Nov 16"), then "+N more in Study" (N = the hidden `upcoming` items plus
  `upcoming_more`). With only one item in all, the headline names it and no list follows.
  The whole row opens Study, in the proactive Brief and in the Brief without it.
- **Day** (today only): school deadlines and study blocks sit in the agenda (below); after
  the agenda's lower sections a "SCHOOL THIS WEEK" group lists up to 4 of `upcoming` that are
  not due on the day shown, with "+N more in Study ›" in its heading. Nothing beyond today's
  items shows nothing. Tapping it opens Study. Another day shows only its own deadlines.

The selection (caps, more-counts, the Day's dedupe) is pure code in `NotchDay.swift`
(`StudyDaySummary.briefUpcoming`, `dayUpcoming`), checked in `tests/NotchDayChecks.swift`.

- `deadlines` gains `{"id": "study:<assignment id>", "title": "<title> · <course code or name>",
  "due_ts", "source": "study"}` for items due on that day (not submitted/graded); an all-day
  item adds `"all_day": true, "due_date": "<day>"`.
- `blocks` gains at most two `{"id": "study-block:<assignment id>", "title": "Study: <title>",
  "start", "end", "kind": "study", "task_id": null, "warnings": []}` blocks of 45 minutes, placed
  in the first free gaps between now (rounded up to :00/:30) and the due time, inside 08:00–22:00,
  only for today, only for items due within 48 hours that are not submitted. Never overlaps a block.

## App (`app/Sources/WispApp/`)

| file | owns |
| --- | --- |
| `Study.swift` | Foundation-only: decoded types, due grouping, quiz and flash-card state, copy |
| `StudyModel.swift` | `@MainActor final class StudyModel: ObservableObject`, injectable transport |
| `StudyCredentialStore.swift` | Keychain `com.wisp.study` |
| `StudyPageReader.swift` | reads the front tab of each open browser window through Accessibility |
| `NotchStudyView.swift` | the tab: courses, due list (one line per assignment, 4 before Show all), make row, library (3 before Show all) |
| `StudyMaterialViews.swift` | quiz player, flash cards, guide and lesson readers |
| `StudyConnectorCard.swift` | key card: provider, key field, model |

### Swift names (exact)

```swift
// Study.swift — Foundation only, no SwiftUI
enum StudyKind: String, Codable, CaseIterable { case guide, quiz, lesson, flashcards }   // .title, .symbol, .verb
struct StudyConnectorState: Decodable, Equatable  // provider, modelID, ready, source, providers: [StudyProvider]
struct StudyProvider: Decodable, Equatable, Identifiable   // id, label, defaultModel, models: [StudyModelOption], keyURL, keyHint
struct StudyModelOption: Decodable, Equatable, Identifiable // id, label
struct StudyCourse: Decodable, Equatable, Identifiable     // id, name, code, grade, score, url, dueCount, sample
struct StudyAssignment: Decodable, Equatable, Identifiable // id, courseID, courseName, title, kind, dueTS, allDay, points, score, status, url, sample
struct StudyMaterialSummary: Decodable, Equatable, Identifiable // id, kind: StudyKind, title, courseID, assignmentID, createdAt, count, bestScore, sample
struct StudyState: Decodable, Equatable            // connector, sample, lastImport, courses, assignments, materials
struct StudyMaterial: Decodable, Equatable, Identifiable   // summary fields + model + body: StudyBody
enum StudyBody: Equatable { case guide(StudyGuide), quiz(StudyQuiz), lesson(StudyLesson), flashcards(StudyDeck) }
struct StudyGuide / StudyQuiz / StudyQuestion / StudyLesson / StudyLessonStep / StudyDeck / StudyCard
struct StudyPage: Encodable, Equatable             // url, title, text
struct StudyDaySummary: Decodable, Equatable       // the "study" object of /assistant/today (lives in NotchDay.swift)

enum StudyDue { static func groups(_ items: [StudyAssignment], now: Date, calendar: Calendar) -> [StudyDueGroup] }
//   groups: Missing, Today, Tomorrow, This week, Later, No date — empty groups dropped
//   StudyText.dueShort: the due list's right column ("Today 11:59 PM", "Mon, Nov 16")
struct QuizRun: Equatable      // init(quiz:); current, chosen, isLast, choose(_:), next(), finished, correct, total, scoreText
struct FlashRun: Equatable     // init(deck:); current, flipped, flip(), mark(known:), again queue, finished, knownCount, total
enum StudyText                 // every user-facing string and formatter (due labels, grade chip, counts, empty states)

// StudyModel.swift
@MainActor final class StudyModel: ObservableObject {
    static let shared: StudyModel
    typealias Transport = (URLRequest) async throws -> (Data, URLResponse)
    init(transport: Transport? = nil, reader: (() async -> [StudyPage])? = nil)
    @Published private(set) var state: StudyState?
    @Published private(set) var busy: StudyBusy?          // .reading, .making(StudyKind), .saving, .loading
    @Published var notice: String?                        // gentle one-line error or result
    @Published var opened: StudyMaterial?                 // the material shown full-tab
    @Published var showingConnector: Bool
    func refresh() async
    func readSchoolPages() async                          // StudyPageReader -> POST /study/import
    func make(_ kind: StudyKind, course: StudyCourse?, assignment: StudyAssignment?, topic: String?) async
    func open(_ id: String) async
    func close()
    func delete(_ id: String) async
    func record(correct: Int, total: Int) async           // for the opened material
    func saveConnector(provider: String, key: String?, modelID: String) async   // key nil keeps the saved key
    func loadSample() async
    func clearAll() async
}

// StudyPageReader.swift
enum StudyPageReader { static func read() async -> [StudyPage]; static var hasAccessibility: Bool }

// StudyCredentialStore.swift
enum StudyCredentialStore { static func save(_ key: String, provider: String) throws
                            static func remove(provider: String); static func has(provider: String) -> Bool }

// Views
struct NotchStudyView: View { init(model: OverlayModel, study: StudyModel, cap: CGFloat? = nil) }
//   The panel stops at half the screen on Study (SurfaceLayout.studyScreenShare, studyContentCap)
//   and the tab scrolls inside it; an opened material keeps its back row and title fixed.
```

JSON keys are snake_case; Swift properties are camelCase through explicit
`CodingKeys` (the repo's convention; no `convertFromSnakeCase`).

## Tests

- Python: `tests/test_study_store.py`, `test_study_api.py`, `test_study_connector.py`,
  `test_study_generate.py`, `test_study_canvas_parse.py`, `test_study_plan.py`. Each is listed in
  `scripts/run_simulation_qa.py` and ends with the manifest self-check.
- Swift: `tests/StudyChecks.swift` (`func studyChecks(_ check: (Bool, String) -> Void)`), compiled
  with `tests/NotchTabChecks.swift`.
- `WISP_NOTCH_QA` scenes: `study`, `study-quiz`, `study-cards`, `study-guide`, `study-lesson`, `study-setup`, `brief-study`, `brief-legacy-study`, `day-study`.
