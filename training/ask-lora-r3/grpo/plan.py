#!/usr/bin/env python3
"""Pure-python planning for the GRPO run: hardware profiles, batch math, advantages, the time and cost estimate.

No torch, no I/O beyond printing, so it runs on the Mac and is unit-tested (test_plan.py). grpo.py imports the
batch math and the advantage function from here; `python plan.py` prints the estimate tables that README.md quotes.

    python plan.py                       # both hardware options, 1 epoch over the exported prompts
    python plan.py --rows 1488 --epochs 2 --usd-per-hour 32

Round 5 (RL-SCOPE) also puts here the torch-free pieces next to guard_problems: the small-n multimodal guard counts, the launch check, the
ideal-at-top rule, truncation and the per-step bucket statistics, S-trunc/S-learn/S-grader, and the LoRA-layout and gradient checks. The text-probe
metrics, S-* stop rules and their calibration are in probes.py.

Everything in ASSUMPTIONS is an ESTIMATE, not a measurement: no GPU step of this kit has run. Replace the
numbers from the first log lines of the smoke run (grpo.py writes seconds per phase) and re-run.
"""
from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass


# ------------------------------------------------------------------ batch math

@dataclass(frozen=True)
class Plan:
    """world GPUs; each optimizer step every GPU takes `ppr` prompts and samples `G` completions of each.

    Generation is data-parallel: a prompt and all of its G completions live on ONE rank, so group advantages
    are computed locally and the only cross-GPU traffic is the gradient reduction of the LoRA parameters."""
    world: int
    G: int
    ppr: int
    micro_bs: int

    def validate(self) -> "Plan":
        if self.G < 2:
            raise ValueError("num_generations must be at least 2 for a group baseline")
        if self.G % self.micro_bs:
            raise ValueError(f"micro_bs {self.micro_bs} must divide G = {self.G}: a forward pass covers one prompt's "
                             f"completions (they share the prompt and its images/audio)")
        return self

    @property
    def prompts_per_step(self) -> int:
        return self.world * self.ppr

    @property
    def completions_per_step(self) -> int:
        return self.prompts_per_step * self.G

    @property
    def completions_per_rank(self) -> int:
        return self.ppr * self.G

    @property
    def grad_accum(self) -> int:
        return self.completions_per_rank // self.micro_bs

    def steps(self, rows: int) -> int:
        return rows // self.prompts_per_step

    def rows_for(self, step: int, rank: int) -> list[int]:
        """Indices into the prompt file for this rank at this optimizer step (consecutive rows per step)."""
        base = step * self.prompts_per_step + rank * self.ppr
        return list(range(base, base + self.ppr))


PROFILES = {
    # one 48 GB card: the old text-only batch (4 prompts x 6 completions); micro_bs 3 is the 48 GB estimate
    "a6000": dict(plan=Plan(world=1, G=6, ppr=4, micro_bs=3), lr=5e-6, accelerate="accelerate_a6000.yaml"),
    # 8 x 288 GB: one prompt x 8 completions per GPU, the whole group in one forward/backward; lr x sqrt(64/24)
    "b300x8": dict(plan=Plan(world=8, G=8, ppr=1, micro_bs=8), lr=8e-6, accelerate="accelerate_b300x8_zero2.yaml"),
}


def advantages(rewards: list[float], std_floor: float) -> list[float]:
    """Group-relative advantage: (r - mean) / max(std, std_floor). The floor stops a group whose rewards differ by
    a hair (a WER of 0.02, say) from being blown up to unit variance: on near-saturated multimodal prompts the
    update is then close to zero and the KL term, not noise, decides."""
    n = len(rewards)
    mean = sum(rewards) / n
    std = math.sqrt(sum((r - mean) ** 2 for r in rewards) / max(1, n - 1))
    d = max(std, std_floor)
    return [(r - mean) / d for r in rewards]


GUARD_MARGINS = {"vision_reward": 0.05, "audio_reward": 0.05}


