# Attachments, batches 2 and 3: find, combine, act, and more sources

Base: main 544b6b9a. Integration branch: `worktree-attachments-find`. One pull request.
Read first: `docs/plans/2026-11-10-attachments-sharper.md` (batch 1 design) and the module
docstring of `service/mail_attachments.py`.

## What the user gets

1. **Search by what's inside.** "Find the PDF with my lease end date." Searches the text Wisp has
   kept (`service/attachment_store.py`). A switch, OFF by default, lets Wisp read new saved Mail
   attachments in the background so there is more to search.
2. **Questions across many files.** "How much did I pay Acme this year." Up to 40 files, the same
   few facts from each, totals added by code.
3. **Save a file.** Save an attachment to Downloads (or a named folder) and show it in Finder.
   Always confirmed, on a card that names the exact file. (Forwarding an email with its files
   already exists as `forward_email`; attaching one file to a new email is NOT in this batch.)
4. **Files in Messages.** Pictures and documents people texted.
5. **Audio.** Voice memos transcribed by the local model.
6. **Emails attached to emails.** A forwarded `.eml` is read, and so are the files inside it.

## Rules that do not change

- A file is never run. Archives are never unpacked. An `.eml` is parsed in the child process, in
  memory; nothing inside it is written to disk.
- Everything from a file is attacker-controlled: names cleaned, contents inside
  `untrusted.fence(...)`, nothing from a file logged or printed. Facts pulled out of a file
  (party, what, amounts) are attacker-controlled too and stay inside the fence.
- Reading with a model happens only when asked, and only with the local model
  (`documents.local_model()` before any byte is fetched).
- NEW, narrower rule for background reading: OFF by default; when on, it uses NO language model
  (plain extraction and Apple's on-device text recognition only), reads only copies Apple Mail
  already saved on this Mac, never downloads from Gmail, never reads Messages files, never shows
  anything unasked. What it keeps follows the store's rules (30 days unused, 200 MB, erased with
  its account, erased by "forget mail memory").
- Tests: set `WISP_HOME` to a tmp dir before importing `service`; never touch `~/.moe`; never
  load Ling or the embedder; no model is loaded by any test. At least one test per seam must use
  the real function on both sides (batch 1 shipped two defects that fakes hid).
- Real files: counts and Wisp's own sentences only; never a name, path, sender or content.

## Interfaces between owners (fixed; ask the integrator before changing one)

### A. `service/attachment_store.py`
```python
def note_seen(digest, *, source, account="", name="", sender="", subject="", ts=0.0) -> bool
    # source: "mail" | "gmail" | "messages". Cleaned and cut (name 200, sender 200, subject 300).
    # At most 8 rows per digest, newest kept. Refused when the digest has no text row.
@dataclass(frozen=True)
class Hit: digest; kind; how; note; cut; name; sender; subject; ts; source; account
           snippets: tuple[str, ...]; all_words: bool
def search(query: str, *, limit=8, since=0.0, until=0.0, account="", source="", name="") -> list[Hit]
    # Up to 8 words of 2+ characters from `query`. Files holding ALL words first (all_words True);
    # when there are none, files holding ANY word. Matches the text and the file name.
    # Up to 2 snippets per hit, 160 characters each, whitespace collapsed, around the matched words.
    # The words are data: never passed to FTS as query syntax. Works without FTS5 (slower).
    # Does not touch last_used. Never raises.
def count() -> int
def get_facts(digest, version: int) -> dict | None
def put_facts(digest, version: int, facts: dict) -> bool        # JSON of at most 4,000 characters
def tried(key: str) -> bool
def mark_tried(key: str, outcome: str) -> bool                  # short token; key at most 300
```
Everything new is removed with its text (`forget_account`, `forget_all`, prune, a digest dropped),
and erased text must not be recoverable from the database file (extend the existing test with a
planted token for every new table and for the search index). An existing database gains the new
tables on open; the search index is filled for rows that predate it. `tried` rows age out after 30 days.

### B. `service/attachment_fields.py` (new)
```python
FACTS_VERSION = 1
async def facts_of(text: str, kind: str, *, client, model, deadline: float) -> dict | None
    # {"what": str<=60, "party": str<=60, "date": "YYYY-MM-DD" | "",
    #  "total": {"value": "240.00", "currency": "USD"} | None,
    #  "amounts": [{"label": str<=40, "value": str, "currency": str}]  (at most 6)}
    # One short thinking-off call through documents.local_chat on head_and_spread of the text.
    # The text is untrusted data. Every amount must appear in the text digit for digit (after
    # removing thousands separators) or it is dropped; a date whose year is not in the text is "".
def line(facts: dict | None, *, name: str, when: str) -> str     # one cleaned line, at most 200 characters
def totals(all_facts: list[dict | None]) -> list[str]
    # Decimal sums per currency: "USD 1,240.00 across 5 files", and how many files had no total.
```

