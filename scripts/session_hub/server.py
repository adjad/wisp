#!/usr/bin/env python3
"""Wisp Session Hub: a local dashboard over your Claude Code chats for this repo.

Reads the desktop app's session metadata and the transcript JSONL files, derives
what each chat is doing / has done / is blocked on, and lets you steer a chat by
resuming it with a new message. Stdlib only. Binds to 127.0.0.1.

    python3 scripts/session_hub/server.py [--port 8790] [--scope MOE_Project]
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
META_ROOT = HOME / "Library/Application Support/Claude/claude-code-sessions"
PROJECTS = HOME / ".claude/projects"
STATE_DIR = HOME / ".wisp-hub"
ORG_FILE = STATE_DIR / "organize.json"
HERE = Path(__file__).parent

RUNNING_WINDOW_S = 90          # transcript touched this recently => running
STUCK_TOOL_S = 600             # tool_use with no result for this long => stuck
TOKEN = secrets.token_urlsafe(24)

# Auto-grouping: first matching workstream wins. Edit freely, or override per chat in the UI.
WORKSTREAMS = [
    ("Routing & tools", r"rout|tool[- ]call|tool[- ]detect|tools audit|semantic"),
    ("Models & performance", r"ling|model|oMLX|omlx|perf|latency|quant|lfm|bench|viab|edge"),
    ("Attention & proactive", r"attention|remind|proactive|behavior|daily summary|summary"),
    ("Browser & web", r"browser|canvas|web"),
    ("Chat app & UI", r"chat app|visual|ui |demo|slide|polish|hackathon"),
    ("Release & repo health", r"release|audit|redundant|stability|open prs|github|bundl|flaky|ci "),
    ("Bugs & debugging", r"bug|fix|debug|issue|lookup|stale"),
]

BLOCKER_RE = re.compile(
    r"\b(blocked|blocker|BLOCK\b|cannot proceed|can't proceed|need your|waiting (on|for) you|"
    r"permission (denied|required)|timed out)\b",
    re.I,
)
ASK_RE = re.compile(r"(\?\s*$)|\b(should I|want me to|which (one|option)|confirm|let me know|your call)\b", re.I)


# --------------------------------------------------------------------------- helpers
def iso(ms_or_str) -> str | None:
    if ms_or_str is None:
        return None
    if isinstance(ms_or_str, (int, float)):
        return datetime.fromtimestamp(ms_or_str / 1000, tz=timezone.utc).isoformat()
    return str(ms_or_str)


def parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
    return "\n".join(out)


def is_noise_prompt(text: str) -> bool:
    t = text.strip()
    return (not t) or t.startswith(("<system-reminder", "<command-", "<local-command", "Caveat:", "[Request interrupted"))


# --------------------------------------------------------------------------- transcript
_tx_cache: dict[str, tuple[float, int, dict]] = {}


def analyze_transcript(path: Path) -> dict:
    st = path.stat()
    hit = _tx_cache.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]

    turns: list[dict] = []
    cur: dict | None = None
    pending: dict[str, dict] = {}   # tool_use id -> info
    recent_results: list[bool] = []  # is_error flags, rolling
    prs: dict[int, dict] = {}
    last_assistant_text, last_assistant_ts, last_stop = "", 0.0, None
    last_kind, last_ts = None, 0.0
    tool_total = 0

    def close():
        nonlocal cur
        if cur:
            turns.append(cur)
            cur = None

    with path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "pr-link":
                prs[d["prNumber"]] = {"number": d["prNumber"], "url": d.get("prUrl"), "repo": d.get("prRepository")}
                continue
            if t not in ("user", "assistant") or d.get("isSidechain"):
                continue
            ts = parse_ts(d.get("timestamp"))
            msg = d.get("message") or {}
            content = msg.get("content")

            if t == "user":
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            err = bool(b.get("is_error"))
                            recent_results.append(err)
                            info = pending.pop(b.get("tool_use_id"), None)
                            if cur is not None and err:
                                cur["errors"] += 1
                                if info:
                                    cur["error_tools"].append(info["name"])
                text = block_text(content)
                if d.get("isMeta") or is_noise_prompt(text):
                    continue
                close()
                cur = {"ts": ts, "end": ts, "prompt": clip(text, 600), "outcome": "", "tools": 0,
                       "errors": 0, "error_tools": [], "edits": 0, "stop": None}
                last_kind, last_ts = "user", ts
            else:
                if cur is None:
                    cur = {"ts": ts, "end": ts, "prompt": "(session continued)", "outcome": "", "tools": 0,
                           "errors": 0, "error_tools": [], "edits": 0, "stop": None}
                cur["end"] = ts
                for b in content if isinstance(content, list) else []:
                    if b.get("type") == "tool_use":
                        cur["tools"] += 1
                        tool_total += 1
                        if b.get("name") in ("Edit", "Write", "NotebookEdit"):
                            cur["edits"] += 1
                        pending[b.get("id")] = {"name": b.get("name"), "ts": ts, "input": b.get("input")}
                text = block_text(content).strip()
                if text:
                    cur["outcome"] = clip(text, 900)
                    last_assistant_text, last_assistant_ts = text, ts
                cur["stop"] = last_stop = msg.get("stop_reason")
                last_kind, last_ts = "assistant", ts

    close()
    now = time.time()
    mtime = st.st_mtime

    # --- blocker detection
    blockers: list[dict] = []
    asking_tool = next((p for p in pending.values() if p["name"] == "AskUserQuestion"), None)
    if asking_tool:
        blockers.append({"kind": "question", "text": "Waiting on your answer to a multiple-choice question."})
    stuck = [p for p in pending.values() if p["name"] != "AskUserQuestion" and now - p["ts"] > STUCK_TOOL_S]
    if stuck and now - mtime > STUCK_TOOL_S:
        blockers.append({"kind": "stuck", "text": f"{stuck[0]['name']} has had no result for {int((now - stuck[0]['ts']) / 60)} min (likely a permission prompt or hang)."})
    tail = recent_results[-12:]
    if len(tail) >= 4 and sum(tail) >= 4:
        blockers.append({"kind": "errors", "text": f"{sum(tail)} of the last {len(tail)} tool calls errored."})
    if last_kind == "assistant" and last_stop == "end_turn" and last_assistant_text:
        paras = [p.strip() for p in re.split(r"\n\s*\n", last_assistant_text.strip()) if p.strip()]
        final = paras[-1] if paras else ""
        plain = re.sub(r"[*_`]+", "", final).strip()
        if plain.endswith("?") and not asking_tool:
            blockers.append({"kind": "question", "text": clip(plain.splitlines()[-1], 240)})
        elif BLOCKER_RE.search(plain):
            m = BLOCKER_RE.search(plain)
            s0 = max(0, m.start() - 90)
            blockers.append({"kind": "mentions", "text": "…" + clip(plain[s0:m.end() + 130], 240)})

    running = (now - mtime < RUNNING_WINDOW_S) or bool(
        pending and now - mtime < STUCK_TOOL_S and not asking_tool
    )

    result = {
        "turns": turns,
        "turnCount": len(turns),
        "toolCalls": tool_total,
        "prs": list(prs.values()),
        "blockers": blockers,
        "running": running,
        "awaitingUser": any(b["kind"] == "question" for b in blockers),
        "lastKind": last_kind,
        "lastTs": last_ts,
        "lastOutcome": clip(last_assistant_text, 500),
        "mtime": mtime,
    }
    _tx_cache[str(path)] = (st.st_mtime, st.st_size, result)
    return result


# --------------------------------------------------------------------------- PR status (gh, best-effort)
_pr_cache: dict[str, dict] = {}
_pr_lock = threading.Lock()


_pr_busy = threading.Lock()


def refresh_prs(keys: set[tuple[str, int]]):
    if not shutil.which("gh") or not _pr_busy.acquire(blocking=False):
        return
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda k: _refresh_one(*k), keys))
    finally:
        _pr_busy.release()


def _refresh_one(repo: str, num: int):
    if True:
        k = f"{repo}#{num}"
        with _pr_lock:
            c = _pr_cache.get(k)
            if c and time.time() - c["at"] < 300:
                return
            _pr_cache[k] = {"at": time.time(), **(c or {})}  # claim to avoid stampede
        try:
            out = subprocess.run(
                ["gh", "pr", "view", str(num), "--repo", repo, "--json",
                 "state,isDraft,title,url,mergeStateStatus,reviewDecision,statusCheckRollup"],
                capture_output=True, text=True, timeout=20)
            if out.returncode:
                return
            j = json.loads(out.stdout)
            checks = j.get("statusCheckRollup") or []
            bad = [c.get("name") or c.get("context") for c in checks
                   if (c.get("conclusion") or c.get("state")) in ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR")]
            pend = [c for c in checks if (c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING")) or c.get("state") == "PENDING"]
            with _pr_lock:
                _pr_cache[k] = {"at": time.time(), "state": j["state"], "draft": j["isDraft"], "title": j["title"],
                                "url": j["url"], "merge": j.get("mergeStateStatus"), "review": j.get("reviewDecision"),
                                "failing": bad, "pending": len(pend), "checks": len(checks)}
        except Exception:
            pass


# --------------------------------------------------------------------------- organize store
_org_lock = threading.Lock()


def load_org() -> dict:
    try:
        return json.loads(ORG_FILE.read_text())
    except Exception:
        return {}


def save_org(org: dict):
    STATE_DIR.mkdir(exist_ok=True)
    tmp = ORG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(org, indent=1))
    tmp.replace(ORG_FILE)


def guess_workstream(title: str) -> str:
    for name, rx in WORKSTREAMS:
        if re.search(rx, title, re.I):
            return name
    return "Other"


# --------------------------------------------------------------------------- summaries
SUM_FILE = STATE_DIR / "summaries.json"
_sum_lock = threading.Lock()
_sums: dict[str, dict] = {}
_cli = {"ready": None, "at": 0.0, "why": ""}


def plain(t: str) -> str:
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t or "")
    t = re.sub(r"[*`#>|_]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def sentences(t: str, n: int, limit: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", plain(t))
    parts = [p for p in parts if len(p) > 12 and not re.fullmatch(r"(done|ok|okay|sure|yes)[.!]?", p, re.I)]
    return clip(" ".join(parts[:n]), limit)


def heuristic_summary(tx: dict | None, title: str) -> dict:
    if not tx or not tx["turns"]:
        return {"goal": title, "now": "", "source": "auto"}
    first = tx["turns"][0]["prompt"]
    first = re.sub(r"<[^>]*>?", " ", first)
    first = re.sub(r"^\W*(hey|hi|ok|okay)?\W*(i (want|need|would like) (you )?(to |a way to )?|can you |could you |please |i am dealing with |i'm )", "",
                   plain(first), flags=re.I)
    goal = sentences(first, 1, 150) or title
    goal = goal[:1].upper() + goal[1:]
    now = sentences(tx["lastOutcome"], 2, 230)
    return {"goal": goal, "now": now, "source": "auto"}


def cli_ready() -> tuple[bool, str]:
    """Is headless `claude -p` usable (logged in)? Cached 2 min."""
    if not shutil.which("claude"):
        return False, "`claude` CLI not found on PATH."
    if _cli["ready"] is not None and time.time() - _cli["at"] < 120:
        return _cli["ready"], _cli["why"]
    ok, why = False, ""
    try:
        p = subprocess.run(["claude", "-p", "Reply: ok", "--model", "haiku", "--no-session-persistence"],
                           cwd=str(STATE_DIR if STATE_DIR.exists() else HOME), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
        out = (p.stdout + p.stderr).strip()
        ok = p.returncode == 0 and "not logged in" not in out.lower()
        why = "" if ok else ("The `claude` CLI is not logged in. Run `claude` in a terminal and use /login."
                             if "logged in" in out.lower() else clip(out, 200))
    except Exception as e:  # noqa: BLE001
        why = str(e)
    _cli.update(ready=ok, at=time.time(), why=why)
    return ok, why


def digest(tx: dict, title: str) -> str:
    t = tx["turns"]
    parts = [f"Chat title: {title}", "FIRST REQUEST:\n" + t[0]["prompt"][:900]]
    for x in t[-4:]:
        parts.append(f"USER: {x['prompt'][:350]}\nASSISTANT RESULT: {x['outcome'][:600]}")
    if tx["blockers"]:
        parts.append("OPEN BLOCKERS: " + "; ".join(b["text"] for b in tx["blockers"]))
    return "\n\n".join(parts)


def ai_summary(tx: dict, title: str) -> dict | None:
    prompt = ("You summarize a developer's coding-assistant chat for a status board. Plain text only, no markdown.\n"
              "Return ONLY JSON: {\"goal\": \"<=14 words: the overall task of the chat, noun phrase\", "
              "\"now\": \"<=30 words: what is being done right now or was just finished, and what is next or blocking, if anything\"}\n\n"
              + digest(tx, title))
    try:
        p = subprocess.run(["claude", "-p", prompt, "--model", "haiku", "--no-session-persistence"],
                           cwd=str(STATE_DIR), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        m = re.search(r"\{.*\}", p.stdout, re.S)
        j = json.loads(m.group(0)) if m else None
        if j and j.get("goal") and j.get("now"):
            return {"goal": clip(plain(j["goal"]), 140), "now": clip(plain(j["now"]), 260), "source": "ai"}
    except Exception:  # noqa: BLE001
        pass
    return None


def summarizer_loop(scope: str):
    STATE_DIR.mkdir(exist_ok=True)
    try:
        _sums.update(json.loads(SUM_FILE.read_text()))
    except Exception:
        pass
    while True:
        time.sleep(15)
        try:
            ok, _ = cli_ready()
            if not ok:
                continue
            for r in load_sessions(scope):
                if r["state"] == "idle" and time.time() - r["lastActivity"] > 7 * 86400:
                    continue
                tp = find_transcript(r["cliId"]) if r["cliId"] else None
                tx = analyze_transcript(tp) if tp else None
                if not tx or not tx["turns"]:
                    continue
                sig = f"{tx['turnCount']}:{int(tx['mtime'])}"
                cur = _sums.get(r["id"])
                if cur and cur.get("sig") == sig:
                    continue
                if cur and tx["running"] and time.time() - cur.get("at", 0) < 120:
                    continue
                res = ai_summary(tx, r["title"])
                if res:
                    with _sum_lock:
                        _sums[r["id"]] = {**res, "sig": sig, "at": time.time()}
                        SUM_FILE.write_text(json.dumps(_sums))
                break  # one per pass keeps load light
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- build the session list
def find_transcript(cli_id: str) -> Path | None:
    for p in PROJECTS.glob(f"*/{cli_id}.jsonl"):
        return p
    return None


def load_sessions(scope: str) -> list[dict]:
    org = load_org()
    rows, pr_keys = [], set()
    for mp in META_ROOT.glob("*/*/local_*.json"):
        try:
            m = json.loads(mp.read_text())
        except Exception:
            continue
        cwd = m.get("cwd") or ""
        if scope not in cwd:
            continue
        sid = m["sessionId"]
        tp = find_transcript(m["cliSessionId"]) if m.get("cliSessionId") else None
        tx = analyze_transcript(tp) if tp else None
        o = org.get(sid, {})
        title = m.get("title") or sid
        prs = []
        for p in sorted(tx["prs"] if tx else [], key=lambda p: -p["number"])[:3]:
            k = f"{p['repo']}#{p['number']}"
            if p["repo"]:
                pr_keys.add((p["repo"], p["number"]))
            prs.append({**p, **{kk: v for kk, v in _pr_cache.get(k, {}).items() if kk != "at"}})
        last_pr = max(prs, key=lambda p: p["number"]) if prs else None

        archived = bool(m.get("isArchived"))
        last_activity = max((m.get("lastActivityAt") or 0) / 1000, tx["mtime"] if tx else 0)
        running = bool(tx and tx["running"])
        blockers = list(tx["blockers"]) if tx else []
        live_pr = last_pr if last_pr and last_pr.get("state") == "OPEN" else None
        if live_pr and live_pr.get("failing"):
            blockers.append({"kind": "ci", "text": f"PR #{last_pr['number']} CI failing: {', '.join(last_pr['failing'][:3])}"})
        if live_pr and live_pr.get("merge") == "DIRTY":
            blockers.append({"kind": "conflict", "text": f"PR #{last_pr['number']} has merge conflicts."})
        if live_pr and live_pr.get("review") == "CHANGES_REQUESTED":
            blockers.append({"kind": "review", "text": f"PR #{last_pr['number']} has requested changes."})
        pr_merged = bool(last_pr and last_pr.get("state") == "MERGED")
        pr_open = bool(last_pr and last_pr.get("state") == "OPEN")

        if o.get("done") or archived or pr_merged:
            state = "done"
        elif running:
            state = "running"
        elif blockers:
            state = "attention"
        elif pr_open:
            state = "in_review"
        else:
            state = "idle"
        if state == "running" and tx and tx["awaitingUser"]:
            state = "attention"

        rows.append({
            "id": sid, "cliId": m.get("cliSessionId"), "title": title,
            "cwd": cwd, "branch": (m.get("worktreeBranch") or m.get("branch")),
            "model": m.get("model"), "effort": m.get("effort"), "permissionMode": m.get("permissionMode"),
            "archived": archived, "created": iso(m.get("createdAt")), "lastActivity": last_activity,
            "state": state, "running": running, "blockers": blockers,
            "turnCount": tx["turnCount"] if tx else 0, "toolCalls": tx["toolCalls"] if tx else 0,
            "lastOutcome": tx["lastOutcome"] if tx else "", "lastPrompt": (tx["turns"][-1]["prompt"] if tx and tx["turns"] else ""),
            "prs": prs, "link": f"claude://claude.ai/epitaxy/{sid}",
            **(lambda sm: {"goal": sm["goal"], "now": sm["now"], "summarySource": sm["source"]})(
                _sums.get(sid) or heuristic_summary(tx, title)),
            "workstream": o.get("workstream") or guess_workstream(title),
            "note": o.get("note", ""), "pinned": bool(o.get("pinned")), "hasTranscript": bool(tp),
        })
    if pr_keys:
        threading.Thread(target=refresh_prs, args=(pr_keys,), daemon=True).start()
    rows.sort(key=lambda r: (not r["pinned"], -r["lastActivity"]))
    return rows


def session_detail(sid: str, scope: str) -> dict | None:
    for r in load_sessions(scope):
        if r["id"] == sid:
            tp = find_transcript(r["cliId"]) if r["cliId"] else None
            tx = analyze_transcript(tp) if tp else None
            r = dict(r)
            r["turns"] = list(reversed(tx["turns"][-60:])) if tx else []
            r["jobs"] = [j for j in JOBS.values() if j["session"] == sid][-5:]
            return r
    return None


# --------------------------------------------------------------------------- steering
JOBS: dict[str, dict] = {}


def steer(session: dict, message: str, mode: str) -> dict:
    ok, why = cli_ready()
    if not ok:
        return {"error": why, "cliDown": True}
    if not session.get("cliId"):
        return {"error": "No transcript id for this chat."}
    if session["running"]:
        return {"error": "This chat is mid-turn. Resuming it from here would fork its transcript. "
                         "Wait for it to finish, or paste the message in the app."}
    jid = secrets.token_hex(4)
    job = {"id": jid, "session": session["id"], "message": message, "status": "running",
           "started": time.time(), "output": "", "mode": mode}
    JOBS[jid] = job

    def run():
        try:
            cwd = session["cwd"] if Path(session["cwd"]).exists() else str(HOME)
            p = subprocess.run(
                ["claude", "-p", "--resume", session["cliId"], "--permission-mode", mode, message],
                cwd=cwd, capture_output=True, text=True, timeout=1800)
            job["output"] = (p.stdout or p.stderr or "").strip()[-6000:]
            job["status"] = "done" if p.returncode == 0 else "failed"
        except Exception as e:  # noqa: BLE001
            job["output"], job["status"] = str(e), "failed"
        job["ended"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return {"job": jid}


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    scope = "MOE_Project"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _authorized(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") and self.headers.get("X-Hub-Token") == TOKEN

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            html = (HERE / "index.html").read_text().replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        if path == "/api/health":
            ok, why = cli_ready()
            return self._send(200, {"cliReady": ok, "why": why})
        if path == "/api/sessions":
            return self._send(200, {"sessions": load_sessions(self.scope), "now": time.time()})
        m = re.fullmatch(r"/api/session/(local_[\w-]+)", path)
        if m:
            d = session_detail(m.group(1), self.scope)
            return self._send(200 if d else 404, d or {"error": "not found"})
        m = re.fullmatch(r"/api/job/(\w+)", path)
        if m and m.group(1) in JOBS:
            return self._send(200, JOBS[m.group(1)])
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        m = re.fullmatch(r"/api/session/(local_[\w-]+)/(organize|steer)", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        sid, action = m.groups()
        if action == "organize":
            with _org_lock:
                org = load_org()
                o = org.setdefault(sid, {})
                for k in ("workstream", "note"):
                    if k in body:
                        o[k] = str(body[k])[:500]
                for k in ("pinned", "done"):
                    if k in body:
                        o[k] = bool(body[k])
                save_org(org)
            return self._send(200, {"ok": True})
        msg = str(body.get("message", "")).strip()
        mode = body.get("mode") if body.get("mode") in ("default", "acceptEdits", "plan", "bypassPermissions") else "acceptEdits"
        if not msg:
            return self._send(400, {"error": "empty message"})
        d = session_detail(sid, self.scope)
        if not d:
            return self._send(404, {"error": "not found"})
        res = steer(d, msg, mode)
        self._send(409 if "error" in res else 200, res)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--scope", default="MOE_Project", help="substring a chat's cwd must contain")
    a = ap.parse_args()
    Handler.scope = a.scope
    threading.Thread(target=summarizer_loop, args=(a.scope,), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Wisp Session Hub → http://127.0.0.1:{a.port}  (scope: {a.scope})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
