"""Checks for train.py's --attn switch and attention-parity gate. No GPU, no weights, no network.

    python training/ask-lora-r3/brev/test_attn_parity.py        (or pytest on this file)

The gate / selection tests are pure python and run anywhere. The tests that build tensors need torch (a CPU
build is enough); without it they print SKIP, so the Mac main venv (no torch) still runs the rest. Run the
full set with a throwaway venv:  uv venv $TMPDIR/tv && uv pip install -p $TMPDIR/tv/bin/python torch numpy

What it shows:
 a. the parity statistics and verdict pass identical outputs and bf16-noise-level perturbations (near-ties
    flipping) and flag a deliberately wrong attention (a stand-in for the fused-kernel bug: wrong scale / noise), at
    the toy scale of Gemma's corner (head_dim 512, scale 1.0); attention_parity() runs end to end on a toy model
    whose "fused" kernel is wrong (forward AND backward, so the LoRA-gradient cosine catches it) but whose math
    backend is right; the OOM fallback (full length -> capped -> inconclusive, never ok) and the row picker;
 b. --attn math leaves only the math SDPA backend enabled; --attn selection (flag, $WISP_ATTN, default) and the
    per-tower attn_implementation maps;
 c. the train-side refusal rule (ROCm + sdpa needs a passing smoke.json for the same attn and torch build).
What it cannot show: that a real MI355X kernel is right or wrong (that is what smoke does on the box).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tr = _load("r3_train_attn", HERE / "train.py")

try:
    import torch
    import torch.nn.functional as F
except ImportError:      # the Mac main venv
    torch = None


def _row(n=100, loss_ref=2.0, d=0.0, n_conf=None, agree=None, kl=0.0, rid="r"):
    """A parity_stats-shaped row: n positions, loss d above the reference, n_conf confident positions of which
    `agree` match (default: all)."""
    n_conf = n if n_conf is None else n_conf
    return {"id": rid, "n_pos": n, "loss_ref": loss_ref, "loss_got": loss_ref + d, "dloss": abs(d), "n_conf": n_conf,
            "conf_agree": n_conf if agree is None else agree, "kl": kl}


def test_selection_and_maps():
    assert tr.resolve_attn(None, {}) == "sdpa"
    assert tr.resolve_attn(None, {"WISP_ATTN": "math"}) == "math"
    assert tr.resolve_attn("eager", {"WISP_ATTN": "math"}) == "eager", "the flag beats the environment"
    assert tr.resolve_attn(None, {"WISP_ATTN": " Math "}) == "math"
    assert tr.resolve_attn(None, {"WISP_ATTN": ""}) == "sdpa"
    for bad in ("flash", "sdp"):
        try:
            tr.resolve_attn(None, {"WISP_ATTN": bad})
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")
    sd, mt, eg = (tr.attn_implementation(x) for x in ("sdpa", "math", "eager"))
    assert sd == mt == {"": "sdpa", "text_config": "sdpa", "vision_config": "sdpa", "audio_config": "eager"}, \
        "sdpa and math load the identical model (today's map)"
    assert eg["text_config"] == "eager" and eg["vision_config"] == "eager" and eg["audio_config"] == "eager"
    out = subprocess.run([sys.executable, "-I", str(HERE / "train.py"), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--attn" in out.stdout and "--skip-attn-parity" in out.stdout, out.stderr[-300:]
    print("attn selection and implementation maps: OK")


def test_verdict_rules():
    v = tr.parity_verdict
    assert v([_row(200, d=0.004), _row(300, d=-0.01, kl=0.01)])["ok"]
    r = v([_row(200, d=0.0), _row(300, d=0.0, kl=0.01)])
    assert r["status"] == "pass" and r["top1"] == 1.0 and r["n_pos"] == 500
    # loss: pooled mean, tolerance max(0.02, 0.1/sqrt(n))
    assert not v([_row(400, d=0.03)])["ok"], "pooled 0.03 > 0.02"
    assert v([_row(400, d=0.019)])["ok"]
    assert v([_row(4, d=0.04)])["ok"], "4 positions: tolerance widens to 0.05"
    assert not v([_row(4, d=0.06)])["ok"]
    assert v([_row(6, d=0.04), _row(500, d=0.0)])["ok"], "a short row's noise is diluted in the pool"
    assert not v([_row(300, d=0.3), _row(300, d=-0.3)])["ok"], "per-row cap catches cancelling rows"
    # KL
    assert not v([_row(300, kl=0.06)])["ok"] and v([_row(300, kl=0.04)])["ok"]
    # top-1 only counts confident positions, pooled
    assert v([_row(300, n_conf=200, agree=197)])["ok"], "98.5%"
    assert not v([_row(300, n_conf=200, agree=195)])["ok"], "97.5%"
    assert v([_row(300, n_conf=0)])["ok"], "no confident position: top-1 is not judged"
    assert v([_row(300, n_conf=0)])["top1"] is None
    # gradient cosine
    assert v([_row(300)], 0.995)["ok"] and not v([_row(300)], 0.98)["ok"] and not v([_row(300)], 0.0)["ok"]
    # nothing to judge is never a pass
    assert not v([])["ok"] and not v([_row(0, n_conf=0)])["ok"]
    assert any("KL" in x for x in v([_row(300, kl=0.5)])["reasons"])
    print("verdict thresholds: OK")


def test_pick_rows_and_fatal():
    mk = lambda i, p, c, mm=None: (f"r{i}", [0] * p, [1] * c, mm)
    toks = [mk(0, 9000, 8), mk(1, 8000, 100), mk(2, 5000, 70), mk(3, 3000, 300), mk(4, 2500, 90), mk(5, 100, 5)]
    vis = [mk(10, 900, 30, {"modality": "image"}), mk(11, 400, 30, {"modality": "image"})]
    ids = lambda rows: [r[0] for r in rows]
    # >= 64 labeled tokens first (longest of those), then the image row; r0's 8-token answer is passed over
    assert ids(tr.pick_parity_rows(toks, vis)) == ["r1", "r2", "r3", "r10"]
    assert ids(tr.pick_parity_rows(toks, vis, cap=4096)) == ["r3", "r4", "r5", "r10"], "capped; topped up with a short row"
    assert ids(tr.pick_parity_rows(toks, [], cap=50)) == [], "nothing fits"
    assert ids(tr.pick_parity_rows(toks[:1], vis)) == ["r0", "r10"], "fewer than 64-token rows: fall back to the longest"
    f = tr.attn_parity_fatal
    bad, inc, ok = {"ok": False, "status": "fail"}, {"ok": False, "status": "inconclusive"}, {"ok": True}
    assert f(bad, "sdpa", True) and f(inc, "sdpa", True), "ROCm: failing OR inconclusive is fatal"
    assert not f(ok, "sdpa", True)
    assert not f(bad, "sdpa", False), "CUDA: warn only"
    assert not f(inc, "math", True) and not f(bad, "eager", True), "math/eager are their own reference"
    print("parity row picker and fatal rule: OK")


def test_gate():
    good = {"attn": "sdpa", "torch": "2.9.0+rocm7.1", "hip": "7.1", "attn_parity": {"ok": True}}
    g = tr.attn_parity_gate
    assert g(good, "7.1", "sdpa", "2.9.0+rocm7.1") is None
    # CUDA and the non-fused paths are never refused, whatever smoke.json says (or if it is missing)
    assert g(None, None, "sdpa", "2.9.0+cu128") is None, "CUDA path unchanged"
    assert g({"attn_parity": {"ok": False}}, None, "sdpa", "x") is None
    assert g(None, "7.1", "math", "x") is None and g(None, "7.1", "eager", "x") is None
    # ROCm + sdpa refusals
    assert "smoke" in g(None, "7.1", "sdpa", "2.9.0+rocm7.1")
    assert "smoke" in g({"ok": True, "peak_gib": 9}, "7.1", "sdpa", "2.9.0+rocm7.1"), "memory-only smoke.json from an old run"
    assert "FAILED" in g({**good, "attn_parity": {"ok": False}}, "7.1", "sdpa", "2.9.0+rocm7.1")
    assert "FAILED" in g({**good, "attn_parity": {}}, "7.1", "sdpa", "2.9.0+rocm7.1")
    assert "inconclusive" in g({**good, "attn_parity": {"ok": False, "status": "inconclusive"}}, "7.1", "sdpa", "2.9.0+rocm7.1"), \
        "an OOM-inconclusive smoke never lets ROCm train"
    assert "--attn math" in g({**good, "attn_parity": {"ok": False}}, "7.1", "sdpa", "2.9.0+rocm7.1")
    assert "torch" in g(good, "7.1", "sdpa", "2.10.0+rocm7.1"), "a different torch build must re-run smoke"
    assert "attn=math" in g({**good, "attn": "math"}, "7.1", "sdpa", "2.9.0+rocm7.1"), "parity was for another attn"
    assert g({}, "7.1", "sdpa", "x", skip=True) is None, "--skip-attn-parity"
    print("train-side refusal rule: OK")


def test_math_switch():
    if torch is None:
        print("math switch: SKIP (no torch)")
        return
    be = torch.backends.cuda
    before = (be.flash_sdp_enabled(), be.mem_efficient_sdp_enabled(), be.math_sdp_enabled())
    try:
        be.enable_flash_sdp(True)
        be.enable_mem_efficient_sdp(True)
        flags = tr.restrict_sdpa_to_math(torch)
        assert not be.flash_sdp_enabled() and not be.mem_efficient_sdp_enabled() and be.math_sdp_enabled()
        if hasattr(be, "cudnn_sdp_enabled"):
            assert not be.cudnn_sdp_enabled()
        assert flags["flash_sdp_enabled"] is False and flags["math_sdp_enabled"] is True
        # a torch module that lacks a switch (older builds) must not break it
        class Old:
            class backends:
                class cuda:
                    enable_flash_sdp = staticmethod(lambda v: None)
                    enable_math_sdp = staticmethod(lambda v: None)
                    flash_sdp_enabled = staticmethod(lambda: False)
        assert tr.restrict_sdpa_to_math(Old) == {"flash_sdp_enabled": False}
    finally:
        be.enable_flash_sdp(before[0])
        be.enable_mem_efficient_sdp(before[1])
        be.enable_math_sdp(before[2])
    print("--attn math disables flash / mem-efficient / cudnn SDPA: OK")


def _toy_attention(x, wq, wk, wv, scale=1.0, noise=0.0):
    """Gemma's corner in miniature: head_dim 512, scale 1.0 (q/k are RMS-normed instead of scaled)."""
    q, k, v = (F.normalize(x @ w, dim=-1) * 22.0 if i < 2 else x @ w for i, w in enumerate((wq, wk, wv)))
    out = F.scaled_dot_product_attention(q[None, None], k[None, None], v[None, None], is_causal=True, scale=scale)[0, 0]
    return out + noise * torch.randn_like(out) if noise else out


