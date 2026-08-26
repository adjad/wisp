# Wisp Air — Periodic Assistant Node Handoff

**For: Claude running on the MacBook Air.**
**Written: 2026-07-31, by Claude on the MacBook Pro.**

You are being asked to turn the MacBook Air into a scheduled background worker for
Wisp, the user's local macOS assistant. You have no prior context on this project, so
this document is written to be self-contained. Read all of §1–§3 before running
anything — §2 contains a hard blocker that may stop this project outright.

The Air has not been updated in a while. Assume nothing about its OS version,
toolchain, or the state of the code already on it.

---

## 1. Context — what Wisp is and what the Air's job is

**Wisp** is a strictly-local, privacy-first assistant that runs on the user's MacBook
Pro (M5 Pro, 24 GB). It has a Swift menu-bar frontend, a Python FastAPI backend on
port 8765, and does all inference locally through **oMLX** (a local model server on
port 8000). No data leaves the user's devices.

**Today, the Pro does everything**: it reads Mail/Messages/Notes/Calendar, answers
live chat, and runs a once-daily 8am email digest. The problem is that the Pro
**sleeps whenever its lid is closed** — verified via `pmset -g log`, and AC power does
not prevent it (lid close overrides the idle-sleep setting). So scheduled work either
doesn't run or runs late, and the user does not want a model waking up on the Pro for
background work anyway.

**The Air's new job — this is what you are building.** Every 3 hours, while the Pro is
asleep or in use, the Air will independently:

1. Read new email and new iMessages since its last run
2. Generate a short text summary of each
3. Extract action items and write them into **Apple Reminders**
4. Hold the summaries until the Pro asks for them

The Air is up **20 hours a day, dark 01:00–05:00**. That gives 7 runs a day: 05:00,
08:00, 11:00, 14:00, 17:00, 20:00, 23:00.

**Why this design works.** A prior evaluation (Phase 0, 2026-07-30) found the
inference engine you'll use here is bad at long context and terrible under concurrent
load, but genuinely good at short, sequential, one-at-a-time jobs. Three hours of mail
is well under a thousand tokens and nothing else touches the server, so this is
precisely the regime that works. Expect ~1–3 minutes of compute per run — roughly a
2% duty cycle.

**Key architectural decision you must not "improve" on: to-dos ride iCloud.** When the
Air writes an `EKReminder`, iCloud syncs it to the Pro *and* the user's iPhone for
free. Do **not** build a custom to-do sync protocol between the machines. Only the
text summaries need to travel over the network, because they have nowhere else to
live.

### Hardware you are working with

| | |
|---|---|
| Machine | MacBook Air, **M4, 16 GB RAM, 256 GB SSD** |
| Current service | `~/WispAir/air_service.py` on `0.0.0.0:8766` (launchd + `caffeinate -s`) |
| Reachable at | `http://10.0.0.142:8766` on LAN today; Tailscale is planned (see §7) |

---

## 2. PREFLIGHT — do these first, in order, before building anything

### 2.1 macOS version — HARD BLOCKER

TurboFieldfare (the inference runtime, §4) requires **macOS 26 with Metal 4**, arm64
only. Older macOS is explicitly unsupported.

```bash
sw_vers
```

- **macOS 26 or newer** → continue.
- **Older than macOS 26** → **STOP.** The Air must be updated before any of this can
  work. Report this to the user immediately, tell them the required version, and do
  not attempt workarounds — there is no fallback runtime that fits in this design.
  (For reference, the Pro is on macOS 27.0.)

### 2.2 Disk space

The model install alone is **~14.3 GB**, plus a Swift release build and a full
`~/Library/Mail` and `~/Library/Messages` store once those accounts sync locally.

```bash
df -h /
```

Require **at least 40 GB free** before starting. On a 256 GB Air this is usually fine,
but check — do not start a 15 GB streaming download that will fail at 90%.

### 2.3 Toolchain

```bash
swift --version          # need Swift 6.x, arm64-apple-macosx
xcode-select -p          # must point at a real Xcode or CLT install
```