### C. `service/attachment_child.py` and `documents.run_attachment_child`
`run_attachment_child(job, name, data, *, timeout, part: int | None = None)`; a workdir also for `audio`.
- `inspect`: audio is recognised (`family: "audio"`): M4A/M4B brands (today they come back as video),
  WAV, MP3, CAF, Ogg/Opus, AMR, AIFF, ADTS AAC. An attached email is `family: "email"` when the
  bytes look like RFC 822 headers.
- `audio` -> `{"seconds": float, "heard": float, "windows": [base64 WAV], "stopped_early": bool}`.
  `/usr/bin/afconvert -f WAVE -d LEI16@16000 -c 1` in the workdir, fixed argv and env like `sips`,
  30 s timeout. At most the first 600 s. Windows of at most 29.5 s, each cut at the quietest
  0.2 s in its last 4 s. 16 kHz mono 16-bit PCM WAV each.
- `eml` -> `{"from","to","cc","subject","date", "body": str, "body_cut": bool,
  "files": [{"index": int, "name": str, "mime": str, "size": int}], "more_files": int}`.
  Header strings cleaned, at most 300 characters. Body at most 200,000 characters, text/plain
  first, else the HTML turned to text. At most 50 files listed. An email inside the email is
  listed as a file. Caps on parts and depth; a malformed file is a plain sentence, never a hang.
- `eml_part` (with `part=index`) -> `{"name","mime","size","data_b64"}`, at most 24 MiB decoded.

### D. `service/voice.py` and `service/attachment_audio.py` (new)
```python
# voice.py
async def transcribe_wav(data: bytes, *, client, model, deadline: float) -> str
    # One window (at most 30.5 s). Same checks as transcribe(): audio-capable model or VoiceUnavailable.
    # Honours `deadline` (time.monotonic()). The audio is never recorded for debugging.
# attachment_audio.py
class Heard(NamedTuple): text: str; heard_s: float; total_s: float; complete: bool
async def listen(name: str, data: bytes, *, client, model, deadline: float) -> Heard
    # child job "audio", then transcribe_wav per window until the deadline. Stops honestly:
    # complete is False when windows were left or the file is longer than 600 s.
    # Raises ValueError(plain sentence) or voice.VoiceUnavailable.
```

### E. Mail bridge: `service/mail_memory/bridge.py` and `app/Sources/WispApp/MailMemoryReader.swift`
```python
async def read_message_attachment_index(terms, handles, since, until, limit=200, timeout=10.0) -> tuple[list[dict], bool]
    # row: {"id": str (digits), "ts": float, "chat": str<=120, "sender": str ("" when from the user),
    #       "from_me": bool, "name": str, "mime": str, "size": int, "on_disk": bool}
async def read_message_attachment(attachment_id: str, timeout=45.0) -> tuple[bytes | None, str, str]
    # (bytes, name, why) exactly like read_attachment; why in not_found | not_on_disk | too_large | unreadable | no_messages_db
```
Kinds `message_attachments` `{terms (<=5), handles (<=10), since, until, limit (1..400)}` and
`message_attachment` `{attachment_id}`, strict key sets like the others. A row matches when every
term is in the chat's name, the sender's handle or the file's name, AND (when `handles` is not
empty) the sender's handle is one of them. Newest first. Left out by the app: link previews
(`.pluginpayloadattachment`), stickers, hidden attachments, tapback rows. The file path comes only
from the app's own query, must be a regular file inside the real `~/Library/Messages/Attachments`
with no symlink on the way, at most 32 MB. chat.db is opened read-only; its path is an init
parameter so the regression test uses a fixture.

### F. `service/mail_attachments.py` (wave 1)
```python
async def run(..., source: str = "mail")          # "mail" | "messages"
async def find(query=None, name=None, start=None, end=None, account=None, count=DEFAULT_COUNT,
               source="mail", most=MAX_EMAILS) -> tuple[list[_Email], list[str]]   # no model, no bytes
async def fetch(email, f, *, deadline: float) -> tuple[bytes, str]                  # raises Unreadable (public alias of _Unreadable)
async def index_one(email, f, *, deadline: float) -> str
    # "kept" | "known" | "skipped" | "unreadable". Fetch, inspect, model-free text, store. Never raises.
    # NEVER calls a model: a picture that is not text, or a scan the app cannot recognise, is "skipped".
async def whole_text(ctx, email, f) -> tuple[_Reading, str, str]   # (reading, the file's whole text unshaped, digest)
```
- Messages: one "email" per message; header `Message from {who} in {chat}`; `who` through the
  contacts map in `service/tools/imessage_tools.py`; words of the query that name a contact become
  `handles`. Store owner label `messages`. Videos stay "Wisp doesn't read videos".
