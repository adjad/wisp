# Wisp Air — MacBook Air Backend Handoff

**Audience: Claude running on the MacBook Air.** This document is self-contained — the
MOE_Project repo is NOT on this machine and you do not need it. Everything required to
build, run, and verify the Air backend is in this file, including the complete source code.
Written 2026-07-11 from the MacBook Pro, where the client side is already implemented and
tested.

---

## 1. Context — what you are building and why

**Wisp** is a local, privacy-first, Siri-style assistant that runs on the user's MacBook Pro
(M5 Pro, 24 GB). It routes each prompt to the best local model via a Mixture-of-Experts
router. The Pro's memory is tight (~18 GB GPU wired cap), so keeping the small router model
warm there competes with the specialist models and forces ~2.6 s hot-swaps.

**This MacBook Air (M4, 16 GB) becomes a headless router server.** The Pro sends ambiguous
prompts here for classification; the Air runs a small always-warm LLM (gemma-4-e4b, ~5.5 GB)
and replies with a role + flags in well under a second. The Pro maps the role to its own
models and does all real work itself.

**Division of labor (non-negotiable in V1):**

| MacBook Pro | MacBook Air |
|---|---|
| Wisp UI, final answers, tools, safety, sessions | Classification only |
| Owns role→model mapping | Never picks the Pro's concrete model |
| Falls back to local routing if Air is slow/offline | No tools, no file writes for the user, no memory, no agent loop |

Do NOT build tools, an agent loop, a safety layer, session memory, or a UI on the Air.
V1 is exactly three endpoints on one small FastAPI service, plus oMLX for inference.

**Graceful degradation is built into the Pro:** if the Air is offline, slow (>1200 ms), or
returns an invalid payload, the Pro silently falls back to its local router. Nothing you
build here can break the Pro — but respect the latency budget.

---

## 2. The wire contract (the Pro is already built against this — match it exactly)

### `POST /router/classify` (required)

Request from the Pro (total budget **1200 ms** including network):

```json
{ "prompt": "the user's text", "has_image": false }
```

Response the Pro expects:

```json
{
  "role": "general",
  "needs_tools": false,
  "needs_vision": false,
  "source": "llm",
  "reason": "llm classification"
}
```

Validation rules on the Pro side (from its `_from_remote()`):

- `role` MUST be one of: `coding`, `reasoning`, `agent`, `vision`, `fast`, `general`.
  Anything else → the Pro treats the whole response as invalid and marks the Air offline.