If Swift is missing, install the Xcode Command Line Tools (`xcode-select --install`).
If `xcode-select` points somewhere broken, that is a host setup problem — most fixes
need the user's password, so report it rather than trying to fix it silently.

### 2.4 Accounts signed in

The Air can only read what macOS has locally. Before the readers can work, **the user**
must have on the Air:

- **Mail.app** signed into the same account(s) as the Pro, with the inbox synced
- **Messages.app** signed into iMessage with "Messages in iCloud" enabled

You cannot do this for them. Check and report:

```bash
ls -la ~/Library/Messages/chat.db 2>/dev/null && echo "chat.db present" || echo "chat.db MISSING"
osascript -e 'tell application "Mail" to count of accounts' 2>&1
```

### 2.5 Benchmark the SSD — this decides how slow runs will be

Phase 0 measured that prefill on this runtime is **SSD-bandwidth-bound, not
compute-bound** (disk sustained ~6,000 MB/s for the entire prefill while the CPU sat
80–87% idle). Base 256 GB Airs sometimes ship a single-NAND-die SSD with roughly half
the read bandwidth of larger configurations, which would roughly double prefill time.

After §4 is built, run a prefill of ~1,500 tokens and record tokens/sec. Report the
number. For reference the Pro measured **~110 tok/s prefill, ~25 tok/s decode**. If the
Air lands near ~55 tok/s prefill, a run takes 3–4 minutes instead of 1–2 — still fine
at this duty cycle. This is a number to *report*, not a gate.

---

## 3. Architecture — what you are building

```
                    ┌─────────────────────── MacBook Air ────────────────────────┐
                    │                                                             │
  Mail.app ────────►│ WispAirReader.app (Swift, signed, holds TCC grants)        │
  chat.db  ────────►│   • MailReader      – AppleScript, needs Automation grant  │
                    │   • MessagesReader  – SQLite read-only, needs Full Disk     │
                    │   • RemindersWriter – EKReminder, needs Reminders grant     │
                    │              │                       ▲                      │
                    │              │ POST localhost:8767   │ create reminder      │
                    │              ▼                       │                      │
                    │      air_periodic.py (FastAPI, :8767)                       │
                    │        • 3-hour scheduler                                   │
                    │        • incremental cursors                                │
                    │        • summary store (~/.wispair/)                        │
                    │              │                                              │
                    │              ▼ localhost:8081                               │
                    │      TurboFieldfareServer (gemma-4-26b-a4b-it)              │
                    └─────────────────────────────────────────────────────────────┘
                                   │                          ▲
                     GET /summaries│ (Pro pulls when it wakes) │
                                   ▼                          │
                            MacBook Pro (Wisp)          Apple Reminders
                                                        └─ iCloud syncs to Pro + iPhone
```

Four components, built in this order: **TurboFieldfare (§4) → reader app (§5) →
periodic service (§6) → networking (§7)**.

Use a **new service on port 8767**. Do not modify `~/WispAir/air_service.py` — it is a
separate, currently-disabled routing experiment, and its routing rules are known to be
stale relative to the Pro. Leave it alone.

---

## 4. TurboFieldfare — the inference runtime

Open-source Swift + Metal runtime that streams expert weights from SSD, so a 26B model
runs with a **server RSS of only ~0.78 GB**. That is why a 16 GB Air can host it
comfortably.

### 4.1 Build

```bash
git clone https://github.com/drumih/turbo-fieldfare ~/turbo-fieldfare
cd ~/turbo-fieldfare
swift build -c release
```

### 4.2 Install the model (~14.3 GB, streams from Hugging Face)

```bash
cd ~/turbo-fieldfare
swift run -c release TurboFieldfareRepack --output scratch/gemma4.gturbo --overwrite
```

If the download is interrupted, resume rather than restarting:

```bash
swift run -c release TurboFieldfareRepack --output scratch/gemma4.gturbo --overwrite --resume
```

Verify without loading:

```bash
swift run -c release TurboFieldfareRepack --verify-install --input-gturbo scratch/gemma4.gturbo
```

### 4.3 Run the server