def guard_problems(base: dict, now: dict, margins: dict | None = None) -> list[str]:
    """Why `now` (a greedy multimodal dev eval, grpo.eval_mm) regressed against `base` (the same eval before any
    update). Empty list = fine. Counts may not rise at all; the two mean rewards may fall by the margin
    (one flipped dev row moves the vision mean by about 0.03)."""
    m = {**GUARD_MARGINS, **(margins or {})}
    out = []
    for k in ("vision_reward", "audio_reward"):
        if base.get(f"n_{k.split('_')[0]}") and now.get(k, 0.0) < base.get(k, 0.0) - m[k]:
            out.append(f"{k} {now.get(k, 0.0):.3f} < {base.get(k, 0.0):.3f} - {m[k]}")
    for k in ("vision_empty", "vision_banned", "vision_invalid", "audio_wrong", "audio_dropped", "audio_banned", "malformed"):
        if now.get(k, 0) > base.get(k, 0):
            out.append(f"{k} {now.get(k, 0):g} > {base.get(k, 0):g}")
    return out


# ------------------------------------------------------------------ round 5: multimodal small-n counts (C13)

# The three small-n guard counts RL-SCOPE C13 adds. Value = the sign of the BAD direction (+1: the count rising is bad).
SMALL_N = {"proposals_right": -1, "proposals_spurious": +1, "vision_facts_total": -1}


def mm_small_n(label: dict, parts: dict) -> dict:
    """One scored vision row's contribution to the C13 counts (eval_mm sums these). Picture-read rows score no
    proposal and no v_facts, so they add nothing."""
    out = {"proposals_right": 0.0, "proposals_spurious": 0.0, "vision_facts_total": 0.0}
    if label.get("modality") != "vision" or label.get("task") == "picture_read":
        return out
    out["vision_facts_total"] = float(parts.get("v_facts", 0.0))
    due = label.get("proposal") is not None
    prop = parts.get("v_proposal", 0.0)
    if due and prop >= 0.5 - 1e-9:
        out["proposals_right"] = 1.0                      # every proposal field matched (0.5 x fields hit / fields)
    if not due and prop <= -1.0 + 1e-9:
        out["proposals_spurious"] = 1.0
    return out


def small_n_problems(base: dict, now: dict, prev_soft: set | None = None) -> tuple[list[str], set]:
    """The C13 counts against the step-0 reference (RL-SCOPE 4.4, 8.2 S-mm): a move of 1 row in the bad direction is
    tolerated on a single eval; 2 or more trips at once; the same breach (even 1 row) at two consecutive cadence
    checkpoints trips. Returns (problems, the keys that breached by 1 row now). A key missing from either dict is skipped."""
    prev_soft = prev_soft or set()
    problems, soft = [], set()
    for k, sign in SMALL_N.items():
        if k not in base or k not in now:
            continue
        worse = sign * (now[k] - base[k])
        if worse >= 2.0 - 1e-9:
            problems.append(f"{k} {now[k]:g} vs step 0 {base[k]:g} (moved {worse:g} rows the wrong way)")
        elif worse >= 1.0 - 1e-9:
            soft.add(k)
            if k in prev_soft:
                problems.append(f"{k} {now[k]:g} vs step 0 {base[k]:g}: the same 1-row breach at two consecutive checkpoints")
    return problems, soft


def small_n_floor_problems(floor: dict, now: dict) -> list[str]:
    """The C13 counts against the untuned floor (--mm-floor): zero tolerance, like the existing counts. A floor file from before
    C13 has none of the keys and is skipped."""
    out = []
    for k, sign in SMALL_N.items():
        if k in floor and k in now and sign * (now[k] - floor[k]) > 1e-6:
            out.append(f"{k} {now[k]:g} vs untuned {floor[k]:g}")
    return out


# ------------------------------------------------------------------ round 5: the launch checks (C13) and the ideal-at-top rule

