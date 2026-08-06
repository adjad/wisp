# Wisp Air — periodic background node

**For Claude running on the MacBook Air. You have no prior context on this
project; this file is self-contained. Read §1–§3 before running anything.**

The framework is **already built and tested** — on the Pro, against a stub
model server (53/53 unit tests, plus a live HTTP run through every endpoint).
Your job is bring-up on real hardware, not construction. Prefer fixing what's
here over rewriting it; the parts that look over-careful are load-bearing and
§7 says which.

---

## 1. What this is

**Wisp** is a strictly-local, privacy-first assistant on the user's MacBook Pro
(M5 Pro, 24 GB). Nothing leaves the user's devices. The Pro sleeps whenever its
lid closes, so scheduled background work either runs late or not at all — and
the user explicitly does not want a model waking the Pro up for it.

**So the Air does that work instead.** Every 3 hours it:

1. Reads new email and iMessages since its last run
2. Summarizes each with a local model
3. Extracts action items → writes real **Apple Reminders**
4. Holds the summaries until the Pro asks for them

Up 20 h/day, dark 01:00–05:00 → 7 runs: 05:00, 08:00, 11:00, 14:00, 17:00,
20:00, 23:00. Roughly a 2% duty cycle.

**To-dos ride iCloud — do not "improve" this.** An `EKReminder` written here
syncs to the Pro *and* the user's iPhone for free. There is deliberately no
custom to-do sync protocol. Only the text summaries travel over the network,
because they have nowhere else to live.

```
┌──────────────────────── MacBook Air ─────────────────────────┐
│  Mail.app ──┐                                                │
│  chat.db  ──┼──► WispAirReader.app   (signed, holds TCC)     │
│             │      MailReader        – AppleScript/Automation │
│             │      MessagesReader    – SQLite RO/Full Disk    │
│             │      RemindersWriter   – EKReminder             │
│             │         │  POST :8767      ▲ polls :8767        │
│             │         ▼                  │                    │
│           air_periodic.py  (FastAPI :8767)                    │
│             • 3-hour scheduler   • durable item store         │
│             • summary store ~/.wispair/                       │
│                       │                                       │
│                       ▼  OpenAI-compatible :8081              │
│              inference server (gemma-4-26b-a4b-it)            │
└───────────────────────────────────────────────────────────────┘
          │ GET /summaries                    ▲
          ▼ (Pro pulls on wake)               │ iCloud
     MacBook Pro                        Apple Reminders → Pro + iPhone
```

---

## 2. Preflight — do these first, report the results

### 2.1 macOS version — decides which model backend you get

The intended runtime is **TurboFieldfare**, which streams expert weights off
SSD so a 26B model runs with ~0.78 GB server RSS — the reason a 16 GB Air can
host this at all. It requires **macOS 26 with Metal 4**, arm64 only.

```bash
sw_vers
```

- **macOS 26+** → build TurboFieldfare (§4). This is the intended path.
- **Older** → **do not build around it and do not silently substitute.** Report
  to the user, and see §4.2 for the fallback. The service does not care which
  backend it talks to, but the user should choose knowingly.

### 2.2 Disk, toolchain, accounts

```bash
df -h /                  # need ≥ 40 GB free (model alone is ~14.3 GB)
swift --version          # need Swift 6.x; else: xcode-select --install
xcode-select -p

ls -la ~/Library/Messages/chat.db 2>/dev/null || echo "chat.db MISSING"
osascript -e 'tell application "Mail" to count of accounts' 2>&1
```

The Air can only read what macOS has locally. **The user** must have Mail.app
signed in with the inbox synced, and Messages.app signed into iMessage with
"Messages in iCloud" on. You cannot do this for them — check and report.

### 2.3 Benchmark the SSD — report this number

Prefill on TurboFieldfare is **SSD-bandwidth-bound, not compute-bound** (the
Pro sustained ~6,000 MB/s while its CPU sat 80–87% idle). Base 256 GB Airs
sometimes ship a single-NAND-die SSD with about half the read bandwidth, which
would roughly double prefill time.

After §4, run a ~1,500-token prefill and record tok/s. The Pro measured **~110
tok/s prefill, ~25 tok/s decode**. If the Air lands near ~55 tok/s a run takes
3–4 minutes instead of 1–2 — still fine at this duty cycle. **This is a number
to report, not a gate.**

---

## 3. Install

Copy this whole `air/` directory to the Air (AirDrop, `rsync`, whatever), then:

```bash
./scripts/install.sh
```

That is idempotent and does everything Python-side: `~/.wispair` at 0700, code
into `~/WispAir/`, a venv, dependencies, the launchd agent
(`com.wisp.air.periodic`, KeepAlive + `caffeinate -s`), and it prints the
**API key** the Pro will need.

It deliberately does **not** touch `~/WispAir/air_service.py` (port 8766, an
older disabled routing experiment with known-stale rules). Leave that alone.

Then the reader app:

```bash
./scripts/package_reader.sh
open ~/Applications/WispAirReader.app
```

**Two things only the user can do** — detect, report clearly, and wait:

1. **The signing certificate.** Ad-hoc signing changes the CDHash every build
   and macOS drops *all* TCC grants each time (this bit the user repeatedly on
   the Pro). Ask them to make one in Keychain Access ▸ Certificate Assistant ▸
   Create a Certificate: name **`WispAir Dev`**, Self-Signed Root, Code
   Signing, then Always Trust. Verify with
   `security find-identity -v -p codesigning`, then re-run
   `package_reader.sh`. `install.sh` warns loudly if it's missing.
2. **Full Disk Access** (not promptable): System Settings ▸ Privacy & Security
   ▸ Full Disk Access ▸ add WispAirReader. Mail Automation and Reminders *do*
   prompt on first use.

Also add the app to Login Items so it starts with the session.

---

## 4. The model backend

The service talks plain **OpenAI-compatible `/v1/chat/completions`** and hard-codes
nothing else, so the backend is a config change rather than a rewrite.

### 4.1 TurboFieldfare (intended, needs macOS 26)

```bash
git clone https://github.com/drumih/turbo-fieldfare ~/turbo-fieldfare
cd ~/turbo-fieldfare && swift build -c release
swift run -c release TurboFieldfareRepack --output scratch/gemma4.gturbo --overwrite
# interrupted? add --resume rather than restarting
swift run -c release TurboFieldfareRepack --verify-install --input-gturbo scratch/gemma4.gturbo

./.build/release/TurboFieldfareServer \
  --model ./scratch/gemma4.gturbo --port 8081 \
  --max-context 8192 --prompt-cache-mode single-prefix
```

**`--max-context 8192` is deliberate.** It's an enum (4096/8192/16384/32768/65536).
Prefill cost is linear in context and a cache miss costs a full re-prefill.
Three hours of mail fits easily. **Do not raise it "just in case."**

Verified API constraints — these return errors or silently misbehave:

| | |
|---|---|
| `model` field | must be exactly `gemma-4-26b-a4b-it` |
| `tool_choice: "required"` | rejected — the service never sends tools at all |
| message order | system/developer before all conversation messages |
| content | text-only, **no vision** |
| endpoints | chat completions only — **no `/v1/embeddings`, no `/v1/rerank`** |
| `reasoning_content` | doesn't exist; Gemma has no reasoning channel |

### 4.2 If TurboFieldfare can't run

Point the service at any other local OpenAI-compatible server and tell the
user what you did:

```bash
curl -X POST localhost:8767/config -H "X-Wisp-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model_base_url":"http://127.0.0.1:PORT/v1","model_name":"MODEL-ID"}'
```

Nothing in the service depends on a TurboFieldfare-specific feature. But a 16 GB
Air cannot hold a 26B model in RAM the ordinary way — a smaller model is the
realistic fallback, and that is a quality trade the user should agree to.

### 4.3 The prompt-cache trap

`ServerPromptCache` holds **exactly one entry**. Any interleaved request evicts
it: a measured 42-token interloper forced a full 2,000-token re-prefill costing
18.5 s to emit 27 tokens. Therefore:

- Requests are serialized process-wide (`_LOCK` in `wispair/model.py`) — **do not
  remove that lock or turn `run_all` into a `gather()`.**
- **Nothing else may use :8081.** It is dedicated to these jobs.
- `/health` does *not* touch the model. Only `/health?deep=1` does, which is
  why it's opt-in — polling a deep health check would evict the cache.

---

## 5. Verify — all of these before reporting done

```bash
KEY=$(cat ~/.wispair/api_key)

curl -s localhost:8081/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"gemma-4-26b-a4b-it","messages":[{"role":"user","content":"Reply with the single word: ready"}],"max_tokens":16}'

curl -s "localhost:8767/health?deep=1" | python3 -m json.tool   # model_check.ok true
curl -s localhost:8767/health | python3 -m json.tool            # diagnostics: available:true, non-zero counts
curl -s -X POST "localhost:8767/run?force=1" -H "X-Wisp-Key: $KEY"
curl -s "localhost:8767/summaries?since=0" -H "X-Wisp-Key: $KEY"
```