```bash
cd ~/turbo-fieldfare
./.build/release/TurboFieldfareServer \
  --model ./scratch/gemma4.gturbo \
  --port 8081 \
  --max-context 8192 \
  --prompt-cache-mode single-prefix
```

**`--max-context 8192` is deliberate.** It is an enum — only 4096/8192/16384/32768/65536
are accepted. Prefill cost is linear in context, and a cache miss costs a full
re-prefill, so keep the ceiling low. Three hours of mail fits easily. Do not raise it
"just in case."

### 4.4 Hard API constraints — verified in source and against live 400s

These are not style preferences. Violating them returns errors or silently misbehaves.

| Constraint | Detail |
|---|---|
| `model` field | Must be **exactly** `gemma-4-26b-a4b-it`. Anything else 400s. |
| `tool_choice: "required"` | **Rejected.** Use `"auto"` if you use tools at all. |
| Message order | `system`/`developer` messages must precede **all** conversation messages. |
| Content | Text-only parts. **No vision.** |
| Endpoints | Chat completions only. **No `/v1/embeddings`, no `/v1/rerank`.** |
| SSE | Standard OpenAI shape, terminated with `data: [DONE]`. |
| `reasoning_content` | Does not exist — Gemma has no reasoning channel. |

### 4.5 The prompt cache trap

`ServerPromptCache` holds **exactly one entry**. Any interleaved request evicts it — a
measured 42-token interloper forced a full 2,000-token re-prefill costing 18.5 s to
emit 27 tokens. Consequences for your design:

- **Run jobs strictly sequentially.** Never issue concurrent requests to :8081.
- **Nothing else may use this server.** It is dedicated to the periodic jobs.
- Prefer **one call carrying all the content** over three calls that each re-prefill.

---

## 5. The reader app — Swift, and why it must be an app

The three readers must live in a **signed .app bundle**, not a script, because macOS
TCC (privacy permissions) attributes grants per-process-bundle:

- Mail requires an **Automation** grant, which only prompts properly for an app bundle
  with `NSAppleEventsUsageDescription` in its Info.plist
- `chat.db` requires **Full Disk Access**
- Reminders requires a **Reminders** grant

### 5.1 Code signing — the user must do one step

Ad-hoc signing (`--sign -`) changes the binary's CDHash on every build, and macOS drops
**all** TCC grants each time. This bit the user repeatedly on the Pro. The fix there was
a self-signed code-signing certificate that keeps the identity stable.

**Ask the user to create one on the Air** (you cannot — it is a security setting):
Keychain Access ▸ Certificate Assistant ▸ Create a Certificate ▸ name it
`WispAir Dev`, type **Self-Signed Root**, purpose **Code Signing**. Then set it to
Always Trust.

Verify it exists before building:

```bash
security find-identity -v -p codesigning
```

Sign every build with it (`codesign --sign "WispAir Dev" --force --deep ...`) and grants
will persist across rebuilds.

### 5.2 Port the readers from the Pro

The Pro's readers are at `~/Desktop/MOE_Project/app/Sources/WispApp/` (get them from the
user — copy the repo over, or have them AirDrop the three files):

| File | Lines | Port effort |
|---|---|---|
| `MailReader.swift` | 397 | Nearly verbatim; change the base URL and add a cursor (§5.3) |
| `MessagesReader.swift` | 216 | Nearly verbatim; change the base URL and the date cutoff |
| `RemindersWriter.swift` | 103 | Use only `create(title:dueTs:)`; drop the read-back sync |

Strip everything else — no overlay, no hotkey, no notch UI. This is a headless
`LSUIElement` menu-bar app whose only job is to run readers and POST to
`http://127.0.0.1:8767`.

**Remove the `SuperModelState.shared.active` guards** when porting. That is a Pro-side
memory-pressure feature that does not exist on the Air; leaving it in will reference a
missing symbol.

### 5.3 Data formats — match these exactly

**Mail headers** (one line per message, newest first):

```
<epochSecs> | <account> | <sender name> | <subject>
```

Timestamps come from an AppleScript trick: build a reference date at 2001-01-01 by
property assignment (locale-independent), subtract, then add `978307200` to reach the
Unix epoch. AppleScript emits these in **scientific notation** (`1.783977044E+9`) —
Python's `float()` parses that natively, no special handling needed.

