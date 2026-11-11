#!/usr/bin/env python3
"""Pull every r5 row whose target makes a write (an action with an effect) into compact review records.

    python extract_writes.py --build ../../ask-lora-r5/data/r5-final --out OUT --chunks 8

One record per row: id, family, split, the user's messages in this turn and the conversation before it (trimmed), the
calls and tool results already made in this turn, the target call(s), and the builder's own notes, cards and approvals.
Writes OUT/writes.jsonl and OUT/chunk-<k>.jsonl. Read-only on the build.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# Tools whose call changes something (messages, mail, calendar, reminders, notes, contacts, files, settings, memory,
# outbound fetches). Drafts count: they create an object the user then sees.
WRITE_TOOLS = {
    "send_message", "send_email", "reply_to_email", "schedule_send", "draft_email", "draft_message",
    "add_reminder", "update_reminder", "complete_reminder", "clear_reminders", "clear_past_reminders",
    "add_calendar_event", "update_event", "cancel_event", "create_note", "append_note", "manage_contacts",
    "organize_files", "move_path", "toggle_setting", "mac_set_volume", "mac_mute", "manage_timers", "set_timer",
    "remember", "forget", "web_fetch",
}


def trim(s, n=700):
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"…[+{len(s) - n}]"


def record(row, meta, split):
    msgs = row["messages"]
    target = msgs[-1]
    calls = [{"name": tc["function"]["name"], "args": tc["function"]["arguments"]} for tc in target.get("tool_calls") or []]
    # the turn = everything after the last user message before the target
    last_user = max(i for i, m in enumerate(msgs[:-1]) if m["role"] == "user")
    history = []
    for m in msgs[1:last_user]:
        if m["role"] == "user":
            history.append({"user": trim(m["content"], 300)})
        elif m["role"] == "assistant" and m.get("content"):
            history.append({"assistant": trim(m["content"], 300)})
    turn = []
    for m in msgs[last_user + 1:-1]:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                turn.append({"called": tc["function"]["name"], "args": tc["function"]["arguments"]})
        elif m["role"] == "tool":
            turn.append({"result": trim(m["content"], 500)})
    return {"id": meta["id"], "family": meta["family"], "split": split, "notes": meta.get("notes"),
            "history": history[-6:], "user": msgs[last_user]["content"], "turn_so_far": turn, "target": calls,
            "expect_cards": meta.get("expect_cards"), "approve": meta.get("approve"), "writes": meta.get("writes"),
            "clock": meta.get("clock")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chunks", type=int, default=8)
    a = ap.parse_args()
    b, out = Path(a.build), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    recs = []
    for split in ("train", "valid"):
        rows = (json.loads(l) for l in (b / f"{split}.jsonl").open())
        metas = (json.loads(l) for l in (b / f"{split}.meta.jsonl").open())
        for row, meta in zip(rows, metas):
            names = {tc["function"]["name"] for tc in row["messages"][-1].get("tool_calls") or []}
            if names & WRITE_TOOLS:
                recs.append(record(row, meta, split))
    recs.sort(key=lambda r: (r["family"], r["id"]))
    with (out / "writes.jsonl").open("w") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    # chunks keep a family's rows together where possible
    k = a.chunks
    size = -(-len(recs) // k)
    for i in range(k):
        with (out / f"chunk-{i}.jsonl").open("w") as fh:
            for r in recs[i * size:(i + 1) * size]:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(len(recs), "write-target rows;", k, "chunks of", size)


if __name__ == "__main__":
    main()