def train_arg_problems(a, dev_rows_exist: bool, rnd: int = 3) -> list[str]:
    """What is wrong with a `train` command line, before torch and the model load (so the torch-free tests reach it).
    `a` is the parsed namespace. --mm-floor is mandatory when multimodal dev rows exist (unless --no-guard); round 5 also needs
    the text dev file, the untuned text floor and at least two SFT replicates (unless --no-text-probes)."""
    out = []
    if dev_rows_exist and not getattr(a, "no_guard", False) and not getattr(a, "mm_floor", None) and not getattr(a, "dry_run_grad", False):
        out.append("--mm-floor is required: run `grpo.py mm-eval --base <UNTUNED Gemma> --mm-dev ...` (no adapter) and pass its "
                   "JSON, or pass --no-guard to give up the multimodal tripwire. Without it a vision/audio regression already "
                   "in the SFT would be accepted as the baseline.")
    if rnd >= 5 and not getattr(a, "dry_run_grad", False) and not getattr(a, "no_text_probes", False):
        if not getattr(a, "text_dev", None):
            out.append("round 5 needs --text-dev (grpo_text_dev.jsonl or its smoke30 subset), or --no-text-probes to give up the S-* tripwires")
        else:
            if not getattr(a, "text_floor", None):
                out.append("round 5 needs --text-floor (a `text-eval` run of the UNTUNED base on the same dev file)")
            if len(getattr(a, "text_replicates", None) or []) < 2:
                out.append("round 5 needs --text-replicates with at least two `text-eval` runs of the SFT (different --seed-offset)")
    return out


def ideal_top(lab: dict, rnd: int) -> tuple[float, float]:
    """(score the ideal completion must reach, tolerance) for `grpo.py check`. Rounds 3 and 4: the historical table. Round 5:
    the export's per-row `label.ceiling` on text rows, at top - 0.01 on non-call finals and top - 0.31 on calls."""
    mod = lab.get("modality")
    if rnd >= 5 and mod not in ("vision", "audio"):
        if lab.get("ceiling") is None:
            raise ValueError("a round-5 text row has no label.ceiling")
        return float(lab["ceiling"]), (0.31 if lab.get("step") in ("call", "load") else 0.01)
    top = {"vision": 1.5, "audio": 1.25}.get(mod, 1.5 if lab.get("step") in ("call", "load") else 1.0)
    if rnd >= 4 and lab.get("narrate") and lab.get("facts"):
        top = 2.0                        # tool set 1.0 + the answer facts, now up to 1.0
    return top, 0.31


# ------------------------------------------------------------------ round 5: truncation and the per-step logs (C10)

def truncated(comp: list[int], stop_ids) -> bool:
    """A completion with no stop id among its tokens ran into the cap (wherever the cut falls). A stop id emitted as the very
    last allowed token is not truncation."""
    return not any(t in stop_ids for t in comp)


BUCKETS = ("call", "select", "narrate", "recall", "recall_none", "decision", "instruction", "chat_anchor", "session_anchor", "text", "mm")
ANCHOR_KINDS = ("chat_anchor", "session_anchor")
INFORMATIVE_TERMS = ("forbidden", "early_write", "planted", "payload_text", "wrong_target", "nothing_claim")
BUCKET_STATS = ("groups", "samples", "trunc", "zv", "zv_pass", "zv_fail", "informative", "abs_adv", "adv_n", "pg_norm", "kl_norm", "norm_n")


def bucket_of(label: dict, modality: str = "text") -> str:
    """A prompt's log bucket: label.rl_kind on round-5 text rows ('text' on older exports), 'mm' for vision and audio."""
    if modality != "text":
        return "mm"
    k = label.get("rl_kind")
    return k if k in BUCKETS else "text"


def bucket_keys() -> list[str]:
    return [f"{b}/{s}" for b in BUCKETS for s in BUCKET_STATS]


def group_flags(totals: list[float], parts_list: list[dict], ceiling: float | None, trunc: list[bool],
                advs: list[float]) -> dict:
    """The counters one prompt's group adds to its bucket: zero variance (split all-pass at the row's ceiling / all-fail below it),
    informative (some but not all samples fire a verifier penalty), truncated samples, |A| sum."""
    n = len(totals)
    zv = (max(totals) - min(totals)) < 1e-9
    at_top = ceiling is not None and min(totals) >= ceiling - 1e-6
    fired = [any(p.get(k, 0.0) < 0 for k in INFORMATIVE_TERMS) for p in parts_list]
    return {"groups": 1, "samples": n, "trunc": sum(trunc), "zv": int(zv), "zv_pass": int(zv and at_top),
            "zv_fail": int(zv and not at_top), "informative": int(0 < sum(fired) < n),
            "abs_adv": sum(abs(x) for x in advs), "adv_n": n}


