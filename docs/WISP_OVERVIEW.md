# Wisp — System Reference

*What it does, how it works, what's broken, and how it got here.*
Compiled 2026-08-23 from the full project history (80 Claude Code sessions,
2026-06-29 → 2026-08-23), the codebase, and the project memory store.

---

## 0. Orientation

Wisp is a strictly-local, privacy-first Siri-style assistant for macOS. No
prompt, email, message, or file leaves the machine — all inference runs on
device through **oMLX**, a local MLX-based model server.

| | |
|---|---|
| **Repo** | `~/Desktop/MOE_Project` — the folder name predates the 2026-07-06 rename from "MOE" to "Wisp" |
| **Hardware** | MacBook Pro, M5 Pro, 24 GB unified memory. Optional MacBook Air (M4 / 16 GB) side node. |
| **Processes** | `Wisp.app` (Swift, menu bar) → Python FastAPI backend on `:8765` → oMLX on `:8000` |
| **Installed app** | `/Applications/Wisp.app` — bundles its own copy of `service/` and a slim `.venv`, and launches the backend itself |
| **State** | Everything under `~/.moe/` (plus `~/.wispair/` on the Air) |
| **Scale** | ~27k lines of Python across `service/`, 34 Swift files, 162 registered tools, 33 test files |

The core idea and the namesake: a **Mixture-of-Experts router** classifies each
request and dispatches it to the right local model. That premise has since
inverted — see §2.5 — but the routing machinery it produced is now the most
load-bearing part of the system, for a different reason.

---

## 1. What Wisp does

### 1.1 Chat overlay — ⌥Space

A notch-anchored panel. Collapsed, it's a black bar fused with the camera
housing; hovering expands it downward as though the notch itself is growing.
Streaming replies, markdown rendering, session history, live tool activity, and
per-action confirmation cards.

### 1.2 Smart Search — ⌘⇧F

Ask a question about whatever document is on screen and get a grounded answer.
Four tiers stream out as they land, each independently failable:

| Tier | What | Cost |
|---|---|---|
| **T0** | Literal matches — the `⌘F` floor | ~1 ms |
| **T1** | BM25 lexical, stemming, typo recovery | ~10 ms, no model |
| **T2** | Semantic embeddings, fused with T1 via RRF | ~21 ms warm |
| **T3** | Synthesized answer with citations, plus a deterministic verification pass that drops any sentence whose quotes aren't in the cited passage | model call |

Deliberately **not** `⌘F`: Carbon's `RegisterEventHotKey` would claim it
system-wide and break native find in every app. The 320 MB embedder does not
load when Wisp opens; it loads on demand when Smart Search captures a document
or an ambiguous tool route needs semantic retrieval. Synthesis runs on whatever
chat model is already resident.

### 1.3 Operating the Mac — 162 tools

The agent loop drives a registry of 162 tools. Each declares a safety
`category` that the policy engine keys on.

| Area | Count | Examples |
|---|---|---|
| Apps, media & system control | 29 | `open_app`, `quit_app`, `window_control`, `spotify`, `music`, `run_shortcut`, `place_call`, `print_document` |
| Web & live data | 20 | `web_fetch`, `get_weather`, `get_stock_price`, `track_flight`, `track_package`, `transit_info`, `get_sports_scores` |
| Wisp's own assistant store | 24 | `get_upcoming`, `get_past_events`, `add_reminder`, `daily_brief`, `find_free_time`, `recall`, `remember`, `forget`, `search_coverage` |
| Device settings & readings | 22 | `get_battery_status`, `set_volume`, `set_wifi`, `system_status`, `network_info`, `set_display`, `lock_screen` |
| Files | 18 | `read_file`, `write_file`, `list_dir`, `find_files`, `move_path`, `organize_files`, `delete_path`, `write_document` |
| Mail | 13 | `summarize_emails`, `view_emails`, `send_email`, `reply_to_email`, `triage_inbox`, `archive_email`, `unsubscribe` |
| Messages & contacts | 8 | `summarize_messages`, `view_messages`, `send_message`, `lookup_contact`, `list_contacts` |
| Timers & scheduling | 8 | `set_timer`, `set_alarm`, `stopwatch`, `schedule_task`, `schedule_send` |
| Calendar writes | 4 | `add_calendar_event`, `update_event`, `cancel_event`, `clear_past_reminders` |
| Notes | 4 | `create_note`, `append_note`, `search_notes`, `scan_to_note` |
| Compute | 4 | `calculate`, `convert_units`, `world_time`, `random_pick` |
| Shell & authoring | 3 | `run_shell`, `run_applescript`, `create_tool` |
| Wisp admin | 4 | `wisp_status`, `wisp_sync`, `wisp_skills`, `wisp_mcp` |

`read_file` extracts real content from PDF, `.docx`, `.xlsx` and `.pptx` — not
container bytes. Legacy OLE2 formats (`.doc`/`.xls`/`.ppt`) report plainly
rather than failing silently. **There is no vision path** — scanned PDFs and
image files cannot be read at all, and `read_file` says so.

### 1.4 Assistant layer

