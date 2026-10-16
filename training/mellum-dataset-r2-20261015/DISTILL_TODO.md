# R2: what still needs the engine

Everything below needs oMLX with **Mellum2.1-12B-A2.5B-Thinking-oQ5e** (the base, not R1) loaded and **Wisp
quit**. The engine is shared with other tests, so get the go-ahead first. Run from this folder with the repo venv
(`../../../.venv/bin/python` from the main checkout, `../../../../../.venv/bin/python` from a worktree).

1. `build.py pool` — deterministic; reproduces the committed pool (2110 items).
2. `build.py gold --engine` — **done 2026-10-15**: identical yields to the offline run (retrieval is lexical
   in production), 2366 rows tagged `engine`. Re-run it only if the router changes.
3. **Blocked 2026-10-15:** a 5-item pilot hit a hung oMLX. It loaded Mellum, then stopped answering, and
   `syspolicyd` was down, so no fresh binary (not even `omlx --help`) got past `_dyld_start`. Fix the host first
   (`sudo launchctl kickstart -k system/com.apple.security.syspolicy`, or reboot), then restart oMLX from its
   menu-bar app, and confirm `curl -m 5 localhost:8000/v1/models` answers. The pilot's log was deleted, so
   nothing is marked done.
   `build.py distill --k 2` — the 676 distill-mode items (R1's distill families, unchanged: cal_tomorrow,
   cal_sunday, cal_past, cal_cancel, cal_free, rem_tomorrow, rem_list, mail_from_coworker, mail_work,
   msg_read_friend, contact_phone, contact_mom, notes_find, notes_create, files_list, web_weather, web_stock,
   dev_toggle, dev_battery, dev_apps, dev_timer, mem_remember). Unpassed items fall back to gold. All R2 families
   are gold-only, because R1 itself was the model that failed them.
4. `build.py render`, `validate --template-checks 100000`, `stats`, `package` → `mellum-r2-data.zip`.
   Remove the `train.jsonl` / `dev.jsonl` lines from `.gitignore` and commit them with the final card.

Training kit: `training/mellum-r1-train/` is data-agnostic, but `vm-setup.sh` expects `mellum-r1-data.zip` and
`mellum-r1-*` paths. Rename the zip on upload or copy the kit to `mellum-r2-train/` before the A6000 run.