- `needs_tools` is OR'd with `role == "agent"`; `needs_vision` is OR'd with `role == "vision"`.
- `reason` is optional (shown in the Pro's UI/debug); extra fields are ignored.
- The Pro re-applies its own invariants (`_finalize`) after receiving this — you do not
  need tool-capability or code-delegation logic on the Air.

### The Pro's "Check" button

Settings → MacBook Air Compute → **Check** does NOT call `/health`. It POSTs
`/router/classify` with `{"prompt": "hello", "has_image": false}` and expects a valid role
within the timeout. `"hello"` is ≤6 words with no `?`, so the rules layer answers it
instantly as `fast` — no LLM call. This must keep working.

### `GET /health` (required, for humans/scripts)

```json
{ "ok": true, "router_model": "gemma-4-e4b-it-4bit", "model_loaded": true }
```

### `GET /air/status` (optional, nice to have)

Uptime + request counters + last classify latency. Shape is yours.

---

## 3. The routing logic (ported verbatim from the Pro — keep behavior identical)

Two layers, same as the Pro:

1. **Regex rules** — instant, catch obvious cases. Note: the Pro only calls the Air for
   prompts *its own identical rules* found ambiguous, so in practice most real traffic
   falls through to layer 2. The rules still matter for the "hello" check probe and for
   direct testing.
2. **LLM classifier** — gemma-4-e4b at temperature 0, max_tokens 40, JSON-only prompt.
   One quirk ported from the Pro: if the LLM says `reasoning` but the prompt doesn't match
   `HARD_REASON_RE`, downgrade to `general` (reasoning-effort is expensive on the Pro and
   reserved for genuinely hard prompts).
3. **Never crash a request**: any classifier exception → return
   `{"role": "general", "source": "llm", "reason": "fallback after classifier error"}`.

---

## 4. Complete source — `~/WispAir/air_service.py`

Create the project at `~/WispAir/` with this single file. This is complete, working code
(the regexes are copied character-for-character from the Pro's `service/router/router.py`
— do not "improve" them, they must stay in sync with the Pro's rules layer):

```python
"""Wisp Air — headless router backend for the MacBook Air (V1).

One job: classify prompts for the MacBook Pro's Wisp backend via
POST /router/classify. Cheap regex rules first; a small local LLM
(gemma-4-e4b via oMLX) resolves ambiguous prompts. No tools, no file
access, no memory, no final answers.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

ROUTER_MODEL = "gemma-4-e4b-it-4bit"  # must match an installed oMLX model id exactly
KEEP_WARM_SECONDS = 60                # how often to verify the model is still resident

VALID_ROLES = {"coding", "reasoning", "agent", "vision", "fast", "general"}

# ---------------------------------------------------------------------------
# Routing rules — copied verbatim from the Pro's service/router/router.py.
# If these ever change on the Pro, change them here too.
# ---------------------------------------------------------------------------

TOOL_RE = re.compile(
    r"\b(run(?:ning|s)?|execute|open|launch|install|uninstall|delete|remove|move|rename|copy|"
    r"organi[sz]e|create (?:a )?(?:file|folder|dir)|make (?:a )?(?:file|folder)|"
    r"screenshot|list (?:the )?files|find (?:my|the|all)|show me my|what'?s on my screen|"
    r"set up|configure|kill|restart|clean up|download|clone|commit|push|build|compile and run|"
    r"start|stop|enable|disable|empty the trash|search my|save)\b", re.I)

CODE_RE = re.compile(
    r"\b(bug|refactor|function|traceback|exception|stack ?trace|regex|async|"
    r"compile|syntax|debug|unit ?test|npm|pip|webpack|docker|api endpoint|"
    r"def |class |import |println|console\.log|nullpointer|segfault|"
    r"html|css|website|webpage|web ?site|landing page|script|program|"
    r"python|rust|javascript|typescript|kotlin)\b|"
    r"\.(py|js|ts|tsx|jsx|swift|rs|go|java|cpp|c|rb|php|sh|html|css)\b|"
    r"\bcode\b|\bwrite a (?:script|program|function)", re.I)

LOCATION_RE = re.compile(
    r"\b(downloads?|desktop|documents?|folder|directory|home directory|"
    r"my (?:mac|computer|files?))\b|"
    r"(?:^|\s)~[/\\]|(?:^|\s)/(?:[\w.-]+/)*[\w.-]+", re.I)

REASON_RE = re.compile(
    r"\b(why|prove|calculate|solve|derive|plan|strateg|compare|analy[sz]e|"
    r"reason|step by step|figure out|trade-?offs?|pros and cons|explain how|"
    r"what would happen if|equation|theorem|probability)\b", re.I)

HARD_REASON_RE = re.compile(
    r"\b(prove|proof|derive|derivation|theorem|lemma|rigorous|formal(?:ly)?|"
    r"optimi[sz]e|time complexity|space complexity|big-?o|asymptotic|"
    r"integral|derivative|differential|matrix|eigen|calculus|"
    r"step by step|show that|think (?:hard|carefully|deeply|through)|"
    r"olympiad|aime|imo|combinatoric|number theory|multi-?step|"
    r"prove that|optimal strategy|edge cases)\b", re.I)

_CLASSIFY_SYS = (
    "Classify the user's request into exactly one role. Reply with ONLY a JSON "
    'object: {"role": "<role>", "needs_tools": <bool>}. Roles: '
    '"coding" (write/fix code, no machine actions), '
    '"reasoning" (analysis, planning, math, explanation), '
    '"agent" (do something on the computer: run/open/create/delete/organize files or apps), '
    '"fast" (trivial chit-chat or a quick fact), '
    '"general" (anything else). needs_tools is true only for the agent role.'
)


def _decision(role: str, *, tools: bool = False, vision: bool = False,
              source: str = "rules", reason: str = "") -> dict[str, Any]:
    return {"role": role, "needs_tools": tools, "needs_vision": vision,
            "source": source, "reason": reason}


def rule_route(text: str, has_image: bool) -> dict[str, Any] | None:
    t = text.strip()
    if has_image:
        return _decision("vision", vision=True, reason="image attached")
    if TOOL_RE.search(t):
        return _decision("agent", tools=True,
                         reason="action verb implies operating the machine")
    if CODE_RE.search(t):
        if LOCATION_RE.search(t):
            return _decision("agent", tools=True,
                             reason="code request also names a save location -> needs file access")
        return _decision("coding", reason="code-related request")
    if HARD_REASON_RE.search(t):
        return _decision("reasoning", reason="hard reasoning -> deliberate model")
    if REASON_RE.search(t):
        return _decision("general", reason="light reasoning -> fast generalist")
    if len(t.split()) <= 6 and "?" not in t:
        return _decision("fast", reason="short/trivial request")
    return None  # ambiguous -> LLM


# ---------------------------------------------------------------------------
# oMLX client (local OpenAI-compatible server on this Air, default :8000)
# ---------------------------------------------------------------------------

class OMLX:
    def __init__(self) -> None:
        s = json.loads((Path.home() / ".omlx" / "settings.json").read_text())
        base = f"http://{s['server']['host']}:{s['server']['port']}"
        self.client = httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {s['auth']['api_key']}"},
            timeout=httpx.Timeout(120.0, connect=5.0),
        )

    async def chat(self, model: str, messages: list[dict], **kw: Any) -> dict:
        r = await self.client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": messages, "stream": False, **kw})
        r.raise_for_status()
        return r.json()

    async def loaded_models(self) -> list[str]:
        r = await self.client.get("/v1/models/status")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("models", []) if m.get("loaded")]

    async def load(self, model: str) -> None:
        await self.client.post(f"/v1/models/{model}/load")


omlx: OMLX | None = None


async def llm_route(text: str) -> dict[str, Any]:
    try:
        resp = await omlx.chat(
            ROUTER_MODEL,
            [{"role": "system", "content": _CLASSIFY_SYS},
             {"role": "user", "content": text}],
            temperature=0.0, max_tokens=40,
        )
        content = resp["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", content, re.S)
        data = json.loads(m.group(0)) if m else {}
        role = data.get("role", "general")
        if role not in {"coding", "reasoning", "agent", "fast", "general"}:
            role = "general"
        # reserve the Pro's expensive reasoning mode for genuinely hard prompts
        if role == "reasoning" and not HARD_REASON_RE.search(text):
            role = "general"
        tools = bool(data.get("needs_tools")) or role == "agent"
        return _decision(role, tools=tools, source="llm", reason="llm classification")
    except Exception:  # noqa: BLE001 — never let routing crash a request
        return _decision("general", source="llm",
                         reason="fallback after classifier error")


# ---------------------------------------------------------------------------
# Keep-warm: the 1200 ms budget only works if gemma is already resident.
# Re-check every minute; also recovers automatically if oMLX restarts.
# ---------------------------------------------------------------------------

async def _keep_warm() -> None:
    while True:
        try:
            if ROUTER_MODEL not in await omlx.loaded_models():
                await omlx.load(ROUTER_MODEL)
        except Exception:  # noqa: BLE001 — oMLX down/restarting; retry next tick
            pass
        await asyncio.sleep(KEEP_WARM_SECONDS)


STATS = {"started": time.time(), "requests": 0, "rules": 0, "llm": 0,
         "last_latency_ms": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global omlx
    omlx = OMLX()
    warm = asyncio.create_task(_keep_warm())
    yield
    warm.cancel()
    await omlx.client.aclose()


app = FastAPI(title="Wisp Air", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    loaded = False
    try:
        loaded = ROUTER_MODEL in await omlx.loaded_models()
    except Exception:  # noqa: BLE001 — health must answer even if oMLX is down
        pass
    return {"ok": True, "router_model": ROUTER_MODEL, "model_loaded": loaded}


@app.get("/air/status")
async def air_status() -> dict[str, Any]:
    return {**STATS, "uptime_s": int(time.time() - STATS["started"])}


@app.post("/router/classify")
async def classify(body: dict) -> dict[str, Any]:
    prompt = str(body.get("prompt", ""))
    has_image = bool(body.get("has_image"))
    started = time.perf_counter()
    decision = rule_route(prompt, has_image)
    if decision is not None:
        STATS["rules"] += 1
    else:
        decision = await llm_route(prompt)
        STATS["llm"] += 1
    STATS["requests"] += 1
    STATS["last_latency_ms"] = int((time.perf_counter() - started) * 1000)
    return decision
```

Dependencies: `fastapi`, `uvicorn`, `httpx` only.

---

## 5. Machine setup (do these in order)

### 5.1 oMLX

1. Install oMLX from **omlx.ai** (it's a menu-bar app; its server listens on
   `127.0.0.1:8000` and writes `~/.omlx/settings.json` with the API key the code above
   reads). Enable its launch-at-login option so it survives reboots.
2. Create a model directory (e.g. `~/OMLX_Model_Files`) and point oMLX at it in its
   settings.

### 5.2 The router model — `gemma-4-e4b-it-4bit` (~5.5 GB)

Easiest and fastest: **copy the exact model folder from the Pro** so the model id matches
byte-for-byte. On the Air (with both Macs on the same Wi-Fi and Remote Login enabled on
the Pro, or just use AirDrop / a USB drive for the folder):

```bash
rsync -avP "adijain@<pro-ip-or-hostname>:~/Desktop/OMLX_Model_Files/gemma-4-e4b-it-4bit" ~/OMLX_Model_Files/
```

Then restart the oMLX server so it rescans the model dir:

```bash
/Applications/oMLX.app/Contents/MacOS/omlx-cli restart
```

Verify the model is visible (API key is in `~/.omlx/settings.json` under `auth.api_key`):

```bash
curl -s http://127.0.0.1:8000/v1/models -H "Authorization: Bearer <api-key>" | python3 -m json.tool
```

The id must be exactly `gemma-4-e4b-it-4bit`; if it differs, update `ROUTER_MODEL` in
`air_service.py`.

### 5.3 Python service

```bash
mkdir -p ~/WispAir && cd ~/WispAir
# create air_service.py with the code from §4
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn httpx
```

### 5.4 Run it — MUST bind 0.0.0.0, not localhost

```bash
cd ~/WispAir
.venv/bin/uvicorn air_service:app --host 0.0.0.0 --port 8766
```

`--host 0.0.0.0` is the single most common failure: without it the service binds localhost
only and the Pro can never reach it. If the macOS firewall prompts to allow incoming
connections for Python, the user should allow it.

### 5.5 Hostname

The Pro's default Air URL is `http://m4air.local:8766`. Either set this Air's local
hostname to `m4air` (System Settings → General → Sharing → Local hostname), or note the
Air's IP/hostname and update the URL on the Pro instead (Settings → MacBook Air Compute).

### 5.6 Always-on (launchd)

Create `~/Library/LaunchAgents/com.wisp.air.plist` (replace `USERNAME` with the real
account name — check with `whoami`). The `caffeinate -s` wrapper keeps the machine awake
while on AC power, so no system power settings need changing:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wisp.air</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
    <string>/Users/USERNAME/WispAir/.venv/bin/uvicorn</string>
    <string>air_service:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8766</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/WispAir</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/wisp-air.log</string>
  <key>StandardErrorPath</key><string>/tmp/wisp-air.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wisp.air.plist
# later, to restart after code changes:
launchctl kickstart -k gui/$(id -u)/com.wisp.air
```

Physical setup advice for the user: keep the Air plugged in, lid open or on a stand with
decent airflow, display allowed to sleep (only the system stays awake), and optimized
battery charging on (a 70–80 % charge cap is kind to the battery for an always-on machine).

---

## 6. Verification

### On the Air (all three should pass before involving the Pro)

```bash
# 1. Health — model_loaded should be true within ~1 min of startup (keep-warm loop)
curl -s http://127.0.0.1:8766/health | python3 -m json.tool

# 2. Rules path — instant, no LLM: expect role "fast", source "rules"
curl -s -X POST http://127.0.0.1:8766/router/classify \
  -H "Content-Type: application/json" \
  -d '{"prompt":"hello","has_image":false}' | python3 -m json.tool

# 3. LLM path — a prompt that misses every regex: expect source "llm", warm latency <1200 ms
curl -s -X POST http://127.0.0.1:8766/router/classify \
  -H "Content-Type: application/json" \
  -d '{"prompt":"What should I have for dinner tonight?","has_image":false}' | python3 -m json.tool
```

**Note on test prompts:** an end-to-end test prompt must miss every regex in §4, or the
Pro's local rules will answer it and never call the Air. Both the dinner prompt above and
`"I need help figuring out the best way to approach this"` are verified ambiguous on the
Pro (2026-07-11) and will reach the Air. Beware near-misses: e.g. a prompt containing the
literal "figure out" WOULD be caught locally by `REASON_RE`.

### From the Pro (the user runs these; include them in your final report)

```bash
curl -s http://m4air.local:8766/health
curl -s -X POST http://m4air.local:8766/router/classify \
  -H "Content-Type: application/json" \
  -d '{"prompt":"What should I have for dinner tonight?","has_image":false}'
```

Then on the Pro: **Wisp Settings → MacBook Air Compute → enable "Use MacBook Air compute"
+ "Routing", set the URL, press Check** → status should read `Available` with a latency.
Streamed answers will then show `route_source: "air_router"` for prompts that were
ambiguous to the Pro's local rules.

---

## 7. Constraints, gotchas, and what NOT to do

- **1200 ms total budget** (Pro-side `timeout_ms`, includes network). Warm gemma classifies
  in ~0.2–0.7 s, so it fits — but only if the model is already resident; that's what the
  keep-warm loop is for. If the Pro logs occasional `fallback` route_sources, that's the
  design working, not a bug. The user can raise the timeout on the Pro
  (`POST /air_compute` with `{"timeout_ms": 1200}` or via Settings) if the Air is reached
  over Tailscale.
- **An invalid `role` string marks the Air offline on the Pro.** Never return anything
  outside the six valid roles.
- **Security:** LAN or Tailscale only. Never port-forward 8766 to the internet. The
  service intentionally has no auth in V1 because it only classifies text on a private
  network — do not add user-file access or shell tools to it, that would change the
  security calculus entirely.
- **Don't run the Swift app, agent loop, or tools on the Air.** V1 is classification only.
- **oMLX quirks known from the Pro:** `/unload` returns before memory is actually freed;
  507 means memory pressure. Neither should matter on the Air (one 5.5 GB model in 16 GB),
  but if you see 507s, check nothing else heavy is loaded (`/v1/models/status`).
- The Air's 16 GB gives a practical ceiling of roughly 10–11 GB for model memory — plenty
  for gemma-e4b, but don't roster big models here in V1.

## 8. Future phases (context only — do NOT build these now)

- **Phase 2 — memory store:** SQLite on the Air SSD; `POST /memory/ingest`,
  `POST /memory/search`, `GET /memory/status`; the Pro sends compact per-turn digests, the
  Air extracts preferences/decisions/summaries.
- **Phase 3 — retrieval before answering:** Pro asks the Air for relevant snippets with a
  strict 150–250 ms timeout, continues without memory if slow.
- **Phase 4 — offload jobs:** thread titles, old-chat compression, prompt cleanup,
  codebase summaries. (Hosting a full specialist model on the Air was assessed and
  deferred — streaming full generations over the network is a much bigger lift.)

## 9. Definition of done for this session (Claude on the Air)

1. `~/WispAir/air_service.py` created, venv set up, service running via launchd on
   `0.0.0.0:8766`.
2. oMLX running with `gemma-4-e4b-it-4bit` installed and kept warm.
3. All three §6 Air-side curl checks pass (show the outputs).
4. Tell the user: (a) the Air's hostname/IP to enter on the Pro, (b) to flip on Settings →
   MacBook Air Compute → Use MacBook Air compute + Routing, and press Check.
