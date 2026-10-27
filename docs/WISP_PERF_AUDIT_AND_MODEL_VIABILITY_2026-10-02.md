# Wisp performance audit and model-viability study — 2026-10-02

Hands-on measurements on this machine (M5 Pro, 24 GB, oMLX 0.6.x, Ling-3.0-tiny-oQ6e resident).
Everything below was measured fresh in this session. Earlier audits and reports were not used as evidence.
No repository code was changed. Scripts and raw data are in `docs/perf-audit-2026-10-02/`.

---

## 1. Bottom line

1. **Decode speed is not why Wisp feels slow.** Ling decodes at about **121 tok/s, flat from 0.5k to 12k tokens of context**. The model-side time of real Wisp turns is only 0.2–2.7 s. The same turns take **7–11 s** through Wisp as installed, and **0.3–2.7 s** with one layer taken out of the path (section 3.1). Doubling decode speed would save roughly 0.04–0.8 s per turn, about 10% or less of the stock turn time.
2. **The biggest cost is Wisp's own oMLX peer-attribution layer, not the model.** `attributed_transport.py` rebuilds and re-verifies trust on every call by walking about 47,000 files and spawning dozens of `lsof`/`ps` processes (about 45 per request by my count). That costs about **1.7–2.1 s per oMLX call**, even for `/health`, which takes 1 ms directly. A trivial turn makes 4–5 such calls. On the audited code it also caused intermittent refusals (fixed upstream since by `7aeb196`, not yet installed) and a constantly busy idle backend. This is a regression: it appears in real usage data on **2026-09-21**, the day commit `538ff12` landed.
3. **Ling has many couplings, but only a few are hard blockers.** The hard ones are a hardcoded model id in the web router, the per-model allow-lists for thinking suppression, and token budgets calibrated to Ling's very short reasoning. Details in section 4.
4. **No tested alternative is a drop-in improvement.** Every dense 2.6–9B model is slower on prefill (1.2–2.7×) and, except gemma-E4B at short context, on decode (1.2–2.6×). All four are also about 3–5× slower per tool call, mostly because they reason longer. They do score higher (90% versus 77%) on first-tool selection against a raw 17-tool menu, but Wisp's router normally narrows that menu. Ling's advantage is speed and cheap memory, not accuracy. Section 5.

---

## 2. Method and what was and was not touched

- **Direct engine probes:** my own streaming client against oMLX `:8000`, bypassing Wisp, so engine speed is separated from Wisp overhead.
- **Wisp overhead:** the real `OMLXClient` and `attributed_transport` code (the installed 1.1.5 app has byte-identical inference files) called from separate processes, plus an isolated backend on `:8775` with the synthetic sandbox on `:8766` and a throwaway `WISP_HOME`.
- **Real-usage evidence:** oMLX's `server.log` (about 11.7k Ling completions since 2026-08-18). Caveat: much of that traffic is the stress/regression suites, not interactive use. I use it for trends and say so where it matters.
- **Candidate models:** same harness for all, loaded one at a time, with Ling restored after each.
- **Not touched:** the real backend on `:8765`, the real Mail/Messages/Calendar, and the repo.
- **Counterfactual patches** (skip attribution) lived only in a scratchpad launcher, never in the repo.

### Side effects on your machine (disclosure)
- I swapped the resident model several times. Ling oQ6e was unavailable for about 2 min at 17:56 and for **about 12 min from 18:10:39 to 18:22:34**. A stray isolated backend of mine, started with oQ4e roles before I corrected them, idle-unloaded it. I killed it and reloaded oQ6e. Further short outages came from candidate swaps (18:25–18:37).
- **oMLX's SSD prefix cache grew from 66.9 GB to 83 GB** from my cold-prompt benchmarks. oMLX bounds it (auto max about 187 GB), and I did not delete anything. Say the word if you want it trimmed.
- Final state: `Ling-3.0-tiny-oQ6e` resident, no leftover processes of mine.

---

## 3. Why "fast decode" still feels slow

### 3.1 Where a turn's time actually goes

Same prompts, same model, isolated backend, 2 reps each.

| Turn | Stock Wisp (s) | Attribution layer bypassed (s) | Model-side time (oMLX log) |
|---|---|---|---|
| "hello" | 8.1, 8.8 | 0.3, 0.3 | 0.22 s (9 tokens) |
| "summarize my unread emails" | 9.0, 11.1 | 1.2, 1.4 | 1.14 s (tool-select step, 3.7k prompt, 51 tokens) |
| "explain a bloom filter in two sentences" | 9.7, 7.9 | 2.7, 2.4 | 1.9–2.7 s (about 4.8k prompt, 107–200 tokens) |
| "what's on my calendar today" | 2.6, 2.6 | 2.6, 2.6 | none (no model call) |

