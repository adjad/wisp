"""Recommended and optional model choices, with separate memory and test status.

Being the configured default does not qualify a model's tool calling. Local
builds without a verified public repository remain discoverable, not downloadable.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalogModel:
    id: str                 # the id oMLX serves (its directory name)
    label: str
    quant: str
    size_gb: float | None   # None when there is no measured download to quote
    min_ram_gb: int         # smallest Mac it is reasonable on
    repo: str | None        # Hugging Face repo, or None when nothing is published
    note: str
    tested: bool = False
    # Per-model settings Wisp seeds into the bundled engine's model_settings.json
    # (see service/engine/profiles.py). Wisp omits sampling from its requests, so
    # these are what the model actually runs with. Empty: the engine's defaults.
    engine_profile: dict = field(default_factory=dict, compare=False, hash=False)


# The repo's name is the model id on purpose: the engine downloads into
# models/<owner>/<name> and serves it under <name>, which roles must match.
MELLUM_R1_Q4 = CatalogModel(
    "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
    "Mellum 2.1 Wisp R1", "merged oQ4e", 7.14, 24,
    "Neo-Arid/Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
    "Optional Wisp fine-tune. Model validation is pending.", tested=False)


# The default. Gemma 4 E4B with its multi-token-prediction head (oQ4e mixed
# 4-bit): the model the live roster runs. Thinking must stay ON in its profile:
# with enable_thinking false it returns zero tokens on the step after a tool
# result, so every tool-using answer came back empty (measured 2026-10-24).
# Sampling: the model card's top_p/top_k, with temperature lowered from its 1.0
# to 0.8 (user decision, 2026-10-25).
GEMMA_E4B_MTP = CatalogModel(
    "gemma-4-E4B-it-oQ4e-mtp", "Gemma 4 E4B", "4-bit (oQ4e) + MTP", 5.5, 16,
    "Jundot/gemma-4-E4B-it-oQ4e-mtp",
    "Wisp's default. Fast on Macs with 16 GB of memory or more, with clean tool calls.", tested=True,
    engine_profile={
        "temperature": 0.8, "top_p": 0.95, "top_k": 64,
        "enable_thinking": True,
        "mtp_enabled": True, "mtp_adaptive_max_depth": 4,
        "max_context_window": 16384,
        "trust_remote_code": True,
        # No gpu_power_limit_w here, ever: a model-level cap throttles the
        # whole Mac's GPU on every request, intensive work included. Wisp sends per-request
        # caps instead (service/inference/power_tier.py).
    })


LING_Q6 = CatalogModel(
    "Ling-3.0-tiny-oQ6e", "Ling 3.0 tiny", "6-bit (oQ6e)", 6.6, 24,
    "mlx-works/Ling-3.0-tiny-oQ6e",
    "Best quality. The build Wisp is developed and tested against.", tested=True)
LING_Q5 = CatalogModel(
    "Ling-3.0-tiny-oQ5e", "Ling 3.0 tiny", "5-bit (oQ5e)", None, 16, None,
    "A good middle for 16 GB Macs. No public download was found; use it if you already have it.", tested=True)
LING_Q4 = CatalogModel(
    "Ling-3.0-tiny-oQ4e", "Ling 3.0 tiny", "4-bit (oQ4e)", 4.6, 8,
    "mlx-works/Ling-3.0-tiny-oQ4e",
    "Smallest build that keeps tool selection reliable. Comfortable on 16 GB.", tested=True)

PRIMARY_MODELS = (GEMMA_E4B_MTP, LING_Q6, LING_Q5, LING_Q4, MELLUM_R1_Q4)

EMBEDDING_MODEL = CatalogModel(
    "Qwen3-Embedding-0.6B-4bit-DWQ", "Qwen3 Embedding 0.6B", "4-bit", 0.35, 8,
    "mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ",
    "Powers Smart Search (⌘⇧F). Small enough to stay loaded beside the main model.", tested=True)

# Below 4 bits the model picked the wrong tool about half the time (measured).
TESTED_NOTE = "Tool selection measured 10/10 on Wisp's test."


def ranked_for(ram_gb: int, installed: list[str] | None = None) -> tuple[CatalogModel, ...]:
    """Best-first order for this much memory.

    With `installed` and no catalog model on this Mac yet (a fresh install), a
    model with no public download is left out: a new user must be offered
    something they can actually get, never a private build they cannot fetch.
    Once any catalog model is installed the full order applies, keeping locally
    available private builds eligible for the memory-appropriate ranking.
    """
    # Gemma first on every Mac; the Ling builds follow in their memory order.
    if ram_gb >= 24:
        order = (GEMMA_E4B_MTP, LING_Q6, LING_Q5, LING_Q4, MELLUM_R1_Q4)
    elif ram_gb >= 16:
        order = (GEMMA_E4B_MTP, LING_Q5, LING_Q4, LING_Q6, MELLUM_R1_Q4)
    else:
        order = (GEMMA_E4B_MTP, LING_Q4, LING_Q5, LING_Q6, MELLUM_R1_Q4)
    if installed is None:
        return order
    have = set(installed)
    if any(m.id in have for m in order):
        return order
    obtainable = tuple(m for m in order if m.repo)
    return obtainable or order


# Wisp's estimate of the engine's admission check, applied before a download
# (an approximation of oMLX's memory guard, not a reading of it): a model loads
# only when its weights plus about 5% (runtime buffers) and half a GiB of
# working room stay under what the engine leaves itself, about the Mac's memory
# less 3 GiB for macOS and other apps. Gemma 4 E4B (5.5 GB, 5.88 GiB
# with that overhead) fits on 16 GB and is refused on 8 GB (a 5 GiB ceiling),
# where the first question would fail with "Free some memory". Ling's 4-bit
# build (4.6 GB) lands within a few MB of the 8 GB ceiling, so it is no
# small-Mac fallback either: below LOCAL_FLOOR_GB nothing is offered.
LOCAL_FLOOR_GB = 16
LOAD_OVERHEAD = 1.05
LOAD_WORKING_GIB = 0.5
SYSTEM_RESERVE_GIB = 3.0


def fits_memory(model: CatalogModel, ram_gb: int) -> bool:
    """Whether the engine would admit this model on a Mac with `ram_gb` GiB,
    with room to spare: at least LOCAL_FLOOR_GB, and inside the admission math.

    Unreadable memory (0) fits nothing: Wisp can't promise a load it can't
    check. A model with no measured size falls back to its `min_ram_gb`.
    """
    if ram_gb < LOCAL_FLOOR_GB:
        return False
    if model.size_gb is None:
        return ram_gb >= model.min_ram_gb
    needed_gib = model.size_gb * 1e9 / 2**30 * LOAD_OVERHEAD + LOAD_WORKING_GIB
    return needed_gib < ram_gb - SYSTEM_RESERVE_GIB


def fit(model: CatalogModel, ram_gb: int, installed: list[str] | None = None) -> str:
    """`recommended` (first choice), `good`, or `heavy` (bigger than advised, or
    more than the engine would load on this Mac). Unreadable memory (0) claims
    neither: every model reads `good` until the next read says more."""
    if ram_gb <= 0:
        return "good"
    if model.min_ram_gb > ram_gb or not fits_memory(model, ram_gb):
        return "heavy"
    return "recommended" if model == ranked_for(ram_gb, installed)[0] else "good"


def recommend(ram_gb: int, installed: list[str]) -> dict:
    """Pick what to run and, when something better is missing, how to get it.

    `use` is the best-ranked catalog model already installed (None if none is).
    `get` is the best-ranked model that ranks above `use`, is not installed, has
    a public download, and fits this Mac's memory; None when nothing better can
    be fetched. `fits` is False when no downloadable model fits this Mac (or its
    memory can't be read): setup then says so and offers another route instead
    of a download the engine would refuse to load.
    """
    ranked = ranked_for(ram_gb, installed)
    have = set(installed)
    use = next((m for m in ranked if m.id in have), None)
    better = ranked if use is None else ranked[:ranked.index(use)]
    get = next((m for m in better if m.repo and m.id not in have and fits_memory(m, ram_gb)), None)
    # A local preferred model must not silently turn into an offer to download
    # a different family. Existing installed choices remain usable. (On a fresh
    # Mac it was dropped from `ranked`, so the best public build is offered.)
    if ranked[0].repo is None:
        get = None
    fits = any(m.repo and fits_memory(m, ram_gb) for m in ranked)
    warning = None
    if ram_gb == 0:
        warning = ("Wisp couldn't read how much memory this Mac has, so it can't tell yet which "
                   "model will fit. It tries again in a moment.")
    elif not fits and use is not None:
        warning = (f"This Mac has {ram_gb} GB of memory, less than Wisp's models are made for. "
                   "Close other apps while you use Wisp, and expect slower replies.")
    elif not fits:
        warning = (f"Wisp's models need a Mac with at least {LOCAL_FLOOR_GB} GB of memory, and this one "
                   f"has {ram_gb} GB. If you already run a smaller model in another app, such as Ollama "
                   "or LM Studio, you can connect it in the detailed setup.")
    return {"first_choice": ranked[0].id, "use": use.id if use else None,
            "get": get.id if get else None, "fits": fits, "warning": warning}


def by_id(model_id: str) -> CatalogModel | None:
    return next((m for m in (*PRIMARY_MODELS, EMBEDDING_MODEL) if m.id == model_id), None)