def bucket_log(tot: dict) -> dict:
    """Shares per bucket from the rank-summed counters (a bucket with no groups this step is left out). Zero-variance shares
    are over the bucket's groups; all-pass / all-fail are the two halves of that."""
    out = {}
    for b in BUCKETS:
        g = tot.get(f"{b}/groups", 0.0)
        if not g:
            continue
        d = {"groups": int(g), "samples": int(tot[f"{b}/samples"]), "trunc_share": round(tot[f"{b}/trunc"] / max(1.0, tot[f"{b}/samples"]), 4),
             "zv_share": round(tot[f"{b}/zv"] / g, 4), "zv_pass": round(tot[f"{b}/zv_pass"] / g, 4),
             "zv_fail": round(tot[f"{b}/zv_fail"] / g, 4), "informative_share": round(tot[f"{b}/informative"] / g, 4),
             "mean_abs_adv": round(tot[f"{b}/abs_adv"] / max(1.0, tot[f"{b}/adv_n"]), 4)}
        if tot.get(f"{b}/norm_n"):
            n = tot[f"{b}/norm_n"]
            pg, kl = tot[f"{b}/pg_norm"] / n, tot[f"{b}/kl_norm"] / n
            d.update(pg_grad_norm=round(pg, 6), kl_grad_norm=round(kl, 6), pg_kl_ratio=round(pg / kl, 3) if kl > 0 else None)
        out[b] = d
    return out


def trunc_problem(hist: list[tuple[int, int, int]], window_prompts: int = 40, limit: float = 0.05) -> str | None:
    """S-trunc (patience 1, a logged number, not a sampled one): the truncated share of text completions over the last
    `window_prompts` prompts. hist = per step (prompts, truncated, completions) over text rows, oldest first. Nothing fires
    before a full window has been seen."""
    prompts = trunc = n = 0
    for p, t, c in reversed(hist):
        prompts, trunc, n = prompts + p, trunc + t, n + c
        if prompts >= window_prompts:
            break
    if prompts < window_prompts or not n:
        return None
    share = trunc / n
    return f"S-trunc: {trunc} of {n} text completions ({share:.1%}) hit the cap over the last {prompts} prompts (limit {limit:.0%})" if share > limit else None


def adjusted_reward(rows: list[dict], weights: dict) -> float | None:
    """Composition-adjusted text reward (S-learn, S-grader, the lr bracket): per text row r / label.ceiling, averaged within each
    rl_kind, then combined with FIXED export-share weights. Rows with no ceiling (multimodal) are left out. Each row is
    {"kind", "ceiling", "reward"}."""
    by: dict[str, list[float]] = {}
    for r in rows:
        if r.get("ceiling"):
            by.setdefault(r["kind"], []).append(r["reward"] / r["ceiling"])
    num = den = 0.0
    for k, xs in by.items():
        w = weights.get(k, 0.0)
        num += w * sum(xs) / len(xs)
        den += w
    return num / den if den else None


def s_learn_hard(adj_first: float | None, adj_last: float | None, delta: float, zv_fail: float, zv0_fail: float,
                 zv_total: float, zv0_total: float, margin: float = 0.15) -> bool:
    """S-learn hard stop at prompt 400 (RL-SCOPE 8.2): the adjusted reward over prompts 301-400 has not risen above prompts 1-100
    by `delta` (null-calibrated) AND on PG-bearing text groups the all-fail zero-variance share is above its M6b baseline + 15
    points, or the total zero-variance share is. A rising all-pass share is saturation (success), never a stop."""
    if adj_first is None or adj_last is None:
        return False
    return (adj_last - adj_first) < delta and (zv_fail > zv0_fail + margin or zv_total > zv0_total + margin)


def s_grader_trips(adj_ma: float | None, adj_first100: float | None, delta: float, facts_gain: float | None, facts_floor: float,
                   recall_gain_points: float | None, unsafe_now: float | None, unsafe_m0: float | None) -> bool:
    """S-grader (an overfitting check only): the 10-step moving average of adjusted train reward above its first-100-prompt mean
    by `delta` while, over the last 2 evals, the facts-mean gain is below max(0.01, replicate SD) = facts_floor, the recall-hit gain
    is below 5 points and the S-unsafe count is at least m0."""
    if None in (adj_ma, adj_first100, facts_gain, recall_gain_points, unsafe_now, unsafe_m0):
        return False
    return (adj_ma - adj_first100) > delta and facts_gain < facts_floor and recall_gain_points < 5.0 and unsafe_now >= unsafe_m0