def test_parity_stats_flag_a_wrong_attention():
    if torch is None:
        print("parity stats: SKIP (no torch)")
        return
    g = torch.Generator().manual_seed(0)
    T, D, V = 48, 512, 64
    x = torch.randn(T, D, generator=g)
    wq, wk, wv = (torch.randn(D, D, generator=g) / D ** 0.5 for _ in range(3))
    head = torch.randn(D, V, generator=g) / D ** 0.5
    target = torch.randint(0, V, (T,), generator=g)

    def run(**kw):
        logits = _toy_attention(x, wq, wk, wv, **kw) @ head * 6.0
        return F.cross_entropy(logits, target).item(), logits

    ref_loss, ref = run()
    again_loss, again = run()
    same = tr.parity_stats(ref_loss, again_loss, ref, again)
    assert same["dloss"] == 0 and same["max_dlogit"] == 0 and same["top1_agree"] == T and same["kl"] == 0
    assert tr.parity_verdict([{"id": "a", **same}])["ok"], "identical outputs pass"
    bad = {}
    for name, kw in (("wrong scale", {"scale": 0.2}), ("noise", {"noise": 0.5}), ("scale 1/sqrt(d)", {"scale": 512 ** -0.5})):
        loss, lg = run(**kw)
        st = tr.parity_stats(ref_loss, loss, ref, lg)
        v = tr.parity_verdict([{"id": name, **st}])
        bad[name] = (st["dloss"], st["top1_agree"] / st["n_pos"], v["ok"])
        assert not v["ok"], (name, st)
    print("perturbed attention flagged:", {k: f"dloss {a:.2f}, top1 {b:.2f}" for k, (a, b, _) in bad.items()})
    # Perturbations at bf16 noise level must NOT fail, even when they flip near-ties (kernels legitimately differ):
    # round the logits to bf16, then add noise of ~1 ulp at |logit| ~ 30 (0.125) to a third of the positions.
    big = 30.0 * torch.tanh(ref * 2.0 / 30.0)               # a softcapped head: logits up to ~tens, bf16 ulp 0.06-0.125
    tgt = torch.where(torch.rand(T, generator=g) < 0.7, big.argmax(-1), target)    # a trained-ish model: loss ~1, not 60
    target = tgt
    for name, lg in (("bf16 rounding", big.bfloat16().float()),
                     ("1-ulp noise", big + 0.0625 * torch.sign(torch.randn(big.shape, generator=g)) * (torch.rand(big.shape, generator=g) < 0.33))):
        st = tr.parity_stats(F.cross_entropy(big, target).item(), F.cross_entropy(lg, target).item(), big, lg)
        v = tr.parity_verdict([{"id": name, **st}])
        assert v["ok"], (name, v["reasons"], st)
        print(f"  {name}: dloss {st['dloss']:.4f}, flipped argmax {st['n_pos'] - st['top1_agree']}/{st['n_pos']}, "
              f"confident agree {st['conf_agree']}/{st['n_conf']}, KL {st['kl']:.5f} -> pass")
    # ... and a near-tie flip is not judged: flip every position that has a sub-0.5 top-2 margin
    t2 = big.topk(2, dim=-1)
    tie = (t2.values[:, 0] - t2.values[:, 1]) <= tr.ATTN_MARGIN
    lg = big.clone()
    lg[tie, t2.indices[tie, 1]] = t2.values[tie, 0] + 0.01       # runner-up becomes top-1 only where it was a near-tie
    st = tr.parity_stats(F.cross_entropy(big, target).item(), F.cross_entropy(lg, target).item(), big, lg)
    assert st["top1_agree"] == st["n_pos"] - int(tie.sum()) and st["conf_agree"] == st["n_conf"], st
    print("parity statistics: OK")


