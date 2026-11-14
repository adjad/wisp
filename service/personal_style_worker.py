"""Write one draft with the person's own style adapter. Runs OUTSIDE the Wisp service.

Wisp's service never imports MLX (requirements-runtime.txt): the engine does all
inference. The engine cannot load a LoRA adapter, so the one job that needs it,
writing a draft in the person's voice, runs here as a short-lived child process
under a Python that has `mlx_lm` (service/personal_style.py finds one). Training
is the same Python's `-m mlx_lm lora`, started by the parent.

IMPORTANT rules:
- Standard library and mlx_lm only. Never import `service`: this file is run by
  path under another interpreter.
- Text in, text out, no tools. The request is one JSON object on stdin
  ({"model", "adapter", "messages", "max_tokens"}); the reply is one JSON object
  on stdout ({"text", "finish"} or {"error"}). "finish" is "stop" when the draft
  ended by itself and "length" when it was cut off at max_tokens; the parent uses
  only a draft that stopped. Nothing is written to disk and nothing is sent
  anywhere.
- Run as `python -I [-S] personal_style_worker.py <site-packages or "">`: no
  environment, no start-up code, and this file's folder (service/) is never on
  the import path. The one extra import folder is argv[1].
- The process exits after one draft, so the second copy of the model leaves
  memory at once.
"""
from __future__ import annotations

import json
import sys


def _reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        model_path, adapter = request["model"], request["adapter"]
        messages = request["messages"]
        max_tokens = max(16, min(int(request.get("max_tokens", 400)), 800))
    except (ValueError, KeyError, TypeError):
        _reply({"error": "bad request"})
        return 2
    try:
        here = __file__.rsplit("/", 1)[0]
        sys.path[:] = [p for p in sys.path if p and p != here]
        if len(sys.argv) > 1 and sys.argv[1]:
            sys.path.append(sys.argv[1])
        from mlx_lm import load, stream_generate
        from mlx_lm.sample_utils import make_sampler
        model, tokenizer = load(model_path, adapter_path=adapter)
        try:
            prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True,
                                                   tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
        parts, finish = [], None
        for piece in stream_generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens,
                                     sampler=make_sampler(temp=0.4, top_p=0.9)):
            parts.append(piece.text)
            finish = getattr(piece, "finish_reason", None) or finish
        text = "".join(parts)
    except Exception as error:  # noqa: BLE001 — the parent falls back to the usual model
        _reply({"error": type(error).__name__})
        return 1
    _reply({"text": text, "finish": finish if finish in ("stop", "length") else "length"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
