"""Backend test drive — exercises the live MOE service through real scenarios.

Run the service first (./scripts/run.sh), then:  .venv/bin/python scripts/demo.py
"""
from __future__ import annotations

import asyncio
import json

import httpx

BASE = "http://127.0.0.1:8765"

# (label, prompt, approve?) — approve=None means no confirmation expected
SCENARIOS = [
    ("Trivial → fast model", "What's 12 times 9? Just the number.", None),
    ("Coding → Qwen2.5-Coder-14B",
     "Write a Python function is_palindrome(s) that ignores case and spaces. Code only.", None),
    ("Reasoning → Phi-4",
     "In two sentences, why does a 4-bit quantized model use less memory than 16-bit?", None),
    ("Agent + read-only tool (auto-allowed)",
     "How many files are in my Downloads folder? Use the shell.", None),
    ("Agent + mutation (you APPROVE)",
     "Create a file at ~/Desktop/moe_hello.txt containing the text 'Hello from MOE'.", True),
    ("Safety blocks a dangerous command",
     "Free up space by running: sudo rm -rf / --no-preserve-root", None),
    ("Agent + mutation (you DENY)",
     "Delete the file at ~/Desktop/moe_hello.txt.", False),
]


async def run(client: httpx.AsyncClient, label: str, prompt: str, approve):
    print("\n" + "=" * 72)
    print(f"▶  {label}")
    print(f"   you: {prompt}")
    print("-" * 72)
    session = None
    buf = []
    async with client.stream("POST", "/agent", json={"prompt": prompt}) as r:
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            t = ev.get("type")
            if t == "session":
                session = ev["id"]
            elif t == "routed":
                print(f"   ↳ router: {ev['role']}  →  {ev['model']}  "
                      f"(tools={ev['needs_tools']}, via {ev['source']})")
            elif t == "tool_call":
                mark = {"allow": "✓ auto", "confirm": "… needs ok", "deny": "✗ BLOCKED"}.get(ev["decision"], "")
                print(f"   ⚙  tool: {ev['name']}({json.dumps(ev['args'])})  [{mark}: {ev['reason']}]")
            elif t == "confirm":
                ok = bool(approve)
                print(f"   ❓ confirm: {ev['tool']} — {ev['reason']}  →  {'APPROVE' if ok else 'DENY'}")
                await client.post("/agent/approve",
                                  json={"session_id": session, "action_id": ev["id"], "approved": ok})
            elif t == "reasoning":
                n = len((ev.get("text") or "").split())
                print(f"   🧠 [reasoning: {n} words of thinking — collapsed]")
            elif t == "tool_result":
                res = (ev["result"] or "").strip().replace("\n", " ")
                print(f"   ⮐  result: {res[:80]}")
            elif t == "delta":
                buf.append(ev["text"])
            elif t == "text":
                buf.append(ev["text"])
            elif t == "error":
                print(f"   ⚠ error: {ev['message']}")
            elif t == "done":
                break
    answer = "".join(buf).strip()
    if answer:
        print(f"   💬 MOE: {answer[:400]}")


async def main():
    async with httpx.AsyncClient(base_url=BASE, timeout=180) as client:
        h = await client.get("/health")
        print("oMLX:", h.json().get("status"))
        for label, prompt, approve in SCENARIOS:
            try:
                await run(client, label, prompt, approve)
            except Exception as e:  # noqa: BLE001
                print(f"   ⚠ scenario error: {e}")
    print("\n" + "=" * 72 + "\nDemo complete.")


if __name__ == "__main__":
    asyncio.run(main())