class _ToyLM(torch.nn.Module if torch else object):
    """Stands in for the Gemma model in attention_parity(): embeds ids, one scale-1.0 SDPA layer, a head.
    `logits_to_keep` / `use_cache` mirror the real forward so train._logits drives it unchanged."""

    def __init__(self, V=48, D=512, buggy=False):
        super().__init__()
        g = torch.Generator().manual_seed(1)
        self.emb = torch.nn.Parameter(torch.randn(V, D, generator=g))
        self.w = torch.nn.ParameterList([torch.nn.Parameter(torch.randn(D, D, generator=g) / D ** 0.5) for _ in range(3)])
        self.head = torch.nn.Parameter(torch.randn(D, V, generator=g) / D ** 0.5)
        self.buggy = buggy

    def forward(self, input_ids, logits_to_keep, use_cache=False):
        x = self.emb[input_ids[0]]
        h = _toy_attention(x, *self.w)
        return type("Out", (), {"logits": (h @ self.head * 4.0)[None, -logits_to_keep:]})()


def _patched(real, wrong_scale=0.5):
    def fused_but_wrong(q, k, v, *a, **kw):
        # the fused kernel is "enabled" unless sdpa_kernel(MATH) switched it off; only then is it right
        if torch.backends.cuda.flash_sdp_enabled():
            kw["scale"] = wrong_scale
        return real(q, k, v, *a, **kw)
    return fused_but_wrong


