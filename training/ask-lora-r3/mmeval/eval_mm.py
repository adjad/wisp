#!/usr/bin/env python
"""GPU side: run the BASE model and every saved LoRA checkpoint on the held-out multimodal dev rows and score
them (score_mm.py). transformers generate, greedy (= temperature 0), thinking off, batch 1, and the production
request shapes: vision.py's system prompt + question (max_tokens 400), the READ call (picture_read rows, 700) and voice.py's prompt with a 16 kHz mono
clip (max_tokens 240). One base load; each adapter is attached with peft and the base is run with the adapter
disabled, so the comparison is on the same weights, same code path.

    python eval_mm.py --base ~/wisp/models/gemma-4-E4B-it --ckpt-root ~/wisp/gemma/out \
        --data ~/wisp/mmeval-data --out ~/wisp/gemma/mmeval        # all step-*, final (+ best if its dir was pruned)
    python eval_mm.py ... --ckpts best,final --limit 8             # a quick look
    python eval_mm.py ... --smoke                                   # 2 rows per modality, base only, prints everything

Resumable: answers.jsonl is appended row by row and an existing (ckpt, modality, id) is skipped.
Then:  python pick_checkpoint.py --results OUT/results.json --ckpt-root CKPT_ROOT --strict

Nothing here runs on the Mac (it needs torch + the weights); the scoring and the picker are tested there.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import rows as R              # noqa: E402
import score_mm               # noqa: E402
import serving as SV          # noqa: E402
import scoring as S           # noqa: E402
from pick_checkpoint import adapter_dirs, checkpoint_losses, read_log, run_root  # noqa: E402

STOP_IDS = {106, 50, 1}       # <turn|>, <|tool_response>, <eos>; train.py's sample uses the same set
VISION_MAX_TOKENS = 400       # service/vision.py _MAX_TOKENS
READING_MAX_TOKENS = S.READING_MAX_TOKENS  # service/vision.py READ_MAX_TOKENS (read_picture: TEXT + DESCRIPTION)
TEXT_DOCUMENT_MAX_TOKENS = 600  # service/documents.py _MAX_TOKENS (answer_text: extracted .docx/.xlsx/.pptx text)
AUDIO_MAX_TOKENS = 240        # service/voice.py _MAX_TOKENS
SR = 16000


def die(msg: str):
    sys.exit(f"eval_mm: {msg}")


# ------------------------------------------------------------------ environment

def check_gpu(device: int) -> None:
    """Fail fast and loudly on a torch build that cannot run on this GPU (a B300 is sm_103 and needs a CUDA
    12.8+/13 build with Blackwell kernels), before a 16 GB load."""
    import torch
    if not torch.cuda.is_available():
        die("torch.cuda is not available")
    cap = torch.cuda.get_device_capability(device)
    name = torch.cuda.get_device_name(device)
    arch = torch.cuda.get_arch_list()
    if torch.version.hip:
        # ROCm: the arch is the gfx name (gcnArchName "gfx950:sramecc+:xnack-"), not an sm_ capability
        gfx = torch.cuda.get_device_properties(device).gcnArchName.split(":")[0]
        print(f"gpu {device}: {name} {gfx}; torch {torch.__version__} hip {torch.version.hip}; arch list {arch}")
        if gfx not in arch:
            die(f"this torch ({torch.__version__}, hip {torch.version.hip}) has no kernels for {name} ({gfx})")
        cap = None
    else:
        print(f"gpu {device}: {name} sm_{cap[0]}{cap[1]}; torch {torch.__version__} cuda {torch.version.cuda}; arch list {arch}")
    sm = f"sm_{cap[0]}{cap[1]}" if cap else ""
    same_major = [a for a in arch if cap and a.startswith(f"sm_{cap[0]}") and int(a[3:]) <= int(sm[3:])]
    if cap and sm not in arch and not same_major and f"compute_{cap[0]}{cap[1]}" not in arch:
        die(f"this torch ({torch.__version__}, cuda {torch.version.cuda}) has no kernels for {name} ({sm}); "
            f"install a build with {sm} (CUDA 12.8+/13) or run on the A6000")
    try:
        x = torch.randn(256, 256, device=device, dtype=torch.bfloat16)
        torch.cuda.synchronize()
        float((x @ x).float().sum())
    except Exception as e:  # noqa: BLE001
        die(f"a bf16 matmul failed on {name}: {type(e).__name__}: {e}")


def load_base(base: Path, device: int):
    import torch
    import transformers
    from transformers import AutoProcessor, AutoTokenizer
    cls = next((getattr(transformers, n) for n in ("AutoModelForMultimodalLM", "AutoModelForImageTextToText")
                if hasattr(transformers, n)), None)
    if cls is None:
        die("this transformers has no multimodal auto class; need >= 5.10.1 as in vm-setup.sh")
    tok = AutoTokenizer.from_pretrained(base, local_files_only=True)
    proc = AutoProcessor.from_pretrained(base, local_files_only=True)
    attn = {"": "sdpa", "text_config": "sdpa", "vision_config": "sdpa", "audio_config": "eager"}   # as train.py
    model = cls.from_pretrained(base, dtype=torch.bfloat16, attn_implementation=attn, device_map={"": device})
    model.eval()
    print(f"loaded {type(model).__name__} {sum(p.numel() for p in model.parameters()) / 1e9:.2f}B, "
          f"{torch.cuda.memory_allocated(device) / 2**30:.1f} GiB")
    return tok, proc, model


def discover(ckpt_root: Path, wanted: str | None) -> list[str]:
    """Checkpoint names to evaluate. Auto: every step-* (OUT and OUT/ckpt) and final, plus best when its step dir is gone."""
    dirs = set(adapter_dirs(ckpt_root))
    if wanted:
        names = [n.strip() for n in wanted.split(",") if n.strip()]
        missing = [n for n in names if n not in dirs]
        if missing:
            die(f"no adapter in {ckpt_root}/{{{','.join(missing)}}} (found {sorted(dirs)})")
        return names
    names = sorted(n for n in dirs if re.fullmatch(r"step-\d+", n))
    if "final" in dirs:
        names.append("final")
    if "best" in dirs:
        log = run_root(ckpt_root) / "train.log.jsonl"
        best = next((r["best"][0] for r in reversed(read_log(log)) if isinstance(r.get("best"), list) and r["best"]), None) \
            if log.is_file() else None
        if best not in names:
            names.append("best")
    return names


# ------------------------------------------------------------------ requests

def _device_inputs(enc, device: int):
    import torch
    out = {}
    for k, v in enc.items():
        if hasattr(v, "to"):
            v = v.to(device)
            if v.is_floating_point():
                v = v.to(torch.bfloat16)
        out[k] = v
    return out


def vision_inputs(tok, proc, row: dict, device: int):
    from PIL import Image
    root = Path(row["_root"])
    msgs = []
    for m in row["messages"]:
        m = dict(m)
        if isinstance(m.get("content"), list):
            m["content"] = [{"type": "image"} if p.get("type") in ("image", "image_url") else p for p in m["content"]]
        msgs.append(m)
    prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    imgs = []
    for rel in row["images"]:
        with Image.open(root / rel) as im:
            imgs.append(im.convert("RGB"))
    if prompt.count("<|image|>") != len(imgs):
        die(f"{row['id']}: {prompt.count('<|image|>')} image placeholders for {len(imgs)} images")
    # text_document rows carry no image: the processor rejects images=[] ("0 images for 1 text")
    enc = proc(text=prompt, images=imgs or None, add_special_tokens=False, return_tensors="pt")
    return _device_inputs(enc, device)


def read_wav(path: Path):
    import numpy as np
    with wave.open(str(path)) as w:
        if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
            die(f"{path}: need 16 kHz mono 16-bit WAV (got {w.getframerate()} Hz, {w.getnchannels()} ch, {8 * w.getsampwidth()} bit)")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def serving_audio_inputs(tok, enc, n_samples: int):
    """The same clip in oMLX's serving shape (see serving.py): ceil(ms/40) audio tokens, the first padded
    mel frame(s) switched on. Returns enc untouched when the two counts already agree."""
    import torch
    want, extra = SV.serving_extra_frames(n_samples)
    audio_id = tok.convert_tokens_to_ids("<|audio|>")
    ids = enc["input_ids"][0].tolist()
    if want == ids.count(audio_id) and extra == 0:
        return enc
    feats, fmask = enc["input_features"], enc["input_features_mask"]
    n = int(fmask[0].sum())
    need = n + extra
    if need > fmask.shape[1]:
        pad = need - fmask.shape[1]
        feats = torch.nn.functional.pad(feats, (0, 0, 0, pad))
        fmask = torch.nn.functional.pad(fmask, (0, pad), value=False)
    am = enc["attention_mask"][0].tolist() if "attention_mask" in enc else None
    tt = enc["mm_token_type_ids"][0].tolist() if "mm_token_type_ids" in enc else None
    ids, am, tt, mask = SV.apply_serving(ids, am, tt, fmask[0].tolist(), audio_id, want, extra)
    out = dict(enc)
    dev = enc["input_ids"].device
    out["input_ids"] = torch.tensor([ids], device=dev)
    if am is not None:
        out["attention_mask"] = torch.tensor([am], device=dev, dtype=enc["attention_mask"].dtype)
    if tt is not None:
        out["mm_token_type_ids"] = torch.tensor([tt], device=dev, dtype=enc["mm_token_type_ids"].dtype)
    out["input_features"] = feats
    out["input_features_mask"] = torch.tensor([mask], device=dev)
    return out


def audio_inputs(tok, proc, row: dict, device: int):
    rel = R.audio_path(row)
    if not rel:
        die(f"{row['id']}: audio row has no audio path")
    arr = read_wav(Path(row["_root"]) / rel)
    msgs = [{"role": "user", "content": [{"type": "audio"}, {"type": "text", "text": R.AUDIO_PROMPT}]}]
    prompt = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    try:
        enc = proc(text=prompt, audio=[arr], sampling_rate=SR, add_special_tokens=False, return_tensors="pt")
    except TypeError:       # older processors spell it audios=
        enc = proc(text=prompt, audios=[arr], sampling_rate=SR, add_special_tokens=False, return_tensors="pt")
    enc = _device_inputs(enc, device)
    if row.get("_serving"):
        enc = serving_audio_inputs(tok, enc, len(arr))
    return enc


def generate(model, tok, enc: dict, max_new: int) -> str:
    import torch
    with torch.no_grad():
        out = model.generate(**enc, max_new_tokens=max_new, do_sample=False, temperature=None, top_p=None,
                             top_k=None, eos_token_id=sorted(STOP_IDS), pad_token_id=tok.pad_token_id)
    ids = out[0, enc["input_ids"].shape[1]:]
    return S.strip_generation(tok.decode(ids, skip_special_tokens=False))


def smoke_checks(tok, proc, enc: dict, kind: str) -> None:
    ids = enc["input_ids"][0].tolist()
    print(f"  [{kind}] {len(ids)} prompt tokens; keys {sorted(enc)}")
    if kind == "vision":
        n = sum(1 for t in ids if t == tok.convert_tokens_to_ids("<|image|>"))
        print(f"  image soft tokens in prompt: {n}")
        if not n or "pixel_values" not in enc:
            die("vision smoke: no image tokens or no pixel_values; the processor call is wrong for this transformers")
    else:
        if not any(k in enc for k in ("input_features", "audio_values", "input_values")):
            die("audio smoke: the processor returned no audio features; check the audio= keyword for this transformers")


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="bf16 base dir (tokenizer, processor_config, weights)")
    ap.add_argument("--ckpt-root", required=True, help="adapter run dir holding step-*/best/final and train.log.jsonl")
    ap.add_argument("--ckpts", help="comma list of checkpoint dir names (default: auto)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--smoke", action="store_true", help="2 rows per modality, base only, verbose")
    ap.add_argument("--train-log", help="default: CKPT_ROOT/train.log.jsonl")
    ap.add_argument("--tol", action="append", metavar="CHECK=VALUE")
    score_mm.add_row_args(ap)
    a = ap.parse_args()

    vis, aud = score_mm.load_eval_rows(a)
    if a.smoke:
        vis, aud = vis[:2], aud[:2]
    if not vis and not aud:
        die("no dev rows (see --data / --vision-rows / --audio-rows / --vision-cases / --audio-truth)")
    print("dev rows: vision " + ", ".join(f"{k} {v}" for k, v in R.vision_stats(vis).items())
          + f"; audio {sum(not r.get('_serving') for r in aud)} (+{sum(bool(r.get('_serving')) for r in aud)} in serving shape)")
    if not aud:
        print("WARNING: no audio rows; the audio checks will be skipped (pick_checkpoint --strict will refuse)")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    ckpt_root = Path(a.ckpt_root)
    names = [] if a.smoke else discover(ckpt_root, a.ckpts)
    dirs = adapter_dirs(ckpt_root)
    print("checkpoints:", names or "(base only)")

    check_gpu(a.device)
    tok, proc, model = load_base(Path(a.base), a.device)
    peft = None
    if names:
        from peft import PeftModel
        peft = PeftModel.from_pretrained(model, str(dirs[names[0]]), adapter_name=names[0])
        for n in names[1:]:
            peft.load_adapter(str(dirs[n]), adapter_name=n)
        peft.eval()

    ans_path = out / "answers.jsonl"
    done = set()
    if ans_path.is_file():
        for line in ans_path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["ckpt"], r["modality"], r["id"]))
    fh = ans_path.open("a")
    (out / "run_info.json").write_text(json.dumps({
        "base": str(a.base), "ckpt_root": str(ckpt_root), "checkpoints": names, "vision_rows": len(vis),
        "audio_rows": len(aud), "vision_groups": R.vision_stats(vis), "when": time.strftime("%FT%T"), "decoding": "greedy, thinking off, batch 1",
        "max_new_tokens": {"vision": VISION_MAX_TOKENS, "picture_read": READING_MAX_TOKENS, "text_document": TEXT_DOCUMENT_MAX_TOKENS, "audio": AUDIO_MAX_TOKENS}}, indent=1))

    def run_all(ckpt: str, m) -> None:
        for modality, rws, build, cap in (("vision", vis, vision_inputs, VISION_MAX_TOKENS),
                                           ("audio", aud, audio_inputs, AUDIO_MAX_TOKENS)):
            for i, row in enumerate(rws):
                if (ckpt, modality, row["id"]) in done:
                    continue
                t0 = time.monotonic()
                rec = {"ckpt": ckpt, "modality": modality, "id": row["id"]}
                try:
                    enc = build(tok, proc, row, a.device)
                    if a.smoke and i == 0:
                        smoke_checks(tok, proc, enc, modality)
                    rec["raw"] = generate(m, tok, enc, READING_MAX_TOKENS if R.is_reading(row)
                                          else TEXT_DOCUMENT_MAX_TOKENS if row.get("kind") == "text_document" else cap)
                except Exception as e:  # noqa: BLE001 - recorded and scored as an error, never as an answer
                    rec["error"] = f"{type(e).__name__}: {e}"[:500]
                rec["secs"] = round(time.monotonic() - t0, 2)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                shown = (rec.get("raw") if "raw" in rec else rec["error"]).replace("\n", " ")
                print(f"{ckpt:>12} {modality:6} {row['id']:<28} {rec['secs']:5.1f}s | {shown[:110]}")

    if peft is not None:
        with peft.disable_adapter():
            run_all("base", peft)
        for n in names:
            peft.set_adapter(n)
            run_all(n, peft)
    else:
        run_all("base", model)
    fh.close()

    if a.smoke:
        print("smoke done; read the lines above (empty replies or errors mean a request-shape problem)")
        return 0
    log = a.train_log or (str(run_root(ckpt_root) / "train.log.jsonl") if (run_root(ckpt_root) / "train.log.jsonl").is_file() else None)
    res = score_mm.run(out, vis, aud, log, score_mm.parse_tol(a.tol), ckpt_root)
    print((out / "report.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
