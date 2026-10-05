#!/usr/bin/env python3
"""Compare routing arms on the frozen held-out set (test_fixtures/routing/comparison_heldout.json).

  A  current deterministic router (route() exactly as main.py calls it).
  B  Ling-led intent classification — needs the live engine; this script never
     contacts it. Record B as blocked unless it is run separately under the
     coordinator's idle-engine conditions.
  C  Laya shortlist: Laya (local Core ML, cached snapshot only, offline) picks
     the intent among the fixed labels and the first tool among arm A's
     reachable menu (<= 31 tools + "none"), within Laya's 1,024-token and
     32-option limits.

Reports intent accuracy and first-tool accuracy per arm and category, plus
latency (cold/warm p50/p95) and process memory. End-to-end answer quality
needs a generation model and is out of scope for this offline script.

Usage:
  python scripts/compare_routing_arms.py --arm A --json-out a.json
  HF_HUB_OFFLINE=1 python scripts/compare_routing_arms.py --arm C --json-out c.json \
      [--limit N] [--max-swap-rise-mb 512]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import resource
import socket
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
HELDOUT = ROOT / "test_fixtures/routing/comparison_heldout.json"
LAYA_MODEL_ID = "aac6fef/laya-multilingual-coreml"
LAYA_REVISION = "8139e9089273319512c730218903784074133187"
_PINNED = {"recall", "run_shell"}

TOOL_INTENT = {
    **dict.fromkeys(["get_upcoming", "get_past_events", "find_free_time", "daily_brief"], "calendar_read"),
    **dict.fromkeys(["add_calendar_event", "cancel_event", "update_event"], "calendar_write"),
    **dict.fromkeys(["add_reminder", "update_reminder"], "reminder_create"),
    **dict.fromkeys(["search_reminders", "complete_reminder"], "reminder_read"),
    **dict.fromkeys(["view_emails", "summarize_emails", "triage_inbox", "summarize_thread",
                     "scan_subscriptions"], "mail_read"),
    **dict.fromkeys(["send_email", "reply_to_email", "forward_email"], "mail_send"),
    "draft_email": "mail_draft",
    **dict.fromkeys(["view_messages", "summarize_messages"], "messages_read"),
    "send_message": "messages_send", "draft_message": "messages_draft",
    "search_notes": "notes_read",
    **dict.fromkeys(["create_note", "append_note"], "notes_write"),
    **dict.fromkeys(["find_files", "list_dir", "read_file", "reveal_in_finder"], "files"),
    **dict.fromkeys(["web_search", "web_fetch", "get_stock_price", "get_sports_scores", "find_place",
                     "travel_time", "get_directions", "world_time", "recipe_lookup",
                     "wikipedia_summary"], "web_public"),
    **dict.fromkeys(["get_weather", "rain_radar", "weather_alerts"], "weather"),
    **dict.fromkeys(["recall", "search_conversations"], "memory_recall"),
    "remember": "memory_save",
}


def _device_intent(name: str) -> str | None:
    from service.tools.registry import REGISTRY
    tool = REGISTRY.get(name)
    if tool and tool.category in {"system_read", "system_write", "app_control", "timer_write"}:
        return "device"
    return None


def tool_intent(name: str | None) -> str | None:
    if not name:
        return None
    return TOOL_INTENT.get(name) or _device_intent(name)


def reachable(decision, registry) -> list[str]:
    if not decision.needs_tools:
        tools: set[str] = set()
    elif decision.tool_subset is None:
        tools = set(registry)
    else:
        tools = set(decision.tool_subset)
    tools |= {n for n, _ in decision.direct_calls}
    tools |= {n for g in decision.required_tool_groups for n in g}
    if decision.force_first_tool:
        tools.add(decision.force_first_tool)
    return sorted(tools - set(decision.forbidden_tools))


def arm_a_prediction(decision, menu: list[str]) -> tuple[str, str | None]:
    """(intent, first tool) the deterministic router commits to."""
    first = decision.force_first_tool
    if not first and decision.direct_calls:
        first = decision.direct_calls[0][0]
    if not first:
        singles = [next(iter(g)) for g in decision.required_tool_groups if len(g) == 1]
        first = singles[0] if singles else None
    if first and first in decision.forbidden_tools:
        first = None
    if first:
        return tool_intent(first) or "device", first
    if not menu:
        return ("clarify" if "clarif" in (decision.reason or "").lower() else "chat"), None
    votes = Counter(i for t in menu if t not in _PINNED for i in [tool_intent(t)] if i)
    if not votes:
        return "chat", None
    (top, n), *rest = votes.most_common()
    if rest and rest[0][1] == n:
        return "ambiguous", None
    return top, None


def _swap_used_mb() -> float:
    out = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    used = out.split("used =")[1].split("M")[0]
    return float(used)


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


async def run(arm: str, cases: list[dict], args) -> dict:
    import service.router.router as R
    import service.tools  # noqa: F401
    from service.router.pinning import apply_session_pin
    from service.tools.registry import REGISTRY
    from tests.stub_embedder import install

    install()
    intents = json.loads(HELDOUT.read_text())["intents"]
    agent = None
    memory = {"swap_before_mb": _swap_used_mb(), "rss_before_mb": round(_rss_mb(), 1)}
    if arm == "C":
        os.environ["HF_HUB_OFFLINE"] = "1"
        import laya_coreml as laya
        t0 = time.perf_counter()
        agent = laya.load(LAYA_MODEL_ID, revision=LAYA_REVISION, local_files_only=True,
                          compute_units=args.compute_units)
        memory["load_seconds"] = round(time.perf_counter() - t0, 2)
        memory["rss_after_load_mb"] = round(_rss_mb(), 1)
        memory["swap_after_load_mb"] = _swap_used_mb()
    rows = []
    for case in cases:
        t0 = time.perf_counter()
        decision = await R.route(case["prompt"], last_user=case["last_user"],
                                 recent_users=[case["last_user"]] if case["last_user"] else None,
                                 last_assistant=case["last_assistant"], last_tools=case["last_tools"])
        decision = apply_session_pin(decision, None, case["prompt"])
        menu = reachable(decision, REGISTRY)
        route_ms = (time.perf_counter() - t0) * 1000
        intent, first = arm_a_prediction(decision, menu)
        extra = {}
        if arm == "C":
            state = {"request": case["prompt"]}
            if case["last_user"]:
                state["previous_user_turn"] = case["last_user"]
            if case["last_assistant"]:
                state["previous_assistant_turn"] = case["last_assistant"]
            options = [t for t in menu if t not in _PINNED][:31]
            tool_options = {t: (REGISTRY[t].description or "")[:90] for t in options}
            tool_options["none"] = "No tool: answer directly or ask a clarifying question"
            questions = {
                "intent": {"type": "choice", "instructions": "What does the user want right now?",
                           "criteria": intents},
                "tool": {"type": "choice", "instructions": "Which tool should be called first?",
                         "criteria": tool_options},
            }
            t1 = time.perf_counter()
            try:
                result = agent.predict(state, questions)
                answers = result["answers"]
                intent = answers["intent"]["choice"]
                first = None if answers["tool"]["choice"] == "none" else answers["tool"]["choice"]
                extra = {"intent_conf": answers["intent"]["confidence"],
                         "tool_conf": answers["tool"]["confidence"],
                         "act_probability": answers["intent"]["action"]["act_probability"]}
            except Exception as exc:  # noqa: BLE001 — a failed call is recorded, not hidden
                intent, first, extra = "error", None, {"error": f"{type(exc).__name__}: {exc}"[:160]}
            extra["laya_ms"] = round((time.perf_counter() - t1) * 1000, 1)
            swap = _swap_used_mb()
            if swap - memory["swap_before_mb"] > args.max_swap_rise_mb:
                memory["aborted"] = f"swap rose {swap - memory['swap_before_mb']:.0f} MB"
                break
        gold_first = set(case["first"])
        first_ok = (first is None) if not gold_first else (first in gold_first)
        rows.append(dict(id=case["id"], category=case["category"], prompt=case["prompt"],
                         gold_intent=case["intent"], intent=intent, intent_ok=intent == case["intent"],
                         gold_first=sorted(gold_first), first=first, first_ok=first_ok,
                         menu=len(menu), route_ms=round(route_ms, 2), **extra))
    memory["rss_end_mb"] = round(_rss_mb(), 1)
    memory["swap_end_mb"] = _swap_used_mb()
    return {"arm": arm, "rows": rows, "memory": memory}


def summarize(result: dict) -> dict:
    rows = result["rows"]
    by_cat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    def acc(rs, key):
        return round(sum(r[key] for r in rs) / len(rs), 3) if rs else None
    lat_key = "laya_ms" if result["arm"] == "C" else "route_ms"
    lats = [r[lat_key] for r in rows if lat_key in r]
    warm = sorted(lats[1:]) or sorted(lats)
    return {
        "n": len(rows), "intent_acc": acc(rows, "intent_ok"), "first_tool_acc": acc(rows, "first_ok"),
        "by_category": {c: {"n": len(rs), "intent_acc": acc(rs, "intent_ok"), "first_tool_acc": acc(rs, "first_ok")}
                        for c, rs in sorted(by_cat.items())},
        "latency_ms": {"cold": lats[0] if lats else None,
                       "warm_p50": warm[len(warm) // 2] if warm else None,
                       "warm_p95": warm[int(len(warm) * 0.95)] if warm else None},
        "errors": sum(1 for r in rows if r["intent"] == "error"),
        "memory": result["memory"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["A", "C"], required=True)
    ap.add_argument("--json-out", type=Path)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-swap-rise-mb", type=float, default=512)
    ap.add_argument("--compute-units", default="cpu_gpu")
    args = ap.parse_args()
    raw = HELDOUT.read_bytes()
    expected = (HELDOUT.with_suffix(".sha256")).read_text().split()[0]
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit("held-out set changed after it was frozen")
    if args.arm == "A":
        socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network"))
    cases = json.loads(raw)["cases"][: args.limit]
    result = asyncio.run(run(args.arm, cases, args))
    summary = summarize(result)
    print(json.dumps(summary, indent=1))
    if args.json_out:
        args.json_out.write_text(json.dumps({"summary": summary, **result}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
