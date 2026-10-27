# Private Super Model on the ASUS GX10 — implementation brief

**Status:** proposed, not started. **For:** Codex (or any builder). **Written:** 2026-10-10.
**Base:** current `origin/main`. Start from a clean Worktree; do not build on the dirty Local checkout.

> Read `AGENTS.md` first. It governs ownership, validation, review triggers and the handoff you must
> finish with. This brief is the *what and why*; it does not waive any gate there.

## 1. The idea in one paragraph

Wisp keeps quick, private work on the Mac and sends work that needs top quality to **the user's own
GX10 over Tailscale**. The GX10 is a *private cloud the user owns*: a large model on hardware nobody else
holds, reached through the user's private network. It replaces two earlier answers to "what do we do
when the on-device model isn't good enough":

| Generation | What it was | Why it fell short |
| --- | --- | --- |
| Old Super Model | A 27B model forced onto the 24 GB Mac: quit apps, pause syncs, raise `iogpu.wired_limit_mb` through an admin dialog (`VRAMLimit.swift`) | Disruptive; one button, one machine |
| Cloud Super Model (exists today) | Privacy-gated escalation to a public HTTPS provider; **fail-local** | Quality, but data leaves; so a classifier keeps nearly everything personal on the Mac |
| **Private Super Model (this brief)** | The same escalation, aimed at a **tailnet node the user owns** | Quality *and* the data stays inside infrastructure the user controls |

Positioning for the hackathon: Meta Muse and OpenAI Dots run on a cloud computer in someone else's data
center. Wisp has one in the user's own house.

## 2. What already exists (reuse it; do not rebuild)

- **Per-request escalation seam.** `service/main.py` (~lines 1589–1625) already picks `local_role_target` vs
  `cloud_super_model_target` per turn and records `decision.route_source` as
  `super_model_cloud` / `super_model_local`, with a human-readable `decision.reason`.
- **Eligibility policy.** `service/inference/super_model.py::cloud_super_model_eligible` is a deliberate
  fail-local gate: hard-local rules (explicit "keep this local", secrets and paths, personal-scope
  phrases, page references) plus a local Laya classifier (`private` / `computer` / `context` risk ceiling
  `0.05`). Anything uncertain stays local.
- **Targets and endpoints.** `service/config/endpoints.py`: `Endpoint`, `Target`, `endpoint_from_config`,
  `role_target`, `local_role_target`, `cloud_super_model_target`. Config lives under `inference:` in
  `~/.moe/config.yaml` (`endpoints`, `bindings`, `super_model`); see `docs/inference-providers.md`.
- **Setting up the endpoint.** `service/main.py` (~lines 478–530) validates a provider, probes `/models`
  for the exact model id, then calls `set_cloud_provider(...)`. `service/config/__init__.py` has
  `cloud_super_model_enabled()` and `CLOUD_ASSIGNABLE_ROLES = ("reasoning", "coding", "research")`.
- **Fallback.** `TurnInferenceClient` (`service/inference/readiness.py`) and the binding's `fallback_role`.
- **Trusted-node precedent.** `service/nodes.py` and `mini/` already encode: remote data never executes
  tools, and effects from a remote node are *proposals* only. Memory/decision 2026-09-12: sensitive data
  **may** cross the tailnet when transport is authenticated and private.
- **Tests to extend.** `tests/test_super_model_routing.py`, `tests/test_inference_endpoints.py`,
  `tests/test_cloud_provider_settings.py`, `tests/test_inference_providers.py`.

## 3. Outcome to deliver

A user with a GX10 on their tailnet can enable **Private Super Model**. Then, for eligible turns, Wisp
generates on the GX10 and shows that it did so. If the GX10 is unreachable, Wisp answers on the Mac and
says so. The GX10 never executes a tool and never sends anything.

### 3.1 Tier policy

Two tiers, chosen per turn *after* the existing router has produced its `decision`:

| Tier | Runs where | Gets |
| --- | --- | --- |
| `device` | Managed oMLX on the Mac | routing, mail/message summaries, calendar, Smart Search, Catch, every approval, every effectful tool, skills |
| `node` | The GX10 (generation only) | research synthesis, coding, long-document work, careful drafting, and any turn the user explicitly escalates |

Rules, in order (first match wins):

1. **Hard local, always.** Explicit "keep it local / on this Mac", an active skill, a skill turn, and
   `_OBVIOUS_SECRET_RE` matches stay on `device` even over the tailnet (defence in depth).
