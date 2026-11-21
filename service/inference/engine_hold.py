"""One model held on the engine for a foreground job (an Ideate run), so nothing loads beside it.

While a hold is set, loading or making resident any OTHER model is refused at once with a
plain message, instead of swapping the held model out mid-run or putting two big models in
memory together (the user, 2026-11-21: "unload any other harbor model while ideate is in use
with inkling"). The held model itself loads as usual. Dependency-free and thread-safe: Ideate
sets it from its runner thread; the engine client checks it on the service loop.
"""
from __future__ import annotations

import threading

from .inference_errors import ModelLoadError

_lock = threading.Lock()
_holder: str | None = None
_label = ""


class ModelHeldError(ModelLoadError):
    reason = "model_held"

    def __init__(self, label: str = "") -> None:
        name = label or "Another tab"
        super().__init__(
            f"{name} is using Wisp's model for a build right now, so Ask can't load its own model "
            f"beside it. Ask again when the build finishes, or stop it in {name}.")


def hold(model: str, label: str = "") -> None:
    global _holder, _label
    with _lock:
        _holder, _label = model, label


def release(model: str) -> None:
    global _holder, _label
    with _lock:
        if _holder == model:
            _holder, _label = None, ""


def holder() -> str | None:
    with _lock:
        return _holder


def check(model: str) -> None:
    """Raise ModelHeldError when another model holds the engine."""
    with _lock:
        held, label = _holder, _label
    if held is not None and model != held:
        raise ModelHeldError(label)