The calendar turn is a 2.5 s `ensure_sources` wait for a native Reminders read that my sandbox never answers. It is a sandbox artifact, not a real-app measurement.

Sensitivity: with Ling at 121 tok/s, 2× faster decode saves about 0.04 s on "hello", 0.2 s on the email turn and 0.5–0.8 s on the bloom turn. Removing the transport tax saves 6.5–9.7 s on the same turns.

### 3.2 The five causes, ranked by measured impact

**A. Attribution tax: about 1.7–2.1 s per oMLX call (dominant).**
- Direct `GET /health` takes 1.2 ms. Through Wisp's client it takes 1.7–1.9 s. `/v1/models/status`, `/v1/models` and a 4-token completion measure the same way (completion: 48 ms direct versus 2.0–2.2 s through Wisp).
- Cost breakdown:
  - `RuntimeAuthority().load()` builds a new `DesktopOmlx` for every connection and costs **1.0–1.1 s**. It walks 47,319 files twice (`rglob` alone is 247 ms) and spawns 5 processes.
  - One `connected_peer()` check costs **266 ms with 12 subprocess spawns** (`lsof` ×6, `ps` ×4).
  - Per request I counted 7 `binding()` calls and 3 peer checks (about 45 process spawns by my count), because `CheckedStream.write` re-checks on every write that carries bytes and keep-alive is disabled (`max_keepalive_connections=0`).
- Calls per turn: a "hello" turn made **4 calls before the answer**: status 1.86 s, health 1.98 s, status 1.75 s, then chat 2.8 s. The agent loop adds a readiness status check before every step.
- Reliability: **12 of 89 attempted oMLX calls (13.5%) were refused** with `ModelLoadError: Local inference peer attribution unavailable`. The cause is `connection_kernel_unqualified`, where two `lsof` snapshots disagree because the background pollers overlap with the turn. In my runs this errored a warm-up turn and killed a bloom-filter turn outright. Upstream commit `7aeb196` (2026-09-30) fixes this by comparing only this connection's records and retrying transient inspection failures, and reports roughly half of live turns failing; it is not in the installed app.
- Real-usage trend (oMLX log, gap between a tool-call step ending and the next step starting):
  - median **0.3–0.4 s** through 2026-09-20;
  - median **3.1–4.6 s** on every real-use day from 2026-09-21 onward except 09-22 (p10 up to 3.5 s). 09-22 shows 53 samples at 0.5 s, which I could not explain and suspect is a harness run on a different path.
  - This matches commit `538ff12` ("Fix desktop oMLX peer attribution", 09-21). The per-write check dates from `0b1e3c8` (09-14).
- Idle cost, same root cause: the backend used **5 h 29 min of CPU in 20 h 49 min uptime (26% of a core)**. At rest it bursts to 40–87% every 3–4 s. A native sample shows `os_scandir`, `readdir`, `lstat` and `__fork` frames. The memory-capture loop (3 s sleep) and the idle unloader (30 s) both call `loaded_models()` through the same path, which is consistent with that burst cadence. I did not isolate which caller produced which burst.

**B. No streaming in the agent loop.** `loop.py` sets `stream = False` unconditionally ("verify before publication"). The user sees nothing until the whole generation, including hidden reasoning, is done. First visible text equals total latency. The plain-chat path does stream, which is why "hello" showed deltas.

**C. Thinking tokens.**
- Same question, thinking ON: 317–1,438 tokens, 3.9–12.1 s. Thinking OFF: 64–80 tokens, 0.6–1.35 s.
- In Ling's real completions, **16.3% of requests (those over 300 tokens) account for 63.8% of all generated tokens**. The median tool-call step is only 57 tokens, so a few long reasoning steps dominate the felt wait.

**D. Prefill.** Cold prefill is about **5,000 tok/s**. A 4k prompt costs 0.78 s, 8k costs 1.57 s, 12k costs 2.45 s. Typical Wisp prompts are 4–6k tokens, and a 17-tool menu alone is about 6k tokens.
- oMLX's paged prefix cache works in **2,048-token blocks**. At 8k tokens: cold 1.67 s, identical repeat **0.11 s**, tail-only change **0.54 s**, head change (miss) 1.62 s.
- In real traffic where a cache restore was logged, the token-weighted hit rate was 78.5% (n=179 requests of 1,500 tokens or more; misses are not logged, so this is an upper bound).

