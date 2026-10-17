#!/usr/bin/env python3
"""Compare behavior ARMS of Wisp on the same prompts: one isolated backend per arm.

An arm is a set of `WISP_EXPERIMENTS` switches (see service/config/experiments.py);
`baseline` is none. For each arm this script starts a fresh, isolated Wisp
backend plus the sandbox server (synthetic Mail/Calendar/Messages — see
sandbox/run.sh for why those ports and that WISP_HOME), drives the eval prompt
set through the real /agent endpoint, tears both down, and moves to the next
arm. Mechanical metrics come out as a Markdown table; answers are paired per
prompt in a JSONL file for blind grading on the eval board.

    .venv/bin/python scripts/eval_arms.py --arms baseline --search --reps 3
    .venv/bin/python scripts/eval_arms.py --arms baseline,<experiment>,baseline --tasks cat:brief,personal --reps 2
    .venv/bin/python scripts/eval_arms.py --report ~/.moe/evals/arms/<run>.jsonl

Bracket an experiment with a second baseline: arm-level latency drifts with
time (measured 2.3 -> 4.3 -> 7.1s p50 across three identical-code arms), so
the per-task pairs are the signal and the merged baseline averages the drift.

SAFETY. Same rules as scripts/eval_core.py: `plan` tasks run under test_mode,
every confirmation is denied, nothing writes or sends. The backend is NEVER the
real one: ports 8765/8000 are refused outright, WISP_HOME is a scratch
directory, and WISP_SANDBOX_HOME marks the process as a sandbox. The live
`roles:` block is copied into the scratch config so the isolated backend keeps
the resident model warm instead of swapping it for the packaged default (a
mistake this repo has made before — see the memory note in moe-safe-live-testing).

Web tools (news, weather, markets) still reach the public internet; file tools
still read the real disk. Both are read-only.

What is NOT measured here: `capture_think` runs in an idle-only background
worker and needs a session plus idle time; it is measured separately by
replaying stored turns through service/memory/capture.py. It is still a valid
arm name (the switch exists), but an /agent run under it measures nothing.

The isolated backend shares the real oMLX engine on :8000, so a run contends
with — and can idle-unload or swap — the live model. Run it when Wisp is idle.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Progress lines are the only sign a 30-minute run is alive; never buffer them.
sys.stdout.reconfigure(line_buffering=True)

import httpx  # noqa: E402

from scripts import eval_core  # noqa: E402
from scripts.eval_ling_tasks import TASKS, Task  # noqa: E402
from service.config.experiments import KNOWN  # noqa: E402

# Not an experiment switch: model-led routing is its own launch flag.
MODEL_LED_ARM = "model_led"

OUT_DIR = Path.home() / ".moe" / "evals" / "arms"
REAL_PORTS = {8765, 8000}
BACKEND_PORT = 8775
SANDBOX_PORT = 8766
STARTUP_S = 90.0
# Let the sandbox's first world push land before the first prompt reads Mail.
SANDBOX_SETTLE_S = 4.0

# Smart Search cases for the `search_think` arm: synthetic documents, questions
# whose answers are in the text, and the substring a correct answer contains.
SEARCH_DOC_A = (
    "Quarterly operations review — Northwind Bakery\n\n"
    "Revenue for the quarter was 412,000, up 9 percent on the prior quarter. The "
    "Oak Street location contributed 61 percent of sales. Flour costs rose 14 "
    "percent after the March supplier change; the team switched from Harvest "
    "Mills to Prairie Grain on March 18. Staff headcount is 23, including four "
    "part-time bakers hired in February. The delivery van (plate 7KLM221) needs "
    "its brake pads replaced before the May inspection. Customer complaints fell "
    "from 31 to 12 after the new ordering app launched on April 2. The board "
    "approved a second oven for Oak Street at a cost of 28,500, to be installed "
    "the week of June 8. Open risk: the lease on the Elm Avenue kitchen expires "
    "on September 30 and the landlord has not confirmed renewal terms."
)
SEARCH_DOC_B = (
    "Lab notebook — enzyme assay, week 14\n\n"
    "Monday: prepared buffer at pH 7.4, 50 mM phosphate. Enzyme stock lot E-2291 "
    "thawed once; aliquoted into 20 tubes of 50 uL. Tuesday: ran the activity "
    "assay at 25 C and 37 C. Activity at 37 C was 2.3 times the 25 C value. The "
    "control without substrate showed no signal. Wednesday: the plate reader "
    "(serial PR-4410) drifted by 6 percent over 40 minutes; recalibrated with "
    "the fluorescein standard. Thursday: repeated Tuesday's assay with lot "
    "E-2291 and lot E-2305. Lot E-2305 gave 18 percent lower activity, so it is "
    "flagged and will not be used for the inhibitor screen. Friday: ordered "
    "fresh substrate (catalog S-7720, 5 g). Next week: inhibitor screen at "
    "37 C using lot E-2291 only, triplicates, with the recalibrated reader."
)
SEARCH_CASES = [
    ("A1", SEARCH_DOC_A, "Which supplier did they switch to and when?", "Prairie Grain"),
    ("A2", SEARCH_DOC_A, "What is the open risk?", "Elm Avenue"),
    ("A3", SEARCH_DOC_A, "How much did complaints fall after the app launched?", "12"),
    ("B1", SEARCH_DOC_B, "Which enzyme lot is flagged and why?", "E-2305"),
    ("B2", SEARCH_DOC_B, "What temperature will the inhibitor screen use?", "37"),
    ("B3", SEARCH_DOC_B, "What went wrong with the plate reader?", "drift"),
]

_LEAK_RE = eval_core._LEAK_RE


@dataclass
class Rec:
    run: str
    arm: str
    kind: str                 # agent | search
    task: str
    rep: int
    prompt: str
    answer: str = ""
    reasoning_chars: int = 0
    tools: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    route_reason: str = ""
    error: str = ""
    elapsed_s: float = 0.0
    first_token_s: float | None = None
    # derived
    answered: bool = False
    leak: bool = False
    tool_ok: bool | None = None
    answer_chars: int = 0
    correct: bool | None = None


# ---------------------------------------------------------------- environment
def _refuse_real_ports(*ports: int) -> None:
    for p in ports:
        if p in REAL_PORTS:
            sys.exit(f"refusing port {p}: it belongs to the real Wisp (use 8775 / 8766)")


def _port_busy(port: int) -> bool:
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def live_roles() -> dict:
    """The resident roster, copied so the isolated backend keeps the same model
    warm. Falls back to a line parse if PyYAML is unavailable."""
    cfg = Path.home() / ".moe" / "config.yaml"
    if not cfg.exists():
        return {}
    text = cfg.read_text()
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        roles = data.get("roles") or {}
        return dict(roles) if isinstance(roles, dict) else {}
    except ImportError:
        roles: dict = {}
        in_block = False
        for line in text.splitlines():
            if re.match(r"^roles:\s*$", line):
                in_block = True
                continue
            if in_block:
                m = re.match(r"^\s+([A-Za-z_]+):\s*(\S+)\s*$", line)
                if m:
                    roles[m.group(1)] = m.group(2).strip("'\"")
                elif line.strip() and not line.startswith(" "):
                    break
        return roles


class Arm:
    """One isolated backend + sandbox server, for one arm."""

    def __init__(self, name: str, scratch: Path, port: int, sandbox_port: int,
                 python: str):
        self.name = name
        self.home = scratch / name
        self.port = port
        self.sandbox_port = sandbox_port
        self.base = f"http://127.0.0.1:{port}"
        self.python = python
        self.backend: subprocess.Popen | None = None
        self.sandbox: subprocess.Popen | None = None

    def experiments(self) -> str:
        return "" if self.name in ("baseline", MODEL_LED_ARM) else self.name

    def start(self) -> None:
        if self.home.exists():
            shutil.rmtree(self.home)
        wisp_home = self.home / "moe"
        sandbox_home = self.home / "sandbox"
        wisp_home.mkdir(parents=True)
        sandbox_home.mkdir(parents=True)
        roles = live_roles()
        if roles:
            lines = ["roles:"] + [f"  {k}: {v}" for k, v in roles.items()]
            (wisp_home / "config.yaml").write_text("\n".join(lines) + "\n")
        env = dict(os.environ)
        env.update({
            "WISP_HOME": str(wisp_home),
            "WISP_SANDBOX_HOME": str(sandbox_home),
            "WISP_BACKEND_URL": self.base,
            "WISP_EXPERIMENTS": self.experiments(),
            "PYTHONUNBUFFERED": "1",
        })
        env.pop("WISP_MODEL_LED_ROUTING", None)
        if self.name == MODEL_LED_ARM:
            env["WISP_MODEL_LED_ROUTING"] = "1"
        self.backend = subprocess.Popen(
            [self.python, "-m", "uvicorn", "service.main:app", "--port", str(self.port)],
            cwd=ROOT, env=env, stdout=(self.home / "backend.log").open("w"),
            stderr=subprocess.STDOUT)
        self._wait(f"{self.base}/mode", "backend")
        self.sandbox = subprocess.Popen(
            [self.python, "-m", "uvicorn", "sandbox.server:app", "--port", str(self.sandbox_port)],
            cwd=ROOT, env=env, stdout=(self.home / "sandbox.log").open("w"),
            stderr=subprocess.STDOUT)
        self._wait(f"http://127.0.0.1:{self.sandbox_port}/", "sandbox server")
        time.sleep(SANDBOX_SETTLE_S)

    def _wait(self, url: str, what: str) -> None:
        deadline = time.monotonic() + STARTUP_S
        while time.monotonic() < deadline:
            proc = self.backend if what == "backend" else self.sandbox
            if proc is not None and proc.poll() is not None:
                break
            try:
                if httpx.get(url, timeout=2).status_code < 500:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        self.stop()
        sys.exit(f"[{self.name}] {what} never came up — see {self.home}/{what.split()[0]}.log")

    def stop(self) -> None:
        for proc in (self.sandbox, self.backend):
            if proc is None or proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.sandbox = self.backend = None


# ---------------------------------------------------------------- running
def _tool_ok(task: Task, tools: list[str]) -> bool | None:
    if task.mode == "plan" and not tools:
        return None   # plan mode with no selection: route reported it, nothing to judge
    if not task.expect:
        return not tools
    return any(t in task.expect for t in tools)


async def run_agent_task(client: httpx.AsyncClient, arm: Arm, task: Task, rep: int,
                         run: str) -> list[Rec]:
    out: list[Rec] = []
    turn = await eval_core.run_turn(client, task, task.prompt, base=arm.base)
    out.append(_rec_from_turn(run, arm, task, rep, task.prompt, turn))
    if task.followup:
        follow = await eval_core.run_turn(client, task, task.followup,
                                          session_id=turn.session_id, base=arm.base)
        r = _rec_from_turn(run, arm, task, rep, task.followup, follow)
        r.task = f"{task.id}+followup"
        out.append(r)
    return out


def _rec_from_turn(run: str, arm: Arm, task: Task, rep: int, prompt: str,
                   turn: eval_core.Turn) -> Rec:
    tools = [t["name"] for t in turn.tools]
    answer = turn.answer.strip()
    return Rec(run=run, arm=arm.name, kind="agent", task=task.id, rep=rep, prompt=prompt,
               answer=answer, reasoning_chars=len(turn.reasoning or ""), tools=tools,
               denied=list(turn.denied), route_reason=str(turn.route.get("reason", ""))[:160],
               error=turn.error, elapsed_s=turn.elapsed_s, first_token_s=turn.first_token_s,
               answered=bool(answer) and not turn.error,
               leak=bool(_LEAK_RE.match(answer)), tool_ok=_tool_ok(task, tools),
               answer_chars=len(answer))


async def run_search_case(client: httpx.AsyncClient, arm: Arm, case, rep: int,
                          run: str) -> Rec:
    cid, text, query, must = case
    started = time.monotonic()
    answer, error, first = "", "", None
    try:
        async with client.stream("POST", f"{arm.base}/search", timeout=eval_core.TURN_TIMEOUT,
                                 json={"text": text, "query": query, "want_answer": True,
                                       "force_answer": True}) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                kind = ev.get("event") or ev.get("type")
                # search/engine.py: `answer` carries synth's result keys
                # (text, model, citations, scope); the two failure shapes
                # are distinct events, not an error field on `answer`.
                if kind == "answer":
                    if first is None:
                        first = time.monotonic() - started
                    answer = str(ev.get("text") or "")
                elif kind == "answer_not_found":
                    error = "not_found"
                elif kind == "answer_error":
                    error = str(ev.get("reason") or "answer_error")
                elif kind == "error":
                    error = str(ev.get("message") or ev.get("error") or "error")
                elif kind == "done":
                    break
    except Exception as exc:                                      # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    answer = answer.strip()
    return Rec(run=run, arm=arm.name, kind="search", task=f"search:{cid}", rep=rep,
               prompt=query, answer=answer, error=error,
               elapsed_s=round(time.monotonic() - started, 1),
               first_token_s=round(first, 1) if first is not None else None,
               answered=bool(answer) and not error, leak=bool(_LEAK_RE.match(answer)),
               answer_chars=len(answer),
               correct=(must.lower() in answer.lower()) if answer else False)


def select_tasks(spec: str) -> list[Task]:
    if spec == "all":
        return list(TASKS)
    if spec == "compound":
        return [t for t in TASKS if len(t.expect) >= 2]
    if spec == "quick":
        wanted = {"news_top", "weather_local", "mail_triage", "msgs_attribution",
                  "agentic_compound", "agentic_freetime", "convert_units",
                  "crypto_boundary"}
        picked = [t for t in TASKS if t.id in wanted]
        return picked or list(TASKS)[:8]
    if spec.startswith("ids:"):
        ids = {s.strip() for s in spec[4:].split(",") if s.strip()}
        missing = ids - {t.id for t in TASKS}
        if missing:
            sys.exit(f"unknown task ids: {sorted(missing)}")
        return [t for t in TASKS if t.id in ids]
    if spec.startswith("cat:"):
        cats = {s.strip() for s in spec[4:].split(",") if s.strip()}
        return [t for t in TASKS if t.category in cats]
    sys.exit(f"unknown --tasks {spec!r}")


async def run_arm(arm: Arm, tasks: list[Task], reps: int, run: str, log: Path,
                  search: bool) -> list[Rec]:
    recs: list[Rec] = []
    arm.start()
    try:
        async with httpx.AsyncClient() as client:
            # Warm-up: the first turn on a fresh backend pays for the engine
            # handshake, tool registry, and prompt-cache priming — the smoke
            # run measured 7.9s vs 3.0s for the same prompt across arms. Not
            # recorded; it is the backend warming, not the arm.
            warm = Task("warmup", "warmup", "What time is it?", (), mode="live")
            await eval_core.run_turn(client, warm, warm.prompt, base=arm.base)
            for rep in range(1, reps + 1):
                for task in tasks:
                    for r in await run_agent_task(client, arm, task, rep, run):
                        recs.append(r)
                        _append(log, r)
                        print(f"  [{arm.name}] {r.task:<28} {r.elapsed_s:6.1f}s "
                              f"{'ok ' if r.answered else 'ERR'} tools={r.tools}"
                              f"{' LEAK' if r.leak else ''}"
                              f"{' tool-miss' if r.tool_ok is False else ''}")
                if search:
                    for case in SEARCH_CASES:
                        r = await run_search_case(client, arm, case, rep, run)
                        recs.append(r)
                        _append(log, r)
                        print(f"  [{arm.name}] {r.task:<28} {r.elapsed_s:6.1f}s "
                              f"{'ok ' if r.answered else 'ERR'} "
                              f"{'correct' if r.correct else 'wrong'}")
    finally:
        arm.stop()
    return recs


def _append(path: Path, rec: Rec) -> None:
    with path.open("a") as f:
        f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- report
def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else float("nan")


def _p90(xs):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))] if xs else float("nan")


def report(recs: list[Rec]) -> str:
    arms = sorted({r.arm for r in recs}, key=lambda a: (a != "baseline", a))
    lines = ["| metric | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]

    def row(label, fn, fmt="{:.2f}"):
        cells = []
        for a in arms:
            v = fn([r for r in recs if r.arm == a])
            cells.append("—" if v is None or v != v else fmt.format(v))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    def rate(rs, pred, kind=None):
        rs = [r for r in rs if kind is None or r.kind == kind]
        rs = [r for r in rs if pred(r) is not None]
        return (sum(1 for r in rs if pred(r)) / len(rs)) if rs else None

    ag = "agent"
    row("agent turns", lambda rs: float(sum(1 for r in rs if r.kind == ag)), "{:.0f}")
    row("answered (non-empty, no error)", lambda rs: rate(rs, lambda r: r.answered, ag), "{:.0%}")
    row("expected tool used", lambda rs: rate(rs, lambda r: r.tool_ok, ag), "{:.0%}")
    row("reasoning leaked into answer", lambda rs: rate(rs, lambda r: r.leak, ag), "{:.0%}")
    row("errors", lambda rs: rate(rs, lambda r: bool(r.error), ag), "{:.0%}")
    row("elapsed p50 (s)", lambda rs: _med([r.elapsed_s for r in rs if r.kind == ag]), "{:.1f}")
    row("elapsed p90 (s)", lambda rs: _p90([r.elapsed_s for r in rs if r.kind == ag]), "{:.1f}")
    row("first token p50 (s)", lambda rs: _med([r.first_token_s for r in rs if r.kind == ag]), "{:.1f}")
    row("answer chars p50", lambda rs: _med([r.answer_chars for r in rs if r.kind == ag]), "{:.0f}")
    row("reasoning chars p50", lambda rs: _med([r.reasoning_chars for r in rs if r.kind == ag]), "{:.0f}")
    if any(r.kind == "search" for r in recs):
        se = "search"
        row("search cases", lambda rs: float(sum(1 for r in rs if r.kind == se)), "{:.0f}")
        row("search answered", lambda rs: rate(rs, lambda r: r.answered, se), "{:.0%}")
        row("search correct (substring)", lambda rs: rate(rs, lambda r: r.correct, se), "{:.0%}")
        row("search elapsed p50 (s)", lambda rs: _med([r.elapsed_s for r in rs if r.kind == se]), "{:.1f}")
        row("search answer chars p50", lambda rs: _med([r.answer_chars for r in rs if r.kind == se]), "{:.0f}")
    # Per-task elapsed side by side, so a regression on one prompt is visible.
    lines += ["", "| task | " + " | ".join(f"{a} s" for a in arms) + " | " +
              " | ".join(f"{a} chars" for a in arms) + " |",
              "|---|" + "---|" * (2 * len(arms))]
    for task in sorted({r.task for r in recs}):
        cells_s, cells_c = [], []
        for a in arms:
            rs = [r for r in recs if r.arm == a and r.task == task]
            cells_s.append(f"{_med([r.elapsed_s for r in rs]):.1f}" if rs else "—")
            cells_c.append(f"{_med([r.answer_chars for r in rs]):.0f}" if rs else "—")
        lines.append(f"| {task} | " + " | ".join(cells_s) + " | " + " | ".join(cells_c) + " |")
    return "\n".join(lines)


def pairs(recs: list[Rec], path: Path) -> int:
    """Blind pairs for human grading: same task and rep, baseline vs each arm,
    sides shuffled per pair so the grader can't learn which is which."""
    import random
    rng = random.Random(7)
    base = {(r.task, r.rep): r for r in recs if r.arm == "baseline"}
    n = 0
    with path.open("w") as f:
        for r in recs:
            if r.arm == "baseline":
                continue
            b = base.get((r.task, r.rep))
            if not b:
                continue
            left, right = (b, r) if rng.random() < 0.5 else (r, b)
            f.write(json.dumps({"task": r.task, "rep": r.rep, "prompt": r.prompt,
                                "left": {"arm": left.arm, "answer": left.answer},
                                "right": {"arm": right.arm, "answer": right.answer}},
                               ensure_ascii=False) + "\n")
            n += 1
    return n


