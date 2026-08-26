// Wisp Dev — the dev-instrumented chat app. Unlike the real notch overlay
// (which buries routing/tool/raw-model-I/O behind an export-to-Downloads
// button), everything the backend emits renders inline here: that's the
// entire reason this client exists instead of just reusing the Swift one.
import { createAgentSession } from "../agent.js";

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) node.appendChild(c);
  return node;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Minimal markdown: bold, inline code, and paragraph breaks — enough for
// Wisp's actual answer style without pulling in a dependency.
function renderMarkdown(text) {
  const esc = escapeHtml(text);
  const withInline = esc
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+?)`/g, "<code>$1</code>");
  return withInline.split(/\n{2,}/).map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
}

function fmtMs(ms) {
  if (ms == null) return "…";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function toolCard(entry, pendingResolvers, onResolved) {
  const decision = entry.decision || (entry.confirm ? "confirm" : "allow");
  const awaitingConfirm = entry.confirm && pendingResolvers.has(entry.id) && !entry.confirmSettled;
  const details = el("details", { class: "tool-card", ...(awaitingConfirm ? { open: "" } : {}) });
  const summary = el("summary", {}, [
    el("span", { class: `badge ${decision}`, text: entry.confirmDecision || decision }),
    el("span", { class: "name", text: `${entry.name || entry.confirm?.tool || "tool"}(…)` }),
    el("span", { class: "dur", text: entry.dur != null ? fmtMs(entry.dur) : "…" }),
  ]);
  details.appendChild(summary);

  const body = el("div", { class: "body" });
  if (awaitingConfirm) {
    body.appendChild(confirmBlock(entry, pendingResolvers, onResolved));
  }
  body.appendChild(el("h4", { text: "args" }));
  body.appendChild(el("pre", { text: JSON.stringify(entry.args ?? entry.confirm?.args ?? {}, null, 2) }));
  if (entry.reason || entry.confirm?.reason) {
    body.appendChild(el("h4", { text: "reason" }));
    body.appendChild(el("pre", { text: entry.reason || entry.confirm?.reason }));
  }
  if (entry.result != null) {
    body.appendChild(el("h4", { text: "result" }));
    const truncated = entry.result.length === 20000;
    body.appendChild(el("pre", { text: entry.result + (truncated ? "\n…(truncated at 20,000 chars)" : "") }));
  }
  if (entry.debug?.length) {
    const sub = el("div", { class: "debug-sub" });
    sub.appendChild(el("h4", { text: `internal calls (${entry.debug.length})` }));
    for (const d of entry.debug) {
      if (d.kind === "source") {
        sub.appendChild(el("div", {}, [
          el("strong", { text: d.label + ": " }),
          el("span", { text: (d.text || "").slice(0, 200) }),
        ]));
      } else if (d.kind === "model_call") {
        const dd = el("details", {});
        dd.appendChild(el("summary", { text: `model_call: ${d.model}` }));
        dd.appendChild(el("pre", { text: JSON.stringify({ request: d.request, response: d.response }, null, 2) }));
        sub.appendChild(dd);
      }
    }
    body.appendChild(sub);
  }
  details.appendChild(body);
  return details;
}

function reasoningCard(entry) {
  const details = el("details", { class: "tool-card reasoning-card" });
  details.appendChild(el("summary", { text: `reasoning (${(entry.text || "").split(/\s+/).length} words)` }));
  details.appendChild(el("div", { class: "body", text: entry.text }));
  return details;
}

function rawCard(entry) {
  const details = el("details", { class: "tool-card raw-card" });
  details.appendChild(el("summary", { text: `raw model I/O — ${entry.model}` }));
  const body = el("div", { class: "body" });
  body.appendChild(el("h4", { text: "request" }));
  body.appendChild(el("pre", { text: JSON.stringify(entry.request, null, 2) }));
  body.appendChild(el("h4", { text: "response" }));
  body.appendChild(el("pre", { text: JSON.stringify(entry.response, null, 2) }));
  details.appendChild(body);
  return details;
}

// Rendered INSIDE the owning tool card's body — not a separate element —
// because the timeline re-renders wholesale on every SSE event (routed,
// another tool_call, a status update all trigger onTimeline), and anything
// appended outside that rebuild gets wiped by the very next one. A prior
// version appended a standalone card from the `confirm` handler and lost it
// to the onTimeline call agent.js fires immediately afterward for the same
// event — caught interactively while testing this against a real turn.
function confirmBlock(entry, pendingResolvers, onResolved) {
  const ev = entry.confirm;
  const block = el("div", { class: "confirm-card" });
  block.appendChild(el("div", { class: "reason", text: ev.reason || "" }));
  if (ev.preview) {
    block.appendChild(el("pre", { text: ev.preview }));
  }
  const waiting = el("div", { class: "waiting", text: "waiting for you — 0s" });
  block.appendChild(waiting);

  if (entry._waitTimer) clearInterval(entry._waitTimer);
  if (entry._waitStart == null) entry._waitStart = performance.now();
  entry._waitTimer = setInterval(() => {
    const secs = Math.round((performance.now() - entry._waitStart) / 1000);
    waiting.textContent = `waiting for you — ${secs}s (an unanswered confirm blocks the turn indefinitely)`;
  }, 1000);

  const finish = (approved, scope) => {
    clearInterval(entry._waitTimer);
    entry.confirmSettled = true;
    entry.confirmDecision = approved ? (scope === "always" ? "always allowed" : "allowed once") : "denied";
    const resolve = pendingResolvers.get(entry.id);
    pendingResolvers.delete(entry.id);
    resolve?.(approved, scope);
    onResolved();
  };

  const actions = el("div", { class: "actions" });
  actions.appendChild(el("button", { class: "deny", text: "Deny", onclick: () => finish(false) }));
  if (ev.grantable) {
    actions.appendChild(el("button", {
      class: "approve-always", text: `Always allow (${ev.scope_hint || "this tool"})`,
      onclick: () => finish(true, "always"),
    }));
  }
  actions.appendChild(el("button", { class: "approve", text: "Allow once", onclick: () => finish(true, "once") }));
  block.appendChild(actions);
  return block;
}

export function render(container) {
  container.innerHTML = "";
  const session = createAgentSession();
  let controller = null;

  const root = el("div", { class: "wispdev" });
  const header = el("div", { class: "wispdev-header" }, [
    el("span", { class: "title", text: "Wisp Dev" }),
    el("button", { text: "New chat", onclick: () => { session.newChat(); transcript.innerHTML = ""; } }),
  ]);
  const transcript = el("div", { class: "transcript" });
  const input = el("input", { type: "text", placeholder: "Message Wisp…" });
  const sendBtn = el("button", { text: "Send" });
  const composer = el("div", { class: "composer" }, [input, sendBtn]);

  root.appendChild(header);
  root.appendChild(transcript);
  root.appendChild(composer);
  container.appendChild(root);

  function scrollToBottom() {
    transcript.scrollTop = transcript.scrollHeight;
  }

  async function submit() {
    const prompt = input.value.trim();
    if (!prompt || controller) return;
    input.value = "";

    transcript.appendChild(el("div", { class: "turn user" }, [
      el("div", { class: "bubble-user", text: prompt }),
    ]));

    const turnEl = el("div", { class: "turn assistant" });
    const chipRow = el("div");
    const statusLine = el("div", { class: "status-line" });
    const activityEl = el("div", {});
    const answerEl = el("div", { class: "bubble-assistant" });
    const footerEl = el("div", { class: "turn-footer" });
    turnEl.appendChild(chipRow);
    turnEl.appendChild(statusLine);
    turnEl.appendChild(activityEl);
    turnEl.appendChild(answerEl);
    turnEl.appendChild(footerEl);
    transcript.appendChild(turnEl);
    scrollToBottom();

    controller = new AbortController();
    sendBtn.textContent = "Stop";
    sendBtn.classList.add("stop");
    sendBtn.onclick = () => controller?.abort();

    let finalState = null;
    let lastTimeline = [];
    const pendingResolvers = new Map(); // action_id -> resolve(approved, scope)
    const t0 = performance.now();

    function renderActivity(timeline) {
      lastTimeline = timeline;
      chipRow.innerHTML = "";
      activityEl.innerHTML = "";
      for (const entry of timeline) {
        if (entry.kind === "routed") {
          chipRow.appendChild(el("span", { class: "routing-chip" }, [
            el("span", { class: "role", text: entry.role }),
            el("span", { text: `→ ${entry.model}` }),
            el("span", { text: entry.needs_tools ? "· tools" : "" }),
            el("span", { text: `· via ${entry.source || entry.route_source || "?"}` }),
          ]));
        } else if (entry.kind === "reasoning") {
          activityEl.appendChild(reasoningCard(entry));
        } else if (entry.kind === "tool") {
          activityEl.appendChild(toolCard(entry, pendingResolvers, () => renderActivity(lastTimeline)));
        } else if (entry.kind === "raw") {
          activityEl.appendChild(rawCard(entry));
        }
      }
      scrollToBottom();
    }

    const result = await session.runTurn(prompt, {
      onSession() {},
      onStatus(text) {
        statusLine.textContent = text;
      },
      onAnswer(full) {
        statusLine.textContent = "";
        answerEl.innerHTML = renderMarkdown(full);
        scrollToBottom();
      },
      onTimeline: renderActivity,
      onConfirm(ev, resolve) {
        // No DOM here — renderActivity (called right after by agent.js's
        // own onTimeline call for this same event) reads entry.confirm and
        // pendingResolvers.has(entry.id) to decide whether to draw the
        // confirm UI, so there is nothing here to be wiped by that rebuild.
        pendingResolvers.set(ev.id, resolve);
      },
      onError(message) {
        statusLine.textContent = "";
        turnEl.appendChild(el("div", { class: "error-banner", text: `⚠ ${message}` }));
        scrollToBottom();
      },
      onDone(state) {
        finalState = state;
      },
    }, controller.signal);

    const totalMs = performance.now() - t0;
    const toolCount = (result.timeline || []).filter((e) => e.kind === "tool").length;
    const rawCount = (result.timeline || []).filter((e) => e.kind === "raw").length;
    footerEl.textContent = `total ${fmtMs(totalMs)} · ${toolCount} tool call(s) · ${rawCount} model call(s)`;
    const copyBtn = el("button", { text: " · copy turn as JSON" });
    copyBtn.addEventListener("click", () => {
      navigator.clipboard?.writeText(JSON.stringify(
        { prompt, answer: result.answer, timeline: result.timeline }, null, 2));
      copyBtn.textContent = " · copied!";
      setTimeout(() => { copyBtn.textContent = " · copy turn as JSON"; }, 1200);
    });
    footerEl.appendChild(copyBtn);

    controller = null;
    sendBtn.textContent = "Send";
    sendBtn.classList.remove("stop");
    sendBtn.onclick = submit;
    scrollToBottom();
  }

  sendBtn.onclick = submit;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submit();
  });
}