**E. Contention and cold start.**
- Two concurrent 8k requests: each decodes at 73–82 tok/s (solo 110) and TTFT doubles to 3.2–3.4 s. Background work that overlaps a foreground turn slows it.
- **First request after long idle:** my first call after about 20 h idle took **21.4 s for a 28-token prompt** (oMLX logged "1 tokens in 21.42s"), then 40–50 ms. Across the full log, for 1–6k prompts, after more than 1 h idle the p90 prefill is 13.2 s and p99 is 27.3 s, with 17% of requests above 3 s. After 5–15 min idle, 0% exceed 3 s. This is **consistent with** weights being paged out under memory pressure (swap 10.5–14.5 GB in use throughout, about 150 MB free with the model resident). I did not prove the mechanism.

### 3.3 Engine speed for reference (Ling oQ6e, thinking off, cold prompts, 3 reps)

| Prompt tokens | TTFT | Prefill tok/s | Decode tok/s |
|---|---|---|---|
| 494 | 0.19 s | 2,627 | 120.7 |
| 2,030 | 0.42 s | 4,817 | 122.4 |
| 3,992 | 0.78 s | 5,096 | 121.5 |
| 8,020 | 1.57 s | 5,121 | 120.7 |
| 11,993 | 2.45 s | 4,896 | 122.5 |

oMLX streams in bursts of about 13 tokens every 113 ms; the true rate is tokens divided by decode time. From the log, oQ4e decodes faster than oQ6e (p50 about 151 versus 116–121 tok/s), consistent with a bandwidth-bound model. That is log-derived across different periods, not a controlled A/B.

Why Ling is fast: `config.json` shows 24 layers, 128 experts with 8 routed plus 1 shared per token, expert size 512, and a hybrid linear-attention / MLA design. My estimate is about 1B active parameters of about 8.5B total (6.94 GB at about 6.5 bits). Only 6 of 24 layers hold KV, giving about **6.75 KB per token** (oMLX log), versus about 125 KB per token for the dense-attention 4B class. At 64k tokens that is about 0.4 GB versus about 8 GB.

---

## 4. Ling-specific optimizations and couplings

| # | Coupling | Where | What happens on a swap | Severity |
|---|---|---|---|---|
| 1 | **Hardcoded model id** `_LING_WEB_MODEL = "Ling-3.0-tiny-oQ6e"`; `_pin_ling_web_decision` forces `decision.model` to it on every web route | `router/router.py:988`, `:5692` and a dozen call sites | Every web query ignores the roster and swaps Ling back in, so you pay a model swap plus its attribution tax | **High** |
| 2 | **Per-model allow-lists**: `no_thinking_capable` and `tool_capable` | `config/models.yaml`, `config.no_thinking_kwargs` | A new model silently gets no thinking suppression. Summaries, narration, profile, capture and search synthesis then run with thinking ON under small caps. The code comments describe the failure: with thinking left on under a small `max_tokens`, the answer is silently blanked. | **High** |
| 3 | `user_facing_summary_kwargs` keeps thinking ON when `model.startswith("ling-")` | `config/__init__.py:248` | A different model is forced into the no-thinking path regardless of its template | Medium |
| 4 | Token and time budgets sized for Ling's short reasoning: brief summary 512 tokens and 12 s, 3,000 default step cap, 2,500 summarizer cap | `assistant/brief.py:715`, `agent/loop.py:1097`, `tools/email_tools.py:51` | Models that reason 3–10× longer hit `finish_reason=length` and get blanked answers. In my battery gemma-E4B's median was 398 output tokens per call versus Ling's 51. | **High** for verbose reasoners |
| 5 | **Prompt layout for Ling's template**: tool schemas follow the first system message, and the clock sits in a second system message | `agent/loop.py:1252`, `:1345` | All four tested models accept two system messages, so no breakage found. Cache behavior per template was not tested. | Low |
| 6 | oMLX per-model sampling profile (`ling-3`: temperature 1.0, top_p 0.95, top_k 20). Wisp omits `temperature` on purpose. | `~/.omlx/model_settings.json` | A new model runs on whatever profile exists, so each needs one tuned and measured | Medium |
| 7 | KV memory assumptions: Ling's 6.75 KB/token makes 64k windows and 6k-token tool menus nearly free | `~/.omlx` window setting, `agent/loop._fit_window` | Dense-KV models cost about 20× more per token, so windows must shrink and `_fit_window` will trim history and tools | Medium |
| 8 | Output parser `bailing_hybrid` detected by oMLX for Ling | oMLX | Another family uses another parser. Reasoning separation and tool-call streaming can differ. In my probe, qwen3_8_9b with thinking ON ran 600 tokens without closing its think block. | Medium |
| 9 | Failure-specific patches: `_wrong_account_path` (Ling inventing `/Users/<name>`), router clarify rules from "10 runs of Ling" | `tools/builtin.py`, `router/router.py:529` | Harmless on other models, but the regression corpus is tuned to Ling's failure modes (`eval_ling_tasks.py`, the stress suites) and would need re-baselining | Low |
| 10 | Strict `response_format` JSON-schema calls at temperature 0, "on the real Ling harness" | `discovery/local_model.py` | Depends on oMLX grammar support per model | Low–Medium |

