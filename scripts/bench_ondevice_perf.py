#!/usr/bin/env python3
"""On-device LLM perf benchmark (Pipette-style) against oMLX.

Measures TTFT, prefill throughput, decode throughput, end-to-end latency, and
peak process memory for one or more oMLX-resident models across a sweep of
fixed prompt lengths (exact token counts per each model's own tokenizer).

Close the live Wisp app before running: every model under test is loaded with
`ensure_only(..., exclusive=True)`, which evicts whatever oMLX has resident
for a clean per-model memory reading -- including whatever a live Wisp turn
was using. The production role's model is restored on exit no matter how the
run ends (including Ctrl-C).

Usage:
    .venv/bin/python -m scripts.bench_ondevice_perf --all
    .venv/bin/python -m scripts.bench_ondevice_perf --only LFM2.5-1.2B-Instruct-MLX-4bit
    .venv/bin/python -m scripts.bench_ondevice_perf --context-lengths 256,1024 --reps 3
    .venv/bin/python -m scripts.bench_ondevice_perf --report-only
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psutil
from mlx_lm.utils import load_tokenizer

from service.config import role_to_model  # noqa: E402
from service.inference.omlx_client import ModelLoadError, OMLXClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
OMLX_MODEL_DIR = Path.home() / "Desktop" / "OMLX_Model_Files"
OMLX_APP_EXE = "/Applications/oMLX.app/Contents/MacOS/oMLX"
RESULTS_DIR = REPO_ROOT / "test_results" / "ondevice_perf_pilot"  # overridden in main()

# oMLX id -> local install dir (relative to OMLX_MODEL_DIR), for tokenizer loading.
PILOT_MODELS = {
    "LFM2.5-1.2B-Instruct-MLX-4bit": "LiquidAI/LFM2.5-1.2B-Instruct-MLX-4bit",
    "LFM2.5-8B-A1B-MLX-8bit": "mlx-community/LFM2.5-8B-A1B-MLX-8bit",
    "gemma-4-E4B-it-qat-4bit": "mlx-community/gemma-4-E4B-it-qat-4bit",
}

# Full 7-model list from the source benchmark spec: the LFM2.5 family plus the
# comparison models already covered by PILOT_MODELS.
FULL_MODELS = {
    **PILOT_MODELS,
    "LFM2.5-350M-MLX-4bit": "LiquidAI/LFM2.5-350M-MLX-4bit",
    "LFM2.5-230M-MLX-4bit": "LiquidAI/LFM2.5-230M-MLX-4bit",
    "LFM2.5-2.6B-oQ6e": "jason-schulz/LFM2.5-2.6B-oQ6e",
    "Qwen3.5-4B-4bit": "mlx-community/Qwen3.5-4B-4bit",
}

DEFAULT_CONTEXT_LENGTHS = [256, 1024, 4096]
DEFAULT_REPS = 5
DEFAULT_MAX_TOKENS = 256
MEM_POLL_INTERVAL_S = 0.1
REQUEST_TIMEOUT_S = 300.0
LOAD_TIMEOUT_S = 180.0

# Long enough to trim down to a 4096-token prompt for any tokenizer; the
# builder below grows it further on its own if a model's tokenizer is
# unusually token-frugal for this text.
_FILLER_PARAGRAPH = (
    "The quick brown fox jumps over the lazy dog near the riverbank at dawn, "
    "watching the mist rise slowly over the water while birds begin to sing "
    "their first songs of the morning. Every quiet town holds a thousand "
    "unremarkable stories, and most of them are never written down. "
)


# --------------------------------------------------------------------------
# Environment detection
# --------------------------------------------------------------------------

def _sysctl(name: str) -> str:
    return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                           check=True).stdout.strip()


def collect_environment() -> dict:
    settings_path = Path.home() / ".omlx" / "settings.json"
    ceiling_gb = None
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            ceiling_gb = settings.get("memory", {}).get("memory_guard_custom_ceiling_gb")
        except (json.JSONDecodeError, OSError):
            pass

    versions = {}
    for pkg in ("mlx", "mlx-lm", "huggingface_hub", "transformers", "psutil"):
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            versions[pkg] = None

    omlx_version = None
    try:
        omlx_version = subprocess.run(
            ["/Applications/oMLX.app/Contents/MacOS/omlx-cli", "--version"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    return {
        "chip": _sysctl("machdep.cpu.brand_string"),
        "hw_model": _sysctl("hw.model"),
        "ram_bytes": int(_sysctl("hw.memsize")),
        "iogpu_wired_limit_mb": _sysctl("iogpu.wired_limit_mb"),
        "omlx_memory_guard_ceiling_gb": ceiling_gb,
        "omlx_cli_version": omlx_version,
        "mac_version": platform.mac_ver()[0],
        "package_versions": versions,
    }


# --------------------------------------------------------------------------
# Peak memory sampling
# --------------------------------------------------------------------------

def find_omlx_process() -> psutil.Process | None:
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            if proc.info["exe"] == OMLX_APP_EXE:
                return psutil.Process(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def _total_rss(proc: psutil.Process) -> int:
    """Parent + children RSS, verified live rather than assumed single-process."""
    total = proc.memory_info().rss
    for child in proc.children(recursive=True):
        try:
            total += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total


async def _poll_rss(proc: psutil.Process, samples: list[int], stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            samples.append(_total_rss(proc))
        except psutil.NoSuchProcess:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=MEM_POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------
# Exact-token-count prompt construction
# --------------------------------------------------------------------------

def build_prompt_at_length(tok, target_tokens: int) -> tuple[str, int]:
    """Return (content, templated_prompt_tokens) where templated_prompt_tokens
    is as close to target_tokens as achievable by trimming filler content.

    Approximates what oMLX will actually prefill: applies the model's own chat
    template locally (add_generation_prompt=True) to account for role/special
    token overhead, then binary-searches the filler content length so the
    FULL templated token count lands on target_tokens (±1-2, since BPE
    re-merging at the cut boundary makes an exact hit not always possible).
    """
    overhead = len(tok.apply_chat_template(
        [{"role": "user", "content": ""}], add_generation_prompt=True))
    content_target = max(1, target_tokens - overhead)

    filler = _FILLER_PARAGRAPH
    while len(tok.encode(filler)) < content_target:
        filler += _FILLER_PARAGRAPH

    filler_ids = tok.encode(filler)
    lo, hi = 1, len(filler_ids)
    best_content = tok.decode(filler_ids[:content_target])
    while lo < hi:
        mid = (lo + hi) // 2
        content = tok.decode(filler_ids[:mid])
        total = len(tok.apply_chat_template(
            [{"role": "user", "content": content}], add_generation_prompt=True))
        if total == target_tokens:
            best_content = content
            break
        if total < target_tokens:
            best_content = content
            lo = mid + 1
        else:
            hi = mid
    total = len(tok.apply_chat_template(
        [{"role": "user", "content": best_content}], add_generation_prompt=True))
    return best_content, total


# --------------------------------------------------------------------------
# One measured rep
# --------------------------------------------------------------------------

async def _run_and_time(client: OMLXClient, model_id: str, messages: list[dict],
                         max_tokens: int) -> tuple[float | None, dict | None, float]:
    t_send = time.perf_counter()
    ttft_s = None
    final_message = None
    async for ev in client.stream_events(model_id, messages, temperature=0.0,
                                          max_tokens=max_tokens):
        if ttft_s is None and ev["kind"] in ("content", "reasoning"):
            ttft_s = time.perf_counter() - t_send
        if ev["kind"] == "final":
            final_message = ev["message"]
    e2e_s = time.perf_counter() - t_send
    return ttft_s, final_message, e2e_s


async def run_rep(client: OMLXClient, model_id: str, tok, content: str,
                   prompt_tokens: int, max_tokens: int,
                   proc: psutil.Process | None) -> dict:
    messages = [{"role": "user", "content": content}]
    samples: list[int] = []
    stop = asyncio.Event()
    poll_task = asyncio.create_task(_poll_rss(proc, samples, stop)) if proc else None

    try:
        ttft_s, final_message, e2e_s = await asyncio.wait_for(
            _run_and_time(client, model_id, messages, max_tokens),
            timeout=REQUEST_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001 - record any failure as data, not a crash
        if poll_task:
            stop.set()
            await poll_task
        return {"status": "failed", "error": f"{type(e).__name__}: {e}",
                "prompt_tokens": prompt_tokens}

    if poll_task:
        stop.set()
        await poll_task
    peak_rss = max(samples) if samples else None

    content_text = (final_message or {}).get("content") or ""
    reasoning_text = (final_message or {}).get("reasoning_content") or ""
    completion_tokens = len(tok.encode(content_text)) if content_text else 0
    completion_tokens += len(tok.encode(reasoning_text)) if reasoning_text else 0

    if ttft_s is None or completion_tokens == 0:
        return {"status": "failed", "error": "no tokens generated",
                "prompt_tokens": prompt_tokens, "e2e_s": e2e_s}

    decode_s = max(e2e_s - ttft_s, 1e-6)
    return {
        "status": "ok",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "ttft_s": ttft_s,
        "e2e_s": e2e_s,
        "prefill_tok_per_s": prompt_tokens / ttft_s,
        "decode_tok_per_s": completion_tokens / decode_s,
        "peak_rss_bytes": peak_rss,
    }


# --------------------------------------------------------------------------
# Per (model, context-length) cell
# --------------------------------------------------------------------------

async def run_cell(client: OMLXClient, model_id: str, tok, ctx_len: int, reps: int,
                    max_tokens: int, proc: psutil.Process | None) -> dict:
    content, prompt_tokens = build_prompt_at_length(tok, ctx_len)
    print(f"    [{model_id} @ {ctx_len}] prompt={prompt_tokens} tok (target {ctx_len}), "
          f"warm-up...", flush=True)

    try:
        await asyncio.wait_for(
            run_rep(client, model_id, tok, content, prompt_tokens, max_tokens, proc),
            timeout=REQUEST_TIMEOUT_S + LOAD_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001
        print(f"      warm-up failed: {type(e).__name__}: {e}", flush=True)

    reps_out = []
    for i in range(reps):
        r = await run_rep(client, model_id, tok, content, prompt_tokens, max_tokens, proc)
        reps_out.append(r)
        if r["status"] == "ok":
            print(f"      rep {i+1}/{reps}: ttft={r['ttft_s']:.3f}s "
                  f"prefill={r['prefill_tok_per_s']:.1f}tok/s "
                  f"decode={r['decode_tok_per_s']:.1f}tok/s "
                  f"e2e={r['e2e_s']:.2f}s "
                  f"peak_rss={(r['peak_rss_bytes'] or 0)/1e9:.2f}GB", flush=True)
        else:
            print(f"      rep {i+1}/{reps}: FAILED - {r.get('error')}", flush=True)

    ok_reps = [r for r in reps_out if r["status"] == "ok"]
    cell = {"model": model_id, "context_length": ctx_len, "target_prompt_tokens": ctx_len,
            "actual_prompt_tokens": prompt_tokens, "reps": reps_out}
    if ok_reps:
        for metric in ("ttft_s", "prefill_tok_per_s", "decode_tok_per_s", "e2e_s"):
            values = [r[metric] for r in ok_reps]
            cell[f"{metric}_mean"] = statistics.mean(values)
            cell[f"{metric}_stdev"] = statistics.stdev(values) if len(values) > 1 else 0.0
        mem_values = [r["peak_rss_bytes"] for r in ok_reps if r.get("peak_rss_bytes")]
        if mem_values:
            cell["peak_rss_bytes_mean"] = statistics.mean(mem_values)
            cell["peak_rss_bytes_stdev"] = statistics.stdev(mem_values) if len(mem_values) > 1 else 0.0
    cell["n_ok"] = len(ok_reps)
    cell["n_failed"] = len(reps_out) - len(ok_reps)

    RESULTS_DIR.joinpath("raw").mkdir(parents=True, exist_ok=True)
    safe = model_id.replace("/", "_")
    out_path = RESULTS_DIR / "raw" / f"{safe}__{ctx_len}.json"
    out_path.write_text(json.dumps(cell, indent=2))
    return cell


# --------------------------------------------------------------------------
# Report generation
# --------------------------------------------------------------------------

def write_report(env: dict, cells: list[dict], restore_model_id: str) -> None:
    lines = ["# On-Device LLM Benchmark Results (Pipette-style, oMLX)", ""]
    lines += ["## Environment", ""]
    lines.append(f"- Chip: {env['chip']} ({env['hw_model']})")
    lines.append(f"- RAM: {env['ram_bytes'] / 1e9:.0f} GB unified memory")
    lines.append(f"- `iogpu.wired_limit_mb`: {env['iogpu_wired_limit_mb']}")
    lines.append(f"- oMLX memory guard ceiling: {env['omlx_memory_guard_ceiling_gb']} GB")
    lines.append(f"- oMLX version: {env['omlx_cli_version']}")
    lines.append(f"- macOS: {env['mac_version']}")
    lines.append("- Package versions: " + ", ".join(
        f"{k}={v}" for k, v in env["package_versions"].items()))
    lines.append("")

    lines += ["## Methodology", ""]
    lines.append(
        "Each (model, context-length) cell: prompt trimmed to an exact token count "
        "via the model's own tokenizer (`apply_chat_template`, add_generation_prompt=True), "
        "one discarded warm-up call, then 5 measured reps via oMLX's streaming "
        "`/v1/chat/completions` at temperature=0 and a 256-token generation budget. "
        "TTFT is wall-clock to the first streamed token (reasoning or content). "
        "Decode tokens are counted by re-tokenizing the assembled reasoning+content "
        "text locally (oMLX's streaming path carries no `usage` field) — an "
        "approximation, not an exact per-token count from the server. "
        "Peak memory is the max sampled RSS (parent + child processes) of the oMLX "
        "process during each rep, polled every 100ms; on Apple Silicon unified "
        "memory this is a proxy for footprint, not the wired GPU allocation itself.")
    lines.append("")

    lines += ["## Results", ""]
    lines.append("| Model | Context | TTFT (s) | Prefill (tok/s) | Decode (tok/s) | "
                 "E2E (s) | Peak Mem (GB) | OK/Total |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for c in cells:
        if c["n_ok"] == 0:
            lines.append(f"| {c['model']} | {c['context_length']} | FAILED | FAILED | "
                         f"FAILED | FAILED | FAILED | 0/{c['n_ok']+c['n_failed']} |")
            continue
        mem_gb = c.get("peak_rss_bytes_mean", 0) / 1e9
        mem_std = c.get("peak_rss_bytes_stdev", 0) / 1e9
        lines.append(
            f"| {c['model']} | {c['context_length']} | "
            f"{c['ttft_s_mean']:.3f} ± {c['ttft_s_stdev']:.3f} | "
            f"{c['prefill_tok_per_s_mean']:.1f} ± {c['prefill_tok_per_s_stdev']:.1f} | "
            f"{c['decode_tok_per_s_mean']:.1f} ± {c['decode_tok_per_s_stdev']:.1f} | "
            f"{c['e2e_s_mean']:.2f} ± {c['e2e_s_stdev']:.2f} | "
            f"{mem_gb:.2f} ± {mem_std:.2f} | "
            f"{c['n_ok']}/{c['n_ok']+c['n_failed']} |")
    lines.append("")

    failed_cells = [c for c in cells if c["n_failed"] > 0]
    lines += ["## Failures / anomalies", ""]
    if failed_cells:
        for c in failed_cells:
            errs = {r.get("error") for r in c["reps"] if r["status"] == "failed"}
            lines.append(f"- {c['model']} @ {c['context_length']} tok: "
                         f"{c['n_failed']}/{c['n_ok']+c['n_failed']} reps failed — "
                         f"{'; '.join(e for e in errs if e)}")
    else:
        lines.append("- None.")
    lines.append("")

    lines += ["## Comparison to published Pipette / Artificial Analysis numbers", ""]
    lines.append(
        "_TODO — fetch https://pipette.liquid.ai/leaderboard and "
        "https://artificialanalysis.ai/articles/mobile-phone-intelligence-inference "
        "and fill in the matching model rows here. Caveat: different hardware, "
        "different serving engine, possibly different quantization, and this pilot "
        "covers only 3 models — a spot-check, not a full reproduction._")
    lines.append("")

    lines += ["## Scope", ""]
    lines.append(
        f"This run covers only this machine: {env['chip']}, "
        f"{env['ram_bytes']/1e9:.0f}GB unified memory. No cross-device claims — "
        "not validated on an M4 Air or any other hardware. Production model "
        f"restored to `{restore_model_id}` on exit.")
    lines.append("")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "ondevice_benchmark_results.md").write_text("\n".join(lines))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="run the 3-model pilot set")
    ap.add_argument("--full", action="store_true", help="run the full 7-model set")
    ap.add_argument("--only", default="", help="comma-separated oMLX model ids")
    ap.add_argument("--context-lengths", default=",".join(map(str, DEFAULT_CONTEXT_LENGTHS)))
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--restore-model", default=None,
                    help="model id to restore on exit (default: current 'general' role)")
    ap.add_argument("--report-only", action="store_true",
                    help="regenerate summary.json + report from existing raw/ files")
    args = ap.parse_args()

    global RESULTS_DIR
    suite_name = "ondevice_perf_full" if args.full else "ondevice_perf_pilot"
    RESULTS_DIR = REPO_ROOT / "test_results" / suite_name

    restore_model_id = args.restore_model or role_to_model("general")
    ctx_lengths = [int(x) for x in args.context_lengths.split(",")]

    if args.report_only:
        cells = []
        for p in sorted(RESULTS_DIR.glob("raw/*.json")):
            cells.append(json.loads(p.read_text()))
        env = json.loads((RESULTS_DIR / "run_metadata.json").read_text())["environment"] \
            if (RESULTS_DIR / "run_metadata.json").exists() else collect_environment()
        write_report(env, cells, restore_model_id)
        print(f"report regenerated at {RESULTS_DIR / 'ondevice_benchmark_results.md'}")
        return 0

    model_dirs = FULL_MODELS if args.full else PILOT_MODELS
    models = [m.strip() for m in args.only.split(",") if m.strip()]
    if not models:
        if args.full:
            models = list(FULL_MODELS)
        elif args.all:
            models = list(PILOT_MODELS)
    if not models:
        ap.error("give --only <ids>, --all, or --full")

    env = collect_environment()
    print(f"environment: {env['chip']}, {env['ram_bytes']/1e9:.0f}GB, "
          f"oMLX {env['omlx_cli_version']}, ceiling {env['omlx_memory_guard_ceiling_gb']}GB")
    print(f"restore target on exit: {restore_model_id}")
    print("NOTE: close the live Wisp app before continuing -- this run will evict "
          "whatever model it has resident.\n")

    client = OMLXClient()
    installed = await client.models()
    for m in models:
        if m not in installed:
            near = [x for x in installed if m.lower()[:12] in x.lower()]
            hint = f" close matches: {near}" if near else ""
            await client.aclose()
            print(f"ERROR: {m!r} not installed in oMLX.{hint}")
            return 1

    proc = find_omlx_process()
    if proc is None:
        print("WARNING: could not locate the oMLX process for memory sampling; "
              "peak-memory figures will be missing.")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "run_metadata.json").write_text(json.dumps({
        "environment": env, "models": models, "context_lengths": ctx_lengths,
        "reps": args.reps, "max_tokens": args.max_tokens,
        "restore_model_id": restore_model_id,
    }, indent=2))

    all_cells: list[dict] = []
    try:
        for model_id in models:
            local_dir = OMLX_MODEL_DIR / model_dirs.get(model_id, model_id)
            print(f"\n=== {model_id} ===")
            tok = load_tokenizer(str(local_dir))
            try:
                await asyncio.wait_for(
                    client.ensure_only(model_id, exclusive=True), timeout=LOAD_TIMEOUT_S)
            except (ModelLoadError, asyncio.TimeoutError) as e:
                print(f"  LOAD FAILED: {type(e).__name__}: {e} -- recording as failure")
                for ctx_len in ctx_lengths:
                    all_cells.append({
                        "model": model_id, "context_length": ctx_len,
                        "target_prompt_tokens": ctx_len, "actual_prompt_tokens": None,
                        "reps": [{"status": "failed", "error": f"load failed: {e}"}],
                        "n_ok": 0, "n_failed": 1,
                    })
                continue

            for ctx_len in ctx_lengths:
                cell = await run_cell(client, model_id, tok, ctx_len, args.reps,
                                      args.max_tokens, proc)
                all_cells.append(cell)
    finally:
        print(f"\nrestoring production model: {restore_model_id}")
        try:
            await asyncio.wait_for(
                client.ensure_only(restore_model_id, exclusive=False), timeout=LOAD_TIMEOUT_S)
            print("  restore OK")
        except Exception as e:  # noqa: BLE001
            print(f"  RESTORE FAILED: {type(e).__name__}: {e} -- "
                  f"fix manually: ensure_only({restore_model_id!r})")
        await client.aclose()

    (RESULTS_DIR / "summary.json").write_text(json.dumps(all_cells, indent=2))
    write_report(env, all_cells, restore_model_id)
    print(f"\nwritten to {RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