# ------------------------------------------------------------------ round 5: the LoRA layout and the 1-step gradient check (C22)

_ADAPTER_RE = re.compile(r"\.layers\.(\d+)\.(self_attn|mlp)\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)\.lora_A")
ALWAYS_ADAPTED = ("q_proj", "o_proj", "gate_proj", "up_proj", "down_proj")


def lora_layout(names: list[str], n_layers: int, kv_first: int | None = None) -> dict:
    """Check where the LoRA adapters landed. `names` = the module names ending '.lora_A'. q/o/gate/up/down must be on every
    language layer and nothing may sit outside the language model; k_proj/v_proj exist only on layers that build their own
    KV, so an adapter on a KV-shared layer (index >= kv_first, from config num_kv_shared_layers) could never get a gradient
    and DDP with find_unused_parameters=False would fail on it. Returns {problems, warnings, per_layer, summary}."""
    problems, warnings = [], []
    per_layer: dict[int, set] = {}
    outside = 0
    for n in names:
        m = _ADAPTER_RE.search(n)
        if "language_model" not in n or not m:
            outside += 1
            continue
        per_layer.setdefault(int(m.group(1)), set()).add(m.group(3))
    if outside:
        problems.append(f"{outside} adapters outside the language-model attention/MLP projections")
    for proj in ALWAYS_ADAPTED:
        have = sum(proj in per_layer.get(i, ()) for i in range(n_layers))
        if have != n_layers:
            problems.append(f"{proj} adapters on {have} of {n_layers} layers (want all)")
    extra = sorted(i for i in per_layer if not 0 <= i < n_layers)
    if extra:
        problems.append(f"adapters on layers outside 0..{n_layers - 1}: {extra[:5]}")
    kv = {p: sorted(i for i, s in per_layer.items() if p in s) for p in ("k_proj", "v_proj")}
    if not kv["k_proj"] and not kv["v_proj"]:
        problems.append("no k_proj/v_proj adapters at all (the target regex missed them)")
    if kv_first is not None:
        for p, layers in kv.items():
            shared = [i for i in layers if i >= kv_first]
            if shared:
                problems.append(f"{p} adapter on KV-shared layers {shared[:5]} (>= {kv_first}): they get no gradient")
    for i in range(n_layers):
        s = per_layer.get(i, set())
        if ("k_proj" in s) != ("v_proj" in s):
            warnings.append(f"layer {i} has {'k_proj' if 'k_proj' in s else 'v_proj'} but not its pair")

    def span(layers):
        return f"{len(layers)}/{n_layers}" + (f" (layers {layers[0]}-{layers[-1]})" if layers else "")
    summary = f"k_proj adapters on {span(kv['k_proj'])}, v_proj on {span(kv['v_proj'])}; q/o/gate/up/down on all {n_layers}" \
        if not problems else "; ".join(problems)
    return {"problems": problems, "warnings": warnings[:5], "per_layer": {i: sorted(s) for i, s in sorted(per_layer.items())},
            "kv_layers": kv, "summary": summary}


def kv_shared_first(config: dict) -> int | None:
    """The first KV-shared layer index from a model config (text_config.num_hidden_layers - num_kv_shared_layers), else None."""
    tc = config.get("text_config", config)
    n, k = tc.get("num_hidden_layers"), tc.get("num_kv_shared_layers")
    return n - k if isinstance(n, int) and isinstance(k, int) and k > 0 else None


def grad_problems(stats: dict) -> list[str]:
    """After one backward on a real row: every adapter parameter must hold a finite gradient, and every module's lora_B a non-zero one.
    (lora_A's gradient is exactly zero at init, B = 0, so a zero there is expected.) stats = {param name: {"grad": bool, "norm": float,
    "finite": bool}} over the trainable parameters."""
    out = []
    mods: dict[str, dict] = {}
    for name, g in stats.items():
        mod = name.split(".lora_")[0]
        kind = "B" if ".lora_B." in name else "A" if ".lora_A." in name else "?"
        mods.setdefault(mod, {})[kind] = g
        if not g["grad"]:
            out.append(f"{name}: no gradient")
        elif not g["finite"]:
            out.append(f"{name}: non-finite gradient")
    for mod, d in mods.items():
        b = d.get("B")
        if b and b["grad"] and b["finite"] and not b["norm"] > 0:
            out.append(f"{mod}: lora_B gradient is exactly zero (the module is not used by this forward)")
        if "A" not in d or "B" not in d:
            out.append(f"{mod}: missing its {'lora_A' if 'A' not in d else 'lora_B'} parameter")
    return out


