#!/usr/bin/env python3
"""Wisp Ask LoRA trainer for Gemma 4 E4B or Nemotron 3.5 Bolt E4B on one NVIDIA GPU (48 GB class).
Bolt is an RL post-train of Gemma 4 E4B with the same architecture and tokenizer, so one script
trains both; `--tag gemma|bolt` picks which (each run is a separate LoRA on the shared dataset).

    python train.py preflight --tag T --data DIR --base DIR            # template sha + render parity, lengths, stop tokens
    python train.py smoke     --tag T --data DIR --base DIR --out DIR  # the longest rows: peak memory + speed
    python train.py train     --tag T --data DIR --base DIR --out DIR [--resume DIR]
    torchrun --standalone --nproc_per_node=8 train.py train ...        # 8 GPUs, same flags (see "Multi-GPU")
    python train.py merge     --tag T --base DIR --adapter DIR --data DIR --out DIR
    python train.py sample    --tag T --base DIR --adapter DIR --data DIR   # greedy decode of dev prompts, for a look

Rows are prompt/completion strings rendered on the Mac by brev/render.py with
Wisp's own chat template and each request's `chat_template_kwargs`, so the
prompt here is byte-for-byte what oMLX feeds the model in production. Loss is
on the completion only (an optional one-sentence thought, then the answer or
tool call, then its stop token).

Recipe (from Google's Gemma 4 QLoRA guide, Unsloth's Gemma 4 guide and the
community E4B write-ups; see brev/README.md for the sources):
- transformers >= 5.10.1 and peft >= 0.19. Before transformers 5.5.2,
  use_cache=False (which gradient checkpointing forces) broke E4B's 18
  KV-shared layers and gave garbage logits; smoke checks cache parity.
- LoRA r=16, alpha=16, dropout 0.05 on the language model's attention AND MLP
  projections. The vision/audio towers wrap their linears in
  Gemma4ClippableLinear, which PEFT cannot adapt, so the target regex is scoped
  to `language_model`.
- lr 2e-4, cosine, 3% warmup, grad-norm clip 0.3, 2 epochs, best-by-dev-loss.
- Train and merge with the same multimodal class (AutoModelForMultimodalLM),
  so adapter keys line up and the merged checkpoint keeps the vision and audio
  towers Wisp uses (image input, voice transcription). The merge refuses to
  save unless its tensor names equal the base's.
- Overfit a few rows before the real run (smoke does this).

Memory: Gemma's vocabulary is 262,144, so full logits for a 9.5k-token row are
~10 GiB in fp32 before their gradient. Each step asks for the completion
positions only (`logits_to_keep`), which is exact because the loss is
completion-only. Rows are weighted equally within an optimizer step, so the few
long answers don't drown the short tool calls.

Vision rows (r3). `vision_train.jsonl` / `vision_dev.jsonl` (made by render.py --vision-rows) carry
`images` (paths inside the data dir) and a prompt with one `<|image|>` placeholder per image. They are
mixed into the text rows at --vision-weight (default 0.12 = about 12% of the rows, hence of the steps):
- the prompt's placeholder is expanded exactly as Gemma4Processor does it, `<|image>` + N x `<|image|>`
  + `<image|>` with N = the image's soft-token count (<= 280), and the completion is tokenized apart
  from the prompt, so the label mask is "everything after the expanded prompt" whatever N is;
- pixel_values / image_position_ids come from the AutoProcessor's image processor at each step, and
  the processor's soft-token count must equal the one used for the expansion (else the row is refused);
  preflight also feeds every vision row through the full processor call and requires identical ids;
- mm_token_type_ids marks the image tokens (1), as the processor does. Towers stay frozen; only the
  language model's LoRA trains. Micro batch is 1, so there is no padding to mask.

Audio rows (r3). `audio_train.jsonl` / `audio_dev.jsonl` (render.py --audio-rows) carry `audios` (16 kHz mono
WAV paths in the data dir) and a prompt with one `<|audio|>` per clip, rendered exactly as oMLX renders
service/voice.py's request (thinking off). They are mixed in at --audio-weight (default 0.06):
- the placeholder is expanded as Gemma4Processor.replace_audio_token does: `<|audio>` + N x `<|audio|>` +
  `<audio|>`, N = the number of valid mel frames (the extractor's 320-sample frames, 160 hop, 160 samples of
  left padding) after two stride-2 subsamplings, capped at 750 (30 s). audio_soft_token_count() reproduces it
  from the WAV header alone, no torch; preflight checks it against the real processor on every row;
- input_features / input_features_mask come from the processor's feature extractor at each step, and their
  mask must give the same N or the row is refused; mm_token_type_ids marks audio tokens 3, as the processor does;
- the label mask is "everything after the expanded prompt", like images.

Base anchor (r2 regressed vision and audio-adjacent behaviour; r3 must not). On every image/audio row, and on
an optional --anchor-text-frac slice of text rows, the loss is CE + --anchor-kl * KL(base || tuned) over the
completion positions only, where the base distribution comes from the SAME weights with the LoRA switched off
(peft disable_adapter, no_grad). KL is taken teacher-forced on the target completion, full 262k vocabulary,
chunked and re-computed in backward so it costs no extra memory. CE and KL are logged separately.

Held-out dev rows. Multimodal rows with meta.split == "dev" (render.py honours it, else a stable hash) never
enter training. Every eval reports dev_loss_text / dev_loss_vision / dev_loss_audio and, for image and audio
rows, dev_kl_* (drift from the base). The step-0 eval is the untuned reference: a checkpoint whose
dev_loss_vision or dev_loss_audio is worse than that reference by more than --mm-tol is not eligible for
`best` while an eligible one exists (see better_checkpoint).

Multi-GPU. `torchrun --standalone --nproc_per_node=N train.py train ...` runs data parallel on N GPUs
(8x B300); a plain `python train.py` is the single-GPU path (A6000). Effective-batch rule: --global-batch rows
(default 8, micro batch 1) make ONE optimizer step on any number of GPUs; each rank processes
global_batch / world_size of them, so 1 GPU = 8 rows each, 8 GPUs = 1 row each, and the lr schedule, steps per
epoch and loss are the same whatever the hardware (global_batch must be a multiple of world_size). Raising
--global-batch changes the optimization; if you do, scale --lr by roughly sqrt(new/8). Rows of a batch are
dealt to ranks by length (longest first, to the least-loaded rank) so no rank idles behind a 9k-token row.
Gradients are summed across ranks by an explicit all-reduce of the LoRA gradients (not a DDP wrapper: the
trainable set is ~tens of MB, some adapters get no gradient on a given row, and the loss is already divided
by the global batch). Rank 0 alone logs, evaluates-and-writes, and saves; every rank joins each evaluation (dev
rows are sharded) and every checkpoint barrier. --save-every N writes an adapter-only snapshot to
OUT/ckpt/step-NNNNN (never pruned) plus OUT/ckpt/index.jsonl, for the GPU multimodal eval to choose from.
"""
from __future__ import annotations

import os

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")  # before torch loads

import argparse
import hashlib
import inspect
import json
import math
import random
import re
import shutil
import sys
import time
from pathlib import Path

STOP_IDS = {106, 50}             # <turn|>, <|tool_response>
VISION_WEIGHT = 0.12             # default share of training rows that carry images
AUDIO_WEIGHT = 0.06              # default share of training rows that carry audio clips
ANCHOR_KL = 0.5                  # default lambda of KL(base || tuned) on image/audio rows
MM_KL_MAX = 0.5                  # a dev_kl_vision/audio above this (nats per token) also makes a checkpoint ineligible
AUDIO_SERVING_FRAC = 0.25        # share of audio TRAIN rows expanded with oMLX's serving token count
MM_TOL = 0.05                    # a dev_loss_vision/audio this much worse than step 0 makes a checkpoint ineligible
AUDIO_MAX_SAMPLES = 480_000      # Gemma4AudioFeatureExtractor.__call__ max_length (30 s at 16 kHz)
IMAGE_TYPE, AUDIO_TYPE = 1, 3    # mm_token_type_ids values (processing_utils.create_mm_token_type_ids)
N_LAYERS = 42
# Per-model template shas live in render.py (shipped in the data zip as render.py).
# Gemma's pinned sha equals the HF bf16 repo's, so a mismatch fails. Bolt's is the local quant's;
# the HF bf16 repo may differ, so there a mismatch warns and render parity decides.
STRICT_SHA_TAGS = {"gemma"}
# language-model attention + MLP only; the towers' ClippableLinear layers must not match
TARGET_RE = r".*language_model.*\.(self_attn\.(q_proj|k_proj|v_proj|o_proj)|mlp\.(gate_proj|up_proj|down_proj))"


# rank / world / local device. A plain `python train.py` is rank 0 of 1; torchrun sets the env.
_DIST = {"rank": 0, "world": 1, "local": 0}


def is_main() -> bool:
    return _DIST["rank"] == 0


def init_dist() -> None:
    """Join the process group when launched by torchrun (WORLD_SIZE > 1); single-process otherwise."""
    world = int(os.environ.get("WORLD_SIZE", "1"))
    if world <= 1:
        return
    import datetime
    import torch
    import torch.distributed as dist
    _DIST.update(rank=int(os.environ["RANK"]), world=world, local=int(os.environ.get("LOCAL_RANK", "0")))
    torch.cuda.set_device(_DIST["local"])
    # evals and checkpoints make non-zero ranks wait on rank 0: a long timeout, not a hang
    dist.init_process_group("nccl", timeout=datetime.timedelta(minutes=90))


def barrier() -> None:
    if _DIST["world"] > 1:
        import torch.distributed as dist
        dist.barrier()


def reduce_sum(values: list[float], dev) -> list[float]:
    """Element-wise sum of a list of floats across ranks (identity on one process)."""
    if _DIST["world"] == 1:
        return list(values)
    import torch
    import torch.distributed as dist
    t = torch.tensor(values, dtype=torch.float64, device=dev)
    dist.all_reduce(t, op=dist.ReduceOp.SUM)
    return t.tolist()


def log(path: Path | None, **kv) -> None:
    """Rank 0 prints and appends; other ranks stay silent."""
    if not is_main():
        return
    line = json.dumps({"t": round(time.time(), 1), **kv})
    print(line, flush=True)
    if path:
        with path.open("a") as fh:
            fh.write(line + "\n")


# ------------------------------------------------------------------- data

def load_rows(data: Path, split: str) -> list[dict]:
    rows = [json.loads(l) for l in (data / f"{split}.jsonl").read_text().splitlines() if l.strip()]
    if not rows or not {"prompt", "completion"} <= set(rows[0]):
        sys.exit(f"{data}/{split}.jsonl: expected rows with prompt and completion")
    return rows


def load_optional_rows(data: Path, split: str) -> list[dict]:
    """vision_* / audio_* train and dev rows, or [] when the zip carries none (a text-only run)."""
    f = Path(data) / f"{split}.jsonl"
    if not f.is_file():
        return []
    key = "audios" if split.startswith("audio") else "images"
    rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    for r in rows:
        if not r.get(key) and r.get("kind") != "text_document":   # extracted-text documents carry no pixels
            sys.exit(f"{f}: row {r.get('id')} has no {key}")
    return rows


# ---- images: the processor's own expansion, reproduced without torch so a Mac can check it

