# Wisp — Local Siri-style Assistant for macOS

A privacy-first, **strictly-local** AI assistant that lives in the menu bar, is summoned
by hotkey, has (gated) full machine access, and **routes each request to the
best local model** — a Mixture-of-Experts dispatcher. Text in, text out (no voice).

**Target hardware:** M5 Pro · 24 GB unified memory · 18 CPU / 20 GPU cores
**GPU wired cap:** manually set to 18 GB (`iogpu.wired_limit_mb`) for larger models.
**Inference layer:** [oMLX](https://omlx.ai/) — MLX inference server with paged SSD KV cache.
**Constraint:** No data leaves the device. All inference runs locally.
**Priority:** quality first (especially for coding); speed matters but never at quality's expense.

---

## 1. Guiding principles

1. **Local-only.** Every model (LLM, vision) runs on-device via **oMLX** (MLX-based). No network calls for inference, ever. SSD paging is on-device and stays consistent with this. Optional: kill switch that blocks the app's outbound network at the firewall to *prove* it.
2. **Safety ≠ locality.** "Local" protects your *data*. It does **not** make a shell-executing agent safe. The hard guardrail is the **policy layer** that gates every state-changing action.
3. **Routing is the product.** The router (the "MoE" part) is what makes this feel smart: trivial things answer instantly on a tiny model; coding and hard reasoning go to a large, high-quality model; screenshots go to a vision model.
4. **Quality first.** Speed matters, but for coding and reasoning, output quality wins. The router is biased to **err toward the larger/better model** when uncertain, rather than optimizing for latency. Cheap models are only for genuinely trivial requests.
5. **Lazy memory.** 24 GB / 18 GB cap is generous but not infinite. Keep the router resident; load/unload experts on demand; keep one mid-tier model warm.

---

## 2. The model lineup (the "experts")

All served by **oMLX** (MLX-based; Apple Silicon optimized). Ollama only as a last-resort
fallback for a model that ships GGUF-only.
Sizes assume 4-bit quantization. Rough resident memory in parentheses.

| Role | Model (example) | ~Mem | When the router picks it |
|------|-----------------|------|--------------------------|
| **Router/classifier** | Qwen3-1.7B or embeddings + logistic head | ~1.2 GB | Always resident. Classifies every request. |
| **Fast chat / commands** | Qwen3-4B (4-bit) | ~2.5 GB | Only genuinely trivial: small talk, simple lookups, tool dispatch. |
| **General reasoning** | Qwen3-8B (4-bit) | ~5 GB | Non-coding multi-step logic, summaries, planning. |
| **Coding (primary)** | **Qwen2.5-Coder-14B** (4-bit) | ~9 GB | **Default for all coding.** High quality, coexists with router+small. |
| **Coding (max, opt-in)** | **Qwen2.5-Coder-32B** (4-bit, or 3-bit/DWQ) | ~16–18 GB | Top-tier open coding model. Load **exclusively**; at 4-bit it nearly fills the 18 GB cap (lean on oMLX SSD paging) — a 3-bit/DWQ build (~14 GB) is the safer fit. |
| **Max reasoning (opt-in)** | Qwen3-30B-A3B (4-bit) | ~16 GB | MoE model, 30B total / ~3B active → 30B-class quality at ~3B speed. For hard *non-coding* reasoning. Load exclusively. |
| **Vision** | Qwen2.5-VL-7B / Moondream2 | ~5 GB | Screenshots, "what's on my screen", image files. |

**Memory budget reality (18 GB wired cap):** the cap is GPU-wired out of 24 GB total,
leaving ~6 GB for the OS + Python/Swift processes. Budget models to live under ~16 GB
**including KV cache** (which grows with context). **oMLX handles this for you** — its
paged SSD KV cache spills cold context to disk instead of OOM-ing, and its multi-model
serving does the load/unload/quick-switch. So you don't hand-write a pool manager.
- Baseline warm: router (1.2) + fast 4B (2.5) ≈ 3.7 GB.
- + Coder-14B (primary coding) → ~13 GB ✅ · + vision → ~18 GB ⚠️ (swap fast/8B out first).
- + 8B general reasoning → ~9 GB ✅.
- Coder-32B or 30B-A3B (~16 GB) → load **exclusively**, everything else swapped out.

> Start with **three** experts (fast 4B + Coder-14B + router). Add general-8B, the opt-in 32B/30B heavies, and vision in later phases. Don't build the whole zoo on day one.

---

## 3. Architecture

```
┌─ FRONTEND (Swift / SwiftUI) ─────────────────────────────┐
│  • Menu-bar app (LSUIElement, no Dock icon)              │
│  • Global hotkey → Spotlight-style overlay (NSPanel)     │
│  • Text input + streaming text output                    │
└───────────────────────────┬──────────────────────────────┘
                            │ localhost (Unix socket / 127.0.0.1)
┌─ AGENT SERVICE (Python, FastAPI) ────────────────────────┐
│  Orchestrator: intent → plan → tool calls → respond      │
│  (ReAct-style loop, streaming)                           │
│                                                          │
│  ┌─ MODEL ROUTER ───────────────────────────────────┐   │
│  │ classify(request) → {model, params}              │   │
│  │ decides WHICH model; oMLX does the switching     │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ TOOLS ──────────────────────────────────────────┐   │
│  │ shell · fs · AppleScript · Accessibility ·       │   │
│  │ Shortcuts · screenshot/vision · app control      │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ SAFETY LAYER (wraps every tool) ────────────────┐   │
│  │ policy engine · confirm prompts · dry-run/diff · │   │
│  │ audit log · path/command allowlists              │   │
│  └──────────────────────────────────────────────────┘   │
└───────────────────────────┬──────────────────────────────┘
                            │ localhost HTTP (Anthropic-compatible API)
┌─ oMLX SERVER (separate local process) ───────────────────┐
│  multi-model serving · continuous batching ·            │
│  paged SSD KV cache (hot→RAM, cold→SSD) · its own        │
│  menu-bar manager · serves MLX 4-bit experts             │
└──────────────────────────────────────────────────────────┘
```

**Why split Swift + Python + oMLX?** Swift gives you the native menu-bar, global hotkey,
NSPanel overlay, Accessibility, and Shortcuts integration. Python runs the agent loop,
router decision logic, tools, and safety. **oMLX** owns inference — model serving,
switching, batching, and the SSD KV cache — exposed as a local Anthropic-compatible
HTTP endpoint the Python service calls. All three stay on-device; nothing hits the
network. This deletes the hand-built model-pool-manager from earlier drafts.

*(Alternative: all-Swift with MLX Swift bindings — fewer moving parts, but you give up
Python's agent tooling. Recommended only if you strongly prefer one language.)*

---

## 4. The router design (MoE dispatcher)

Three escalating strategies — implement in this order:

1. **Rules + keywords** (v0): regex/intent table for the obvious stuff ("set timer",
   "what time", "open X"). Zero latency, no model load.
2. **Embedding classifier** (v1): embed the request, nearest-centroid or a small
   logistic-regression head over labeled categories → route. Fast, cheap, tunable.
3. **LLM router** (v2): the tiny 1.7B model returns a JSON `{category, complexity,
   needs_vision, needs_tools}`. Most flexible; use it as the fallback when 1 & 2
   are low-confidence.

Router output → policy (**quality-biased** — when in doubt, go bigger):
- `is_code: true` → **Coder-14B by default** (never route code to the 4B). Escalate to
  Coder-32B for large/complex tasks or on the user's "think harder".
- `complexity: low` AND not code → fast 4B
- `complexity: high` AND not code → general-8B, escalate to 30B-A3B if confidence low
- `needs_vision` → vision model
- `needs_tools` → enable tool calling on the chosen model

**Escalation defaults toward quality**, not speed: if a model's answer fails a cheap
self-check (or the user says "that's wrong / think harder"), re-run on the next size up.
For coding specifically, prefer correctness over latency — it's fine to start on the
larger coder. Log every routing decision so you can tune it later.

---

## 5. Tool layer (the "full access")

| Tool | Mechanism | Default policy |
|------|-----------|----------------|
| Shell exec | subprocess | **Confirm** for mutating; auto for read-only allowlist (`ls`, `cat`, `git status`…) |
| Filesystem read | direct | Auto (outside sensitive dirs) |
| Filesystem write/delete | direct | **Confirm + show diff/preview** |
| App control | AppleScript / `osascript` | Auto for safe verbs; confirm for send/post/delete |
| UI automation | Accessibility API | Confirm |
| Screenshot / screen read | ScreenCaptureKit | Auto |
| Shortcuts | `shortcuts run` | Per-shortcut policy |
| Web (read only) | local fetch | Optional; off by default given strict-local stance (note: fetching a page *does* hit the network even if the model doesn't) |

macOS permissions you'll grant once in **System Settings → Privacy & Security**:
Accessibility, Full Disk Access, Screen Recording, Automation, Microphone.

---

## 6. Safety layer (non-negotiable)

- **Policy engine**: `allow` / `confirm` / `deny` per tool+argument-pattern. Config file.
- **Three tiers**: read-only (auto) · reversible mutation (confirm) · destructive/irreversible/outbound (confirm + explicit typed acknowledgment).
- **Dry-run + diff**: file edits and shell commands preview their effect before running.
- **Audit log**: append-only log of every tool call, args, decision, result.
- **Allowlists/denylists**: never auto-run `rm -rf`, `sudo`, disk utils, `curl | sh`, etc.
- **Sandboxing**: run shell tools with reduced privileges where feasible; constrain writable paths.
- **Panic key**: global hotkey to kill the agent + any spawned process.

---

## 7. Repository layout

```
wisp/
├── BUILD_PLAN.md            ← this file
├── app/                     # Swift menu-bar frontend
│   ├── MOEApp.swift         #   LSUIElement, status item
│   ├── HotKey.swift         #   global shortcut
│   └── OverlayPanel.swift   #   Spotlight-style input + streaming output
├── service/                 # Python agent service
│   ├── main.py              #   FastAPI + socket
│   ├── router/              #   MoE dispatcher
│   ├── inference/           #   oMLX HTTP client (Anthropic-compatible)
│   ├── agent/               #   orchestrator loop
│   ├── tools/               #   shell, fs, applescript, vision...
│   ├── safety/              #   policy engine, audit, confirm
│   └── config/              #   models.yaml, policy.yaml
├── scripts/                 # model download/quantize, setup
└── tests/
```

---

## 8. Phased milestones

**Phase 0 — Foundations (½–1 day)**
- Install **oMLX**, pull 2 MLX 4-bit models (Qwen3-4B + **Qwen2.5-Coder-14B**), confirm they serve on the GPU.
- **Smoke-test tool calling** through oMLX's Anthropic-compatible endpoint for both models
  (Qwen vs Llama tool-call templates differ — verify before relying on it).
- Bare FastAPI service: `POST /chat` → calls oMLX → streamed reply. No router yet.

**Phase 1 — Agent core + safety (2–3 days)**
- ReAct tool loop with **one** tool: shell exec.
- Safety layer: policy engine + confirm prompt + audit log + read-only allowlist.
- CLI client to test end-to-end before building UI.

**Phase 2 — The router (2–3 days)**
- v0 rules → v1 embedding classifier → v2 LLM router fallback.
- Router emits the target model name per request; oMLX handles load/switch/memory.
- Escalation rule (small → large on low confidence).

**Phase 3 — Native frontend (3–4 days)**
- Swift menu-bar app, global hotkey, NSPanel overlay, streaming display.
- Wire overlay → localhost service.
- Grant Accessibility / Full Disk / Screen Recording.

**Phase 4 — More experts & senses (ongoing)**
- Add general-8B, opt-in Coder-32B + 30B-A3B heavies, vision model + screenshot tool,
  AppleScript/Shortcuts tools.

**Phase 5 — Polish**
- Conversation memory/history, per-app skills, network kill switch to prove locality,
  preferences UI, launch-at-login.

---

## 9. Key risks / decisions

- **Quality ceiling.** Strict-local caps you below cloud frontier models. The router
  mitigates this by sending coding to Coder-14B (→ Coder-32B) and hard reasoning to
  30B-A3B — strong local models, but still ≠ Claude Opus on the hardest coding. Accept
  this, or revisit local-only later. Given quality is the top priority, the Coder-32B
  fit is worth pushing on: confirm a 3-bit/DWQ build holds quality before relying on it.
- **Memory pressure.** 18 GB cap means experts swap in/out. **oMLX owns this** via
  multi-model serving + paged SSD KV cache, so it's largely de-risked — but still don't
  request 30B-A3B alongside other models; it must run exclusively.
- **First-token latency.** oMLX's SSD prefix restore cuts long-context TTFT from tens of
  seconds to ~1–3s. Cold *model* loads still cost seconds — keep the fast model warm.
- **Dependency risk.** oMLX is third-party and young; pin a version. Verify its
  Anthropic-compatible endpoint passes tool calls cleanly for your models (Phase 0 smoke test).
- **Security blast radius.** Full machine access + an LLM that can be prompt-injected
  (e.g. by content it reads on screen/in files) is the real danger. The safety layer is
  what stands between "assistant" and "self-inflicted disaster." Treat content the model
  reads as untrusted.

---

## 10. Immediate next step

Phase 0: install oMLX, pull Qwen3-4B + Qwen3-8B (MLX 4-bit), smoke-test tool calling
through its Anthropic-compatible endpoint, and stand up a ~30-line FastAPI `/chat` that
streams from oMLX. Once that works on your GPU, we build the router, agent loop, and
safety layer on top.