def kl_k3(logp: float, ref_logp: float) -> float:
    """Schulman's k3 estimator of KL(policy || ref) at a sampled token: exp(ref - logp) - (ref - logp) - 1 >= 0."""
    x = ref_logp - logp
    return math.exp(x) - x - 1.0


def token_loss(logp: float, old_logp: float, adv: float, ref_logp: float, beta: float) -> float:
    """Per-token GRPO loss with one update per rollout (old == logp.detach() in training, so the ratio is 1)."""
    return -(math.exp(logp - old_logp) * adv) + beta * kl_k3(logp, ref_logp)


# ------------------------------------------------------------------ estimate

# Every number below is an assumption. `src` says where it comes from.
ASSUMPTIONS = {
    "n_eff": (4.0e9, "Gemma 4 E4B 'effective' parameters that cost FLOPs (the per-layer embeddings are lookups)"),
    "flop_factor": (10, "per completion token: policy fwd+backward with checkpointing = 3 x 2N, reference fwd 1 x 2N, "
                        "generation prefill 1 x 2N (HF repeats the prompt per completion); 5 x 2N"),
    "text_prompt": (6624, "measured: mean rendered text prompt on an r2-build export (README: production 3k-9k, mean ~6.8k)"),
    "text_completion": (130, "assumed mean sampled completion tokens, text rows (cap 384)"),
    "vision_prompt": (330 + 280 * 1.4, "system+question ~330 tokens + 280 per image x 1.4 images/row (video rows have 3-8 frames)"),
    "vision_completion": (70, "assumed mean reply tokens (cap 400)"),
    "audio_prompt": (60 + 25 * 5.0, "~60 template tokens + 25 audio tokens/s (40 ms/token) x 5 s mean clip"),
    "audio_completion": (22, "assumed mean transcript tokens (cap 240)"),
    "max_over_group": (1.8, "decode runs until the LONGEST of the G samples ends: ~1.8 x the mean length (capped)"),
    "mix": ({"text": 1265, "vision": 134, "audio": 89}, "measured: the exported mix on the r2-build test export (85/9/6 %)"),
}
# Round 4 (PLAN.md Stage 2) changes two estimates: the model thinks before it narrates (reward.py allows 120 free thought
# words), so a text completion is a thought of ~60-120 words plus a 1-3 sentence answer, and the cap rises to 512; the mix
# is the r4 export's (35% narrate, 20% compose/safety, 30% other text, 15% vision + audio incl. the picture reads).
ASSUMPTIONS_R4 = {
    "text_completion": (260, "assumed mean sampled completion tokens, text rows in round 4: ~90 thought words (~130 tokens) + answer/call"),
    "text_cap": (512, "completion cap, text rows (grpo.py --max-completion default in round 4)"),
    "mix": ({"text": 1265, "vision": 146, "audio": 77}, "the r3-data r4-mix test export: 85 / 9.8 / 5.2 % of 1,488 rows"),
}
# The r4 A6000 run: one 48 GB card, the settings README_R4 / grpo.py use. The profile itself is "a6000" (unchanged).
R4_A6000 = dict(profile="a6000", G=6, ppr=4, micro_bs=3, beta=0.04, beta_mm=0.2, lr=5e-6, prompts=1488, epochs=1,
                max_completion=512)
HW = {
    "a6000": dict(name="1 x RTX A6000 48 GB (sm_86)", peak=154.8e12, mfu=(0.45, 0.35, 0.25), tok_s=(0.030, 0.040, 0.055),
                  opt_s=0.3, straggler=1.0, usd_h=None,
                  src="bf16 dense tensor peak 154.8 TFLOPS (no FP8); HF eager + SDPA + checkpointing MFU 25-45 %; "
                      "decode is launch-bound at 30-55 ms/token for 42 layers"),
    "b300x8": dict(name="8 x B300 288 GB (sm_103), ZeRO-2", peak=2250e12, mfu=(0.28, 0.18, 0.10), tok_s=(0.022, 0.030, 0.045),
                   opt_s=0.5, straggler=1.3, usd_h=32.0,
                   src="bf16 dense peak ~2.25 PFLOPS per GPU; a 2560-wide model in eager PyTorch reaches only 10-28 %; "
                       "decode launch-bound 22-45 ms/token; one step waits for its slowest rank (x1.3 straggler)"),
}