**Mail raw bodies** use control characters, not pipes: fields separated by `\x01` (FS),
records terminated by `\x02` (RS). Email bodies routinely contain `|` and newlines,
which would corrupt a pipe format.

**Messages** (one line per message):

```
<epochSecs> | <conversation label> | <who>: <text>
```

Read `chat.db` **read-only** (`SQLITE_OPEN_READONLY`) — Messages.app keeps it open in
WAL mode, which safely supports concurrent readers. Note `message.date` is **nanoseconds
since the 2001 Apple epoch**, and `associated_message_type = 0` filters out tapback
reactions. Some messages store text in an `attributedBody` NSKeyedArchiver blob rather
than `text`; the Pro's decoder handles both — port it as-is.

### 5.4 Incremental reads — the point of the exercise

The Pro's readers re-scan a full year every time (a multi-minute job for Mail). You
must not do that every 3 hours. Instead:

- **Mail:** the inbox is ordered newest-first (verified against a real 18K-message
  inbox), so scan from index 1 and **stop early** when a message is older than your
  cursor. Do not use an AppleScript `whose` clause — they are pathologically slow over
  Apple Events.
- **Messages:** the SQL already has `WHERE m.date > cutoffNs`. Just move the cutoff to
  your cursor.

Three rules that will bite you if ignored:

1. **Always re-scan the last 24 hours regardless of cursor.** Some IMAP servers deliver
   mail with a backdated `date received`; pure early-termination would skip it forever.
   Deduplicate on `(epochSecs, sender, subject)`.
2. **Advance the cursor only after the summary is successfully written.** If a run dies
   mid-way and the cursor already moved, you get a permanent hole with no error.
3. **Deletions are invisible to incremental sync.** Accept the drift; a weekly full
   re-baseline is enough.

---

## 6. The periodic service — `~/WispAir/air_periodic.py`

FastAPI on `0.0.0.0:8767`. Responsibilities: receive reader pushes, run the 3-hour job,
call TurboFieldfare, write reminders, store summaries for the Pro.

### 6.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sync/emails` | Reader pushes `{headers, raw, diagnostics}` |
| `POST` | `/sync/messages` | Reader pushes `{lines, diagnostics}` |
| `GET` | `/summaries?since=<epoch>` | **The Pro pulls this.** Returns summaries newer than `since` |
| `POST` | `/summaries/ack` | Pro confirms receipt `{through: <epoch>}` |
| `GET` | `/health` | `{ok, model_loaded, last_run_ts, next_run_ts, cursors}` |
| `POST` | `/run` | Manually trigger a run (for testing) |

Hold summaries until acknowledged — the Pro may be asleep for hours. Do not delete on
read.

### 6.2 The schedule

Run at **05:00, 08:00, 11:00, 14:00, 17:00, 20:00, 23:00** local time. Nothing between
01:00 and 05:00 (the Air is off). The 05:00 run naturally covers the overnight window,
because Mail and Messages backfill from their servers when the Air wakes.

**Skip empty windows.** A 3-hour window will often contain two emails and no messages.
Do not generate seven "nothing happened" summaries a day — if there is nothing
meaningful, record a no-op and roll the content forward into the next run.

### 6.3 The model call

**One call per source per run** (so at most two), each carrying that window's content
and asking for both outputs at once. Two calls, not six — every extra call is another
full prefill.

Ask for a **delimited format, not JSON.** Small models are unreliable JSON emitters and
a parse failure loses the whole run:

```
SUMMARY:
<2-4 sentences>

TODO: <title> | <ISO 8601 datetime, or "none">
TODO: <title> | <ISO 8601 datetime, or "none">
```

Parse `TODO:` lines and pass each to `RemindersWriter.create(title:dueTs:)`. Inject the
**current date** into the system prompt — without it the model cannot resolve "tomorrow"
or "Friday" and will guess. This was a real bug on the Pro.

**Deduplicate reminders.** The 24-hour overlap window means the same email may be seen
twice. Keep a hash of already-created to-dos (title + due date) and skip repeats — the
user should never get the same reminder twice.

