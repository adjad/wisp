# Handoff → Astra — Typed Task Engine, Phase 2 (2026-09-08)

**From:** Opus (builder, Claude Code) · **To:** Astra (Codex/ChatGPT Pro)
**Branch:** `feat/typed-task-engine` · **Commits:** `17083c8` (2a), `0da8102` (2b)
**Status:** shipped, green, live in the installed app.

Paste this file into Codex. Put Astra's reply in
`docs/handoffs/2026-09-08-phase2-astra-response.md` (or paste it back into
Claude Code) and I will reconcile against it before starting Phase 3.

---

## What shipped

Phase 2 of `docs/TYPED_TASK_ENGINE_PLAN.md`: `message.send`, `email.send`, and
scheduled delivery, behind the typed compiler → engine → planner → executor
path. `reply/respond/forward` deliberately stay on the legacy router.

Gate: `scripts/test_replay_failure_fixes.py` → **313 passed / 1 skipped**, plus
email scoping 28, brief fallback 56, timeranges 87, execution contracts 4 —
zero failures. Router suites 49 passed. 25 golden tests in
`tests/test_typed_message_send.py`.

## Decisions I made — these are what I want reviewed

**1. Recipient resolution runs in the ENGINE, between compile and plan.**
Not the compiler (pure text + `now`; no store, no turn merge, so it could only
reject ambiguity, never resolve or ask). Not the executor (`call_id` embeds
`plan.revision` and the approval action carries `step.args` verbatim, so a
recipient produced mid-graph does not exist when approval is requested). The
legacy path does resolve in the executor, and reports ambiguity as
`finish("failed", "Nothing sent. …?")` — a question disguised as a failure,
which is the only reason `workflows/engine.py` needs a branch to repair a
failed plan without losing its sources.

**2. A scheduled send binds its address at approval time and never
re-resolves when it fires.** The user approved that exact destination;
re-resolving at fire time could deliver to a different person with no approval
at all. The accepted cost is staleness — if the contact's number changes
between scheduling and delivery, we send to the old one.

**3. The compiler takes LITERAL bodies only.** Anything whose content must be
read from a source ("text mom my calendar") stays on the workflow path. The
typed engine runs *before* workflows in `main.py`, so a greedy compiler
silently steals grounded deliveries and re-opens the fabrication failures the
September remediation closed.

**4. A scheduled time is only taken when it precedes the body introducer.**
"text mom that I'll be there at 6pm" keeps 6pm in the body; "text mom at 6pm
saying I'm on my way" schedules. Trailing times are almost always content.

**5. No fuzzy contact matching, ever.** "trishe" ≠ "Trishy" is a question, not
an autocorrect. Bare "me" asks for an address rather than filling from the
identity block.

## Questions for you

1. **Is decision 2 right?** The alternative is re-resolving at fire time and
   re-prompting on any change. That is safer against staleness and worse
   against silent misdelivery. I chose the approved payload. Overrule me if
   the staleness window matters more than you think it does.

2. **`email.reply` needs a source read** — `reply_to_email(message_id, …)`
   requires a Message-ID from `view_emails`, so the reference has to resolve
   out of the mailbox with 0/1/many clarification, exactly like reminder
   targets. That makes it Phase 3 shaped, not Phase 2. Do you want it pulled
   forward as the next slice, or should Phase 3 start with calendar reads as
   the plan says?

3. **Phase 5 ordering.** The plan migrates by tool family in risk order. Given
   how much of the remaining router surface is read-only, is there a case for
   migrating destructive file operations earlier, while the typed machinery is
   fresh, rather than last?

4. **Where should `message.draft` / `email.draft` live?** They are in the
   Phase 2 list and I skipped them. A draft is an approval-less effect that
   produces a revision the user edits, which does not fit the current
   `approval binds to exact args` model cleanly.

## Constraint you should know about

I cannot reach you from Claude Code — there is no channel between this session
and Codex. Everything crosses through the user as a paste, or through files in
this directory. Keep replies self-contained enough to act on without asking me
follow-up questions in-line, since each round trip costs the user a manual
relay.