def soft_token_count(height: int, width: int, patch: int, pool: int, max_soft: int) -> int:
    """Image soft tokens for an image of this size: Gemma4's aspect-ratio-preserving resize
    (transformers' get_aspect_ratio_preserving_size), then patches // pool**2."""
    max_patches = max_soft * pool ** 2
    side = pool * patch
    factor = math.sqrt(max_patches * patch ** 2 / (height * width))
    th, tw = int(math.floor(factor * height / side)) * side, int(math.floor(factor * width / side)) * side
    if th == 0 and tw == 0:
        raise ValueError("image resizes to 0 x 0")
    max_side = (max_patches // pool ** 2) * side
    if th == 0:
        th, tw = side, min(int(math.floor(width / height)) * side, max_side)
    elif tw == 0:
        tw, th = side, min(int(math.floor(height / width)) * side, max_side)
    return (th // patch) * (tw // patch) // pool ** 2


def image_config(base: Path) -> dict:
    """Image-processor numbers and token strings from the base's processor_config.json."""
    ip = json.loads((Path(base) / "processor_config.json").read_text())["image_processor"]
    cfg = {"patch": ip["patch_size"], "pool": ip["pooling_kernel_size"], "max_soft": ip["max_soft_tokens"]}
    tc = json.loads((Path(base) / "tokenizer_config.json").read_text())
    cfg.update(boi=tc["boi_token"], eoi=tc["eoi_token"], image=tc["image_token"])
    return cfg


def expand_image_prompt(prompt: str, counts: list[int], boi: str, image: str, eoi: str) -> str:
    """Replace the i-th `<|image|>` placeholder with `<|image>` + counts[i] x `<|image|>` + `<image|>`
    (Gemma4Processor.replace_image_token). Splitting first keeps the inserted image tokens from being re-read."""
    parts = prompt.split(image)
    if len(parts) != len(counts) + 1:
        raise ValueError(f"{len(parts) - 1} image placeholders in the prompt but {len(counts)} images")
    out = [parts[0]]
    for n, rest in zip(counts, parts[1:]):
        out += [boi, image * n, eoi, rest]
    return "".join(out)


def worst_case_image_tokens(n_images: int, max_soft: int) -> int:
    """Upper bound of the prompt tokens n_images images add: each is boi + up to max_soft soft tokens + eoi."""
    return n_images * (max_soft + 2)


# production caps (service/documents.py MAX_SCANNED_PAGES, service/vision.py MAX_FRAMES, MAX_TEXT_CHARS ~ 6k tokens)
MM_WORST_ROWS = {"scanned document, 4 pages": 4, "clip, 8 frames": 8}


def max_len_budget(max_soft: int, max_len: int, text_budget: int = 6000 + 1500) -> dict:
    """{shape: (worst-case tokens, fits max_len)} for the largest production image shapes plus a text budget
    (a 6k-token extraction cap + system prompt/question/answer). Used by preflight so a too-small --max-len is
    reported, never silently truncated (tokenize skips and logs over-length rows; it never cuts them)."""
    out = {}
    for name, n in MM_WORST_ROWS.items():
        t = worst_case_image_tokens(n, max_soft) + (1500 if n else 0)       # pictures replace the 6k text cap
        out[name] = (t, t <= max_len)
    out["text_document (6k-token cap)"] = (text_budget, text_budget <= max_len)
    return out


# ---- audio: the processor's own expansion, reproduced without torch

def audio_mel_frames(num_samples: int, frame_length: int = 320, hop: int = 160) -> int:
    """Valid mel frames of a clip (Gemma4AudioFeatureExtractor._extract_spectrogram): the waveform gets
    frame_length // 2 zeros on the left, a frame spans frame_length + 1 samples, and a frame is valid only when
    its last sample is real audio. Clips beyond 30 s are truncated to AUDIO_MAX_SAMPLES first."""
    n = min(int(num_samples), AUDIO_MAX_SAMPLES)
    return max(0, (n + frame_length // 2 - (frame_length + 1)) // hop + 1)


def audio_tokens_from_frames(frames: int, cap: int = 750) -> int:
    """Soft tokens for `frames` valid mel frames: two stride-2, kernel-3, pad-1 convolutions along time
    ((t + 2 - 3) // 2 + 1 each), capped at audio_seq_length (Gemma4Processor._compute_audio_num_tokens)."""
    t = frames
    if t <= 0:
        return 0
    for _ in range(2):
        t = (t + 2 - 3) // 2 + 1
    return min(t, cap)


def audio_soft_token_count(num_samples: int, frame_length: int = 320, hop: int = 160, cap: int = 750) -> int:
    return audio_tokens_from_frames(audio_mel_frames(num_samples, frame_length, hop), cap)


def audio_tokens_from_mask(mask: list) -> int:
    """Gemma4Processor.replace_audio_token verbatim: simulate the two stride-2 blocks on the (padded) frame
    mask and count what is left True."""
    t = len(mask)
    for _ in range(2):
        t_out = (t + 2 - 3) // 2 + 1
        mask = mask[::2][:t_out]
        t = len(mask)
    return int(sum(1 for m in mask if m))


def serving_audio_token_count(num_samples: int, rate: int = 16000, ms_per_token: int = 40, cap: int = 750) -> int:
    """What the bundled mlx_vlm Gemma4 processor (oMLX) puts in the prompt: ceil(duration_ms / 40), capped.
    Training uses the HF count above (it equals the number of encoder outputs); this one is only reported."""
    return min(math.ceil(num_samples / rate * 1000 / ms_per_token), cap)


def serving_extra_frames(num_samples: int, frame_length: int = 320, hop: int = 160, rate: int = 16000,
                         ms_per_token: int = 40, cap: int = 750, max_extra: int = 8):
    """For the --audio-serving-frac rows: how many padded mel frames to switch to valid so the mask-derived token
    count equals oMLX's serving count ceil(ms/40) (the HF count is sometimes one less). Returns (serving count,
    extra frames), or (HF count, 0) when they already agree or no small extra gets there. The extra frame is the
    first padded one, i.e. what serving's final token is built from."""
    frames = audio_mel_frames(num_samples, frame_length, hop)
    base = audio_tokens_from_frames(frames, cap)
    want = serving_audio_token_count(num_samples, rate, ms_per_token, cap)
    if want == base:
        return base, 0
    for e in range(1, max_extra + 1):
        if audio_tokens_from_frames(frames + e, cap) == want:
            return want, e
    return base, 0


def use_serving_shape(rid: str, frac: float, seed: int) -> bool:
    """Stable hash pick of the audio train rows trained with the serving token count."""
    if frac <= 0:
        return False
    h = int(hashlib.sha1(f"aud-serving:{seed}:{rid}".encode()).hexdigest()[:8], 16) / 0x1_0000_0000
    return h < frac


def missing_mm(weights: dict, train_counts: dict, dev_counts: dict) -> list[str]:
    """--require-mm: a modality with a positive weight needs train rows AND dev rows, else the run would
    silently be text-only (or have no regression metric). Returns human-readable problems."""
    out = []
    for m, w in weights.items():
        if w > 0 and not train_counts.get(m):
            out.append(f"{m}: --{m}-weight {w} but no {m} train rows")
        if w > 0 and not dev_counts.get(m):
            out.append(f"{m}: --{m}-weight {w} but no {m} dev rows (no regression metric)")
    return out


def audio_config(base: Path) -> dict:
    pc = json.loads((Path(base) / "processor_config.json").read_text())
    fe = pc["feature_extractor"]
    tc = json.loads((Path(base) / "tokenizer_config.json").read_text())
    return {"frame_length": fe["frame_length"], "hop": fe["hop_length"], "rate": fe["sampling_rate"],
            "cap": pc.get("audio_seq_length", 750), "ms_per_token": pc.get("audio_ms_per_token", 40),
            "boa": tc["boa_token"], "eoa": tc["eoa_token"], "audio": tc["audio_token"]}


def expand_audio_prompt(prompt: str, counts: list[int], boa: str, audio: str, eoa: str) -> str:
    """Replace the i-th `<|audio|>` with `<|audio>` + counts[i] x `<|audio|>` + `<audio|>`."""
    parts = prompt.split(audio)
    if len(parts) != len(counts) + 1:
        raise ValueError(f"{len(parts) - 1} audio placeholders in the prompt but {len(counts)} clips")
    out = [parts[0]]
    for n, rest in zip(counts, parts[1:]):
        out += [boa, audio * n, eoa, rest]
    return "".join(out)


def wav_samples(path) -> int:
    """Number of samples of a 16 kHz mono 16-bit WAV (stdlib; anything else is refused)."""
    import wave
    with wave.open(str(path), "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            sys.exit(f"{path}: need 16 kHz mono 16-bit WAV, got {w.getframerate()} Hz {w.getnchannels()} ch "
                     f"{8 * w.getsampwidth()}-bit")
        return w.getnframes()


def make_mm(tok, base: Path, data: Path, serving_frac: float = 0.0, seed: int = 0) -> dict:
    """Context for tokenizing image and audio rows: processor numbers, token strings, where the files live."""
    cfg = image_config(base)
    cfg["image_id"] = tok.convert_tokens_to_ids(cfg["image"])
    cfg["data"] = Path(data)
    au = audio_config(base)
    cfg["audio_cfg"] = au
    cfg["audio_id"] = tok.convert_tokens_to_ids(au["audio"])
    cfg["serving_frac"], cfg["seed"] = serving_frac, seed
    return cfg


def sequence(p: list[int], c: list[int], image_id: int | None = None, audio_id: int | None = None):
    """(ids, labels, mm_token_type_ids) for one row. Labels are -100 over the whole prompt (every expanded
    image/audio token included) and the completion's own ids after it, so the loss never depends on how many
    soft tokens the prompt grew by. mm_token_type_ids is 1 on image tokens, 3 on audio tokens, 0 elsewhere."""
    ids = list(p) + list(c)
    labels = [-100] * len(p) + list(c)
    types = [IMAGE_TYPE if (image_id is not None and t == image_id)
             else AUDIO_TYPE if (audio_id is not None and t == audio_id) else 0 for t in ids]
    return ids, labels, types


def row_types(p: list[int], c: list[int], mm: dict | None) -> list[int]:
    """mm_token_type_ids for a row from its info dict (images and audio each use their own id)."""
    if not mm:
        return [0] * (len(p) + len(c))
    return sequence(p, c, mm.get("image_id"), mm.get("audio_id") if mm.get("modality") == "audio" else None)[2]


def modality(mm: dict | None) -> str:
    return mm["modality"] if mm else "text"


def tokenize(tok, rows: list[dict], max_len: int, mm: dict | None = None,
             serving: bool = False) -> tuple[list[tuple], list[str]]:
    """(id, prompt ids, completion ids, image/audio info or None) per row. Image and audio rows need `mm`
    (make_mm); their prompt ids are the expanded prompt's, so len(p) already counts the soft tokens against
    --max-len. `serving` (train audio rows only) lets mm["serving_frac"] of the clips use oMLX's token count."""
    out, skipped = [], []
    for r in rows:
        info = None
        prompt = r["prompt"]
        if r.get("images") and r.get("audios"):
            sys.exit(f"{r['id']}: a row carries images or audio, not both")
        if r.get("audios"):
            if mm is None:
                sys.exit(f"{r['id']}: an audio row needs the processor context (make_mm)")
            au = mm["audio_cfg"]
            paths = [str(mm["data"] / i) for i in r["audios"]]
            counts = [audio_soft_token_count(wav_samples(pth), au["frame_length"], au["hop"], au["cap"])
                      for pth in paths]
            extra = [0] * len(paths)
            if serving and use_serving_shape(r["id"], mm.get("serving_frac", 0.0), mm.get("seed", 0)):
                got = [serving_extra_frames(wav_samples(pth), au["frame_length"], au["hop"], au["rate"],
                                            au["ms_per_token"], au["cap"]) for pth in paths]
                counts, extra = [g[0] for g in got], [g[1] for g in got]
            if not all(counts):
                sys.exit(f"{r['id']}: a clip is too short to make any audio token")
            prompt = expand_audio_prompt(prompt, counts, au["boa"], au["audio"], au["eoa"])
            info = {"modality": "audio", "wavs": paths, "audio_counts": counts, "extra_frames": extra, "audio_id": mm["audio_id"],
                    "image_id": None}
        elif not r.get("images") and r.get("kind") == "text_document":
            if mm is None:       # text_document row: no pixels, no image tokens, still a multimodal-path row
                sys.exit(f"{r['id']}: a vision-pool row needs the processor context (make_mm)")
            info = {"modality": "image", "paths": [], "counts": [], "image_id": mm["image_id"]}
        elif r.get("images"):
            if mm is None:
                sys.exit(f"{r['id']}: an image row needs the processor context (make_mm)")
            from PIL import Image
            paths = [str(mm["data"] / i) for i in r["images"]]
            counts = []
            for pth in paths:
                with Image.open(pth) as im:
                    w, h = im.size
                counts.append(soft_token_count(h, w, mm["patch"], mm["pool"], mm["max_soft"]))
            prompt = expand_image_prompt(prompt, counts, mm["boi"], mm["image"], mm["eoi"])
            info = {"modality": "image", "paths": paths, "counts": counts, "image_id": mm["image_id"]}
        p = tok(prompt, add_special_tokens=False).input_ids
        c = tok(r["completion"], add_special_tokens=False).input_ids
        if len(p) + len(c) > max_len:
            skipped.append(r["id"])
            continue
        out.append((r["id"], p, c, info))
    return out, skipped


def pool_per_epoch(n_text: int, n_pool: int, weight: float, total_weight: float) -> int:
    """Rows a multimodal pool adds to an epoch of n_text text rows so that it is `weight` of all the epoch's
    rows when the pools together are `total_weight` of them."""
    if not n_pool or weight <= 0:
        return 0
    return round(weight / (1 - total_weight) * n_text)


def vision_per_epoch(n_text: int, n_vis: int, weight: float) -> int:
    """Vision rows added to an epoch of n_text text rows so they are `weight` of all its rows."""
    return pool_per_epoch(n_text, n_vis, weight, weight)


def pool_sizes(n_text: int, pools: list[tuple[int, float]]) -> list[int]:
    """Rows each (n_rows, weight) pool contributes per epoch; pools that are empty or weightless count for 0."""
    live = [w for n, w in pools if n and w > 0]
    total = sum(live)
    if total >= 1:
        raise ValueError(f"multimodal weights add to {total}; they must stay below 1")
    return [pool_per_epoch(n_text, n, w, total) for n, w in pools]


def epoch_order_pools(n_text: int, pools: list[tuple[int, float]], seed: int, epoch: int) -> list[int]:
    """Row indices for one epoch over text rows [0, n_text) then each pool's rows in turn (vision, then audio).
    Every text row appears once, shuffled exactly as r2 did; each pool adds `weight` of the epoch (cycling a
    shuffled copy of its rows when it is smaller than that), then everything is mixed. With a single pool this
    is the original vision-only order, bit for bit."""
    order = list(range(n_text))
    random.Random(seed + epoch).shuffle(order)
    sizes = pool_sizes(n_text, pools)
    main = None
    start = n_text
    for i, ((n, _), k) in enumerate(zip(pools, sizes)):
        if k:
            if main is None:
                main = random.Random(seed + epoch + 1_000_003)
                rng = main
            else:
                rng = random.Random(seed + epoch + 1_000_003 + 7_919 * i)
            pool: list[int] = []
            while len(pool) < k:
                chunk = list(range(start, start + n))
                rng.shuffle(chunk)
                pool += chunk
            order += pool[:k]
        start += n
    if main is not None:
        main.shuffle(order)
    return order


def epoch_order(n_text: int, n_vis: int, weight: float, seed: int, epoch: int) -> list[int]:
    """Vision-only epoch order (r3 slice 1); kept as the reference the multi-pool order must reproduce."""
    return epoch_order_pools(n_text, [(n_vis, weight)], seed, epoch)


def local_batch(global_batch: int, world: int) -> int:
    """Rows each rank takes per optimizer step. global_batch must divide evenly (the effective-batch rule)."""
    if global_batch < 1 or world < 1 or global_batch % world:
        raise ValueError(f"--global-batch {global_batch} must be a positive multiple of the {world} GPUs")
    return global_batch // world


def shard_batch(lengths: list[int], world: int) -> list[list[int]]:
    """Deal the rows of one optimizer step to `world` ranks, balanced by token length: longest first, each to
    the least-loaded rank that still has room (ceil(n / world) rows per rank at most). Deterministic, so every
    rank computes the same split. Returns, per rank, the positions in `lengths` it processes."""
    cap = -(-len(lengths) // world) if lengths else 0
    loads, out = [0] * world, [[] for _ in range(world)]
    for i in sorted(range(len(lengths)), key=lambda j: (-lengths[j], j)):
        r = min((r for r in range(world) if len(out[r]) < cap), key=lambda r: (loads[r], r))
        out[r].append(i)
        loads[r] += lengths[i]
    return out


def is_anchor_text(rid: str, frac: float, seed: int) -> bool:
    """Whether a text row belongs to the --anchor-text-frac slice (a stable hash of seed + id)."""
    if frac <= 0:
        return False
    h = int(hashlib.sha1(f"{seed}:{rid}".encode()).hexdigest()[:8], 16) / 0x1_0000_0000
    return h < frac


def scored_window(seq, n_completion: int):
    """The slice of a logits sequence (one entry per input position, kept from the tail) that predicts the
    completion: with k = n_completion + 1 kept positions, drop the last (it predicts past the end). _logits uses
    exactly this slice for both CE and the KL anchor, and test_mm_render checks it against kl_positions."""
    return seq[-(n_completion + 1):-1]


def kl_positions(labels: list[int]) -> list[int]:
    """Logit positions the base-anchor KL is taken at: the predictors of the labelled (completion) tokens,
    i.e. position i - 1 for every label i that is not -100. Prompt tokens, image and audio soft tokens never
    count; this is the same set row_loss scores for CE."""
    return [i - 1 for i, l in enumerate(labels) if l != -100 and i > 0]


def kl_ref(base_logits: list[list[float]], tuned_logits: list[list[float]]) -> float:
    """Pure-python reference of the anchor: mean over positions of KL(softmax(base) || softmax(tuned)),
    sum_v p_b (log p_b - log p_t). The torch version (kl_base_to_tuned) is checked against it in smoke."""
    def logsm(row):
        m = max(row)
        z = math.log(sum(math.exp(x - m) for x in row)) + m
        return [x - z for x in row]
    tot = 0.0
    for b, t in zip(base_logits, tuned_logits):
        lb, lt = logsm(b), logsm(t)
        tot += sum(math.exp(x) * (x - y) for x, y in zip(lb, lt))
    return tot / len(base_logits)


def better_checkpoint(best, cur_loss: float, eligible: bool):
    """Pick the new `best` = (name-less record) among (loss, eligible). `best` is None or (loss, eligible).
    An eligible checkpoint always beats an ineligible one; within the same class the lower dev loss wins.
    Returns True when the current checkpoint replaces `best`."""
    if best is None:
        return True
    b_loss, b_ok = best
    if eligible != b_ok:
        return eligible
    return cur_loss < b_loss


def mm_ok(ev: dict, ref: dict | None, tol: float, kl_max: float = 0.0) -> bool:
    """No multimodal regression, as far as dev numbers can tell: dev_loss_vision / dev_loss_audio not worse than
    the step-0 reference by more than `tol` (nats per token), AND (when kl_max > 0) dev_kl_vision / dev_kl_audio
    not above kl_max. The CE test alone is weak (training on those targets lowers it almost by construction, and
    r2's regression was behavioural); the KL drift from the base is the informative number, and the external
    mmeval/gate run is still the only real check. Absent keys are not checked."""
    if kl_max > 0 and any(ev[k] > kl_max for k in ("dev_kl_vision", "dev_kl_audio") if k in ev):
        return False
    if not ref:
        return True
    return all(ev[k] <= ref[k] + tol for k in ("dev_loss_vision", "dev_loss_audio") if k in ev and k in ref)


def _load_render(data: Path):
    """Import render.py from the unpacked data dir (it travels in the zip next to parity.jsonl)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ask_render", Path(data) / "render.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _template_check(a, tok) -> int:
    """Per-model template sha, then render parity against the downloaded repo's OWN template.

    The pinned sha is that of the local oQ4e build. For Gemma it is also the HF bf16 repo's
    (checked), so a different sha fails. For Bolt the bf16 repo's template was never compared
    with the quant's, so a different sha only warns and the parity check decides. Parity is
    the binding gate for both: sample rows are re-rendered from their raw messages with the
    template the training run will actually load, and must equal the trained rows byte for byte."""
    rd = _load_render(a.data)
    want = rd.TEMPLATE_SHAS[a.tag]
    tpl = Path(a.base) / "chat_template.jinja"
    if tpl.is_file():
        got, where = hashlib.sha256(tpl.read_bytes()).hexdigest(), "chat_template.jinja"
    elif getattr(tok, "chat_template", None):
        got, where = hashlib.sha256(tok.chat_template.encode()).hexdigest(), "tokenizer_config.json (inline)"
    else:
        print("PREFLIGHT FAILED: the base has no chat template (no chat_template.jinja, none inline)")
        return 1
    print(f"template [{a.tag}] {where}: sha256 {got[:16]} (pinned for the local {a.tag} quant: {want[:16]})")
    problems = 0
    if got != want:
        if a.tag in STRICT_SHA_TAGS:
            print(f"PREFLIGHT FAILED: the {a.tag} base's template differs from the pinned one")
            problems += 1
        else:
            print(f"WARNING: the downloaded {a.tag} repo's template differs from the local quant's. "
                  f"Record sha {got} in the run notes; the parity check below decides whether the data fits it.")
    rows = [json.loads(l) for l in (Path(a.data) / "parity.jsonl").read_text().splitlines() if l.strip()]
    if not rows:
        print("PREFLIGHT FAILED: parity.jsonl is empty")
        return 1
    by_split = {s: {r["id"]: r for r in load_rows(Path(a.data), s)} for s in ("train", "dev")}
    done = [by_split[p["split"]][p["meta"]["id"]] for p in rows]
    bad = rd.parity_mismatches(tok, done, [(p["example"], p["meta"]) for p in rows])
    print(f"render parity vs the {a.tag} base's own template: {len(rows) - len(bad)}/{len(rows)} rows byte-identical")
    if bad:
        print(f"PREFLIGHT FAILED: {len(bad)} rows render differently under this template, e.g. {bad[:3]}")
        problems += 1
    return problems


def _arch_check(a) -> int:
    cfg = json.loads((Path(a.base) / "config.json").read_text())
    layers = (cfg.get("text_config") or cfg).get("num_hidden_layers")
    arch = cfg.get("architectures")
    print(f"config: architectures {arch}, language layers {layers}")
    if layers != N_LAYERS or arch != ["Gemma4ForConditionalGeneration"]:
        print(f"PREFLIGHT FAILED: expected Gemma4ForConditionalGeneration with {N_LAYERS} layers "
              "(the LoRA target regex, stop ids and the cache check assume it)")
        return 1
    return 0


def cmd_preflight(a) -> int:
    if not (Path(a.base) / "config.json").is_file():
        print("PREFLIGHT FAILED: the base has no config.json")
        return 1
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    if _arch_check(a) + _template_check(a, tok):
        print("PREFLIGHT FAILED: the base does not match the data - do not train")
        return 1
    problems = 0
    for split in ("train", "dev"):
        rows = load_rows(Path(a.data), split)
        toks, skipped = tokenize(tok, rows, a.max_len)
        kept = [r for r in rows if r["id"] not in set(skipped)]
        lens = sorted(len(p) + len(c) for _, p, c, _ in toks)
        bad_stop = [i for i, _, c, _ in toks if c[-1] not in STOP_IDS]
        # The ids must decode back to the exact text, or training would see a
        # sequence the model is never served.
        roundtrip = sum(tok.decode(p + c) != r["prompt"] + r["completion"] for (_, p, c, _), r in zip(toks, kept))
        problems += len(bad_stop) + roundtrip + len(skipped)
        labeled = sorted(len(c) for _, _, c, _ in toks)
        print(f"{split}: {len(rows)} rows, {len(skipped)} over --max-len {a.max_len}, tokens max {lens[-1]} "
              f"median {lens[len(lens) // 2]}, labeled total {sum(labeled)} (median {labeled[len(labeled) // 2]}), "
              f"thought {sum(r['completion'].startswith('<|channel>thought') or (r['prompt'].endswith('<|channel>thought' + chr(10)) and not r['completion'].startswith(chr(10) + '<channel|>')) for r in rows)} / "
              f"empty thought {sum(r['prompt'].endswith('<|channel>thought' + chr(10)) and r['completion'].startswith(chr(10) + '<channel|>') for r in rows)}, "
              f"bad stop token {len(bad_stop)}, decode mismatches {roundtrip}")
    problems += _vision_preflight(a, tok)
    problems += _audio_preflight(a, tok)
    if a.require_mm:
        count = lambda split: {m: len(load_optional_rows(Path(a.data), f"{m}_{split}")) for m in ("vision", "audio")}
        miss = missing_mm({"vision": a.vision_weight, "audio": a.audio_weight}, count("train"), count("dev"))
        for m_ in miss:
            print(f"--require-mm: {m_}")
        problems += len(miss)
    print("PREFLIGHT OK" if not problems else f"PREFLIGHT FAILED ({problems} problems) - do not train")
    return 1 if problems else 0


def _vision_preflight(a, tok) -> int:
    """Image rows: the offline expansion must equal what the real AutoProcessor makes (ids and soft-token
    counts), the label mask must cover exactly the completion, and no image token may sit in it."""
    rows = {s: load_optional_rows(Path(a.data), s) for s in ("vision_train", "vision_dev")}
    if not any(rows.values()):
        print("vision: no vision rows in this data (a text-only run)")
        return 0
    from transformers import AutoProcessor
    from PIL import Image
    proc = AutoProcessor.from_pretrained(a.base, local_files_only=True)
    mm = make_mm(tok, Path(a.base), Path(a.data))
    problems = 0
    for shape, (t, fits) in max_len_budget(mm["max_soft"], a.max_len).items():
        print(f"vision worst case {shape}: {t} tokens, {'fits' if fits else 'DOES NOT FIT'} --max-len {a.max_len}")
        problems += 0 if fits else 1
    for split, rs in rows.items():
        toks, skipped = tokenize(tok, rs, a.max_len, mm)
        kept = [r for r in rs if r["id"] not in set(skipped)]
        bad = []
        for (rid, p, c, info), r in zip(toks, kept):
            imgs = []
            for pth in info["paths"]:
                with Image.open(pth) as im:
                    imgs.append(im.convert("RGB"))
            enc = (proc(text=r["prompt"], images=imgs, add_special_tokens=False, return_tensors="pt") if imgs
                   else proc(text=r["prompt"], add_special_tokens=False, return_tensors="pt"))
            ids, labels, types = sequence(p, c, mm["image_id"])
            n_img = sum(types)
            ok = (enc["input_ids"][0].tolist() == p                       # offline expansion == the processor's
                  and (not imgs or [int(n) for n in proc.image_processor(images=imgs, return_tensors="pt")
                                    ["num_soft_tokens_per_image"]] == info["counts"])
                  and n_img == sum(info["counts"])                         # every soft token is an image id
                  and enc["mm_token_type_ids"][0].tolist() == types[:len(p)]
                  and sum(l != -100 for l in labels) == len(c) and labels[len(p):] == c
                  and mm["image_id"] not in c and c[-1] in STOP_IDS)
            if not ok:
                bad.append(rid)
        lens = sorted(len(p) + len(c) for _, p, c, _ in toks)
        print(f"{split}: {len(rs)} image rows, {len(skipped)} over --max-len {a.max_len}, tokens max "
              f"{lens[-1] if lens else 0}, soft tokens/image {sorted({n for t in toks for n in t[3]['counts']})[:6]}..., "
              f"processor-vs-offline mismatches {len(bad)}{(' e.g. ' + str(bad[:3])) if bad else ''}")
        problems += len(bad) + len(skipped)
    return problems


def _audio_preflight(a, tok) -> int:
    """Audio rows: the offline expansion must equal what the real AutoProcessor makes (ids, soft-token counts,
    mm_token_type_ids), the feature extractor's mask must give the same count, the label mask must cover exactly
    the completion, and no audio token may sit in it. Also reports how far oMLX's serving count (ceil(ms/40)) is
    from the training count."""
    rows = {s: load_optional_rows(Path(a.data), s) for s in ("audio_train", "audio_dev")}
    if not any(rows.values()):
        print("audio: no audio rows in this data (a run without the voice path)")
        return 0
    import numpy as np
    import wave
    from transformers import AutoProcessor
    proc = AutoProcessor.from_pretrained(a.base, local_files_only=True)
    mm = make_mm(tok, Path(a.base), Path(a.data))
    au = mm["audio_cfg"]
    problems = 0
    for split, rs in rows.items():
        toks, skipped = tokenize(tok, rs, a.max_len, mm)
        kept = [r for r in rs if r["id"] not in set(skipped)]
        bad, diffs = [], []
        for (rid, p, c, info), r in zip(toks, kept):
            waves = []
            for pth in info["wavs"]:
                with wave.open(pth, "rb") as w:
                    waves.append(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0)
            enc = proc(text=r["prompt"], audio=waves, sampling_rate=au["rate"], add_special_tokens=False,
                       return_tensors="pt")
            feats = proc.feature_extractor(waves, return_tensors="pt")
            from_mask = [audio_tokens_from_mask(m.tolist()) for m in feats["input_features_mask"]]
            ids, labels, types = sequence(p, c, None, mm["audio_id"])
            diffs += [serving_audio_token_count(len(w), au["rate"], au["ms_per_token"], au["cap"]) - n
                      for w, n in zip(waves, info["audio_counts"])]
            ok = (enc["input_ids"][0].tolist() == p                       # offline expansion == the processor's
                  and from_mask == info["audio_counts"]                     # mask arithmetic == the closed form
                  and sum(types) == AUDIO_TYPE * sum(info["audio_counts"])
                  and enc["mm_token_type_ids"][0].tolist() == types[:len(p)]
                  and sum(l != -100 for l in labels) == len(c) and labels[len(p):] == c
                  and mm["audio_id"] not in c and c[-1] in STOP_IDS
                  and "<|think|>" not in r["prompt"])                       # voice.py sends thinking OFF
            if not ok:
                bad.append(rid)
        n_srv = n_srv_bad = 0
        if split == "audio_train" and a.audio_serving_frac > 0:
            # serving-shape rows: extending the real extractor mask by extra_frames must give exactly the serving count
            smm = make_mm(tok, Path(a.base), Path(a.data), a.audio_serving_frac, a.seed)
            stoks, _ = tokenize(tok, rs, a.max_len, smm, serving=True)
            for (rid, p, c, info), (_, _, _, hf) in zip(stoks, toks):
                if not any(info["extra_frames"]):
                    continue
                n_srv += 1
                waves = []
                for pth in info["wavs"]:
                    with wave.open(pth, "rb") as w:
                        waves.append(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0)
                masks = [m.tolist() for m in proc.feature_extractor(waves, return_tensors="pt")["input_features_mask"]]
                got = []
                for m, e in zip(masks, info["extra_frames"]):
                    n = sum(1 for x in m if x)
                    m = m + [False] * max(0, n + e - len(m))
                    got.append(audio_tokens_from_mask([i < n + e for i in range(len(m))]))
                if got != info["audio_counts"] or p.count(mm["audio_id"]) != sum(info["audio_counts"]):
                    n_srv_bad += 1
            problems += n_srv_bad
        lens = sorted(len(p) + len(c) for _, p, c, _ in toks)
        print(f"{split}: {len(rs)} audio rows, {len(skipped)} over --max-len {a.max_len}, tokens max "
              f"{lens[-1] if lens else 0}, processor-vs-offline mismatches {len(bad)}{(' e.g. ' + str(bad[:3])) if bad else ''}; "
              f"serving(ceil(ms/40)) - HF count: min {min(diffs, default=0)} max {max(diffs, default=0)}, "
              f"{sum(d != 0 for d in diffs)}/{len(diffs)} differ"
              + (f"; serving-shape rows {n_srv} (mask-vs-count mismatches {n_srv_bad})" if split == "audio_train" else ""))
        problems += len(bad) + len(skipped)
    return problems


# ------------------------------------------------------------------ model

def _model_class():
    import transformers
    for name in ("AutoModelForMultimodalLM", "AutoModelForImageTextToText"):
        if hasattr(transformers, name):
            return getattr(transformers, name)
    sys.exit("this transformers has no multimodal auto class - need >= 5.10.1")


def load_model(a, *, for_training: bool = True):
    import torch
    from transformers import AutoTokenizer
    global _BASE
    _BASE = Path(a.base)
    tok = AutoTokenizer.from_pretrained(a.base, local_files_only=True)
    kw = {}
    if getattr(a, "qlora", False) and for_training:
        from transformers import BitsAndBytesConfig
        kw["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_quant_storage=torch.bfloat16)
    # transformers 5.22 refuses SDPA for Gemma4AudioModel; the frozen audio tower takes eager.
    attn = {"": "sdpa", "text_config": "sdpa", "vision_config": "sdpa", "audio_config": "eager"}
    model = _model_class().from_pretrained(a.base, dtype=torch.bfloat16, attn_implementation=attn,
                                           device_map={"": _DIST["local"]}, **kw)
    print(f"loaded {type(model).__name__} params={sum(p.numel() for p in model.parameters()) / 1e9:.2f}B "
          f"gpu_alloc={torch.cuda.memory_allocated() / 2**30:.1f}GiB qlora={bool(kw)} "
          f"rank={_DIST['rank']}/{_DIST['world']}")
    if not for_training:
        return tok, model
    from peft import LoraConfig, get_peft_model
    if kw:
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=False)
    cfg = LoraConfig(r=a.rank, lora_alpha=a.alpha, lora_dropout=0.05, target_modules=TARGET_RE, bias="none")
    torch.manual_seed(a.seed)        # every rank draws the same LoRA init (rank 0's is broadcast anyway)
    model = get_peft_model(model, cfg)
    torch.manual_seed(a.seed + 17 * _DIST["rank"])   # dropout differs per rank
    names = [n for n, _ in model.named_modules() if n.endswith(".lora_A")]
    lm = [n for n in names if "language_model" in n]
    q = [n for n in lm if ".self_attn.q_proj." in n]
    if len(q) != N_LAYERS or len(lm) != len(names):
        sys.exit(f"LoRA targeting is wrong: {len(q)} language q_proj (want {N_LAYERS}), "
                 f"{len(names) - len(lm)} adapters outside the language model")
    if is_main():
        model.print_trainable_parameters()
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    set_train(model)  # checkpointing only runs in training mode
    return tok, model


def set_train(model) -> None:
    """model.train() for the LoRA'd language model, with the frozen vision and audio towers kept in eval mode:
    their features must be exactly what inference sees (no dropout), and they hold no trainable weights."""
    model.train()
    for name, m in model.named_modules():
        if "vision_tower" in name or "audio_tower" in name:
            m.eval()


def sync_trainable(params, dev) -> None:
    """Broadcast rank 0's trainable tensors to every rank (identical start, also after --resume)."""
    if _DIST["world"] == 1:
        return
    import torch.distributed as dist
    for p in params:
        dist.broadcast(p.data, src=0)


def allreduce_grads(params, dev) -> None:
    """Sum the LoRA gradients over ranks in one flat buffer (rows are already divided by the global batch, so
    the sum is the mean over the whole step). A parameter with no gradient on this rank contributes zeros."""
    if _DIST["world"] == 1:
        return
    import torch
    import torch.distributed as dist
    flat = torch.cat([(p.grad if p.grad is not None else torch.zeros_like(p)).float().flatten() for p in params])
    dist.all_reduce(flat, op=dist.ReduceOp.SUM)
    off = 0
    for p in params:
        n = p.numel()
        g = flat[off:off + n].view_as(p).to(p.dtype)
        if p.grad is None:
            p.grad = g
        else:
            p.grad.copy_(g)
        off += n


_EXTRA: dict | None = None


_PROC = None
_BASE: Path | None = None       # the base dir, set by load_model; the processor loads from it


def _processor(base):
    """The AutoProcessor, loaded once (needs torch + PIL; the GPU box has both)."""
    global _PROC
    if _PROC is None:
        from transformers import AutoProcessor
        _PROC = AutoProcessor.from_pretrained(base, local_files_only=True)
    return _PROC


def image_inputs(mm: dict, dev) -> dict:
    """pixel_values and image_position_ids for one image row, from the AutoProcessor's image processor.
    Its soft-token counts must equal the ones the prompt was expanded with, or the run stops."""
    from PIL import Image
    if not mm["paths"]:                  # text_document row: nothing for the vision tower
        return {}
    imgs = []
    for pth in mm["paths"]:
        with Image.open(pth) as im:
            imgs.append(im.convert("RGB"))
    out = _processor(_BASE).image_processor(images=imgs, return_tensors="pt")
    got = [int(n) for n in out["num_soft_tokens_per_image"]]
    if got != mm["counts"]:
        sys.exit(f"{mm['paths']}: the processor makes {got} soft tokens but the prompt was expanded with "
                 f"{mm['counts']}; the image and its token count disagree")
    return {"pixel_values": out["pixel_values"].to(dev), "image_position_ids": out["image_position_ids"].to(dev)}


def audio_inputs(mm: dict, dev) -> dict:
    """input_features / input_features_mask for one audio row from the AutoProcessor's feature extractor.
    The mask must give the soft-token count the prompt was expanded with, or the run stops."""
    import numpy as np
    import torch
    waves = []
    for pth in mm["wavs"]:
        import wave
        with wave.open(pth, "rb") as w:
            waves.append(np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0)
    out = _processor(_BASE).feature_extractor(waves, return_tensors="pt")
    feats, mask = out["input_features"], out["input_features_mask"]
    extra = mm.get("extra_frames") or [0] * len(waves)
    if any(extra):                       # serving-shape row: switch on the first padded frame(s), like serving does
        mask = mask.clone()
        need = max(int(m.sum()) + e for m, e in zip(mask, extra))
        if need > mask.shape[1]:         # no padded room: append zero frames (log-mel padding) to features and mask
            pad = need - mask.shape[1]
            feats = torch.nn.functional.pad(feats, (0, 0, 0, pad))
            mask = torch.nn.functional.pad(mask, (0, pad), value=False)
        for m, e in zip(mask, extra):
            n = int(m.sum())
            m[n:n + e] = True
        out = {"input_features": feats, "input_features_mask": mask}
    got = [audio_tokens_from_mask(m.tolist()) for m in out["input_features_mask"]]
    if got != mm["audio_counts"]:
        sys.exit(f"{mm['wavs']}: the feature extractor makes {got} audio tokens but the prompt was expanded with "
                 f"{mm['audio_counts']}; the clip and its token count disagree")
    return {"input_features": out["input_features"].to(dev, torch.bfloat16),
            "input_features_mask": out["input_features_mask"].to(dev)}


def _forward_kwargs(model, n: int, dev) -> dict:
    """Text-only rows: zero modality masks if this transformers' forward asks for them."""
    global _EXTRA
    if _EXTRA is None:
        base = model.get_base_model() if hasattr(model, "get_base_model") else model
        params = inspect.signature(base.forward).parameters
        _EXTRA = {k for k in ("token_type_ids", "mm_token_type_ids") if k in params}
    import torch
    return {k: torch.zeros((1, n), dtype=torch.long, device=dev) for k in _EXTRA}


def _logits(model, p, c, dev, use_cache=False, mm=None):
    """(float logits [C, V] for the completion's predictors, whether the model honoured logits_to_keep)."""
    import torch
    ids = torch.tensor([p + c], device=dev)
    k = len(c) + 1                      # positions P-1 .. L-1; the last one predicts past the end
    extra = _forward_kwargs(model, ids.shape[1], dev)
    if mm:
        extra = {**extra, "mm_token_type_ids": torch.tensor([row_types(p, c, mm)], device=dev)}
        extra.update(audio_inputs(mm, dev) if mm.get("modality") == "audio" else image_inputs(mm, dev))
    out = model(input_ids=ids, logits_to_keep=k, use_cache=use_cache, **extra)
    return scored_window(out.logits[0], len(c)).float(), out.logits.shape[1] == k   # P-1 .. L-2 predict c[0..C-1]


def kl_base_to_tuned(base_logits, tuned_logits, chunk: int = 64):
    """Mean over positions of KL(softmax(base) || softmax(tuned)) = sum_v p_b (log p_b - log p_t).
    Chunked over positions and re-computed in backward (checkpoint), so the 262k-wide softmaxes of a long
    completion never sit in memory at once. `base_logits` carries no gradient."""
    import torch
    import torch.nn.functional as F
    from torch.utils.checkpoint import checkpoint

    def piece(b, t):
        lb, lt = F.log_softmax(b, -1), F.log_softmax(t, -1)
        return (lb.exp() * (lb - lt)).sum()

    n = tuned_logits.shape[0]
    total = tuned_logits.new_zeros(())
    for i in range(0, n, chunk):
        total = total + checkpoint(piece, base_logits[i:i + chunk], tuned_logits[i:i + chunk], use_reentrant=False)
    return total / n


def row_losses(model, p, c, dev, use_cache=False, mm=None, kl=False):
    """CE over the completion (tensor, with grad when training), token accuracy, and, when `kl`, the KL from the
    base model (the same weights with the LoRA off, no_grad) over the same positions. Returns a dict."""
    import torch
    import torch.nn.functional as F
    base = None
    if kl:
        with torch.no_grad(), model.disable_adapter():
            base = _logits(model, p, c, dev, use_cache, mm)[0]
    logits, kept = _logits(model, p, c, dev, use_cache, mm)
    target = torch.tensor(c, device=dev)
    ce = F.cross_entropy(logits, target)
    acc = (logits.argmax(-1) == target).float().mean()
    return {"ce": ce, "acc": acc, "kept": kept, "kl": kl_base_to_tuned(base, logits) if kl else None}


def row_loss(model, p, c, dev, use_cache=False, mm=None):
    """Mean CE over the completion, computing logits for its positions only. `mm` (image/audio rows) adds the
    modality inputs and marks the soft tokens; the scored positions are the same either way."""
    r = row_losses(model, p, c, dev, use_cache, mm)
    return r["ce"], r["acc"], r["kept"]


READ_ID_PREFIX = "pr_"      # make_vision_data.py's picture_read ids (the "pictures that lead to actions" READ call)


def is_read_row(rid: str) -> bool:
    return str(rid).startswith(READ_ID_PREFIX)


def evaluate(model, rows, dev, only_mm: bool = False) -> dict:
    """Dev metrics, sharded over ranks (every rank must call it). dev_loss is the mean CE over all rows scored;
    dev_loss_{text,vision,audio} split it by modality, dev_acc_* likewise, and image/audio rows also get
    dev_kl_* = drift from the base model (0 for an untouched adapter). `only_mm` skips the text rows."""
    import torch
    model.eval()
    kinds = ("text", "image", "audio")
    acc_ = {k: [0.0, 0.0, 0.0, 0.0] for k in kinds}      # sum CE, sum acc, sum KL, n
    exact = {k: 0.0 for k in kinds}
    read = [0.0, 0.0, 0.0]                               # picture_read rows (ids "pr_*"): sum CE, sum KL, n
    with torch.no_grad():
        todo = [r for r in rows if not (only_mm and not r[3])]
        for j, (rid, p, c, mm) in enumerate(todo):
            if j % _DIST["world"] != _DIST["rank"]:
                continue
            k = modality(mm)
            r = row_losses(model, p, c, dev, mm=mm, kl=bool(mm))
            if k == "image" and is_read_row(rid):
                read[0] += r["ce"].item()
                read[1] += r["kl"].item() if r["kl"] is not None else 0.0
                read[2] += 1
            a = acc_[k]
            a[0] += r["ce"].item()
            a[1] += r["acc"].item()
            a[2] += r["kl"].item() if r["kl"] is not None else 0.0
            a[3] += 1
            exact[k] += r["acc"].item() == 1.0
    if not todo and _DIST["world"] == 1:
        set_train(model)
        return {}
    flat = reduce_sum([x for k in kinds for x in acc_[k]] + [exact[k] for k in kinds], dev)
    for i, k in enumerate(kinds):
        acc_[k] = flat[4 * i:4 * i + 4]
        exact[k] = flat[12 + i]
    read = reduce_sum(read, dev)
    set_train(model)
    n_all = sum(acc_[k][3] for k in kinds)
    if not n_all:
        return {}
    res = {"dev_loss": round(sum(acc_[k][0] for k in kinds) / n_all, 4),
           "dev_token_acc": round(sum(acc_[k][1] for k in kinds) / n_all, 4),
           "dev_exact_rows": f"{int(sum(exact.values()))}/{int(n_all)}"}
    names = {"text": "text", "image": "vision", "audio": "audio"}
    if sum(acc_[k][3] > 0 for k in kinds) > 1:    # the split shows a modality moving (or not) by itself
        for k in kinds:
            if acc_[k][3]:
                res[f"dev_loss_{names[k]}"] = round(acc_[k][0] / acc_[k][3], 4)
    for k in ("image", "audio"):
        if acc_[k][3]:
            res[f"dev_loss_{names[k]}"] = round(acc_[k][0] / acc_[k][3], 4)
            res[f"dev_acc_{names[k]}"] = round(acc_[k][1] / acc_[k][3], 4)
            res[f"dev_exact_{names[k]}"] = f"{int(exact[k])}/{int(acc_[k][3])}"
            res[f"dev_kl_{names[k]}"] = round(acc_[k][2] / acc_[k][3], 5)
    if read[2]:      # the READ call's own numbers (already inside dev_loss_vision / dev_kl_vision)
        res["dev_loss_read"] = round(read[0] / read[2], 4)
        res["dev_kl_read"] = round(read[1] / read[2], 5)
    return res


def load_all(a, tok, *, vision: bool = True, audio: bool = True):
    """(train, dev, counts, mm context). Train = text rows, then vision rows, then audio rows; dev likewise.
    counts = {"text": n, "vision": n, "audio": n} of TRAIN rows. A zero weight drops that modality entirely."""
    train, skipped = tokenize(tok, load_rows(Path(a.data), "train"), a.max_len)
    dev, _ = tokenize(tok, load_rows(Path(a.data), "dev"), a.max_len)
    if skipped:
        print(f"skipping {len(skipped)} train rows over --max-len {a.max_len}")
    counts = {"text": len(train), "vision": 0, "audio": 0}
    counts_dev = {"text": len(dev), "vision": 0, "audio": 0}
    use = {"vision": vision and getattr(a, "vision_weight", 0) > 0,     # weight 0 = a run without that modality
           "audio": audio and getattr(a, "audio_weight", 0) > 0}
    raw = {m: (load_optional_rows(Path(a.data), f"{m}_train"), load_optional_rows(Path(a.data), f"{m}_dev"))
           if use[m] else ([], []) for m in ("vision", "audio")}
    mm = (make_mm(tok, Path(a.base), Path(a.data), getattr(a, "audio_serving_frac", 0.0), a.seed)
          if any(t or d for t, d in raw.values()) else None)
    for m in ("vision", "audio"):
        t, d = raw[m]
        if not (t or d):
            continue
        mt, mskip = tokenize(tok, t, a.max_len, mm, serving=(m == "audio"))
        md, _ = tokenize(tok, d, a.max_len, mm)
        counts_dev[m] = len(md)
        if m == "vision":
            n_read = sum(is_read_row(r[0]) for r in mt)
            a._read_rows = n_read
            print(f"vision pool: {len(mt)} train rows, {n_read} of them picture_read (the READ call; same mix, same base-anchor KL)")
        if mskip:
            print(f"skipping {len(mskip)} {m} rows over --max-len {a.max_len}: {mskip[:5]}")
        train, dev = train + mt, dev + md
        counts[m] = len(mt)
    a._dev_counts = counts_dev           # for --require-mm
    return train, dev, counts, mm


def step_loss(model, rid, p, c, mm, a, dev):
    """Training loss of one row: CE, plus --anchor-kl x KL(base || tuned) on image/audio rows and on the
    --anchor-text-frac slice of text rows. Returns (total tensor, row_losses dict, anchored?)."""
    anchored = a.anchor_kl > 0 and (mm is not None or is_anchor_text(rid, a.anchor_text_frac, a.seed))
    r = row_losses(model, p, c, dev, mm=mm, kl=anchored)
    total = r["ce"] + a.anchor_kl * r["kl"] if anchored else r["ce"]
    return total, r, anchored


def _kl_selfcheck() -> bool:
    """kl_base_to_tuned == the pure-python reference on a small random problem (CPU), gradient flows to tuned."""
    import torch
    g = torch.Generator().manual_seed(3)
    b, t = torch.randn(5, 11, generator=g), torch.randn(5, 11, generator=g, requires_grad=True)
    got = kl_base_to_tuned(b, t, chunk=2)
    want = kl_ref(b.tolist(), t.tolist())
    got.backward()
    zero = kl_base_to_tuned(b, b.clone().requires_grad_(True), chunk=2).item()
    print(f"  kl self-check: torch {got.item():.6f} vs python {want:.6f}; KL(b||b) = {zero:.2e}; "
          f"grad norm {t.grad.norm().item():.3f}")
    return abs(got.item() - want) < 1e-4 and abs(zero) < 1e-6 and t.grad.abs().sum().item() > 0


def cmd_smoke(a) -> int:
    """Cache parity, the anchor's base-vs-adapter-off check, then the N longest training rows (forward+backward
    each, one optimizer step) for peak memory and speed, then an overfit check on a few rows.

    Single GPU only (run it without torchrun, on the GPU you will train on). Writes OUT/smoke.json.
    Exit 0 = OK, 3 = memory tight, 4 = out of memory, 5 = KV-sharing bug, 6 = cannot overfit, 7 = the base
    anchor is broken (KL not ~0 at init, or still 0 after the adapter moved).
    The first row is excluded from the speed figure (CUDA warm-up)."""
    import torch
    if _DIST["world"] > 1:
        print("smoke runs on one GPU: start it with plain `python train.py smoke`, not torchrun")
        return 2
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if not _kl_selfcheck():
        print("SMOKE FAILED: the KL implementation disagrees with its python reference")
        return 7
    tok, model = load_model(a)
    allrows, alldev, counts, _ = load_all(a, tok)
    n_text = counts["text"]
    toks, mm_train = allrows[:n_text], allrows[n_text:]
    vis_train = [r for r in mm_train if r[3]["modality"] == "image"]
    aud_train = [r for r in mm_train if r[3]["modality"] == "audio"]
    dev = alldev
    # 1. KV-sharing parity: E4B's shared layers read K/V through the cache, and
    #    old transformers broke them when use_cache=False. Loss must not depend on it.
    model.eval()
    with torch.no_grad():
        # three text rows, plus one image row and one audio row so both multimodal paths are covered too
        for _, p, c, mm in (dev[:3] + [r for r in dev if r[3] and r[3]["modality"] == "image"][:1]
                            + [r for r in dev if r[3] and r[3]["modality"] == "audio"][:1]):
            on = row_loss(model, p, c, 0, True, mm=mm)[0].item()
            off = row_loss(model, p, c, 0, False, mm=mm)[0].item()
            print(f"  cache parity{' [' + mm['modality'] + ']' if mm else ''}: loss use_cache=True {on:.4f} vs False {off:.4f}")
            if abs(on - off) > 0.05 or off > 8:
                print("SMOKE FAILED: use_cache changes the loss (KV-sharing bug) - upgrade transformers")
                return 5
        # 2. the anchor: a fresh adapter (B = 0) must equal the base exactly, or disable_adapter is not
        #    doing what the KL assumes
        probe = [r for r in dev if r[3]][:2] or dev[:1]
        for rid, p, c, mm in probe:
            r = row_losses(model, p, c, 0, mm=mm, kl=True)
            print(f"  anchor at init [{modality(mm)}] {rid}: KL(base||tuned) = {r['kl'].item():.2e} (want ~0)")
            if r["kl"].item() > 1e-3:
                print("SMOKE FAILED: the untouched adapter differs from the base (disable_adapter broken?)")
                return 7
    set_train(model)
    toks.sort(key=lambda t: -(len(t[1]) + len(t[2])))
    worst = toks[: a.smoke_rows]
    # the longest image and audio rows too: the tower forward and the base pass add memory the text rows never see
    worst += sorted(vis_train, key=lambda t: -(len(t[1]) + len(t[2])))[:2]
    worst += sorted(aud_train, key=lambda t: -(len(t[1]) + len(t[2])))[:2]
    print(f"  smoke: {len(worst)} rows ({sum(bool(w[3]) for w in worst)} multimodal); "
          f"vision rows in train: {len(vis_train)}, audio: {len(aud_train)}, dev: "
          f"{sum(bool(r[3]) for r in dev)} multimodal of {len(dev)}")
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=a.lr, fused=True)
    torch.cuda.reset_peak_memory_stats()
    total = torch.cuda.get_device_properties(0).total_memory / 2**30
    res = {"max_len": a.max_len, "ok": False}
    t0, timed = time.time(), 0
    try:
        for i, (rid, p, c, mm) in enumerate(worst):
            if i == 1:
                torch.cuda.synchronize()
                t0 = time.time()
            loss, r, anchored = step_loss(model, rid, p, c, mm, a, 0)
            (loss / len(worst)).backward()
            if i >= 1:
                timed += len(p) + len(c)
            res["logits_to_keep"] = r["kept"]
            print(f"  row {i + 1}/{len(worst)} {rid}{' [' + mm['modality'] + ']' if mm else ''}: {len(p) + len(c)} tokens, "
                  f"ce {r['ce'].item():.3f}{', kl %.4f' % r['kl'].item() if anchored else ''}, "
                  f"peak {torch.cuda.max_memory_allocated() / 2**30:.1f}GiB"
                  + ("" if r["kept"] else "  (WARNING: model ignored logits_to_keep - full logits computed)"))
        opt.step()
        torch.cuda.synchronize()
    except torch.cuda.OutOfMemoryError:
        res.update(reason="oom", peak_gib=round(torch.cuda.max_memory_allocated() / 2**30, 1))
        (out / "smoke.json").write_text(json.dumps(res, indent=2))
        print(f"SMOKE: CUDA OUT OF MEMORY at --max-len {a.max_len}")
        return 4
    dt = time.time() - t0
    peak, reserved = torch.cuda.max_memory_allocated() / 2**30, torch.cuda.max_memory_reserved() / 2**30
    # 3. Overfit a few short rows (text, image, audio): the loss must collapse, or the pipeline is broken
    #    (masking, cache, targeting) and the real run would waste the GPU.
    tiny = sorted(toks, key=lambda t: len(t[1]) + len(t[2]))[:2]
    tiny = tiny + sorted(vis_train, key=lambda t: len(t[1]) + len(t[2]))[:1]    # an image row must also learn
    tiny = tiny + sorted(aud_train, key=lambda t: len(t[1]) + len(t[2]))[:1]    # and an audio row
    first = last = None
    for i in range(a.overfit_steps):
        opt.zero_grad(set_to_none=True)
        tot = 0.0
        for rid, p, c, mm in tiny:
            loss, r, _ = step_loss(model, rid, p, c, mm, a, 0)
            (loss / len(tiny)).backward()
            tot += r["ce"].item() / len(tiny)
        opt.step()
        first = tot if first is None else first
        last = tot
    print(f"  overfit check: CE {first:.3f} -> {last:.3f} over {a.overfit_steps} steps on {len(tiny)} rows")
    res["overfit"] = [round(first, 3), round(last, 3)]
    if not last < first * 0.5:
        (out / "smoke.json").write_text(json.dumps(res, indent=2))
        print("SMOKE FAILED: the model cannot overfit a few rows - check masking and targeting before training")
        return 6
    # 4. after the adapter has moved, the KL to the base must be visible on the text row (no anchor there,
    #    so it is free to drift) - proof that the 'base' pass really ran with the adapter off
    model.eval()
    with torch.no_grad():
        rid, p, c, mm = tiny[0]
        drift = row_losses(model, p, c, 0, mm=mm, kl=True)["kl"].item()
    set_train(model)
    print(f"  anchor after overfit [{modality(mm)}] {rid}: KL(base||tuned) = {drift:.4f} (want > 0)")
    if drift < 1e-4:
        print("SMOKE FAILED: the adapter moved but the KL to the base stayed 0 (the base pass used the adapter)")
        return 7
    res["kl_after_overfit"] = round(drift, 4)
    tok_s = timed / dt
    sizes = pool_sizes(len(toks), [(len(vis_train), a.vision_weight), (len(aud_train), a.audio_weight)])
    n_extra = sum(sizes)
    mean_mm = (sum(len(p) + len(c) for _, p, c, _ in mm_train) / len(mm_train)) if mm_train else 0
    all_tok = sum(len(p) + len(c) for _, p, c, _ in toks) + n_extra * mean_mm
    dev_tok = sum(len(p) + len(c) for _, p, c, _ in dev)
    epoch_min = all_tok / tok_s / 60
    steps = math.ceil((len(toks) + n_extra) / a.global_batch)
    eval_min = dev_tok / tok_s / 60 / 3 * max(1, steps // a.eval_every)   # forward-only ≈ a third of a train token
    headroom = total - reserved
    res.update(ok=headroom >= 4, reason="" if headroom >= 4 else "tight", tok_s=round(tok_s),
               peak_gib=round(peak, 1), reserved_gib=round(reserved, 1), total_gib=round(total, 1),
               epoch_min=round(epoch_min, 1), eval_min_per_epoch=round(eval_min, 1), rows=len(toks),
               vision_rows=len(vis_train), audio_rows=len(aud_train), vision_rows_per_epoch=sizes[0],
               audio_rows_per_epoch=sizes[1])
    (out / "smoke.json").write_text(json.dumps(res, indent=2))
    print(f"SMOKE: {len(worst)} longest rows, {tok_s:.0f} tok/s; peak allocated {peak:.1f}GiB, reserved "
          f"{reserved:.1f}GiB of {total:.0f}GiB; one epoch ~{epoch_min + eval_min:.0f} min incl. evals on ONE GPU "
          f"({a.epochs} epochs ~{(epoch_min + eval_min) * a.epochs:.0f} min) over {len(toks)} rows "
          f"+ {sizes[0]} vision + {sizes[1]} audio rows/epoch")
    print("MEMORY OK" if headroom >= 4 else f"MEMORY TIGHT: {headroom:.1f}GiB headroom - lower --max-len")
    return 0 if headroom >= 4 else 3


# ------------------------------------------------------------------ train

def cmd_train(a) -> int:
    import torch
    from transformers import get_cosine_schedule_with_warmup
    init_dist()
    rank, world, dev = _DIST["rank"], _DIST["world"], _DIST["local"]
    try:
        lb = local_batch(a.global_batch, world)
    except ValueError as exc:
        sys.exit(str(exc))
    out = Path(a.out)
    if is_main():
        out.mkdir(parents=True, exist_ok=True)
    barrier()
    logf = out / "train.log.jsonl"
    tok, model = load_model(a)
    train, dev_rows, counts, _ = load_all(a, tok)
    n_text, n_vis, n_aud = counts["text"], counts["vision"], counts["audio"]
    if a.require_mm:
        miss = missing_mm({"vision": a.vision_weight, "audio": a.audio_weight}, counts, a._dev_counts)
        if miss:
            sys.exit("--require-mm: " + "; ".join(miss) + ". Build the zip with --vision-rows/--audio-rows, "
                     "or pass --no-require-mm (or weight 0) for a deliberate text-only run")
    pools = [(n_vis, a.vision_weight), (n_aud, a.audio_weight)]
    sizes = pool_sizes(n_text, pools)
    skipped = []
    params = [p for p in model.parameters() if p.requires_grad]
    sync_trainable(params, dev)
    opt = torch.optim.AdamW(params, lr=a.lr, weight_decay=0.0, fused=True)
    per_epoch = n_text + sum(sizes)
    steps_per_epoch = math.ceil(per_epoch / a.global_batch)
    if is_main():
        for name, n, k in (("vision", n_vis, sizes[0]), ("audio", n_aud, sizes[1])):
            if n:
                print(f"{name}: {n} rows in the pool, {k} drawn per epoch ({k / per_epoch:.1%} of {per_epoch} rows), "
                      f"each used about {k / n:.1f}x per epoch")
        print(f"world {world} x {lb} rows = global batch {a.global_batch}; {steps_per_epoch} steps/epoch")
    total = steps_per_epoch * a.epochs
    sched = get_cosine_schedule_with_warmup(opt, max(1, round(0.03 * total)), total)
    state = {"step": 0, "best": None, "ref": None}
    if a.resume:
        from peft import set_peft_model_state_dict
        from safetensors.torch import load_file
        r = Path(a.resume)
        set_peft_model_state_dict(model, load_file(str(r / "adapter_model.safetensors")))
        ck = torch.load(r / "trainer_state.pt", map_location="cpu", weights_only=False)
        opt.load_state_dict(ck["opt"])
        sched.load_state_dict(ck["sched"])
        state = {"ref": None, **ck["state"]}
        print(f"resumed from {r} at optimizer step {state['step']}")
    else:
        ev0 = evaluate(model, dev_rows, dev)     # the untuned reference every later checkpoint is held to
        state["ref"] = {k: ev0[k] for k in ("dev_loss_vision", "dev_loss_audio") if k in ev0}
        log(logf, event="eval", step=0, **ev0, best=None)
    log(logf, event="start", rows=n_text, vision_pool=n_vis, read_rows=getattr(a, "_read_rows", 0), audio_pool=n_aud, rows_per_epoch=per_epoch,
        vision_weight=a.vision_weight, audio_weight=a.audio_weight, dev=len(dev_rows), epochs=a.epochs,
        global_batch=a.global_batch, world=world, local_batch=lb, steps=total, lr=a.lr, rank=a.rank, alpha=a.alpha,
        max_len=a.max_len, skipped=len(skipped), anchor_kl=a.anchor_kl, anchor_text_frac=a.anchor_text_frac,
        mm_tol=a.mm_tol, mm_kl_max=a.mm_kl_max, audio_serving_frac=a.audio_serving_frac, save_every=a.save_every, ref=state["ref"])

    def save(tag: str) -> Path | None:
        d = out / tag
        if is_main():
            model.save_pretrained(d)
            torch.save({"opt": opt.state_dict(), "sched": sched.state_dict(), "state": state}, d / "trainer_state.pt")
        return d

    def snapshot(ev: dict) -> None:
        """Adapter-only snapshot at OUT/ckpt/step-NNNNN for the GPU multimodal eval to pick from."""
        if is_main():
            d = out / "ckpt" / f"step-{state['step']:05d}"
            model.save_pretrained(d)
            with (out / "ckpt" / "index.jsonl").open("a") as fh:
                fh.write(json.dumps({"step": state["step"], "dir": d.name, **ev}) + "\n")

    def checkpoint_and_eval(event: str) -> None:
        ev = evaluate(model, dev_rows, dev)
        ok = mm_ok(ev, state.get("ref"), a.mm_tol, a.mm_kl_max)
        if is_main():
            d = save(f"step-{state['step']:05d}")
            prev = None if state["best"] is None else (state["best"][1], state["best"][2] if len(state["best"]) > 2 else True)
            if better_checkpoint(prev, ev["dev_loss"], ok):
                state["best"] = (d.name, ev["dev_loss"], ok)
                shutil.rmtree(out / "best", ignore_errors=True)
                shutil.copytree(d, out / "best")
            for old in sorted(out.glob("step-*"))[:-3]:
                shutil.rmtree(old, ignore_errors=True)
            if a.save_every and state["step"] % a.save_every == 0:
                snapshot({**ev, "mm_ok": ok})
        log(logf, event=event, step=state["step"], of=total, **ev, mm_ok=ok, best=state["best"])
        barrier()

    set_train(model)
    t0, tok_count, step0 = time.time(), 0, state["step"]
    while state["step"] < total:
        epoch = state["step"] // steps_per_epoch
        order = epoch_order_pools(n_text, pools, a.seed, epoch)
        start = (state["step"] % steps_per_epoch) * a.global_batch
        stop = False
        for b in range(start, len(order), a.global_batch):
            gidx = order[b:b + a.global_batch]
            lens = [len(train[i][1]) + len(train[i][2]) for i in gidx]
            mine = shard_batch(lens, world)[rank]
            st = {"ce": 0.0, "acc": 0.0, "rows": 0.0, "kl": 0.0, "kl_rows": 0.0, "tok": 0.0, "vis": 0.0, "aud": 0.0}
            for j in mine:
                rid, p, c, mm = train[gidx[j]]
                try:
                    loss, r, anchored = step_loss(model, rid, p, c, mm, a, dev)
                except torch.cuda.OutOfMemoryError:
                    save("oom-recovery")
                    sys.exit(f"CUDA OOM on {rid} ({len(p) + len(c)} tokens) on rank {rank}. Rank 0 saved "
                             f"{out}/oom-recovery (if this was another rank, resume from the last step-* dir); "
                             f"rerun with a lower --max-len and --resume")
                (loss / len(gidx)).backward()       # divided by the GLOBAL batch: the all-reduce sums the mean
                st["ce"] += r["ce"].item()
                st["acc"] += r["acc"].item()
                st["rows"] += 1
                if anchored:
                    st["kl"] += r["kl"].item()
                    st["kl_rows"] += 1
                st["tok"] += len(p) + len(c)
                st["vis"] += modality(mm) == "image"
                st["aud"] += modality(mm) == "audio"
            allreduce_grads(params, dev)
            gnorm = torch.nn.utils.clip_grad_norm_(params, 0.3).item()
            opt.step()
            sched.step()
            opt.zero_grad(set_to_none=True)
            state["step"] += 1
            late = float(bool(a.stop_at) and time.time() >= a.stop_at)
            tot = dict(zip(("ce", "acc", "rows", "kl", "kl_rows", "tok", "vis", "aud", "late"),
                           reduce_sum([st["ce"], st["acc"], st["rows"], st["kl"], st["kl_rows"], st["tok"],
                                       st["vis"], st["aud"], late], dev)))
            tok_count += tot["tok"]
            el = time.time() - t0
            rows = max(1.0, tot["rows"])
            kl_mean = tot["kl"] / tot["kl_rows"] if tot["kl_rows"] else 0.0
            log(logf, step=state["step"], of=total, epoch=epoch + 1, loss=round(tot["ce"] / rows, 4),
                kl=round(kl_mean, 5), loss_total=round((tot["ce"] + a.anchor_kl * tot["kl"]) / rows, 4),
                n_anchor=int(tot["kl_rows"]), n_vision=int(tot["vis"]), n_audio=int(tot["aud"]),
                token_acc=round(tot["acc"] / rows, 4), grad_norm=round(gnorm, 3),
                lr=f"{sched.get_last_lr()[0]:.2e}", tok_s=round(tok_count / el),
                s_per_step=round(el / max(1, state["step"] - step0), 1),
                peak_gib=round(torch.cuda.max_memory_allocated() / 2**30, 1),
                eta_min=round(el / max(1, state["step"] - step0) * (total - state["step"]) / 60))
            evaluated = state["step"] % a.eval_every == 0 or state["step"] == total
            if evaluated:
                checkpoint_and_eval("eval")
            elif a.save_every and state["step"] % a.save_every == 0:
                ev = evaluate(model, dev_rows, dev, only_mm=True)      # cheap: multimodal dev rows only
                snapshot({**ev, "mm_only": True})
                log(logf, event="ckpt", step=state["step"], **ev)
                barrier()
            if state["step"] >= total:
                break
            if tot["late"] > 0:
                checkpoint_and_eval("stopped_at_deadline")
                stop = True
                break
        if stop:
            break
    save("final")
    log(logf, event="done", best=state["best"], minutes=round((time.time() - t0) / 60, 1))
    barrier()
    if world > 1:
        import torch.distributed as dist
        dist.destroy_process_group()
    return 0


def _restore_kv_shared(base: Path, out: Path, missing: set[str]) -> None:
    """Copy the base's k_proj/v_proj/k_norm of E4B's KV-shared layers into the merged folder.

    Those layers reuse an earlier layer's KV, so transformers (5.22) never builds these weights and
    save_pretrained drops them. No LoRA reaches them, so the base values are exact; restoring them
    keeps the merged folder's tensor layout equal to the base's for the like-for-like quantisation."""
    if not missing:
        return
    cfg = json.load(open(base / "config.json"))
    tc = cfg.get("text_config", cfg)
    first = tc["num_hidden_layers"] - tc.get("num_kv_shared_layers", 0)
    pat = re.compile(r"\.layers\.(\d+)\.self_attn\.(k_proj|v_proj|k_norm)\.weight$")
    bad = [k for k in missing if not ((m := pat.search(k)) and int(m.group(1)) >= first)]
    if bad:
        return  # not the KV-sharing gap; the caller's name check fails loudly
    from safetensors import safe_open
    from safetensors.torch import save_file
    idx = base / "model.safetensors.index.json"
    files = sorted(set(json.load(open(idx))["weight_map"].values())) if idx.exists() else \
        [f.name for f in base.glob("*.safetensors")]
    tensors = {}
    for f in files:
        with safe_open(str(base / f), "pt") as fh:
            for k in missing & set(fh.keys()):
                tensors[k] = fh.get_tensor(k).contiguous()
    shard = "model-kv-shared.safetensors"
    save_file(tensors, str(out / shard), metadata={"format": "pt"})
    oidx = out / "model.safetensors.index.json"
    data = json.load(open(oidx))
    data["weight_map"].update({k: shard for k in tensors})
    data.setdefault("metadata", {})["total_size"] = data.get("metadata", {}).get("total_size", 0) + \
        sum(t.numel() * t.element_size() for t in tensors.values())
    oidx.write_text(json.dumps(data, indent=2) + "\n")
    print(f"restored {len(tensors)} KV-shared-layer tensors (layers {first}+) from the base into {shard}")


def _base_keys(base: Path) -> set[str]:
    idx = base / "model.safetensors.index.json"
    if idx.exists():
        return set(json.load(open(idx))["weight_map"])
    from safetensors import safe_open
    keys = set()
    for f in base.glob("*.safetensors"):
        with safe_open(str(f), "pt") as fh:
            keys |= set(fh.keys())
    return keys


def cmd_merge(a) -> int:
    """Fold the adapter into the bf16 base and save the full multimodal model.

    Refuses to save when the adapter has no effect, when merging changes the
    output, or when the saved tensor names differ from the base's (a renamed or
    dropped vision/audio tensor would be silently re-initialized by a loader)."""
    import torch
    from peft import PeftModel
    tok, base = load_model(a, for_training=False)
    model = PeftModel.from_pretrained(base, a.adapter)
    model.eval()
    _, p, c, _ = tokenize(tok, load_rows(Path(a.data), "dev"), a.max_len)[0][0]
    with torch.no_grad():
        with model.disable_adapter():
            plain = row_loss(model, p, c, 0)[0].item()
        before = row_loss(model, p, c, 0)[0].item()
    merged = model.merge_and_unload()
    with torch.no_grad():
        after = row_loss(merged, p, c, 0)[0].item()
    print(f"merge check: dev-row loss base {plain:.4f}, adapter {before:.4f}, merged {after:.4f}")
    if abs(before - plain) < 1e-4:
        sys.exit("the adapter does not change the model - it did not load onto these modules")
    if abs(after - before) > 0.02:
        sys.exit("merge changed the model's output - not saving")
    out = Path(a.out)
    merged.save_pretrained(out, max_shard_size="5GB", safe_serialization=True)
    try:
        from transformers import AutoProcessor
        AutoProcessor.from_pretrained(a.base, local_files_only=True).save_pretrained(out)
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: processor not saved ({exc}); copying its files")
    tok.save_pretrained(out)
    for f in ("chat_template.jinja", "generation_config.json", "processor_config.json", "tokenizer_config.json"):
        if (Path(a.base) / f).exists():
            shutil.copy(Path(a.base) / f, out / f)
    want, got = _base_keys(Path(a.base)), _base_keys(out)
    _restore_kv_shared(Path(a.base), out, want - got)
    got = _base_keys(out)
    if want != got:
        print(f"missing {sorted(want - got)[:5]} … extra {sorted(got - want)[:5]} …")
        sys.exit(f"merged tensor names differ from the base ({len(want - got)} missing, {len(got - want)} extra)")
    print(f"merged model saved to {out}: {len(got)} tensors, names identical to the base")
    return 0


def cmd_sample(a) -> int:
    """Greedy-decode a few dev prompts with the adapter, next to the target."""
    import torch
    from peft import PeftModel
    tok, base = load_model(a, for_training=False)
    model = PeftModel.from_pretrained(base, a.adapter) if a.adapter else base
    model.eval()
    rows = load_rows(Path(a.data), "dev")
    random.Random(0).shuffle(rows)
    for r in rows[: a.n]:
        ids = tok(r["prompt"], add_special_tokens=False, return_tensors="pt").input_ids.to(0)
        with torch.no_grad():
            gen = model.generate(input_ids=ids, max_new_tokens=200, do_sample=False,
                                 eos_token_id=sorted(STOP_IDS | {1}), **_forward_kwargs(model, ids.shape[1], 0))
        text = tok.decode(gen[0, ids.shape[1]:])
        print(f"== {r['id']} ({r['family']}, {r['kind']})\n  want: {r['completion'][:300]!r}\n  got:  {text[:300]!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preflight", "smoke", "train", "merge", "sample"])
    ap.add_argument("--data", help="dir with train.jsonl and dev.jsonl (from ask-lora-data.zip)")
    ap.add_argument("--tag", required=True, choices=["gemma", "bolt"],
                    help="which model this run trains (run.sh passes it); selects the pinned template sha")
    ap.add_argument("--base", required=True, help="bf16 snapshot dir of that model (Gemma 4 E4B-it or Nemotron 3.5 Bolt E4B)")
    ap.add_argument("--out")
    ap.add_argument("--adapter", help="merge/sample: adapter dir (e.g. OUT/best)")
    ap.add_argument("--resume", help="train: a step-NNNNN or oom-recovery dir")
    ap.add_argument("--max-len", type=int, default=10240, help="rows longer than this are skipped (longest is ~9.5k)")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--global-batch", "--accum", dest="global_batch", type=int, default=8,
                    help="rows per optimizer step across ALL GPUs (micro batch is always 1); each of N GPUs takes "
                         "global-batch / N, so 1, 2, 4 or 8 GPUs train the same recipe. A multiple of the GPU count. "
                         "If you raise it, scale --lr by about sqrt(new / 8)")
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=16)
    ap.add_argument("--eval-every", type=int, default=26, help="optimizer steps between dev evals (~4 per epoch)")
    ap.add_argument("--qlora", action="store_true", help="4-bit NF4 base (for a 24 GB GPU); merge stays bf16")
    ap.add_argument("--overfit-steps", type=int, default=15, help="smoke: steps for the 2-row overfit check")
    ap.add_argument("--smoke-rows", type=int, default=8)
    ap.add_argument("--vision-weight", type=float, default=VISION_WEIGHT,
                    help="share of training rows that carry images (default 0.12 = about 12%% of the steps); "
                         "0 trains on text only even if the zip has vision rows")
    ap.add_argument("--audio-weight", type=float, default=AUDIO_WEIGHT,
                    help="share of training rows that carry audio clips (default 0.06 = about 6%% of the steps); "
                         "0 trains without them even if the zip has audio rows")
    ap.add_argument("--anchor-kl", type=float, default=ANCHOR_KL,
                    help="lambda of KL(base || tuned) added on image/audio rows (and the --anchor-text-frac slice); "
                         "0 switches the anchor off (default 0.5)")
    ap.add_argument("--anchor-text-frac", type=float, default=0.0,
                    help="share of TEXT rows that also carry the KL anchor (default 0 = none; they are the rows that "
                         "are meant to change behaviour, so keep this small)")
    ap.add_argument("--mm-tol", type=float, default=MM_TOL,
                    help="a checkpoint whose dev_loss_vision/audio is worse than the untuned model's by more than this "
                         "is not eligible for `best` while an eligible one exists")
    ap.add_argument("--mm-kl-max", type=float, default=MM_KL_MAX,
                    help="a checkpoint whose dev_kl_vision/dev_kl_audio exceeds this (nats/token) is not eligible for "
                         "best (0 = off). Uncalibrated until the first GPU run; read the logged dev_kl_* values")
    ap.add_argument("--audio-serving-frac", type=float, default=AUDIO_SERVING_FRAC,
                    help="share of audio train rows expanded with oMLX's serving token count ceil(ms/40) instead of "
                         "the HF mask count (they differ by +1 on ~24%% of clips); 0 = HF count only")
    ap.add_argument("--require-mm", action=argparse.BooleanOptionalAction, default=True,
                    help="fail preflight/train when a modality has weight > 0 but no train or dev rows "
                         "(default on; --no-require-mm for a deliberate text-only run)")
    ap.add_argument("--save-every", type=int, default=50,
                    help="train: optimizer steps between adapter-only snapshots in OUT/ckpt (0 = off; never pruned)")
    ap.add_argument("--stop-at", type=float, default=0, help="train: unix time to stop at (saves + evals first)")
    ap.add_argument("--n", type=int, default=8, help="sample: how many dev prompts")
    ap.add_argument("--seed", type=int, default=20261029)
    a = ap.parse_args()
    if not 0 <= a.vision_weight < 1:
        ap.error("--vision-weight must be in [0, 1)")
    if not 0 <= a.audio_weight < 1 or a.vision_weight + a.audio_weight >= 1:
        ap.error("--audio-weight must be in [0, 1) and --vision-weight + --audio-weight below 1")
    if a.anchor_kl < 0 or not 0 <= a.anchor_text_frac <= 1:
        ap.error("--anchor-kl must be >= 0 and --anchor-text-frac in [0, 1]")
    return {"preflight": cmd_preflight, "smoke": cmd_smoke, "train": cmd_train, "merge": cmd_merge,
            "sample": cmd_sample}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