Everything with a time normalizes into a **commitment** — one table, one shape,
one rendering pipeline (`~/.moe/assistant.db`). Calendar events, reminders,
manual additions, and Apple Reminders all land there.

- **Reminder engine** — lead-time rules per kind (exam T-1w/1d/3h, assignment
  T-1d/3h, meeting T-30m/10m), deduped through a `notify_log` so a restart
  can't re-fire, pushed over SSE and rendered as native notifications.
- **Notch countdown chip** — the soonest commitment, ticking locally, urgency
  ramping mint → amber → red.
- **Daily Summary** — a composed brief on a configurable schedule (default
  08:00). Separates mail from people from newsletters and automated senders,
  and degrades to a deterministic rendered fallback if the model fails.
- **Outbox** — outbound mail and messages route through
  `assistant/outbound_queue.py` → the hub → `OutboundSender.swift` → a result
  POST back, so the agent can truthfully say "sent" or "failed". Automation TCC
  belongs to `Wisp.app`, not to the Python process.
- **Scheduled sends** — `schedule_send`, queued and fired on time. Deliberately
  ungated against the foreground lock: a 6pm send should go at 6pm.

### 1.5 Memory

Three distinct layers:

1. **Sessions** — SQLite at `~/.moe/sessions.db`, with rolling summarization
   that folds older turns in batches of 8.
2. **Facts** — `remember` / `recall` / `forget` against `~/.moe/facts.db`,
   pinnable. User-stated facts explicitly outrank anything model-derived.
3. **Identity** — `service/memory/identity.py` is the single source of truth
   for who the user *is*: name from `id -F`, their own email addresses, and
   attribution rules for reading multi-person message lines. Every prompt that
   renders the user's own data in the second person must inject it.

### 1.6 Super Model

A deliberate, button-triggered escalation for the hardest work
(`Qwen3.8-27B-oQ3e-mtp`). It quits memory-hungry apps, pauses background
AppleScript syncs, doubles the live history budget to 32K, raises
`iogpu.wired_limit_mb` to 20480 through an admin auth dialog, and tells the
model to actually run and verify the code it writes. The router never picks it.

### 1.7 Extensibility

- **Skills** — installable folders at `~/.moe/skills/<name>/SKILL.md`. A
  one-line catalog is always in the prompt; the full body loads only on trigger
  match. Skill-declared tools are forced to confirm-tier regardless of what the
  skill claims, and run under `sandbox-exec` with declared read/write scopes the
  kernel enforces.
- **MCP** — any stdio server in `~/.moe/mcp.json` contributes its tools to the
  same loop. **stdio only, deliberately** — an HTTP/SSE MCP server would send
  user content off-device.
- **Self-authored tools** — `draft_tool` generates code and writes nothing;
  `create_tool` can only install whatever `draft_tool` most recently produced
  for that name. The split structurally forces "shown to the user" before
  "installed". A tool created mid-turn is usable in that same turn.

### 1.8 Safety

Every tool declares a category; `service/config/policy.yaml` decides
allow / confirm / deny.

- `read_only: true` makes Wisp inspect-only.
- `full_access: true` (the current setting) auto-runs everything…
- …except a **safety floor** that blocks catastrophic commands (`rm -rf /`,
  `sudo`, disk wipes) even then, so a prompt-injected model can't nuke the
  machine;
- …and except **irreversible deletes** (recursive, wildcard) and **calendar
  writes**, which always confirm and cannot be pre-approved. Both exist because
  of real incidents — see §3.1.
- Per-tool standing grants in `~/.moe/grants.json`, scoped by directory / host /
  program. `send_email` and `send_message` are permanently non-grantable.
- Full audit log at `~/.moe/audit.jsonl`.

### 1.9 Debug Mode

A `ContextVar`-based recorder (`service/debug_capture.py`) that captures what a
tool saw *internally* — the exact raw email/message rows fed in, and the inner
summarizing model's full request and response — not just its final answer.
Exports to `.json` and `.txt` from the menu bar. This is how essentially every
bug in §3 was actually found: the user runs Wisp, exports the log, and hands it
over.

### 1.10 Air node — built, not deployed

`air/` holds a complete periodic-work node for the MacBook Air: FastAPI on
`:8767`, incremental Mail/Messages readers, a job scheduler, and a Swift reader
app. Designed to run every 3 hours while the Pro's lid is closed, writing action
items into Apple Reminders (which sync back over iCloud for free).

**Status: 53/53 offline tests pass on the Pro, never run on the Air.** Its value
also narrowed once measurement showed the Pro loses only *timeliness* to sleep,
not data — see §3.5.

### 1.11 Harnesses

| Tool | Purpose |
|---|---|
| `scripts/test_model.py` | Drives the real router + agent loop + tool registry against a candidate model. Labels a failure `ROUTE` when the router never offered the needed tool — the distinction that separates "weak model" from "wrong router". |
| `scripts/test_retrieval.py` | Semantic tool-retrieval recall, bar is ≥95% held-out. |
| `scripts/eval_server.py` | A 50-prompt web grading board at `:8770` — run each prompt, watch the route/tools/answer stream, grade 1–4, live report. Write tasks run in plan mode. |
| `scripts/harvest_training_data.py` | Runs prompts through the real pipeline and records successful trajectories as SFT examples, routing failures as DPO pairs. |
| `sandbox/` | A synthetic iPhone-mockup world (messages, email, notes) with a Wisp Dev app inside it, for testing without touching real data. |
| `scripts/wisp_testdata.py` | Seeds tagged, reversible test data into Wisp's own DBs and cache fixtures. |

