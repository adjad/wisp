"""Build an mlx-lm LoRA dataset from real Wisp usage.

Turns sessions.db (user/assistant text) + audit.jsonl (real tool calls with
args) into mlx-lm's chat-format JSONL: {"messages": [...]} per line, with
tool_calls attached to assistant turns where the audit log shows a tool ran
in that window. This is real router/agent behavior on this user's actual
traffic, not synthetic data.

Usage:
    python scripts/build_lora_dataset.py --out ~/lora_data --val-frac 0.1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

from service.agent.loop import SYSTEM
from service.router.router import route
from service.tools import tool_schemas

SESSIONS_DB = Path.home() / ".moe" / "sessions.db"
AUDIT_LOG = Path.home() / ".moe" / "audit.jsonl"

MIN_ASSISTANT_CHARS = 8   # drop near-empty / truncated turns
MAX_MESSAGE_CHARS = 4000  # drop runaway turns (bad for a 2.6B context budget)


def load_audit_events() -> list[dict]:
    events = []
    if not AUDIT_LOG.exists():
        return events
    with AUDIT_LOG.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") == "allow" and "tool" in ev:
                events.append(ev)
    return events


async def build_examples(conn: sqlite3.Connection, audit: list[dict]) -> list[dict]:
    cur = conn.execute(
        "SELECT session_id, idx, role, content, tool_digest, created_at "
        "FROM turns ORDER BY session_id, idx"
    )
    rows = cur.fetchall()

    by_session: dict[str, list[tuple]] = {}
    for row in rows:
        by_session.setdefault(row[0], []).append(row)

    examples = []
    for sid, turns in by_session.items():
        messages = [{"role": "system", "content": SYSTEM}]
        last_user_text = None
        for i, (_, idx, role, content, tool_digest, created_at) in enumerate(turns):
            if not content or len(content) > MAX_MESSAGE_CHARS:
                continue
            if role == "assistant" and len(content) < MIN_ASSISTANT_CHARS:
                continue

            msg = {"role": role, "content": content}

            if role == "user":
                last_user_text = content

            if role == "assistant" and tool_digest:
                # window = (previous turn's ts, this turn's ts] within the same session
                prev_ts = turns[i - 1][5] if i > 0 else 0
                calls = [
                    ev for ev in audit
                    if prev_ts < _ts(ev["ts"]) <= created_at + 1
                ]
                if calls:
                    msg["tool_calls"] = [
                        {
                            "type": "function",
                            "function": {"name": ev["tool"], "arguments": ev.get("args", {})},
                        }
                        for ev in calls[:4]  # cap noise from unrelated concurrent calls
                    ]

            messages.append(msg)

            # emit one training example per completed assistant turn, using
            # everything up to and including it as the target
            if role == "assistant":
                # Re-run the SAME routing logic that decided the tool subset at
                # SERVE time, on the user text that triggered this turn — the
                # audit log records which tool ran, never what subset it was
                # offered from, so reconstructing it any other way would be a
                # guess. Without this, the model trains on a `tools`-less
                # prompt shape it will never actually see at inference,
                # exactly the mismatch harvest_training_data.py's raw_model_io
                # capture avoids. See moe-context-window-budget /
                # moe-tool-subset-routing.
                tools = None
                if last_user_text:
                    decision = await route(last_user_text)
                    if decision.tool_subset:
                        tools = tool_schemas(decision.tool_subset)
                examples.append({"messages": [m.copy() for m in messages], "tools": tools})

    return examples


def _ts(iso: str) -> float:
    import datetime
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="~/lora_data")
    ap.add_argument("--val-frac", type=float, default=0.1)
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(SESSIONS_DB)
    audit = load_audit_events()
    examples = await build_examples(conn, audit)

    n_val = max(1, int(len(examples) * args.val_frac))
    val, train = examples[:n_val], examples[n_val:]

    (out_dir / "train.jsonl").write_text(
        "\n".join(json.dumps(e) for e in train) + "\n"
    )
    (out_dir / "valid.jsonl").write_text(
        "\n".join(json.dumps(e) for e in val) + "\n"
    )
    # mlx_lm.lora requires a test.jsonl to exist even if --test isn't passed
    (out_dir / "test.jsonl").write_text(
        "\n".join(json.dumps(e) for e in val[: max(1, n_val // 2)]) + "\n"
    )

    with_tools = sum(1 for e in examples if any("tool_calls" in m for m in e["messages"]))
    with_subset = sum(1 for e in examples if e.get("tools"))
    print(f"{len(examples)} examples ({with_tools} with real tool_calls, "
          f"{with_subset} with a routed tool subset) "
          f"-> {len(train)} train / {len(val)} valid, written to {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