- Audio branch before the video check; note `Transcribed on this Mac; some words may be wrong.`
  plus how much was heard; kind `audio`, how `transcribed`; kept only when complete.
- Email branch: headers and body as the text (kind `eml`); up to 3 of its files read in memory by
  the same rules (programs and archives inspected only, an email inside it listed only) and shown
  under it within the budget; note `An email attached to an email. Nothing inside it was saved or opened.`
- Every kept reading calls `attachment_store.note_seen`.

### G. Wave 2
- `service/attachment_index.py` (new): `enabled()`, `set_enabled(bool)`, `status() -> dict`,
  `schedule_tick()` called from `service/assistant/scheduler.py` beside `mail_memory.schedule_tick()`.
  Settings file `MOE_DIR/"attachments"/"settings.json"`, key `read_in_background`, default False,
  written like `power_tier.set_save_when_away`. Gates: enabled; `idle.foreground_busy()`;
  `brief_pace.may_start`; no other background work; app connected; not Low Power Mode; battery at
  least 30% when unplugged. One file per tick; at most 40 files a day; files of at most 10 MB from
  the last 30 days that are on disk, not tiny images, not `tried`. Lists with
  `bridge.read_attachment_index` (no terms) and reads with `mail_attachments.index_one`.
  API in `service/attachment_api.py` (new): `GET` and `PUT /assistant/attachments/background`
  (`_native_only`, strict bool), reply `{"enabled", "kept", "last_run", "last_skip"}`.
- Swift switch in the Settings window next to the mail memory switch: "Read new attachments in
  the background", caption "Lets Wisp find a file by what's inside it. Reading happens on this Mac
  without the model, and the text is kept for 30 days."
- `mail_attachments.run(..., inside=None, across=False)`:
  `inside`: `attachment_store.search`, narrowed by `name`, dates, account, source; up to 5 hits,
  each with who/what/when from the store, its snippets, and (with `question`) the stored text
  shaped for the question. Says how many kept files were searched and whether background reading
  is on. No hit and other filters given: falls through to the normal read with the words as the question.
  `across`: up to 40 emails / 40 files, `whole_text` then `attachment_fields.facts_of` (facts kept
  with `put_facts`), one line per file, totals by code, how many were not reached before the
  150 s deadline (asking again continues, because readings and facts are kept).
- Tools. `read_email_attachments` gains `inside` and `across`. New `read_message_attachments`
  (same engine, `source="messages"`, args `query, name, period, day, count, question, list_only,
  pages, sheet, inside`). New `save_email_attachment` (`query, name, period, day, account,
  source, folder`): a prepare step before the card resolves ONE file and freezes it into the
  arguments (like `prepare_reply_args`); the card names the file, its size, its sender and the
  folder; always confirmed in every mode, never grantable, denied in view-only; a `high` risk file
  is refused; the name is cleaned (no path parts, no control characters, no leading dot, at most
  120 characters); the folder must be an existing real directory in the user's home outside
  `~/Library` and hidden folders (default `~/Downloads`); exclusive no-follow create, never
  overwrites (" (2)"); the saved file gets `com.apple.quarantine` or the save is undone; then
  `/usr/bin/open -R` shows it. Typed not-done results before any write. Both new read paths mark
  the picture turn and are excluded from remote inference like `read_email_attachments`.

## Owners

Wave 1 (parallel): W1 store (A) · W2 fields (B) · W3 child + `run_attachment_child` (C) ·
W4 voice + attachment_audio (D) · W5 bridge + Swift reader (E) · W6 mail_attachments (F).
Wave 2 (after wave 1 is merged): W7 index + API + scheduler hook · W8 Swift settings switch ·
W9 `inside`/`across` in mail_attachments, the tool file, the new read tool and every registry-wide
count, budget, router and docs file for BOTH new tools · W10 save engine, its tool module and its
policy/loop/registry wiring.
The integrator merges, writes seam tests, runs the wide test set, the Swift regression, the
real-file checks and the one preview build.

Another session's rc3 fixes touch `_fetch`, `_read_file`, `_recognised`, `_finish_text` in
`mail_attachments.py`, `_SIGNATURES`/`_risk`/`ocr_images` in the child, the sent-mailbox lookup
in `MailMemoryReader.swift`, the bridge's connect wait, and add `attachment_store.accounts()`.
Keep changes in those functions small and additive so the later merge is easy.
