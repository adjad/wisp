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
    # (file name, sha256, bytes) a download must match before Wisp uses it. The engine's
    # downloader takes no revision, so a changed file on the Hub is caught here instead.
    pinned_files: tuple = field(default=(), compare=False, hash=False)


# The repo's name is the model id on purpose: the engine downloads into
# models/<owner>/<name> and serves it under <name>, which roles must match.
MELLUM_R1_Q4 = CatalogModel(
    "Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
    "Mellum 2.1 Wisp R1", "merged oQ4e", 7.14, 24,
    "Neo-Arid/Mellum2.1-12B-A2.5B-Thinking-Wisp-R1-merged-oQ4e",
    "Optional Wisp fine-tune. Model validation is pending.", tested=False)


# Base Gemma 4 E4B with its multi-token-prediction head (oQ4e mixed 4-bit). Since
# 0.5.0 it is KEPT, not offered (user decision 2026-11-12: "do not keep the base gemma
# as a fallback"): a Mac that already has it keeps its profile, lists and label, and
# the code roles in models.yaml still name it (see the note there), but setup never
# ranks, recommends or downloads it. Thinking must stay ON in its profile:
# with enable_thinking false it returns zero tokens on the step after a tool
# result, so every tool-using answer came back empty (measured 2026-10-24).
# Sampling: the model card's top_p/top_k, with temperature lowered from its 1.0
# to 0.8 (user decision, 2026-10-25).
GEMMA_E4B_MTP = CatalogModel(
    "gemma-4-E4B-it-oQ4e-mtp", "Gemma 4 E4B", "4-bit (oQ4e) + MTP", 5.5, 16,
    "Jundot/gemma-4-E4B-it-oQ4e-mtp",
    "The original Gemma, before Wisp's tuning. Wisp keeps it working on Macs that have it, "
    "but no longer offers it.", tested=True,
    engine_profile={
        "temperature": 0.8, "top_p": 0.95, "top_k": 64,
        "enable_thinking": True,
        "mtp_enabled": True, "mtp_adaptive_max_depth": 4,
        "max_context_window": 16384,
        "trust_remote_code": True,
        # No gpu_power_limit_w here, ever: a model-level cap throttles the
        # whole Mac's GPU on every request, replies included. Wisp caps only
        # background requests, per request (service/inference/power_tier.py).
    })


# The default since 0.5.0: base Gemma 4 E4B with Wisp's Ask LoRA r5 (step 1800)
# merged in, quantized with base's recipe (oQ4e, group 64) and base's own MTP head
# copied unchanged. Same template, so the same thinking rules and engine profile.
# Measured against base: Wisp's public tool test 77% vs 68% and median turn 6.5 s
# vs 8.8 s (one run, 2026-11-11), the private one 84% vs 72%; but two to three
# times base's unsafe proposals on the safety slice (2026-11-12). Sends and
# calendar changes still always ask, even under Full access. Published as
# Neo-Arid/Gemma-Wisp (the user's name for it, 2026-11-12). The engine serves a
# download under the repo's name, so the id is "Gemma-Wisp"; the developer's own
# Mac has the same weights under the pre-release folder name in WISP_LEGACY_IDS.
GEMMA_WISP_FILES = (   # sha256 and bytes of each file Wisp was tested with (HF upload manifest)
    ("chat_template.jinja", "0a2c8073c878ab1da004bee933a998606537bbb62016310352c7285c3f01c5b5", 18569),
    ("config.json", "295c29be6170fd93bc50309ff2b27f6a6dce6020be5f69dcb2b061dfcb77b800", 35165),
    ("generation_config.json", "d4226bbe3117d2d253ba4609720ba82c6c4ce4627a9a6ae05387c78983ac03de", 208),
    ("model-00001-of-00002.safetensors", "b22e7af393e6d0ecfcf210e94d4b1b29ee34c65213ece4410bd7e515815664f7", 5001242218),
    ("model-00002-of-00002.safetensors", "27ec7843f33fab7d8c40d9c4d145ab374fdc5cf0ddf02c1d9f1a2eee6f4d2a07", 425560497),
    ("model-mtp.safetensors", "deb34acb448ba75d2087bcf76dda343cdcf77c2bfcb1eba79c21561dd5956c6c", 159139040),
    ("model.safetensors.index.json", "36f9da8215f08eb926109f0f8e4ce360101552e21c9b4cf4254fad7144d9ca2a", 275856),
    ("processor_config.json", "32bdf45d2ad4cc29a0822ddd157a182de76644f0419a6228d151495256e9813c", 1689),
    ("tokenizer.json", "cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f", 32169626),
    ("tokenizer_config.json", "9f4fec4b1dc6ecddf8f4a92e9caea5971c0e67d81309f3f9066a2bee8c362633", 3082),
)