def _rows(n=3, plen=35, clen=70):
    return [(f"t{i}", list(range(5 + i, plen + 5 + i)), [1 + (3 * j + i) % 40 for j in range(clen)], None) for i in range(n)]


def test_attention_parity_end_to_end():
    if torch is None:
        print("attention_parity(): SKIP (no torch)")
        return
    real = F.scaled_dot_product_attention
    rows = _rows()
    model = _ToyLM()
    be = torch.backends.cuda
    flash0 = be.flash_sdp_enabled()
    try:
        be.enable_flash_sdp(True)
        tr._EXTRA = None
        good = tr.attention_parity(model, rows, "sdpa", dev="cpu", grad_row=rows[0])
        assert good["ok"] and good["reference"] == "math" and len(good["rows"]) == 3, good
        assert good["grad_cos"] is not None and good["grad_cos"] > 0.999, good["grad_cos"]
        F.scaled_dot_product_attention = torch.nn.functional.scaled_dot_product_attention = _patched(real)
        bad = tr.attention_parity(model, rows, "sdpa", dev="cpu", grad_row=rows[0])
        assert not bad["ok"] and bad["worst_dloss"] > 0.25 and bad["grad_cos"] < tr.ATTN_GRAD_COS_MIN, bad
        assert be.flash_sdp_enabled(), "the math context must restore the flags"
        # grad cosine alone: even if the forward looked fine, a wrong backward fails the verdict
        assert not tr.parity_verdict([_row(300)], bad["grad_cos"])["ok"]
        assert tr.parity_verdict([_row(300)], good["grad_cos"])["ok"]
        # --attn math: the whole process is on math, so the wrong "fused" path is never taken
        tr.restrict_sdpa_to_math(torch)
        selfref = tr.attention_parity(model, rows, "math", dev="cpu", grad_row=rows[0])
        assert selfref["ok"] and selfref["reference"] == "self" and selfref["worst_dloss"] == 0, selfref
        assert not any(p.grad is not None for p in model.parameters()), "the grad check must leave no .grad behind"
    finally:
        F.scaled_dot_product_attention = torch.nn.functional.scaled_dot_product_attention = real
        be.enable_flash_sdp(flash0)
        be.enable_mem_efficient_sdp(True)
        be.enable_math_sdp(True)
    print(f"attention_parity(): clean kernel passes (grad cos {good['grad_cos']:.5f}), wrong kernel fails (worst dloss "
          f"{bad['worst_dloss']:.3f}, grad cos {bad['grad_cos']:.3f}), math is self-referenced: OK")