def assumptions(rnd: int = 3) -> dict:
    return {**ASSUMPTIONS, **(ASSUMPTIONS_R4 if rnd >= 4 else {})}


def prompt_seconds(kind: str, G: int, peak: float, mfu: float, tok_s: float, rnd: int = 3) -> float:
    A = assumptions(rnd)
    n = A["n_eff"][0]
    k = A["flop_factor"][0]
    P = A[f"{kind}_prompt"][0]
    C = A[f"{kind}_completion"][0]
    cap = {"text": A["text_cap"][0] if "text_cap" in A else 384, "vision": 400, "audio": 240}[kind]
    flops = k * n * (P + C) * G
    steps = min(cap, A["max_over_group"][0] * C)
    return flops / (peak * mfu) + steps * tok_s


def step_seconds(profile: str, case: int, rnd: int = 3) -> dict:
    """Seconds for one optimizer step; case 0 = optimistic, 1 = middle, 2 = pessimistic."""
    hw, plan = HW[profile], PROFILES[profile]["plan"]
    mix = assumptions(rnd)["mix"][0]
    tot = sum(mix.values())
    per_kind = {k: prompt_seconds(k, plan.G, hw["peak"], hw["mfu"][case], hw["tok_s"][case], rnd) for k in mix}
    mean_prompt = sum(per_kind[k] * mix[k] / tot for k in mix)
    rank = plan.ppr * mean_prompt
    return {"per_kind": per_kind, "mean_prompt": mean_prompt, "rank_seconds": rank,
            "step": rank * hw["straggler"] + hw["opt_s"]}


def hours(profile: str, rows: int, epochs: float, case: int, rnd: int = 3) -> float:
    plan = PROFILES[profile]["plan"]
    return plan.steps(rows) * epochs * step_seconds(profile, case, rnd)["step"] / 3600.0


def a6000_r4_estimate(guard_evals: int | None = None) -> dict:
    """Hours for the round-4 GRPO epoch on one A6000 (R4_A6000): training steps plus the multimodal guard evals (every 25
    steps; one eval is about 120 greedy short decodes on one GPU, ~4x the 8-GPU MM_EVAL_H). Setup is paid once."""
    c = R4_A6000
    plan = PROFILES[c["profile"]]["plan"]
    steps = plan.steps(c["prompts"])
    evals = guard_evals if guard_evals is not None else steps // 25 + 1
    out = {"steps": steps, "evals": evals, "prompts_per_step": plan.prompts_per_step,
           "completions_per_step": plan.completions_per_step}
    for i, label in enumerate(("optimistic", "middle", "pessimistic")):
        train = hours(c["profile"], c["prompts"], c["epochs"], i, rnd=4)
        guard = evals * MM_EVAL_H * 4
        out[label] = {"train_h": round(train, 2), "guard_h": round(guard, 2), "total_h": round(train + guard + SETUP_H, 2),
                      "s_per_step": round(step_seconds(c["profile"], i, 4)["step"], 1)}
    return out


def report_r4(rows: int | None = None, epochs: float | None = None) -> str:
    """The round-4 one-A6000 estimate, as README_R4.md quotes it."""
    c = R4_A6000
    rows, epochs = rows or c["prompts"], epochs or c["epochs"]
    est = a6000_r4_estimate()
    plan = PROFILES[c["profile"]]["plan"]
    lines = [f"Round 4 GRPO on one {HW['a6000']['name']}: G={plan.G}, beta {c['beta']} (beta_mm {c['beta_mm']}), lr {c['lr']:g}, "
             f"{rows} prompts, {epochs:g} epoch, max completion {c['max_completion']}.",
             f"  {plan.ppr} prompts x G={plan.G} = {plan.completions_per_step} completions per step, micro_bs {plan.micro_bs}, "
             f"grad-accum {plan.grad_accum}: {plan.steps(rows)} steps; {est['evals']} multimodal guard evals."]
    for k, v in ASSUMPTIONS_R4.items():
        lines.append(f"  {k} = {v[0]}   [{v[1]}]")
    for label in ("optimistic", "middle", "pessimistic"):
        v = est[label]
        lines.append(f"  {label:11s} {v['s_per_step']:.0f} s/step -> train {hours('a6000', rows, epochs, ('optimistic', 'middle', 'pessimistic').index(label), 4):.1f} h "
                     f"+ guard {v['guard_h']:.1f} h + setup {SETUP_H} h = {v['total_h']:.1f} h")
    return "\n".join(lines)


