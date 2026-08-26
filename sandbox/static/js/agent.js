// The /agent SSE reducer. Mirrors app/Sources/WispApp/OverlayModel.swift's
// handle(_:) — the reference implementation for this event stream — but
// surfaces everything inline instead of hiding it behind an export button,
// since this client's whole purpose is showing what the real overlay hides.
//
// Three rules are NOT optional, all confirmed against service/main.py and
// service/agent/loop.py:
//   1. `clear_answer` wipes the answer buffer. The backend uses this to
//      retract a streamed preamble/chain-of-thought leak — skip it and
//      duplicated or CoT text visibly leaks into the transcript.
//   2. `delta` and `text` are BOTH-APPEND, and mutually exclusive within one
//      turn (loop.py never emits both for the same answer).
//   3. Terminal is `done` OR `error`. A clean stream close with neither must
//      be synthesized as an error — the alternative is a turn that just
//      stops with no visible ending.
import { sseEvents } from "./sse.js";

export function createAgentSession() {
  let sessionId = null;

  async function runTurn(prompt, handlers, signal) {
    const state = {
      answer: "",
      timeline: [],       // {kind, ...} — routed/reasoning/tool/raw entries, in order
      toolById: new Map(), // action_id -> timeline entry, for tool_result/confirm correlation
      sawTerminal: false,
      t0: performance.now(),
    };
    const stamp = () => performance.now() - state.t0;

    const body = { prompt };
    if (sessionId) body.session_id = sessionId;

    try {
      for await (const ev of sseEvents("/api/agent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      })) {
        switch (ev.type) {
          case "session":
            sessionId = ev.id;
            handlers.onSession?.(ev.id);
            break;
          case "routed": {
            const entry = { kind: "routed", stamp: stamp(), ...ev };
            state.timeline.push(entry);
            handlers.onTimeline?.(state.timeline);
            break;
          }
          case "status":
            handlers.onStatus?.(ev.text);
            break;
          case "heartbeat":
            handlers.onHeartbeat?.();
            break;
          case "delta":
            state.answer += ev.text;
            handlers.onAnswer?.(state.answer);
            break;
          case "text":
            // Additive, same as delta — loop.py never emits both for one turn.
            state.answer += ev.text;
            handlers.onAnswer?.(state.answer);
            break;
          case "reasoning": {
            const entry = { kind: "reasoning", stamp: stamp(), text: ev.text };
            state.timeline.push(entry);
            handlers.onTimeline?.(state.timeline);
            break;
          }
          case "clear_answer":
            state.answer = "";
            handlers.onAnswer?.(state.answer);
            break;
          case "tool_call": {
            const entry = {
              kind: "tool", id: ev.id, name: ev.name, args: ev.args,
              decision: ev.decision, reason: ev.reason, stamp: stamp(),
              result: null, debug: null, dur: null,
            };
            state.timeline.push(entry);
            state.toolById.set(ev.id, entry);
            handlers.onTimeline?.(state.timeline);
            break;
          }
          case "tool_result": {
            const entry = state.toolById.get(ev.id);
            if (entry) {
              entry.result = ev.result;
              entry.debug = ev.debug || null;
              entry.dur = stamp() - entry.stamp;
            }
            handlers.onTimeline?.(state.timeline);
            break;
          }
          case "confirm": {
            const entry = state.toolById.get(ev.id) || { kind: "tool", id: ev.id, stamp: stamp() };
            entry.confirm = ev;
            if (!state.timeline.includes(entry)) state.timeline.push(entry);
            handlers.onConfirm?.(ev, (approved, scope) => approve(ev.id, approved, scope));
            handlers.onTimeline?.(state.timeline);
            break;
          }
          case "raw_model_io": {
            const entry = {
              kind: "raw", stamp: stamp(), model: ev.model,
              request: ev.request, response: ev.response,
            };
            state.timeline.push(entry);
            handlers.onTimeline?.(state.timeline);
            break;
          }
          case "error":
            state.sawTerminal = true;
            handlers.onError?.(ev.message);
            break;
          case "done":
            state.sawTerminal = true;
            handlers.onDone?.(state);
            break;
          default:
            // Unknown event types are ignored, not fatal — forward
            // compatibility with whatever the backend adds next.
            break;
        }
      }
    } catch (e) {
      if (signal?.aborted) {
        handlers.onError?.("stopped");
        return state;
      }
      handlers.onError?.(String(e?.message || e));
      return state;
    }

    if (!state.sawTerminal) {
      handlers.onError?.("the stream closed without finishing");
    }
    return state;
  }

  async function approve(actionId, approved, scope = "once") {
    await fetch("/api/agent/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, action_id: actionId, approved, scope }),
    });
  }

  function newChat() {
    sessionId = null;
  }

  return {
    get sessionId() { return sessionId; },
    runTurn,
    approve,
    newChat,
  };
}
