"""Default and legacy model choices, with separate memory and test status.

Being the configured default does not qualify a model's tool calling. Local
builds without a verified public repository remain discoverable, not downloadable.
"""
from __future__ import annotations

from dataclasses import dataclass


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


MELLUM_R1_Q4 = CatalogModel(
    "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
    "Mellum 2.1 Wisp R1", "merged oQ4e", 7.14, 24, None,
    "Local merged build. Add its folder to the engine's model directory. "
    "Model validation is pending.", tested=False)


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

PRIMARY_MODELS = (MELLUM_R1_Q4, LING_Q6, LING_Q5, LING_Q4)

EMBEDDING_MODEL = CatalogModel(
    "Qwen3-Embedding-0.6B-4bit-DWQ", "Qwen3 Embedding 0.6B", "4-bit", 0.35, 8,
    "mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ",
    "Powers Smart Search (⌘⇧F). Small enough to stay loaded beside the main model.", tested=True)

# Below 4 bits the model picked the wrong tool about half the time (measured).
TESTED_NOTE = "Tool selection measured 10/10 on Wisp's test."


def ranked_for(ram_gb: int) -> tuple[CatalogModel, ...]:
    """Best-first order for this much memory."""
    if ram_gb >= 24:
        return (MELLUM_R1_Q4, LING_Q6, LING_Q5, LING_Q4)
    if ram_gb >= 16:
        return (LING_Q5, LING_Q4, LING_Q6, MELLUM_R1_Q4)
    return (LING_Q4, LING_Q5, LING_Q6, MELLUM_R1_Q4)


def fit(model: CatalogModel, ram_gb: int) -> str:
    """`recommended` (first choice), `good`, or `heavy` (bigger than advised)."""
    ranked = ranked_for(ram_gb)
    known_ram = ram_gb if ram_gb > 0 else 8      # unreadable memory: assume the smallest tier
    if model.min_ram_gb > known_ram:
        return "heavy"
    return "recommended" if model == ranked[0] else "good"


def recommend(ram_gb: int, installed: list[str]) -> dict:
    """Pick what to run and, when something better is missing, how to get it.

    `use` is the best-ranked catalog model already installed (None if none is).
    `get` is the best-ranked model that ranks above `use`, is not installed, and
    has a public download; None when nothing better can be fetched.
    """
    ranked = ranked_for(ram_gb)
    have = set(installed)
    use = next((m for m in ranked if m.id in have), None)
    better = ranked if use is None else ranked[:ranked.index(use)]
    get = next((m for m in better if m.repo and m.id not in have), None)
    # A local preferred model must not silently turn into an offer to download
    # a different family. Existing installed choices remain usable.
    if ranked[0] == MELLUM_R1_Q4:
        get = None
    warning = None
    if ram_gb == 0:
        warning = "Wisp could not read this Mac's memory, so it is recommending the smallest build."
    elif ram_gb < 16:
        warning = ("This Mac has less than 16 GB. Wisp will run the 4-bit model, but "
                   "close other heavy apps and expect slower replies.")
    return {"first_choice": ranked[0].id, "use": use.id if use else None,
            "get": get.id if get else None, "warning": warning}


def by_id(model_id: str) -> CatalogModel | None:
    return next((m for m in (*PRIMARY_MODELS, EMBEDDING_MODEL) if m.id == model_id), None)
