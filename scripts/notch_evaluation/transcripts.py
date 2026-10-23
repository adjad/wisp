"""transcripts.html: every evaluated case as the notch showed it, offline.

Stdlib only. Reads the private run directory (results.jsonl, cases.json,
sections.json) and writes one self-contained page next to them: no network,
no external script, style or font. Each case reads like a Wisp chat: the
user's prompt, then Wisp's reply with its status lines, tool steps (collapsed)
and any confirmation card, then the complete answer. The latest attempt of
each case is shown. Filterable by section, tag and failure class; searchable.

Everything from a case is inserted with textContent, never as markup, so a
prompt, answer or tool result can never run in the page. The file is
private (0600) and replaced atomically, so it can be opened mid-run.

    transcripts.py render --run-dir DIR
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PAGE = "transcripts.html"
# A tool result in the page; results.jsonl keeps what the app sent in full.
RESULT_CHARS = 8000


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _latest(run: Path) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    try:
        lines = (run / "results.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return latest
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record.get("case_id"), int):
            latest[record["case_id"]] = record
    return latest


def _clip(text, limit: int = RESULT_CHARS) -> dict:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return {"text": text[:limit], "clipped": max(0, len(text) - limit)}


def _turn(turn: dict) -> dict:
    events = []
    for event in turn.get("events") or []:
        if not isinstance(event, dict):
            continue
        entry = {k: v for k, v in event.items() if k != "result"}
        if "result" in event:
            result = event["result"]
            entry["result"] = _clip(result.get("text", "") if isinstance(result, dict) else result)
            if isinstance(result, dict) and result.get("truncated_from"):
                entry["result"]["app_truncated_from"] = result["truncated_from"]
        events.append(entry)
    return {"prompt": turn.get("prompt") or "", "answer": turn.get("final_text") or "",
            "failure_class": turn.get("failure_class"), "outcome": turn.get("outcome"),
            "model": turn.get("model"), "route": turn.get("route") or {},
            "latency_s": turn.get("latency_s"), "ttft_s": turn.get("ttft_s"),
            "events": events, "activity": turn.get("activity") or [],
            "confirmation": turn.get("confirmation"),
            "tools": [{"name": t.get("name"), "decision": t.get("decision"), "args": t.get("args", ""),
                       "result": _clip(t.get("result", ""))} for t in turn.get("tools") or []
                      if isinstance(t, dict)],
            "error": turn.get("error")}


def build(run: Path) -> dict:
    cases = {c["case_id"]: c for c in _read_json(run / "cases.json", {}).get("cases", [])}
    sections = _read_json(run / "sections.json", {})
    shown = []
    for case_id, record in sorted(_latest(run).items()):
        case = cases.get(case_id, {})
        shown.append({"id": case_id, "tag": record.get("risk_tag") or case.get("risk_tag"),
                      "section": sections.get(str(case_id), ""), "class": record.get("failure_class"),
                      "model": record.get("model"), "latency_s": record.get("latency_s"),
                      "attempt": record.get("attempt"), "label": record.get("label"),
                      "candidate": (record.get("candidate_sha") or "")[:12],
                      "recorded_at": record.get("recorded_at"),
                      "turns": [_turn(t) for t in record.get("turns") or [] if isinstance(t, dict)]})
    return {"generated_at": time.time(), "total_cases": len(cases), "cases": shown}


def _embed(data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Inside <script type="application/json">: nothing may close the element.
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render(run_dir: str | os.PathLike) -> Path:
    run = Path(run_dir)
    page = HTML.replace("__DATA__", _embed(build(run)))
    target = run / PAGE
    stage = run / f".{PAGE}.{os.getpid()}.tmp"
    fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(page)
        os.replace(stage, target)
    except BaseException:
        try:
            os.unlink(stage)
        except FileNotFoundError:
            pass
        raise
    return target


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>Wisp notch transcripts</title>
<style>
:root{--bg:#0d1017;--panel:#151a24;--panel2:#1b2130;--line:#283044;--text:#e7ebf3;--muted:#98a2b8;
--user:#2f5fd0;--user-text:#fff;--wisp:#1d2433;--accent:#8fb4ff;--ok:#5fd39a;--warn:#f0b65a;--bad:#ff7b7b;
--chip:#222a3b;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media (prefers-color-scheme: light){:root{--bg:#f4f6fa;--panel:#fff;--panel2:#f0f3f8;--line:#dde3ee;
--text:#1a2030;--muted:#5d6880;--user:#2f63e0;--wisp:#eef1f7;--accent:#2f63e0;--chip:#e9edf5}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:color-mix(in srgb,var(--bg) 88%,transparent);
backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:12px 16px}
h1{font-size:16px;margin:0 0 8px;font-weight:600}
.summary{color:var(--muted);font-size:12px;margin-bottom:8px}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font:inherit}
input[type=search]{flex:1 1 220px;min-width:0}
label.tag{display:inline-flex;gap:4px;align-items:center;color:var(--muted)}
main{max-width:860px;margin:0 auto;padding:16px}
.case{background:var(--panel);border:1px solid var(--line);border-radius:14px;margin:0 0 16px;overflow:hidden}
.case-head{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);background:var(--panel2)}
.chip{background:var(--chip);border-radius:999px;padding:1px 8px;font-size:12px;color:var(--muted)}
.chip.cls-OK{color:var(--ok)}.chip.bad{color:var(--bad)}.chip.warn{color:var(--warn)}
.id{font-weight:600}
.convo{padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.bubble{max-width:88%;border-radius:16px;padding:9px 12px;white-space:pre-wrap;overflow-wrap:anywhere}
.user{align-self:flex-end;background:var(--user);color:var(--user-text);border-bottom-right-radius:4px}
.wisp{align-self:flex-start;background:var(--wisp);border-bottom-left-radius:4px;width:88%}
.status{color:var(--muted);font-size:12px;font-style:italic;margin:2px 0}
.steps{margin:6px 0;font-size:12px}
.steps summary{cursor:pointer;color:var(--accent)}
.step{border-left:2px solid var(--line);margin:6px 0 6px 4px;padding:2px 0 2px 8px}
.step .name{font-family:var(--mono);font-weight:600}
pre{font-family:var(--mono);font-size:11.5px;white-space:pre-wrap;overflow-wrap:anywhere;margin:4px 0;
background:var(--panel2);border-radius:6px;padding:6px 8px;max-height:320px;overflow:auto}
.card{border:1px solid var(--warn);border-radius:10px;padding:6px 10px;margin:6px 0;font-size:12.5px}
.answer{margin-top:6px}
.meta{color:var(--muted);font-size:11.5px;margin-top:6px}
.empty{color:var(--muted);text-align:center;padding:40px 0}
ul.md{margin:4px 0;padding-left:20px}
</style>
</head>
<body>
<header>
<h1>Wisp notch transcripts</h1>
<div class="summary" id="summary"></div>
<div class="filters">
<input type="search" id="q" placeholder="Search prompts, answers, tools" aria-label="Search">
<select id="section" aria-label="Section"><option value="">All sections</option></select>
<select id="cls" aria-label="Failure class"><option value="">All results</option></select>
<label class="tag"><input type="checkbox" value="R" checked>R</label>
<label class="tag"><input type="checkbox" value="W" checked>W</label>
<label class="tag"><input type="checkbox" value="S" checked>S</label>
</div>
</header>
<main id="list"></main>
<script type="application/json" id="data">__DATA__</script>
<script>
(function () {
  "use strict";
  var data = JSON.parse(document.getElementById("data").textContent);
  var BAD = {EVIDENCE_UNHEALTHY:1, HOST_UNAVAILABLE:1, ENGINE_NOT_READY:1, INTERRUPTED:1, INPUT_MISMATCH:1,
             MODEL_MISMATCH:1, ERROR_TURN:1, TIMEOUT:1, DROPPED:1, NOT_SUBMITTED:1, HOST_BUSY:1};
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function chip(text, cls) { return el("span", "chip" + (cls ? " " + cls : ""), text); }
  function secs(v) { return typeof v === "number" ? v.toFixed(1) + "s" : "–"; }
  function clipped(result) {
    var box = el("pre", null, result && result.text || "");
    if (result && (result.clipped || result.app_truncated_from)) {
      box.appendChild(document.createTextNode("\n… " + (result.clipped || 0) +
        " more characters in results.jsonl" + (result.app_truncated_from ? " (app sent the first 64 KB of " +
        result.app_truncated_from + ")" : "")));
    }
    return box;
  }
  // Minimal, text-only Markdown: paragraphs, "- " lists and **bold**.
  function answer(text) {
    var box = el("div", "answer");
    var list = null;
    text.split("\n").forEach(function (line) {
      var item = /^\s*[-*•]\s+(.*)$/.exec(line);
      if (item) {
        if (!list) { list = el("ul", "md"); box.appendChild(list); }
        list.appendChild(inline(el("li"), item[1]));
        return;
      }
      list = null;
      box.appendChild(inline(el("div"), line || " "));
    });
    return box;
  }
  function inline(node, text) {
    text.split(/(\*\*[^*]+\*\*)/).forEach(function (part) {
      if (/^\*\*[^*]+\*\*$/.test(part)) node.appendChild(el("strong", null, part.slice(2, -2)));
      else if (part) node.appendChild(document.createTextNode(part));
    });
    return node;
  }
  function wispReply(turn) {
    var box = el("div", "bubble wisp");
    var calls = {}, steps = [];
    (turn.events || []).forEach(function (ev) {
      if (ev.type === "status" && ev.text) box.appendChild(el("div", "status", ev.text));
      else if (ev.type === "tool_call") { calls[ev.id] = {call: ev}; steps.push(calls[ev.id]); }
      else if (ev.type === "tool_result") {
        if (calls[ev.id]) calls[ev.id].result = ev.result; else steps.push({result: ev.result});
      } else if (ev.type === "confirm") {
        var card = el("div", "card");
        card.appendChild(el("strong", null, "Confirmation: " + (ev.tool || "")));
        if (ev.reason) card.appendChild(el("div", null, ev.reason));
        if (ev.preview) card.appendChild(el("pre", null, ev.preview));
        card.appendChild(el("div", "status", "Declined by the evaluation (nothing was done)"));
        box.appendChild(card);
      } else if (ev.type === "message_draft") {
        var draft = el("div", "card");
        draft.appendChild(el("strong", null, "Message draft to " + (ev.to || "")));
        draft.appendChild(el("pre", null, ev.text || ""));
        draft.appendChild(el("div", "status", "Not sent: the evaluation build never sends"));
        box.appendChild(draft);
      } else if (ev.type === "error") {
        box.appendChild(el("div", "status", "Error: " + (ev.error || ev.text || ev.message || "")));
      }
    });
    if (!turn.events || !turn.events.length) {
      (turn.tools || []).forEach(function (t) {
        steps.push({call: {name: t.name, args: t.args, decision: t.decision}, result: t.result});
      });
    }
    if (steps.length) {
      var details = el("details", "steps");
      details.appendChild(el("summary", null, steps.length + " tool step" + (steps.length > 1 ? "s" : "")));
      steps.forEach(function (s) {
        var step = el("div", "step");
        var head = el("div");
        head.appendChild(el("span", "name", s.call ? s.call.name : "result"));
        if (s.call && s.call.decision) head.appendChild(document.createTextNode("  · " + s.call.decision));
        step.appendChild(head);
        if (s.call && s.call.args) step.appendChild(el("pre", null, s.call.args));
        if (s.result) step.appendChild(clipped(s.result));
        details.appendChild(step);
      });
      box.appendChild(details);
    }
    if (turn.confirmation && !(turn.events || []).some(function (e) { return e.type === "confirm"; })) {
      box.appendChild(el("div", "card", "Confirmation for " + (turn.confirmation.tool || "") + ": declined"));
    }
    if (turn.answer) box.appendChild(answer(turn.answer));
    else box.appendChild(el("div", "status", turn.outcome ? "(no answer: " + turn.outcome + ")" : "(no reply)"));
    var route = turn.route || {};
    box.appendChild(el("div", "meta", [turn.model || "model unknown", route.role, route.source,
      "latency " + secs(turn.latency_s), "first token " + secs(turn.ttft_s), turn.failure_class]
      .filter(Boolean).join(" · ")));
    return box;
  }
  function render(c) {
    var card = el("article", "case");
    var head = el("div", "case-head");
    head.appendChild(el("span", "id", "#" + c.id));
    head.appendChild(chip(c.tag));
    if (c.section) head.appendChild(chip(c.section));
    head.appendChild(chip(c.class, "cls-" + c.class + (BAD[c.class] ? " bad" : c.class === "OK" ? "" : " warn")));
    head.appendChild(chip(c.model || "model ?"));
    head.appendChild(chip(secs(c.latency_s)));
    if (c.attempt > 1) head.appendChild(chip("attempt " + c.attempt));
    card.appendChild(head);
    var convo = el("div", "convo");
    c.turns.forEach(function (t) {
      convo.appendChild(el("div", "bubble user", t.prompt));
      convo.appendChild(wispReply(t));
    });
    card.appendChild(convo);
    return card;
  }
  function haystack(c) {
    return [c.section, c.class, c.model].concat(c.turns.map(function (t) {
      return [t.prompt, t.answer].concat((t.events || []).map(function (e) {
        return [e.name, e.text, e.tool, e.args].join(" ");
      })).join(" ");
    })).join(" ").toLowerCase();
  }
  var list = document.getElementById("list"), q = document.getElementById("q");
  var section = document.getElementById("section"), cls = document.getElementById("cls");
  var tags = Array.prototype.slice.call(document.querySelectorAll(".tag input"));
  var sections = {}, classes = {}, counts = {};
  data.cases.forEach(function (c) {
    c._text = haystack(c); c._node = null;
    if (c.section) sections[c.section] = 1;
    classes[c.class] = 1; counts[c.class] = (counts[c.class] || 0) + 1;
  });
  Object.keys(sections).sort().forEach(function (s) { section.appendChild(el("option", null, s)).value = s; });
  Object.keys(classes).sort().forEach(function (s) { cls.appendChild(el("option", null, s + " (" + counts[s] + ")")).value = s; });
  document.getElementById("summary").textContent = data.cases.length + " of " + data.total_cases +
    " cases recorded · updated " + new Date(data.generated_at * 1000).toLocaleString() + " · " +
    Object.keys(counts).sort().map(function (k) { return k + " " + counts[k]; }).join(" · ");
  function apply() {
    var words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
    var allowed = tags.filter(function (t) { return t.checked; }).map(function (t) { return t.value; });
    list.textContent = "";
    var shown = 0;
    data.cases.forEach(function (c) {
      if (section.value && c.section !== section.value) return;
      if (cls.value && c.class !== cls.value) return;
      if (allowed.indexOf(c.tag) < 0) return;
      if (words.some(function (w) { return c._text.indexOf(w) < 0; })) return;
      if (!c._node) c._node = render(c);
      list.appendChild(c._node); shown++;
    });
    if (!shown) list.appendChild(el("div", "empty", data.cases.length ? "No case matches." : "No case has run yet."));
  }
  [q, section, cls].concat(tags).forEach(function (n) { n.addEventListener("input", apply); });
  apply();
})();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("render")
    go.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    print(render(args.run_dir))
    return 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