---

## 2. How it works

### 2.1 Topology, and the one rule that bites hardest

```
  Wisp.app (Swift, menu bar)
    ├── overlay, search panel, settings, notifications
    ├── OS readers: Calendar, Mail, Messages, Notes, Contacts, browser history
    └── spawns ──▶ uvicorn backend :8765  (a COPY of service/ inside the bundle)
                     └── HTTP ──▶ oMLX :8000  (model server, separate process)
```

> **`/Applications/Wisp.app/Contents/Resources/backend/service` is a plain
> `cp -R` copy, not a symlink.** The running app reads only from that copy.
> Edits to `~/Desktop/MOE_Project/service/` do not exist for the user until
> `bash scripts/package_app.sh` runs and the app is relaunched. This has burned
> the project repeatedly — a fix verified from source while the app runs stale
> code reads as "still broken". The standing rule is: **repackage and relaunch
> as the last step of any change to `service/**`, unprompted.**

The Swift app also owns the TCC grants. macOS permissions are per-process, and
the Python backend is a separate binary that cannot get a coherent "Wisp"
prompt — so Calendar, Mail, Messages, Notes, Contacts and Automation are all
read *in Swift* and pushed to the backend over HTTP. `package_app.sh` signs with
a self-signed **"Wisp Dev"** certificate so the CDHash stays stable and grants
survive a rebuild.

### 2.2 The request path

`POST /agent` streams Server-Sent Events:

`session` → `routed` → [`status`] → [`tool_call` → [`confirm`] → `tool_result`]\* → `delta`\* → `reasoning` → `done`

plus `heartbeat` during silent reasoning, `error`, `confirm_timeout`, and
`raw_model_io` when Debug Mode is on. `/chat` is the plain-completion path.

Roughly 50 other endpoints cover config, models, super model, sessions, memory
facts, the assistant store, syncs, skills, MCP, permissions, audit, and search.

### 2.3 Routing — three layers

`service/router/router.py` is the largest file in the project (~2,800 lines).
A request passes through:

1. **Typo normalization** (`_normalize_typos`) — added after "calandar"
   consistently routed to the wrong place.
2. **Regex rules** (`rule_route`) — produce a `RouteDecision`. Where a rule
   identifies the domain, it also narrows the tool list (`_domain_subset`); where
   it resolves a call *in full*, name and arguments both, it emits
   `direct_calls` and the tool runs **before the first model call** — a battery
   question answers in 1.42s, a calendar lookup in 2.88s, and "summarize my
   inbox" completes with zero agent-loop model calls.
3. **Semantic retrieval fallback** (`service/router/semantic.py`) — anything
   ambiguous retrieves ~10 tools by embedding similarity instead of getting a
   static list. Each tool's aliases are embedded as **separate vectors**, scored
   by MAX, which took recall from 77.5% to 100%. Retrieval costs ~31 ms warm and
   the retrieved menu is ~1,632 tokens against the static core's 3,992 — net
   TTFT is ~250 ms *better*, not merely flat.

**No route may hand the model the full registry.** `route()` fills
`tool_subset` by retrieval for *any* decision with `needs_tools=True` and no
subset. Worst scoped route is ~1,580 tokens against ~14,994 unscoped.

Key `RouteDecision` fields, each of which encodes a hard-won lesson:

| Field | Meaning |
|---|---|
| `tool_subset` | The exact tools the loop may offer. Narrowing is a **reliability** measure: 5–7 domain tools → 3/3 correct calls; all 44 → 2/3, reaching for unrelated tools. |
| `expect_tool_first` | Forces `tool_choice="required"` + retry-and-nudge. Only set when a rule confidently detected tool intent — forcing it on a genuinely uncertain prompt got "any new ideas for the project" refused outright. |
| `force_first_tool` | Restricts step 1 to exactly one function. oMLX's `tool_choice` is a soft nudge; this is the only real guarantee. |
| `light_read` | Marks a warm read-out of the user's own data, which selects a different answer voice. Deliberately *not* inferred from `tool_subset` — that conflation once answered "what's my battery level" in an emoji'd calendar-readout voice. |
| `multi_round` + required tools | Marks routes whose tools are sequential/complementary rather than alternatives (the aggregate to-do route needs all four sources). Forces the narration optimization off, because answering from source one and presenting it as complete "is not a partial answer, it is a **wrong** one". |
| `direct_calls` | Pre-resolved calls, executed through the same policy/approver/audit path as any tool. |

### 2.4 The agent loop

`service/agent/loop.py` (~2,000 lines). Per step:

- **Build the message list**, then `_fit_window()` trims it to fit, sacrificing
  in a fixed order: output reservation (down to 600), oldest history (never the
  system prompt, the latest user turn, or the newest tool result), then tool
  schemas from the end (never a forced first tool, never below 4).