**Model-agnostic (will carry over):** greedy pin on `required` tool steps, narration-step thinking suppression, `_demote_unclosed_think`, `_ensure_choices`, router scoping, the direct-dispatch path. I verified `tool_choice=required` at temperature 0 produced the correct call on all five models.

---

## 5. Other models: measured comparison

Same harness for all: speed from the streaming probe, plus a **15-prompt tool battery using Wisp's real schemas** (a 17-tool menu, about 6k prompt tokens), default sampling, thinking at the model default. "Strict" means every call is in the accepted set with the right argument. "Lenient" means at least one accepted tool and no unaccepted write/send. Candidates ran 2 reps (n=30); Ling ran 5 reps (n=75) and its first 30 calls are shown for like-for-like.

| Model | Resident | Decode tok/s (0.5k / 4k / 8k) | Prefill tok/s (4k / 8k) | TTFT at 8k | Battery strict / lenient | Per-call p50 / p90 | Output tokens p50 | Reasoning chars p50 |
|---|---|---|---|---|---|---|---|---|
| **Ling-3.0-tiny-oQ6e** (baseline) | 6.9 GB | 121 / 122 / 121 | 5,096 / 5,121 | 1.57 s | 77% / 80% (n=30); 77% / 81% (n=75) | **0.88 s / 1.28 s** | 50 | 134 |
| Agents-A1-4B-oQe6 | 4.3 GB | 58 / 57 / 56 | 1,856 / 1,948 | 4.11 s | 90% / 90% | 3.03 s / 10.2 s | 102 | 292 |
| gemma-4-E4B-it-oQ4e-mtp | 5.8 GB | 126 / 110 / 101 | 2,820 / 2,918 | 2.74 s | 90% / 90% | 4.60 s / 6.0 s | 398 | 1,368 |
| LFM2.5-2.6B-oQ6e | 2.4 GB | 95 / 87 / 76 | 4,314 / 3,481 | 2.31 s | 80% / 80% | 2.65 s / 3.95 s | 82 | 277 |
| qwen3_8_9b_mlx_4bit | 5.3 GB | 50 / 49 / 47 | 1,990 / 2,021 | 3.96 s | 90% / 90% | 2.36 s / 3.61 s | 64 | 166 |

Compatibility probes (all five): two system messages accepted; `tool_choice=required` at greedy returned the right tool. The `enable_thinking` toggle was honored by Ling, A1, gemma-E4B and qwen3_8_9b, but **not by LFM2.5**, which thinks at length either way. LFM2.5 also **called a tool that was not offered (`lookup_contact`) in 4 of 30 calls**; Wisp rejects unoffered calls, so that costs a retry.