GEMMA_E4B_WISP = CatalogModel(
    "Gemma-Wisp", "Gemma Wisp", "4-bit (oQ4e) + MTP", 5.6, 16,
    "Neo-Arid/Gemma-Wisp",
    "Wisp's model. More accurate on Wisp's tool tests than the original Gemma, but it "
    "proposes more actions you'll want to decline.", tested=True,
    engine_profile=dict(GEMMA_E4B_MTP.engine_profile), pinned_files=GEMMA_WISP_FILES)


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

PRIMARY_MODELS = (GEMMA_E4B_WISP, LING_Q6, LING_Q5, LING_Q4, MELLUM_R1_Q4)
# The tuned Gemma's pre-release folder name: the same weights, on the developer's own
# Mac. No repo, so nothing offers or downloads it; models.yaml lists it beside Gemma-Wisp.
GEMMA_E4B_WISP_PRERELEASE = CatalogModel(
    "gemma-4-E4B-it-r5-step-01800-oQ4e-mtp", "Gemma Wisp (pre-release folder)", GEMMA_E4B_WISP.quant,
    GEMMA_E4B_WISP.size_gb, GEMMA_E4B_WISP.min_ram_gb, None,
    "The same weights as Gemma Wisp under their pre-release name.", tested=True,
    engine_profile=dict(GEMMA_E4B_MTP.engine_profile))
# Known so a Mac that has them keeps working (profile, label, disk scan), never
# ranked, recommended or downloadable.
KEPT_MODELS = (GEMMA_E4B_MTP, GEMMA_E4B_WISP_PRERELEASE)
# A kept id holding exactly the weights of an offered model: installed, it counts as that model,
# so a Mac with the pre-release folder is never offered a second 5.6 GB copy of the same files.
SAME_WEIGHTS = {GEMMA_E4B_WISP_PRERELEASE.id: GEMMA_E4B_WISP.id}

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
    # The tuned Gemma first on every Mac; the Ling builds follow in their memory
    # order. Base Gemma is not ranked (KEPT_MODELS): an install that has it keeps
    # it (`use`), and the tuned one is only offered (`get`), never switched to.
    if ram_gb >= 24:
        order = (GEMMA_E4B_WISP, LING_Q6, LING_Q5, LING_Q4, MELLUM_R1_Q4)
    elif ram_gb >= 16:
        order = (GEMMA_E4B_WISP, LING_Q5, LING_Q4, LING_Q6, MELLUM_R1_Q4)
    else:
        order = (GEMMA_E4B_WISP, LING_Q4, LING_Q5, LING_Q6, MELLUM_R1_Q4)
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


def in_use_order(ram_gb: int, installed: list[str] | None = None) -> tuple[CatalogModel, ...]:
    """`ranked_for` with the kept models (base Gemma) right below the first: the order
    in which an install's own models count as what it runs, and are listed. Kept models
    rank where base ranked before 0.5.0, but only `ranked_for` decides what is offered."""
    ranked = ranked_for(ram_gb, installed)
    kept = sorted(KEPT_MODELS, key=lambda m: m.id not in SAME_WEIGHTS)   # the same weights first
    return (*ranked[:1], *kept, *ranked[1:])


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
    use = next((m for m in in_use_order(ram_gb, installed) if m.id in have), None)
    same = by_id(SAME_WEIGHTS[use.id]) if use and use.id in SAME_WEIGHTS else None
    anchor = same if same in ranked else use
    better = ranked[:ranked.index(anchor)] if anchor in ranked else ranked[:1] if use else ranked
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
    return next((m for m in (*PRIMARY_MODELS, *KEPT_MODELS, EMBEDDING_MODEL) if m.id == model_id), None)