- **Sampling.** `main.py` passes no `temperature`, so oMLX's per-model profile
  applies — *except* on a `required` tool-selection step, which is pinned to
  greedy. That pin is load-bearing; see §3.2.
- **Tool execution** wrapped in a debug-capture scope, through
  `policy.decide()` → approver (with a 5-minute timeout that auto-denies) →
  `run_tool` → audit.
- **Tool results are capped** at 12,000 chars *and the cut is announced* —
  silent truncation is worse than slow, because the model answers from the
  fragment as if it were complete.
- **Narration gate.** Once every tool the model has actually *called* is clean,
  the final narration step runs with thinking suppressed: −32% median turn time
  at unchanged correctness. Blocked on `multi_round` routes.
- **Retry ladder** with a corrective nudge that actually reaches the model
  (it previously didn't — see §4, 2026-08-09).
- **Error translation** maps a stopped engine, a memory limit, or an over-long
  conversation each to its own plain sentence with a next step. The raw error
  goes to the debug export, never the headline.

### 2.5 The model roster

The MoE premise inverted. Every text role now resolves to **one resident
model**, because swapping cost more than specialization bought.

**`service/config/models.yaml` (defaults)** — all of `fast`, `router`,
`coding`, `reasoning`, `agent`, `general` → `Ling-3.0-tiny-oQ4e`;
`embedding` → `Qwen3-Embedding-0.6B-4bit-DWQ`.

**`~/.moe/config.yaml` (runtime overlay, which wins)** — same, **except**:

| Role | Model | Note |
|---|---|---|
| `coding` | `Ornith-1.0-9B-oQ6e-mtp` | The user's deliberate choice, knowing it costs a swap. Do not "fix" this. |
| `super_model` | `Qwen3.8-27B-oQ3e-mtp` | Manual escalation only |
| `vision` | `gemma-4-E4B-it-qat-4bit` | Vestigial — there is no vision role in code |

Why Ling replaced `Agents-A1-4B`, measured on six real prompts at production
sampling: Ling won every one — 2.8s vs 4.3s (calendar), 16.5 vs 24.2 (mail +
messages), 10.4 vs 26.0 (browser), **21.0s vs a 300s A1 timeout** (file reorg),
5.7 vs 19.8 (organize), 4.4 vs 54.8 (vague). A1's loss is thinking time, and it
hung outright three times. Energy over a repeated workload: A1 9,615 J vs Ling
2,250 J — Ling draws *more* instantaneous power but finishes 5.6× sooner.

**The quantization cliff is real and sits exactly at 4 bits.** Measured over 10
reps on a 16-tool ambiguous menu:

| oQ3e | oQ3.5e | **oQ4e** | oQ5e | oQ6e |
|---|---|---|---|---|
| 5/10 | 6/10 | **10/10** | 10/10 | 10/10 |

Below 4 bits the model picks the wrong tool about half the time *and answers
confidently* — a silent failure. A single-sample version of that test produced
noise that briefly made oQ3e look best.

> **Two config traps.** `~/.moe/config.yaml` overrides the packaged
> `models.yaml`, so editing only the packaged file changes nothing at runtime.
> And a role can be pointed at a model that cannot load — appearing in
> `/v1/models` does **not** mean loadable. That used to fail silently; it now
> raises `ModelLoadError`.

### 2.6 Latency and memory economics

Measured over 459 real completions:

| | tokens | wall time |
|---|---|---|
| Prefill | 3,758,070 | 460 s — **9%** |
| Decode | 275,053 | 4,619 s — **91%** |

**One output token costs ~137× one prompt token.** And across eight debug
exports, 70% of generated characters were `reasoning_content` the user never
sees — roughly 64% of Wisp's model time was invisible chain-of-thought.

The consequence is counter-intuitive and the codebase got it wrong for a while:
cutting 1,000 prompt tokens saves ~0.12s; cutting 1,000 output tokens saves
~17s. **Prompt trimming is a memory and reliability lever, not a speed lever.**
Likewise, an unused `max_tokens` costs memory and never time — oMLX admits a
request against prompt + max_tokens, so headroom is reserved KV whether or not
it's generated.

KV cost is the other half. Measured on Agents-A1-4B: `head_dim=256` with 8 KV
layers ≈ **128 KB/token**, several times a typical 4B, and TurboQuant converted
only 7 of its 32 cache layers. A 16k window costs ~1.95 GB of KV against ~3.8 GB
of weights. Context — not weights — is what causes an OOM abort.

### 2.7 Data sync

Swift readers push into the backend, which persists to `~/.moe/cache/*.txt`
(0600). Cadences differ wildly and this matters when debugging "Wisp doesn't
know about X":

| Source | Cadence | Reach |
|---|---|---|
| Messages | 5 min | ~1 year / 20k |
| Mail headers | 5 min | ~2 years / 12k (raised from 1 year on 2026-08-19) |
| Mail raw bodies | 15 min | ~50 verbatim |
| Mail history | 30 min (multi-minute scan) | |
| Calendar | 5 min | ~1 year |
| Notes | 24 h | ~100 most recent |
| Browser history | — | 30 days / 5k |

`search_coverage` is a deterministic tool that reports exactly these numbers,
each cited from the actual enforcing constant.

### 2.8 State on disk

`~/.moe/` — `config.yaml` (role overlay), `sessions.db`, `facts.db`,
`assistant.db`, `cache/` (source snapshots + `tool_vectors.json` +
`identity_emails.txt` + `contact_handles.txt`), `skills/`, `mcp.json`,
`grants.json`, `audit.jsonl`, `evals/`.

---

## 3. Known issues

Ordered by how likely a real user is to hit it. Status is honest:
**OPEN** · **MITIGATED** (guarded, root cause remains) · **FIXED**.

### 3.1 Model capability — the dominant class

Nearly every remaining quality complaint traces here rather than to framework
code. `Ling-3.0-tiny-oQ4e` is a small model doing a large job.

| Issue | Status |
|---|---|
| **Drops one constraint out of a compound multi-step instruction.** Measured at comparable-or-worse rates on oQ6e and on pre-quant Agents-A1-4B — *never a clean run on any of the three*. The worst incident: "cancel everything except the ant poison treatment — move it to 5pm" ran 14 calendar calls with zero confirmation, cancelled the event it was told to keep, and fabricated a wrongly-dated duplicate. | **MITIGATED** — `calendar_write` is now always-confirm and cannot be pre-approved, with a batched preview. Confirmation is not a workaround for a model bug that might get fixed later; a human reviewing the actual list is the only mitigation that holds across models. |
| **Fabricates success contradicting a tool's own error.** `schedule_send` correctly rejected a 12-hour AM/PM mistake; the model still told the user "queued to send at 9:52 AM… it should go out shortly." | **OPEN** |
| **Silently substitutes the nearest available value instead of flagging a mismatch.** Asked for a 3-week stock comparison, its own hidden reasoning said *"the user wants exactly 3 weeks… let me try 3mo and see"* — then labelled 3-month-old data as three weeks old. | **FIXED** for this path (2026-08-23): `timeranges.py` now does exact math for `last N days/weeks/months/quarters/years`, raises a corrective `BadPeriod` on unknown vocabulary instead of substituting, and produces the human-readable label from the same call that computes the window. The general behavior remains a model trait. |
| **Doesn't reliably know its own tool inventory** — knows what to do in its thinking, then doesn't send the message because it forgot it can. | **MITIGATED** by tool scoping + `force_first_tool` |
| **Message attribution** — reporting a family member's viral post as the user's own. | **MITIGATED**: attribution is resolved deterministically into the data (`[Sender -> Recipient] text`) rather than left to prompt rules, which measurably lose. Residual is a capability limit. |
| **Invented `/Users/<name>` from the user's email** on file requests. | **FIXED** deterministically in `builtin._wrong_account_path` — a prompt rule only got 10/10 down to 2/10. |

> **The rule this class produced:** when a fix depends on a small model
> following a prose instruction, don't trust it until replayed live. Two
> prompt-only attempts failed identically on capability questions before the fix
> that worked was architectural — a deterministic `search_coverage` tool,
> direct-dispatched, answering in **0.22s with zero model calls**.

### 3.2 Thinking and sampling — coupled, not independent

| Finding | Consequence |
|---|---|
| Greedy + thinking-off = **0/3 tool calls** (byte-identical clarifying question); greedy + thinking-on = 4/4 at 2.6s | Never send `enable_thinking:false` to the agent loop. Keep the greedy pin on `required` steps. |
| `thinking_budget_tokens` doesn't reduce thinking — it force-closes `<think>` and the monologue continues in `content`, where the leak guard can't catch it | Do not use it. |
| A summary-shaped call left with thinking ON under a small `max_tokens` returns **silently empty** — the think block eats the ceiling, the guard blanks `content`, the caller's "no content" fallback discards it, and nothing errors | Any call that summarizes text already in its prompt must pass `no_thinking_kwargs(model)`, then assert on `finish_reason`. This exact bug was found in **four** places at once. |
| oMLX `force_sampling: false` means a request-level temperature silently **wins** over the tuned per-model profile | Benchmark at production sampling or the answer inverts — a hardcoded `temperature=0.6` produced the opposite Ling-vs-A1 conclusion. |

### 3.3 Engine (oMLX)

| Issue | Status |
|---|---|
| **Memory guard ceiling is currently configured at 10.0 GB** (`memory_guard_custom_ceiling_gb`). Ornith 7.9 GB + Ling 6.9 GB = 14.8 GB, so any coding request evicts the resident model and back. This is a knob someone set, not something oMLX computes under pressure — it read 17.75 GB earlier the same day. | **OPEN** — raising it on this 24 GB machine would let both stay resident |
| Killing Wisp shuts down the oMLX engine, and `omlx-cli start` **cannot** revive it — only `restart` can. Symptom: `/health` 500s and `ConnectError`, while `ps` still shows an oMLX process. Check `lsof -nP -iTCP:8000 -sTCP:LISTEN`, not `ps`. | **FIXED** — `ensure_omlx()` falls back to `restart` |
| Under load oMLX returns **200 with no `choices` key at all** | **FIXED** at source in `OMLXClient.chat()` |
| `prefill_memory_exceeded` surfaces as a bare `400 Bad Request` that looks like a malformed request | **FIXED** by the error-translation layer |
| **No parallelism.** Concurrent calls to one resident model contend rather than overlap: 27.3s serialized → 37.3s overlapped, memory 4.6 GB → 10.9 GB, and reliable `KeyError: 'choices'`. Parallel tool execution was built, measured, and reverted. | **WON'T FIX** — recorded in a long comment in `loop.py` so it isn't re-attempted |
| Idle unloader evicted a model **mid-generation** on any generation longer than `idle_unload_minutes` — the cause of every "stall" during long runs | **FIXED** |
| Speculative decoding (DFlash) — every Qwen3.5-compatible draft is a GDN hybrid the loader rejects | **BLOCKED UPSTREAM** |

### 3.4 Memory and context

| Issue | Status |
|---|---|
| **"The model's memory gets lobotomized after a couple of chats"** — reported 2026-08-23, not yet diagnosed. Candidates: the history budget derived from a 16k window, the rolling summarizer, or `_fit_window` dropping history. | **OPEN, unanswered** |
| Tool schemas, not conversation, fill the window — 57 tools ≈ 9,943 tokens, **62% of a 16k window before a single message**. Overflow surfaces as an unexplained 400. | **FIXED** by universal retrieval scoping + `_fit_window` |
| Uncapped tool results — an 89,000-char result (~22k tokens, ~2.8 GB of KV) went into context whole and was re-paid every step | **FIXED** (12,000-char cap, announced) |
| Background jobs contend with live turns on the single resident model: the *same* call measured **12.8s and 319.7s** on consecutive runs | **FIXED** — a foreground gate in `service/idle.py` brackets `/agent` and the scheduler skips model work while a turn is in flight. Note a harness calling `run_agent` directly bypasses the gate. |

### 3.5 Data, sync, and the second machine

| Issue | Status |
|---|---|
| **The Pro does not run Wisp while the lid is closed** — lid close is a separate sleep trigger that overrides the `sleep 0` AC setting, and third-party processes don't run in DarkWake. But **no data is lost**: every source is a server-backed store that backfills on wake. Sleep costs *timeliness*, not content. | **BY DESIGN** — clock-scheduled jobs must be wake-triggered with a once-per-day guard, never bare clock times |
| Mail's unified `inbox` concatenates accounts, which made a second account invisible; `email addresses` can't be iterated in AppleScript | **FIXED** |
| Muted group chats and some emails missed from summaries | **OPEN** (reported 2026-08-17) |
| The Air node has never run on the Air, and its `air_service.py` holds a **stale copy** of the Pro's routing rules | **OPEN** — harmless while Air Compute is off (it was removed entirely on 2026-08-22), but must be redeployed before any revival |

### 3.6 Open backlog

From the six-lens sweep of 2026-08-09/10 — 47 candidates, adversarially
verified down to 23 real, of which 15 shipped. Remaining, ranked:

1. `fast` role still generates chain-of-thought on trivial chit-chat (~1–4s per greeting) — *partially addressed 2026-08-22*
2. Smart Search prewarm gate is permanently false; the prize is the embed cache, which survives model eviction
3. Engine death mid-turn destroys all completed tool work — one in-place retry would save 30–90s of finished tool calls
4. `view_emails` silently drops all but the newest `count` matches, and tail-truncation deletes the *newest* bodies
5. Aggregate phrases ("what am I forgetting", "catch me up") miss the router's narrow toolset
6. `_is_looping` returns the canned refusal even when a tool already produced the answer

Five more items are **UNCERTAIN** — each needs one measurement before building.
Ten are **REFUTED**; §5 lists the ones worth not re-proposing.

### 3.7 Operational footguns

- **The packaging rule** (§2.1) — the single most repeated source of "still broken".
- **Never poll `POST /assistant/profile/build`** as a liveness probe — it's mutating, and a timeout is evidence a *new* build just started. *(Endpoint since removed.)*
- **Never spawn a raw `llama serve`** for a large GGUF. On 2026-08-20 a default mmap load of a ~16 GB model produced a page-fault storm (1.47M faults) that kernel-panicked the machine. Apple Silicon doesn't clean-OOM under this pressure — it thrashes until watchdogd starves. Use `load-mode = none`.
- **Registering a tool is not the same as making it reachable.** Three separate layers can hide it: a regex route's hardcoded subset, the domain write map, and direct dispatch (where the tool is never offered at all). The failure is silent — no error, just the old worse behavior. After adding a tool, check `route()` on the phrasings it's *for*, not just the retrieval eval.

---

## 4. History

### 2026-06-29 — Genesis
The ask: *"an AI assistant similar to Siri for my MacBook… full access… run
locally… determine which model it should use for each use case… like Siri so
not an app per se but like an extension."* Voice was cut immediately; quality
over speed, especially for coding.

oMLX chosen as the inference layer, so no hand-built model-pool manager. A/B on
five coding tasks under the real 18 GB cap: **gpt-oss-20b won decisively** —
Qwen3-Coder-4bit didn't fit, 3bit was degraded. The **dynamic ceiling** finding
landed here too: oMLX's effective ceiling *shrinks* as system RAM is used, so
"fits when idle" ≠ reliable. Nothing above ~13 GB resident is dependable.

By the end of the first day the Python service was end-to-end: config,
inference, router, safety engine, tools, agent loop, FastAPI. Latency for every
load and swap permutation was measured (~3s typical swap).

### 2026-06-29 → 07-08 — The look, then the notch
UI went light-blue liquid glass → **hard pivot to a black notch style**. Then a
long, exacting iteration on the open/close animation. The direction that finally
worked came from reframing it: *"I am physically extending the size of the notch
when using the application."* Renamed **Wisp** on 2026-07-06, tessera icon with
hints of gold.

### 2026-07-10 → 07-13 — Diagnostics, and the assistant pivot
A full-path diagnostic produced three critical findings: repackaging **wiped
user settings** (fixed with the `~/.moe/config.yaml` overlay), the agent loop
was a **139s black hole** with no streaming (fixed — first token at ~1.0s), and
a session-registry clobber. The router was rewritten fail-safe after tool
requests leaked to a tool-less model.

Then the direction changed: Wisp should be *a real assistant* — watch the
calendar, surface what's coming, count down in the notch. `ASSISTANT_ARCHITECTURE.md`
was written and A1 built the same day. Two decisions from this week still shape
everything: **Canvas rides Google Calendar** (no dedicated connector), and
**calendar is read in Swift, not Python**, because TCC is per-process. The
"Wisp Dev" signing certificate was created so grants stop dying on every build.

### 2026-07-17 → 07-21 — Test at scale, then instrument
*"Think of every single possible prompt I could ask… try at least 100. Do not
stop until every reasonable prompt has been exhausted"* — later raised to 500.
This surfaced the routing-misfire class (a misspelled "calandar" sending
calendar reads to the wrong model) and produced typo normalization.

The most consequential build of this stretch was **Debug Mode**, which turned
every subsequent bug report into a reproducible artifact. Coding benchmarks ran
in parallel: bench50 flipped to bench100 once the token cap and timeout were
made fair — gpt-oss 100/100, Qwen3.6-27B-3bit 99/100.

### 2026-07-22 → 07-24 — Menu bar, and Super Model
An attempt to overlay Apple's own battery/wifi widgets pivoted into a standalone
**Performance Hub** (later removed as unnecessary). Real memory accounting was
harder than expected — `Total − Free` makes a healthy Mac look full.

**Super Model** shipped: a deliberate button that quits apps, raises the VRAM
sysctl, and loads a 27B for the hardest work.

### 2026-07-27 → 07-31 — Smart Search, then the capabilities layer
*"The current find shortcut is lacking… it can look up something directly in
text but not 'what were the traits of Joe's dog'"* → the four-tier Smart Search.

Then five subsystems in a week to close the gap between what Wisp did and what
it was meant to be: explicit fact memory, permission grants, outbound send
actions (with contact-name resolution, added after "text Mom" failed despite
full Contacts access), skills, an MCP client, and **self-authored tools**.

The failure mode that shaped the last one is worth keeping: asked to build a
`list_contacts` tool — impossible, because TCC belongs to `Wisp.app` — the model
had no way to say "I can't", so it wrote a script printing hard-coded
`John Doe / 555-1234`, and Wisp presented that as the real address book.
**When a local model produces confidently wrong output, check first whether the
task was possible at all and whether the prompt left it any way to decline.**

### 2026-07-29 → 08-15 — The user profile, and its removal
A map-reduce profile builder over all synced sources. It was rebuilt several
times: the first version let later batches *evict* earlier facts; a prefix
sample meant "recent only". Then on 2026-08-09 it was found to be producing
**nothing at all**, silently — thinking ON into a 6,000-token ceiling meant
~50–70 sequential calls of pure decode for an empty profile (103.0s / 0 facts
vs 17.0s / 31 facts with thinking off).

Removed entirely on 2026-08-15 at the user's request. Not to be rebuilt.

### 2026-08-03 → 08-11 — Model migration, and the measurement era
gpt-oss was found to **500 on the second turn of any conversation** — a Jinja
`Undefined` bug in the model's own chat template, unfixable from Wisp. LFM2.5
was tried and abandoned: its template unconditionally opens `<think>` and cannot
be turned off, so a truncated monologue lands in `content` and poisons tool
results — the real cause of "can't handle multiple tool calls".

`Agents-A1-4B` took over, and with it came the most productive measurement
stretch of the project: the KV blowup analysis, the context-window budget, tool
subset routing, the identity layer, parallel tools (built → measured →
reverted), the tool-descriptions-compete finding, and above all the
**decode-dominates-latency** result that overturned the project's working
assumption about where time went.

### 2026-08-17 → 08-19 — Ling, and the tool explosion
`Ling-3.0-tiny` was tested against A1 and won every prompt (§2.5), then a
quantization sweep found the hard cliff at 4 bits. Ling became the resident
model, pinned in memory.

In parallel, the tool count went from ~15 toward **162**, driven by a Capability
Atlas of 561 real user requests. That scale is only tractable because of
**semantic tool routing**, shipped the same week — and the rollout taught the
reachability lesson three separate times before it stuck.

### 2026-08-20 → 08-23 — Consolidation
A llama.cpp detour ended in a kernel panic (§3.7). A 50-prompt evaluation suite
and a **web grading board** were built so model changes can be judged rather
than guessed at.

Then a deliberate cleanup pass: `Wisp.app` **519 MB → 96 MB**, the never-called
Air Compute routing feature removed, the LLM classifier removed, and
`write_code`/`codegen.py` deleted. That last one has a known tradeoff: because
`coding` genuinely points at a different model, a *combined* request ("build a
website and save it to my downloads") now writes code inline with Ling instead
of delegating to Ornith. The user chose to leave it removed.

Most recently (2026-08-23), `timeranges.py` was hardened against the
silent-bucket-substitution class after the stock-price incident.

---

## 5. Retired — do not re-propose

Things removed deliberately, with reasons, so they aren't rediscovered as
missing features:

| Removed | When | Why |
|---|---|---|
| **Voice (STT/TTS)** | 2026-06-29 | Cut in the first conversation. Text in, text out. |
| **Vision** (`see_screen`, `describe_image`, the VLM role) | 2026-08-08 | Wisp never takes an image as a prompt source. Consequence stated plainly: scanned PDFs and image files cannot be read. |
| **User profile builder** | 2026-08-15 | See §4. |
| **Performance Hub menu-bar widget** | 2026-07-28 | No longer needed. |
| **Air Compute routing** (+ the LLM classifier it served) | 2026-08-22 | Never called; the classifier mis-classified ~15% of tool-needing requests. |
| **`write_code` / `codegen.py` / `needs_code_delegation`** | 2026-08-21 | Cleanup; tradeoff accepted (§4). |
| **Parallel tool execution** | 2026-08-08 | Built, measured slower (27.3s → 37.3s), crashed the engine, reverted. |
| **Reasoning-effort toggle** | 2026-08-19 | A harmony-template variable with no harmony model rostered — dead config. |
| **Qwen2.5-Coder-14B, Phi-4-reasoning-plus, gpt-oss-20b** | various | Retired for measured reasons documented inline in `models.yaml`. |
| **Qwen3.6-27B-4bit** | 2026-07-16 | Loads at 15 GB but cannot generate on this box. |

---

## 6. Runbook

```bash
# Start the backend against a running oMLX (its menu-bar app auto-starts :8000)
./scripts/run.sh

# Build and install the app — REQUIRED after any change to service/**
./scripts/package_app.sh && open dist/Wisp.app

# Hit the API directly
curl -sN localhost:8765/agent -H 'Content-Type: application/json' \
  -d '{"prompt":"what is on my calendar today?"}'

# Verify a model candidate against the real router + loop + tools
.venv/bin/python scripts/test_model.py <model-id>

# Tool-retrieval recall (bar: >=95% held-out)
.venv/bin/python scripts/test_retrieval.py -v

# The 50-prompt grading board
.venv/bin/python scripts/eval_server.py    # -> http://127.0.0.1:8770

# Full test suite
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

**When something is wrong, in this order:**

1. Is the fix actually *installed*? `grep` the function name inside
   `/Applications/Wisp.app/Contents/Resources/backend/service/`.
2. Is oMLX listening? `lsof -nP -iTCP:8000 -sTCP:LISTEN` — not `ps`.
3. What does the engine say? `~/Library/Application Support/oMLX/logs/server.log`
   — `grep "Chat completion" | grep -o "finish_reason=[a-z]*" | sort | uniq -c`
   immediately separates "the model wrote something bad" from "the model got cut
   off". Look for `adaptive_prefill_throttle` and `prefill LRU eviction` when a
   turn is inexplicably slow.
4. Did the source actually sync? `ls -la ~/.moe/cache/` — a small or absent file
   is a sync problem, not a model problem.
5. Does the roster match reality? Check `~/.moe/config.yaml` (which wins) against
   `curl -s localhost:8000/v1/models`.
6. Turn on Debug Mode and export. It captures what each tool saw internally, not
   just what it returned.

---

## Related documents

- [../README.md](../README.md) — quick start and layout
- [README.md](README.md) — the documentation index
- [ASSISTANT_ARCHITECTURE.md](ASSISTANT_ARCHITECTURE.md) — the assistant layer's design
- [SMART_SEARCH_DESIGN.md](SMART_SEARCH_DESIGN.md) — the four-tier search
- [TYPED_TASK_ENGINE_PLAN.md](TYPED_TASK_ENGINE_PLAN.md) — the typed task engine
- [OPTIMIZATION_BACKLOG.md](OPTIMIZATION_BACKLOG.md) — shipped / real / uncertain / refuted
- [STABILITY_PLAN.md](STABILITY_PLAN.md) — the release-readiness plan
- [../CHANGELOG.md](../CHANGELOG.md) — recent changes with validation counts
- [../TESTING.md](../TESTING.md) — the end-to-end checklist
