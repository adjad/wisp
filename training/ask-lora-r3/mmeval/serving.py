"""oMLX's audio token count, reproduced without torch, for evaluating a clip in the shape the app serves.

Training (brev/train.py --audio-serving-frac) expands a share of audio train rows with the serving token count
ceil(duration_ms / 40) instead of the Hugging Face processor's count (which is sometimes one less), by switching
the first padded mel frame(s) on in the feature mask. The functions below are copies of train.py's, unchanged;
tests/test_scoring.py::test_serving_matches_train diffs them against train.py on the Mac. eval_mm.py uses
`serving_plan` + `apply_serving` so a dev clip can be scored in both shapes.
"""
from __future__ import annotations

import hashlib
import math

AUDIO_MAX_SAMPLES = 480_000          # 30 s at 16 kHz
SERVING_FRAC = 0.25                  # brev/train.py AUDIO_SERVING_FRAC
SERVING_SUFFIX = "~srv"              # id suffix of the serving-shape copy of a dev row


def audio_mel_frames(num_samples: int, frame_length: int = 320, hop: int = 160) -> int:
    n = min(int(num_samples), AUDIO_MAX_SAMPLES)
    return max(0, (n + frame_length // 2 - (frame_length + 1)) // hop + 1)


def audio_tokens_from_frames(frames: int, cap: int = 750) -> int:
    t = frames
    if t <= 0:
        return 0
    for _ in range(2):
        t = (t + 2 - 3) // 2 + 1
    return min(t, cap)


def audio_tokens_from_mask(mask: list) -> int:
    t = len(mask)
    for _ in range(2):
        t_out = (t + 2 - 3) // 2 + 1
        mask = mask[::2][:t_out]
        t = len(mask)
    return int(sum(1 for m in mask if m))


def serving_audio_token_count(num_samples: int, rate: int = 16000, ms_per_token: int = 40, cap: int = 750) -> int:
    return min(math.ceil(num_samples / rate * 1000 / ms_per_token), cap)


def serving_extra_frames(num_samples: int, frame_length: int = 320, hop: int = 160, rate: int = 16000,
                         ms_per_token: int = 40, cap: int = 750, max_extra: int = 8):
    """(serving token count, padded frames to switch on), or (HF count, 0) when they already agree."""
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
    if frac <= 0:
        return False
    h = int(hashlib.sha1(f"aud-serving:{seed}:{rid}".encode()).hexdigest()[:8], 16) / 0x1_0000_0000
    return h < frac


def base_id(rid: str) -> str:
    return rid[:-len(SERVING_SUFFIX)] if rid.endswith(SERVING_SUFFIX) else rid


def apply_serving(input_ids: list[int], attention_mask: list[int] | None, type_ids: list[int] | None,
                  feat_mask: list[bool], audio_id: int, want: int, extra: int):
    """Pure core of the torch step in eval_mm.audio_inputs. Switches `extra` padded frames on (the first padded
    one, like serving's last token) and grows the run of audio placeholder tokens to `want`. Returns the new
    (input_ids, attention_mask, type_ids, feat_mask) and checks that the mask now yields exactly the tokens the
    prompt holds, as train.py's audio_inputs does."""
    mask = list(feat_mask)
    n = sum(1 for m in mask if m)
    mask += [False] * max(0, n + extra - len(mask))
    for i in range(n, n + extra):
        mask[i] = True
    pos = [i for i, t in enumerate(input_ids) if t == audio_id]
    if not pos:
        raise ValueError("no audio placeholder tokens in the prompt")
    add = want - len(pos)
    if add:
        at = pos[-1] + 1
        input_ids = input_ids[:at] + [audio_id] * add + input_ids[at:]
        if attention_mask is not None:
            attention_mask = attention_mask[:at] + [1] * add + attention_mask[at:]
        if type_ids is not None:
            type_ids = type_ids[:at] + [type_ids[pos[-1]]] * add + type_ids[at:]
    got = audio_tokens_from_mask(mask)
    if got != input_ids.count(audio_id):
        raise ValueError(f"serving shape: mask gives {got} audio tokens, prompt holds {input_ids.count(audio_id)}")
    return input_ids, attention_mask, type_ids, mask
