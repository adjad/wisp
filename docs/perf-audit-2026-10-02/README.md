# Reproduction material for the 2026-10-02 audit

Report: `../WISP_PERF_AUDIT_AND_MODEL_VIABILITY_2026-10-02.md`

`scripts/` was run from a session scratchpad, so `run_iso.py`, `start_iso.sh` and `drive*.py` contain absolute scratchpad paths. Edit them before reuse.

| Script | What it measures |
|---|---|
| `parse_log.py` | Parses oMLX `server.log` completion lines into JSON |
| `bench_raw.py scaling` | Decode and prefill tok/s versus context, direct to oMLX |
| `bench_cache.py` | Prefix-cache hit and miss TTFT |
| `bench_think.py` | Thinking ON versus OFF cost, and 2-way concurrency |
| `overhead*.py`, `authcost.py` | Cost of Wisp's oMLX attribution layer per call, with raw `httpx` as control |
| `run_iso.py`, `start_iso.sh` | Isolated backend on :8775 with transport tracing; `MODE=none|cache|off` |
| `drive.py`, `drive2.py`, `windows.py` | Timed `/agent` turns and per-turn call traces |
| `battery.py`, `grade.py`, `probe.py`, `cand.py` | Tool-call battery on Wisp's real schemas, grading, compatibility probes, and the candidate swap runner |

SAFETY: `cand.py` unloads and loads models in the live oMLX and restores `Ling-3.0-tiny-oQ6e` in a `finally`. Stop any isolated backend you start before running it, because an isolated backend's idle unloader will evict models it does not keep warm.

`data/` holds the raw battery results, per-turn timelines and transport traces behind the report's tables.