def test_oom_fallback():
    if torch is None:
        print("OOM fallback: SKIP (no torch)")
        return
    tr._EXTRA = None
    calls = []

    class Oom(_ToyLM):
        limit = 150            # tokens a forward may hold before "OOM"

        def forward(self, input_ids, logits_to_keep, use_cache=False):
            calls.append(input_ids.shape[1])
            if input_ids.shape[1] > self.limit:
                raise torch.cuda.OutOfMemoryError("CUDA out of memory (simulated)")
            return super().forward(input_ids, logits_to_keep, use_cache)

    big = [(f"b{i}", list(range(5, 5 + 300 + i)), [1 + j % 40 for j in range(70)], None) for i in range(3)]
    small = [(f"s{i}", list(range(5, 45 + i)), [1 + j % 40 for j in range(70)], None) for i in range(3)]
    m = Oom()
    # ROCm (strict): full length OOMs, retried on rows <= cap, which fit
    r = tr.run_attention_parity(m, big + small, [], "sdpa", True, dev="cpu", cap=200)
    assert r["status"] == "pass" and r["cap_tokens"] == 200 and {x["id"][0] for x in r["rows"]} == {"s"}, r
    # still OOM at the cap -> inconclusive, and inconclusive is never ok / always fatal when strict
    Oom.limit = 10
    r = tr.run_attention_parity(Oom(), big + small, [], "sdpa", True, dev="cpu", cap=200)
    assert r["status"] == "inconclusive" and not r["ok"] and tr.attn_parity_fatal(r, "sdpa", True), r
    assert tr.attn_parity_gate({"attn": "sdpa", "torch": "t", "attn_parity": r}, "7.1", "sdpa", "t") is not None
    # CUDA (not strict): never tries full length, only rows <= cap
    Oom.limit = 10 ** 6
    calls.clear()
    r = tr.run_attention_parity(Oom(), big + small, [], "sdpa", False, dev="cpu", cap=200)
    assert r["status"] == "pass" and r["cap_tokens"] == 200 and max(calls) <= 200 + 70, (r["cap_tokens"], max(calls))
    # no row fits the cap at all
    r = tr.run_attention_parity(Oom(), big, [], "sdpa", False, dev="cpu", cap=50)
    assert r["status"] == "inconclusive" and not r["ok"]
    print("OOM fallback (full -> capped -> inconclusive), CUDA capped from the start: OK")


def main() -> int:
    for fn in (test_selection_and_maps, test_verdict_rules, test_pick_rows_and_fatal, test_gate, test_math_switch,
               test_parity_stats_flag_a_wrong_attention, test_attention_parity_end_to_end, test_oom_fallback):
        fn()
    print("test_attn_parity: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