def report(rows: int, epochs: float, usd_h: float, rnd: int = 3) -> str:
    out = []
    a = assumptions(rnd)
    out.append("Assumptions (estimates, not measurements):")
    for k, (v, src) in a.items():
        out.append(f"  {k} = {v}   [{src}]")
    for p in ("a6000", "b300x8"):
        hw, plan = HW[p], PROFILES[p]["plan"]
        out.append(f"\n{hw['name']}: {hw['src']}")
        out.append(f"  batch: {plan.world} GPU x {plan.ppr} prompt x G={plan.G} = {plan.prompts_per_step} prompts, "
                   f"{plan.completions_per_step} completions per step; micro_bs {plan.micro_bs}, grad-accum {plan.grad_accum}; "
                   f"{plan.steps(rows)} steps for {rows} rows")
        for i, label in enumerate(("optimistic", "middle", "pessimistic")):
            s = step_seconds(p, i, rnd)
            pk = s["per_kind"]
            out.append(f"  {label:11s} MFU {hw['mfu'][i]:.2f} decode {hw['tok_s'][i] * 1000:.0f} ms/tok: "
                       f"prompt seconds text {pk['text']:.1f} / vision {pk['vision']:.1f} / audio {pk['audio']:.1f}; "
                       f"mean {s['mean_prompt']:.1f} s x {plan.ppr} per rank x {hw['straggler']} + {hw['opt_s']} s "
                       f"= {s['step']:.1f} s/step -> {hours(p, rows, epochs, i, rnd):.2f} h for {epochs:g} epoch(s)")
    out.append("\nB300 cost at ${:.2f}/h for the whole node (billed per node-hour, whatever the GPUs do):".format(usd_h))
    out.append(cost_table(rows, usd_h, rnd))
    if rnd >= 4:
        out.append("\n" + report_r4(rows, epochs))
    return "\n".join(out)


# fixed per-session overheads on a rented node, in hours (assumed): venv + 16 GB model copy + processor smoke
SETUP_H = 0.75
MM_EVAL_H = 0.03        # one guard eval (about 60 greedy short decodes spread over the GPUs)


def runs(rows: int):
    """(label, optimizer steps fraction of an epoch-run, evals) rows for the cost table."""
    plan = PROFILES["b300x8"]["plan"]
    n = plan.steps(rows)
    return [
        ("smoke: 5 steps + check", 5, 1),
        ("first look: 50 steps", 50, 2),
        ("one epoch, all rows", n, max(1, n // 25)),
        ("two epochs", 2 * n, max(1, 2 * n // 25)),
        ("one epoch x 3 seeds", 3 * n, 3 * max(1, n // 25)),
    ]


def cost_table(rows: int, usd_h: float, rnd: int = 3) -> str:
    lines = ["| run | steps | optimistic h / $ | middle h / $ | pessimistic h / $ |", "|---|---|---|---|---|"]
    for label, steps, evals in runs(rows):
        cells = []
        for case in (0, 1, 2):
            t = steps * step_seconds("b300x8", case, rnd)["step"] / 3600.0 + evals * MM_EVAL_H + SETUP_H
            cells.append(f"{t:.1f} h / ${t * usd_h:,.0f}")
        lines.append(f"| {label} | {steps} | " + " | ".join(cells) + " |")
    lines.append(f"(each run includes {SETUP_H} h setup and {MM_EVAL_H} h per mm-guard eval; setup is paid once per "
                 f"session in practice, so chained runs cost less than the sum)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=1488)
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--usd-per-hour", type=float, default=32.0)
    ap.add_argument("--round", type=int, default=3, choices=(3, 4, 5), help="4 (and 5, estimated like 4): the round-4 mix and completion length, plus the one-A6000 estimate")
    a = ap.parse_args()
    print(report(a.rows, a.epochs, a.usd_per_hour, a.round))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