def load(path: Path) -> list[Rec]:
    recs = []
    for line in path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            recs.append(Rec(**{k: v for k, v in d.items() if k in Rec.__dataclass_fields__}))
    return recs


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", default="baseline",
                    help="comma list; 'baseline' or experiment names from service/config/experiments.py")
    ap.add_argument("--tasks", default="quick",
                    help="all | quick | compound | ids:a,b | cat:news,weather")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--prompts-file", default=None,
                    help="JSON list of prompts to replay in plan mode (tool calls intercepted, nothing "
                         "executes); replaces --tasks. Used by scripts/audit_rule_precision.py --emit-pins")
    ap.add_argument("--search", action="store_true",
                    help="also run the built-in Smart Search cases")
    ap.add_argument("--port", type=int, default=BACKEND_PORT)
    ap.add_argument("--sandbox-port", type=int, default=SANDBOX_PORT)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--scratch", default=None, help="where arm homes go (default: a temp dir)")
    ap.add_argument("--report", default=None, help="re-print the report for an existing run JSONL")
    args = ap.parse_args()

    if args.report:
        recs = load(Path(args.report))
        print(report(recs))
        return

    _refuse_real_ports(args.port, args.sandbox_port)
    for p in (args.port, args.sandbox_port):
        if _port_busy(p):
            sys.exit(f"port {p} is in use — stop that process or pick another")
    try:
        httpx.get("http://127.0.0.1:8000/v1/models", timeout=3)
    except httpx.HTTPError:
        sys.exit("oMLX is not answering on :8000; start it before measuring")

    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arm_names if a not in ("baseline", MODEL_LED_ARM) and a not in KNOWN]
    if bad:
        sys.exit(f"unknown arms {bad}; known experiments: {sorted(KNOWN)}")
    if args.prompts_file:
        prompts = json.loads(Path(args.prompts_file).expanduser().read_text())
        tasks = [Task(f"p{i:03d}", "audit", str(text), (), mode="plan")
                 for i, text in enumerate(prompts, 1)]
    else:
        tasks = select_tasks(args.tasks)
    search = args.search

    run = time.strftime("%Y%m%d-%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = OUT_DIR / f"{run}.jsonl"
    scratch = Path(args.scratch) if args.scratch else Path(
        os.environ.get("TMPDIR", "/tmp")) / f"wisp-arms-{run}"
    # Same refusal as sandbox/run.sh: the arm homes are deleted and recreated,
    # so they must never resolve inside the real ~/.moe.
    real_home = (Path.home() / ".moe").resolve()
    if scratch.resolve() == real_home or real_home in scratch.resolve().parents:
        sys.exit(f"refusing --scratch {scratch}: it is inside the real ~/.moe")
    print(f"run {run}: arms={arm_names} tasks={len(tasks)} reps={args.reps} "
          f"search={search}\nlog {log}\nscratch {scratch}")

    recs: list[Rec] = []
    for name in arm_names:
        print(f"\n== arm {name} ==")
        arm = Arm(name, scratch, args.port, args.sandbox_port, args.python)
        recs += asyncio.run(run_arm(arm, tasks, args.reps, run, log, search))

    md = report(recs)
    (OUT_DIR / f"{run}.md").write_text(md + "\n")
    n = pairs(recs, OUT_DIR / f"{run}.pairs.jsonl")
    print("\n" + md)
    print(f"\n{n} blind pairs -> {OUT_DIR / f'{run}.pairs.jsonl'}")


if __name__ == "__main__":
    main()