Then by inspection:

- A reminder from the run **appears in Reminders.app on the Air**
- The same reminder **appears on the Pro** within a minute or two — this is
  what proves the iCloud path works and no custom sync is needed
- Running `/run?force=1` twice does **not** create a duplicate reminder
- Report the §2.3 prefill tok/s

You can also run the offline suite (no model, no permissions needed) to confirm
nothing broke in transit:

```bash
python3 tests/test_air.py     # expect 53 passed, 0 failed
```

---

## 6. Endpoints

Everything except `/health` requires `X-Wisp-Key: <~/.wispair/api_key>`.

| Method | Path | Who calls it |
|---|---|---|
| POST | `/sync/emails` `/sync/messages` | reader app |
| GET | `/cursors` | reader app — how far back to scan |
| GET | `/reminders/pending` · POST `/reminders/ack` | reader app |
| GET | `/summaries?since=` · POST `/summaries/ack` | **the Pro** |
| GET | `/health` (`?deep=1`) | anyone; open |
| POST | `/run` (`?force=1`) | manual trigger |
| GET/POST | `/config` | runtime settings |

---

## 7. Things that look odd and are load-bearing

Read this before "cleaning up" anything.

- **The scheduler polls every 60 s instead of sleeping until the next run.**
  `asyncio.sleep()` does not advance across system sleep, and this machine
  sleeps nightly. Polling for "have we crossed a slot since the last one we
  fired?" is what makes the 05:00 run happen on wake instead of silently never.
- **Two separate notions of progress.** `cursors` = how far back a reader must
  scan; `items.summarized_at` = what the model has processed. Merging them
  means a failed run either re-reads all of Mail or loses a window.
- **Readers always re-scan the last 24 h regardless of cursor.** Some IMAP
  servers deliver mail with a backdated `date received`; pure early termination
  would skip those forever. Duplicates are free (UNIQUE constraint); a hole
  isn't.
- **Reminder dedupe buckets the due date to the DAY** and normalizes the title.
  The same email seen in two overlapping windows otherwise yields the "same"
  to-do minutes apart. The user must never get a reminder twice.
- **Reminders are polled from a durable queue, not pushed over SSE** (the Pro
  uses SSE). If the app is down or unauthorized the queue just doesn't drain —
  nothing is lost, and it drains once the grant arrives.
- **Summaries are acked, never deleted on read.** The Pro may be asleep for
  hours; delete-on-read loses a summary to one dropped response.
- **The model is asked for a delimited format, not JSON.** Small models are
  unreliable JSON emitters and one parse failure would lose the whole run.
- **The current date is injected into every system prompt.** Without it the
  model cannot resolve "tomorrow" or "Friday" and guesses — a real bug that
  shipped on the Pro.
- **Field parsing splits on a regex, not `" | "`.** An empty account field
  really does emit two spaces between pipes, which a fixed-string split
  mis-assigns.

## 8. Do not

- **Route live chat here.** Latency is far too high; the Pro answers
  interactively with its own models. This node is batch-only.
- **Attempt embeddings, reranking, or vision.** The runtime has none of them;
  Wisp's Smart Search stays on the Pro's oMLX.
- **Move Calendar syncing here.** Events get rescheduled and cancelled, so
  append-only sync would be actively wrong; the Pro already does source-of-truth
  diffing correctly.
- **Modify `~/WispAir/air_service.py`** or its launchd plist.
- **Run concurrent requests against :8081** (§4.3).
- **Raise `--max-context`** to be safe. It multiplies every cache-miss cost.
- **Grant TCC permissions or create the signing cert yourself** — you can't.
  Detect, report, wait.

---

## 9. Still to do after bring-up

- **Tailscale** is not installed on either machine. Install it here, sign in to
  the user's tailnet, report the MagicDNS hostname. Until then the Air is
  LAN-only, reachable at its LAN address. The service binds `0.0.0.0` already.
- **The Pro-side puller does not exist yet** — nothing on the Pro calls
  `GET /summaries` or `/summaries/ack`. That is Pro-side work; report the
  Air's hostname + API key and the user will have it built there.

The design rationale behind all of this is in `CLAUDE_AIR_PERIODIC_HANDOFF.md`
in the Pro's project root, if the user copies it over.
