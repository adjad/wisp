"""Small, direct MLX runtime for a local Ling-3.0-tiny-oQ4e checkpoint.

The installed oMLX bundle supplies mlx-lm, its Ling model adapter, and an M5
MLX correctness workaround. This module does not use the oMLX server or engine.
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


MAX_GENERATION_TOKENS = 2048
MAX_PROMPT_TOKENS = 8192


class GenerationCancelled(Exception):
    """The requesting client closed its stream during generation."""


def inspect_checkpoint(model_path: str | Path) -> tuple[Path, dict[str, Any]]:
    """Reject missing, remote, or incompatible checkpoints before loading weights."""
    path = Path(model_path).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"Checkpoint is not a directory: {path}")
    config_file = path / "config.json"
    tokenizer_file = path / "tokenizer.json"
    if not config_file.is_file() or not tokenizer_file.is_file():
        raise ValueError("Checkpoint must contain config.json and tokenizer.json")
    if not list(path.glob("*.safetensors")):
        raise ValueError("Checkpoint has no local safetensors weights")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    quant = config.get("quantization")
    expected = {"model_type": "bailing_hybrid", "num_hidden_layers": 24}
    for key, value in expected.items():
        if config.get(key) != value:
            raise ValueError(f"Expected {key}={value!r}; got {config.get(key)!r}")
    if not isinstance(quant, dict) or any(
        quant.get(key) != value
        for key, value in {"bits": 4, "group_size": 64, "mode": "affine"}.items()
    ):
        raise ValueError("Expected the local Ling oQ4e 4-bit/group-64 checkpoint")
    return path, config


class Engine:
    """One resident model; every generation request uses a fresh model cache.

    `optimized` is retained for benchmark callers but runs the stock path.
    """

    def __init__(self, model_path: str | Path, optimized: bool = False):
        self.model_path, self.config = inspect_checkpoint(model_path)
        self.optimized = bool(optimized)
        self.implementation = "stock"
        self._lock = threading.Lock()

        # oMLX's pre-load compatibility hooks register the vendored Ling
        # architecture, retain per-module mixed quantization, and work around
        # an M5 sorted-gather MLX defect. Weight loading and generation remain
        # direct mlx-lm calls in this process; no oMLX server is contacted.
        from omlx.utils.model_loading import load_text_model, materialize_lazy_state

        self.model, self.tokenizer = load_text_model(str(self.model_path))
        # The HTTP listener generates on request threads. Evaluate any lazy
        # auxiliary arrays on the loader thread before those requests arrive.
        materialize_lazy_state(self.model)

    def tokenize(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        thinking: bool = False,
    ) -> list[int]:
        """Apply the checkpoint's own Bailing chat template."""
        rendered = self.render_prompt(messages, tools=tools, thinking=thinking)
        tokens = self.tokenizer.encode(rendered, add_special_tokens=False)
        if not tokens:
            raise ValueError("Chat template produced an empty prompt")
        return [int(token) for token in tokens]

    def render_prompt(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        thinking: bool = False,
    ) -> str:
        """Return the exact chat-template string for reproducible comparisons."""
        if not messages or not all(isinstance(m, Mapping) for m in messages):
            raise ValueError("messages must be a nonempty list of message objects")
        if tools is not None and (
            not isinstance(tools, Sequence)
            or any(not isinstance(tool, Mapping) for tool in tools)
        ):
            raise ValueError("tools must be a list of tool definitions")
        for message in messages:
            if message.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ValueError("Unsupported chat role")
            if not isinstance(message.get("content"), str):
                raise ValueError("Every message content must be a string")
        rendered = self.tokenizer.apply_chat_template(
            list(messages), tokenize=False, add_generation_prompt=True,
            enable_thinking=bool(thinking), tools=list(tools or []),
        )
        if not isinstance(rendered, str) or not rendered:
            raise ValueError("Chat template produced an empty prompt")
        return rendered

    def generate_tokens(
        self,
        prompt_ids: Sequence[int],
        max_tokens: int = 128,
        temperature: float = 0.0,
        seed: int = 0,
        use_cache: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Generate synchronously with a fresh hybrid KV/KDA cache.

        `prefill_seconds` is time to the first sampled token, including that
        token's decode. `decode_seconds` is the remaining token generation.
        The split is based on synchronized token yields from mlx-lm.
        """
        return self._generate(
            prompt_ids, max_tokens=max_tokens, temperature=temperature,
            seed=seed, use_cache=use_cache, cancelled=cancelled,
        )

    def stream_text(
        self,
        prompt_ids: Sequence[int],
        on_text: Callable[[str], None],
        *,
        max_tokens: int = 128,
        temperature: float = 0.0,
        seed: int = 0,
        use_cache: bool = False,
        on_idle: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Deliver valid Unicode segments as tokens arrive, then return metrics.

        Callbacks run while the per-engine lock is held. Raising from a callback
        closes the underlying mlx-lm generator before another request starts.
        """
        if not callable(on_text):
            raise ValueError("on_text must be callable")
        detokenizer = self.tokenizer.detokenizer
        segments: list[str] = []

        def deliver(segment: str) -> None:
            if segment:
                on_text(segment)
                segments.append(segment)

        def on_token(token: int) -> None:
            detokenizer.add_token(token)
            segment = detokenizer.last_segment
            if segment:
                deliver(segment)
            elif on_idle is not None:
                on_idle()

        result = self._generate(
            prompt_ids, max_tokens=max_tokens, temperature=temperature,
            seed=seed, use_cache=use_cache, on_token=on_token,
            cancelled=cancelled,
        )
        detokenizer.finalize()
        deliver(detokenizer.last_segment)
        emitted = "".join(segments)
        if emitted != result["text"]:
            if not result["text"].startswith(emitted):
                raise RuntimeError("Streaming detokenizer disagrees with final text")
            deliver(result["text"][len(emitted):])
        return result

    def _generate(
        self,
        prompt_ids: Sequence[int],
        *,
        max_tokens: int,
        temperature: float,
        seed: int,
        use_cache: bool,
        on_token: Callable[[int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if type(use_cache) is not bool:
            raise ValueError("use_cache must be a boolean")
        if use_cache:
            raise ValueError("Cross-request prefix caching is not implemented")
        if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_GENERATION_TOKENS:
            raise ValueError(f"max_tokens must be 1..{MAX_GENERATION_TOKENS}")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
            or temperature < 0
        ):
            raise ValueError("temperature must be a finite nonnegative number")
        if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
            raise ValueError("seed must be a 32-bit nonnegative integer")
        if not prompt_ids or any(
            type(t) is not int or not 0 <= t < self.config["vocab_size"]
            for t in prompt_ids
        ):
            raise ValueError("prompt_ids must be a nonempty list of token IDs")
        if len(prompt_ids) > MAX_PROMPT_TOKENS:
            raise ValueError(f"prompt cannot exceed {MAX_PROMPT_TOKENS} tokens")
        if len(prompt_ids) > self.config["max_position_embeddings"] - max_tokens:
            raise ValueError("prompt plus generation exceeds model context length")

        import mlx.core as mx
        from mlx_lm.generate import generate_step
        from mlx_lm.sample_utils import make_sampler

        with self._lock:
            mx.random.seed(seed)
            sampler = make_sampler(temp=float(temperature))
            start = time.perf_counter()
            first_token_at: float | None = None
            tokens: list[int] = []
            stop_token_id: int | None = None
            finish_reason = "length"
            generator = generate_step(
                mx.array(list(prompt_ids)), self.model,
                max_tokens=max_tokens, sampler=sampler,
                prefill_step_size=2048,
            )
            try:
                for token, _logprobs in generator:
                    if cancelled is not None and cancelled():
                        raise GenerationCancelled("Client disconnected")
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    token = int(token)
                    if token in self.tokenizer.eos_token_ids:
                        stop_token_id = token
                        finish_reason = "stop"
                        break
                    tokens.append(token)
                    if on_token is not None:
                        on_token(token)
            finally:
                try:
                    generator.close()
                finally:
                    mx.synchronize()
            end = time.perf_counter()
            first_token_at = first_token_at or end
            return {
                "tokens": tokens,
                "text": self.tokenizer.decode(tokens, skip_special_tokens=False),
                "prompt_tokens": len(prompt_ids),
                "cached_tokens": 0,
                "generation_tokens": len(tokens),
                "sampled_tokens": len(tokens) + (stop_token_id is not None),
                "stop_token_id": stop_token_id,
                "prefill_seconds": first_token_at - start,
                "decode_seconds": end - first_token_at,
                "total_seconds": end - start,
                "peak_memory_bytes": int(mx.get_peak_memory()),
                "finish_reason": finish_reason,
                "implementation": self.implementation,
            }