2. **Roles that must stay local stay local:** `fast`, `router`, `embedding`, `reranker` (already enforced in
   `role_target`; do not weaken it).
3. **Explicit escalation** ("think harder", a UI toggle) → `node` if healthy.
4. **Workload rule** → `node` for the roles in the table above, or when assembled context exceeds a
   configurable threshold (default proposal: 12,000 tokens, the point where the local window gets tight).
5. Otherwise `device`.

Unlike the public `cloud` endpoint, the **Laya private-content gate does not apply to the `node` tier**:
the tailnet is the privacy boundary the user already decided on. It must still apply to the public
`cloud` endpoint, unchanged. Keep both paths working and independently tested.

### 3.2 Two modes, ship the first

- **v1 — synthesis on the node.** The Mac runs the router and every tool locally; tool results and
  conversation context are sent to the GX10, which composes the answer. No tool schemas cross the wire
  (matching the current cloud pass: "a cloud synthesis pass has no remote tool schemas"). Effects stay on
  the Mac.
- **v2 (out of scope here) — node-driven tool loop.** The GX10 emits tool calls, the Mac executes them and
  returns results. Needs tool schemas to cross and a server-run qualification via
  `service/inference/qualify.py`. Do not start this without a separate approved brief.

**Out of scope:** computer use / reading the screen. Wisp has no vision path today, and the GX10 doing the
looking while the Mac does the clicking is a roadmap item to name on slides, not to build now.

## 4. Design

### 4.1 Endpoint: a named private node

- Add a named endpoint (propose `node`) alongside `cloud`. Provider `openai-compatible` (the GX10 serves an
  OpenAI-compatible API; the stack and model are an open question — §8).
- **Do not relax the HTTPS rule.** `endpoint_from_config` requires authenticated HTTPS for every
  non-loopback endpoint. Use Tailscale's HTTPS (`tailscale serve` with the node's MagicDNS name) rather than
  allowing plain HTTP to a `100.x` address. If that proves impractical, stop and ask; a new "tailnet HTTP"
  exception is a security-boundary change and triggers the Auditor.
- Credential via `keychain:<name>` as documented in `docs/inference-providers.md`. Never inline secrets.
- Validate that the host is a tailnet name or address when the endpoint is marked `node`; reject public
  hosts for this endpoint class so it cannot be mistaken for a cloud provider.
- Reuse the existing readiness probe and `/models` exact-id check from the setup flow.

### 4.2 Routing

- Factor the current Super Model branch in `service/main.py` into a small, testable function
  (`service/inference/tiering.py` is a suggested new module) that returns `(tier, reason)`; the call site
  keeps setting `decision.model`, `decision.route_source` (`node_super_model` / existing values) and
  `decision.reason`.
- A node turn must remove nothing the existing standalone/public-tool preparation relies on:
  reuse `prepare_cloud_standalone` semantics where tools are not needed; where tools are needed, run them
  locally first, then hand results to the node for synthesis (v1).
- Keep `route_source` values stable for anything that already consumes them; add, don't rename.

### 4.3 Fallback, never silent

- Health check before the turn (reuse the readiness path; bounded timeout, default from
  `readiness_timeout`).
- Unreachable, slow, or failing mid-turn → complete the turn on `device`. Emit a visible status line
  ("GX10 offline — answered on this Mac"). A turn must never fail because the node is down.
- After a mid-stream failure, do not splice partial node output into a local answer; restart the generation
  locally and tell the user.

### 4.4 Visibility (UI)

- Show which tier produced each answer (a small chip such as "GX10" / "This Mac") using the existing
  `routed` event payload (`await emit({"type": "routed", **decision.as_dict()})`). Swift side needs to read
  the new `route_source`; keep it additive.
- Settings: a Private Super Model section — connection test, on/off, health, threshold. The Swift UI work is
  a separate owned path; define the backend contract first and keep the UI task dependent on it.
- Status text during a node turn should read "Thinking on your GX10…", consistent with the live
  "Loading model… (Ns)" status.

### 4.5 Privacy and safety invariants (must hold; test each)

1. Effects (`send_email`, `send_message`, calendar writes, deletes, `run_shell`) execute only on the Mac and
   keep their existing confirm/deny policy (`service/config/policy.yaml`). `send_email`/`send_message` stay
   permanently non-grantable.
