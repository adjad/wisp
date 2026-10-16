# R2 build notes

Built 2026-10-15/16 on `main` at 40ffe997 (router PRs #239 + #241), with every step run in order:
`pool` → `gold --engine` → `distill --k 2` → `render` → `validate --template-checks 100000` → `stats` → `package`.

- **Engine:** oMLX with base **Mellum2.1-12B-A2.5B-Thinking-oQ5e** loaded, Wisp quit. Production tool retrieval
  is `lexical` (packaged default, no user override), so `gold --engine` and the offline `gold` (stub embedder)
  gave identical yields.
- **Distill:** Mellum passed 591/676 items at production sampling (try 0 greedy tool steps, retry at 0.6),
  within the reasoning caps (1500 chars/step, 2500/turn). Of the 85 unpassed, 72 fell back to gold and 4 fallbacks
  were dropped by the router. The remaining 9 belong to families with no gold plan (cal_free, rem_list, contact_mom).
- **Router #241 check:** the distill ran on the #239 router. Routing all 2110 pool items under #239 and #241 found
  11 menus that differ, all gold `todo.txt` items, and only in tool order (`find_files` / `list_dir` swapped). No
  distill item changed. Gold re-run on #241: no family's yield changed. `family_route_audit.py` agrees with
  ROUTER_FINDINGS.md.
- **Host incident:** the first distill pilot hit an oMLX that hung right after loading Mellum, while `syspolicyd`
  was down and fresh binaries parked in `_dyld_start`. It recovered without a reboot; that pilot's log was
  discarded.
- **Package:** `mellum-r2-data.zip` (gitignored; sha256 in `upload-zip.json`).

## Before training
The R1 kit (`training/mellum-r1-train/`) is data-agnostic, but `vm-setup.sh` expects `mellum-r1-data.zip` and
`mellum-r1-*` paths. Rename the zip on upload, or copy the kit to `mellum-r2-train/`, before the A6000 run.
