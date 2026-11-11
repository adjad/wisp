"""Run scripts/run_toolbench.py unchanged, and record the clock every case actually sees.

The harness pins one simulated "now" for the whole run (runner.set_clock(world.now)), so every model in one run
shares it. This wrapper proves that per arm: around each runner.run_case it writes one line to <out>/clock.jsonl
with the model, case, trial, datetime.now(), date.today() and time.strftime as the patched clock returns them
(the same calls Wisp's prompts and tools make). Nothing else in the harness is changed.

    cd <wisp checkout> && python clock_wrapper.py <run_toolbench.py args...>   # --out is required
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
args = sys.argv[1:]
if "--out" not in args:
    sys.exit("clock_wrapper: pass --out so the clock log has a home")
out = Path(args[args.index("--out") + 1]).expanduser()
out.mkdir(parents=True, exist_ok=True)

from scripts.toolbench import runner  # noqa: E402

_orig = runner.run_case


async def run_case(client, model, case, world, **kw):
    import datetime as _dt
    import time as _time
    rec = {"model": model, "case": case.id, "trial": getattr(client, "seed", None),
           "now": _dt.datetime.now().isoformat(), "today": _dt.date.today().isoformat(),
           "strftime": _time.strftime("%Y-%m-%d %H:%M"), "world_now": world.now.isoformat()}
    with (out / "clock.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return await _orig(client, model, case, world, **kw)


runner.run_case = run_case

import scripts.run_toolbench as rt  # noqa: E402

sys.argv = ["scripts/run_toolbench.py", *args]
sys.exit(rt.main())