Reading the results:
- **Speed:** Ling is the fastest on both decode and prefill, and its decode does not degrade with context (gemma-E4B falls from 126 to 101 tok/s by 8k, LFM from 95 to 76). Against Ling, the others are 1.2–2.6× slower on decode (gemma-E4B matches it at short context) and 1.2–2.7× slower on prefill.
- **Per-call latency** is also a story about reasoning length. gemma-E4B's median call took 4.6 s because it writes about 1,400 characters of reasoning before acting, where Ling writes about 130.
- **Accuracy:** on this raw, un-routed menu, A1, gemma-E4B and qwen3_8_9b beat Ling (90% versus 77%). Ling's misses cluster on four prompts: "text mom" 0/5, "email Dave" 0/5, "schedule lunch" 1/5, "Lakers game" 2/5. It tends to look things up (`get_upcoming`, `view_messages`) before acting. It made zero wrong write/send calls. Production routing scopes the menu to a few tools, so this gap is likely smaller in practice. I did not measure that.
- **Not tested here:** `gemma-4-26B-A4B` (16 GB, too large for this machine's memory headroom), cloud models through the OpenRouter binding (`fast` and `router` must stay local; tool use needs `qualified_capabilities`; a cloud agent role conflicts with the strictly-local goal), and any model not already installed.

**Verdict:** keep Ling as the default. Swapping would cost speed and memory for an accuracy gain that routing may already provide, and it would need items 1, 2 and 4 in section 4 fixed first. If quality is the goal, qwen3_8_9b is the best accuracy-for-latency trade (90% at 2.4 s p50) but is about 2.7× slower per call and has the longest-reasoning risk with thinking on.

---

## 6. Prioritized audit findings

| Pri | Finding | Evidence | Fix direction | Expected gain |
|---|---|---|---|---|
| **P0** | Peer-attribution tax | 1.7–2.1 s per oMLX call; turns 7–10 s versus 0.3–2.7 s floor; step gap 0.4 s to 3–4.6 s since 09-21 | (a) qualify `DesktopOmlx` once per oMLX pid incarnation; (b) turn on keep-alive and re-attest periodically; (c) replace `lsof`/`ps` with libproc (oMLX socket table in 0.1 ms versus 30 ms per `lsof`); (d) retry once on transient `connection_kernel_unqualified`; (e) do not re-check on every write | Most of the 6–10 s per turn |
| **P0** | Spurious refusals | 13.5% of calls refused; one turn killed outright | Already fixed on `origin/main` by `7aeb196` (2026-09-30); not in the installed 1.1.5 app | Needs a rebuilt app to take effect |
| **P0** | Idle CPU burn | 26% of a core average; bursts every 3–4 s | Falls out of the P0 fix. Then slow the capture poll or make it event-driven. | Battery and thermals |
| P1 | Redundant readiness calls | "hello" makes status, health, status before the model call, then status per step | Cache readiness for the turn; use health only on failure | 2–4 calls per turn |
| P1 | Cold start after long idle | 21.4 s repro; p90 13 s after more than 1 h idle | Tiny keep-alive request, less memory pressure, or the lighter oQ4e (about 1.8 GB smaller). Test each. | 10–25 s on the first message of a session |
| P2 | No streaming on agent turns | `stream = False` | Stream verified read-only routes, or stream reasoning progress | Perceived latency |
| P2 | Long reasoning tail | 16% of requests make 64% of tokens | Extend narrow no-thinking to more steps with a per-route A/B | Seconds on tail turns |
| P2 | Foreground/background contention | 110 to 73–82 tok/s with 2 concurrent requests | Keep the foreground gate strict | Tail latency |
| P3 | oMLX SSD cache at 83 GB (earlier today about 59 of its 67 GB were GDN sidecars) | `du`, oMLX cache scan log | Cap the cache size | Disk |

**Checked and healthy:** Swift app idle CPU 0–2%; backend RSS about 254 MB; `~/.moe` totals 160 MB; routing and workflow overhead is negligible (with attribution off, "hello" takes 0.3 s end to end).

**Unmeasured:** oMLX `gpu_keep_warm_interval: 0.5` power cost (needs `powermetrics`/sudo).

---

## 7. Limits of this study

- Turn timings were taken on an **isolated backend running the same code** as the installed 1.1.5 app, not by instrumenting the live app on `:8765`. The step-gap trend in the real log corroborates the size of the effect.
- The "attribution bypassed" figures use a scratchpad-only patch to find the floor; the "cache the authority" variant I tried produced its own errors and is not reported.
- Battery samples are small for candidates (n=30). Differences of 1–2 calls are noise.
- Log-wide aggregates include stress-suite traffic.
- The audited working tree (`c60a42c`) was 97 commits behind `origin/main` (`73b0a83` on 2026-10-02). Upstream already fixes the refusals (`7aeb196`) but still builds a new `DesktopOmlx` per connection and has keep-alive off, so the per-call tax stands. The fix plan (P0–P3) is the second tab of the Docs version of this report.
- The cold-after-idle cause is inferred from timing and memory state, not proven.
- The attribution layer is a security control. Any change needs the review the repo requires for security-boundary changes, and the replacement must keep the same guarantee that the connected peer is the qualified oMLX process.