### 6.4 Storage

Everything under `~/.wispair/`, created `0700` with files `0600`. This holds real
personal content (email subjects, message text), same machine-local trust model the
Pro uses for `~/.moe/`.

```
~/.wispair/
  cursors.json      # {"email": <epoch>, "messages": <epoch>}
  summaries.db      # SQLite: (id, ts, source, summary, acked)
  todo_hashes.txt   # dedupe ledger
```

Write cursors atomically (temp file + rename) so a crash mid-write cannot corrupt them.

### 6.5 launchd

The Air already runs `air_service.py` under launchd with `caffeinate -s`. Mirror that
pattern for `air_periodic.py` in a **separate** plist —
`~/Library/LaunchAgents/com.wisp.air.periodic.plist` — with `KeepAlive` true and
`RunAtLoad` true. The reader app needs a login item so it starts with the user session.

---

## 7. Networking

Tailscale is **not yet installed on either machine**. Install it on the Air, sign in to
the user's tailnet, and report the Air's MagicDNS hostname. The Pro side is a separate
step the user will handle.

Until then the Air stays reachable on the LAN at `10.0.0.142:8767`.

Bind to `0.0.0.0`, not `127.0.0.1`, or the Pro cannot reach it. Require a shared API key
header on every endpoint except `/health` — cheap defense in depth on top of Tailscale's
own ACLs. Generate one, store it in `~/.wispair/api_key`, and report it so the user can
configure the Pro.

---

## 8. Verification — all of these must pass before you report done

Run on the Air:

```bash
# 1. Runtime alive and answering
curl -s localhost:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemma-4-26b-a4b-it","messages":[{"role":"user","content":"Reply with the single word: ready"}],"max_tokens":16}'

# 2. Service healthy, cursors present
curl -s localhost:8767/health

# 3. Readers actually delivered data (non-zero counts, available:true)
curl -s localhost:8767/health | grep -i diagnostic

# 4. A full manual run end to end
curl -s -X POST localhost:8767/run

# 5. Summaries retrievable
curl -s "localhost:8767/summaries?since=0"
```

Then verify by inspection:

- A reminder created by the run **appears in Reminders.app on the Air**
- The same reminder **appears on the Pro** within a minute or two (proves the iCloud
  path works and no custom sync is needed)
- Running `/run` twice does **not** create duplicate reminders
- Report the measured prefill tok/s from §2.5

---

## 9. Constraints — what NOT to do

- **Do not route live chat to the Air.** Latency is far too high; the Pro answers
  interactively with its own models. This node is batch-only.
- **Do not attempt embeddings, reranking, or vision.** The runtime supports none of
  them. Wisp's Smart Search stays on the Pro's oMLX.
- **Do not move Calendar syncing here.** Calendar events get rescheduled and cancelled,
  so append-only sync would be actively wrong, and the Pro already handles it correctly
  with source-of-truth diffing.
- **Do not modify `~/WispAir/air_service.py`** or its launchd plist.
- **Do not run concurrent requests against :8081.** See §4.5.
- **Do not use `tool_choice: "required"`.** It is rejected outright.
- **You cannot grant TCC permissions or create the signing certificate.** Those need the
  user. Detect, report clearly, and wait — do not try to work around macOS security.
- **Do not raise `--max-context` to "be safe."** It directly multiplies the cost of every
  cache miss.

---

## 10. Definition of done

1. §2 preflight passed and reported (especially the macOS version and measured prefill
   speed)
2. TurboFieldfare builds, model installed and verified, server runs on :8081
3. `WispAirReader.app` signed with a stable identity, all three TCC grants held, pushing
   real data
4. `air_periodic.py` on :8767 under launchd, scheduled for the seven daily runs
5. A manual run produces a summary **and** a real Reminder that reaches the user's other
   devices
6. Rerunning does not duplicate reminders
7. Reported to the user: the Air's Tailscale hostname, the API key, the prefill
   benchmark, and anything in §2 that needed their action

**If you hit the macOS 26 blocker in §2.1, stop and report. Do not build around it.**