2. The node receives text only: no tool schemas, no credentials, no `~/.moe` paths beyond what the prompt
   already contains.
3. Skills and skill tools stay on the managed model (existing behaviour at the `skill_turn` branch). Don't
   regress it.
4. The `node` endpoint can never be used for `fast`, `router`, `embedding`, `reranker`.
5. Log/audit which tier handled each turn in `~/.moe/audit.jsonl` without logging prompt content.
6. If the user says "keep this local", it is honoured even when the node is healthy and the role would
   otherwise escalate.

## 5. Owned paths (proposal; confirm with the Orchestrator before editing)

- `service/inference/tiering.py` (new) and its tests.
- `service/config/endpoints.py`, `service/config/__init__.py` (node endpoint + config overlay).
- `service/main.py`: the Super Model block only (~1589–1625) and the provider-setup route.
- `docs/inference-providers.md`: add a "Private node" section.
- Swift UI: **separate task**, depends on the backend event contract.

Do not edit paths owned by another active task. Current Local checkout has unrelated uncommitted changes
(`service/assistant/*`, `service/attention/*`, `demo/`): they belong to the Catch/attention track. Leave
them alone.

## 6. Validation (record exact commands and results in the handoff)

Standing rules from project memory:

- After **any** `service/router/router.py` edit, run `test_router_scoping.py` + `test_router_no_vision.py`
  and add a new regression test. (You should not need to touch the router; if you do, this applies.)
- Bare `pytest tests/` silently runs zero tests here; use the project's explicit invocations and
  `scripts/test_replay_failure_fixes.py`.
- Never launch the built app or replay against the live backend on `:8765`. Use an isolated backend
  (`8775`) plus sandbox. The Mac is near its swap limit.
- Model-backed tests use `Ling-3.0-tiny-Wisp-V2-merged-oQ4e`.
- Tests must use a **fake node server** with synthetic data. No real GX10, no real keys.

New tests to write:

1. Tier selection table: each rule in §3.1, including precedence (hard-local beats explicit escalation).
2. Public `cloud` path unchanged (existing `test_super_model_routing.py` still passes untouched).
3. Node endpoint validation: rejects public hosts, plain HTTP, inline secrets, `fast/router/embedding/reranker`.
4. Fallback: node down at start, node dies mid-stream, node slow past timeout; each ends with a local
   answer and a visible notice.
5. Invariants in §4.5, especially: no tool schemas sent; effects never run from node output; skill turns
   stay local.
6. Event contract: `routed` payload carries the new `route_source`; old consumers unaffected.

## 7. Review and gates

Per `AGENTS.md`, this change touches a **security/privacy boundary** and **external integration**, so it
needs the independent **Wisp Release Auditor** on the frozen candidate SHA, and Simulation QA on
isolated/synthetic state. Expect to freeze one candidate, run exact-SHA mechanical validation and CI, then
get the verdict. The Auditor is read-only; a `BLOCK` stops the candidate and needs a recorded repair owner.

## 8. Open questions (resolve before building; do not guess)

1. **GX10 serving stack and model.** What serves the API (vLLM, llama.cpp, Ollama, other) and which model?
   This decides tool-call behaviour, context window, and the `qualified_capabilities` we may claim.
2. **Tailscale HTTPS.** Is `tailscale serve` HTTPS in place? If not, §4.1 needs a decision.
3. **Default behaviour.** Opt-in per user (recommended) or on by default when a node is configured?
4. **Escalation UX.** Automatic by workload, an explicit "think harder" control, or both (as proposed)?
5. **Threshold.** Is 12k tokens the right default for the long-context rule?
6. **Which turns are in v1?** Confirm the list in §3.1 (research, coding, long documents, careful drafting).

## 9. Handoff (finish with this)

Per `AGENTS.md`: the outcome delivered; files changed; tests and checks run (including failures);
incomplete work and risks; integration dependencies or conflicts; and the commit/branch, pushed to `origin`
and clean. State the base SHA and the frozen candidate SHA.

## 10. Hackathon note (for slides, not code)

Name the feature **Private Super Model** (or "Super Model, reborn"). The story: *old Super Model squeezed a
big model onto the Mac; the cloud version gave quality but sent data away; now the big model lives on the
user's own GX10, reached over Tailscale, and only ever composes text, so every action still happens on the
Mac, after the user approves.* Do not claim computer use or a node-driven tool loop until v2 exists.
