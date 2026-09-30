"""Turns raw facts about this Mac and its inference apps into a setup checklist.

Pure: everything it needs is passed in, so the same logic serves the first-run
guide, the Settings entry point, and tests. I/O lives in `service/main.py`.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass

from . import catalog
from .engines import EXTERNAL_ENGINES, OMLX, EngineState
from .hardware import Hardware

# Text roles a single "use this model" choice sets, and their labels.
TEXT_ROLES = ("fast", "general", "coding", "reasoning")
ROLE_LABELS = {"fast": "Fast & routing", "general": "General & agentic",
               "coding": "Coding", "reasoning": "Reasoning"}
OMLX_MIN_MACOS = 15


@dataclass(frozen=True)
class OmlxState:
    installed: bool
    running: bool
    model_dir: str | None = None
    admin_url: str = "http://127.0.0.1:8000/admin"


def _entry(model_id: str, hardware: Hardware, tool_capable: list[str]) -> dict:
    known = catalog.by_id(model_id)
    if known:
        return {"id": model_id, "label": f"{known.label} · {known.quant}",
                "size_gb": known.size_gb, "fit": catalog.fit(known, hardware.ram_gb),
                "tested": True, "note": known.note}
    return {"id": model_id, "label": model_id, "size_gb": None, "fit": "unknown",
            "tested": model_id in tool_capable,
            "note": "Not one of Wisp's tested models." if model_id not in tool_capable
                    else "Qualified for Wisp's tool loop."}


def download_info(model_id: str | None, model_dir: str | None) -> dict | None:
    """How to fetch a catalog model. Wisp never downloads weights by itself."""
    known = catalog.by_id(model_id) if model_id else None
    if not known or not known.repo:
        return None
    target = f"{model_dir.rstrip('/')}/{known.repo}" if model_dir else None
    # The folder comes from oMLX's settings and the user pastes this into Terminal.
    command = (f"hf download {known.repo} --local-dir {shlex.quote(target)}" if target
               else f"hf download {known.repo}")
    return {"model": known.id, "label": f"{known.label} · {known.quant}",
            "size_gb": known.size_gb, "repo": known.repo,
            "url": f"https://huggingface.co/{known.repo}", "command": command,
            "install_hf": "python3 -m pip install -U huggingface_hub"}


def build_status(*, hardware: Hardware, omlx: OmlxState, installed: list[str],
                 models_source: str, roles: dict[str, dict], externals: list[EngineState],
                 tool_capable: list[str]) -> dict:
    ranked = [m.id for m in catalog.ranked_for(hardware.ram_gb)]
    rec = catalog.recommend(hardware.ram_gb, installed)
    chat_models = [m for m in installed if m != catalog.EMBEDDING_MODEL.id
                   and "embedding" not in m.lower() and "rerank" not in m.lower()]
    # Catalog models first in recommended order, then everything else.
    ordered = sorted(chat_models, key=lambda m: (ranked.index(m) if m in ranked else len(ranked), m))
    entries = [_entry(m, hardware, tool_capable) for m in ordered]

    checks: list[dict] = []
    if omlx.running:
        checks.append({"id": "engine", "state": "ok", "title": "oMLX is running",
                       "detail": "Wisp's main inference engine is ready."})
    elif omlx.installed:
        checks.append({"id": "engine", "state": "warn", "title": "oMLX isn't running",
                       "detail": "It is installed. Start it and Wisp can use it.",
                       "action": "start_engine"})
    else:
        checks.append({"id": "engine", "state": "todo", "title": "Install oMLX",
                       "detail": f"Wisp runs its assistant on oMLX, which needs macOS {OMLX_MIN_MACOS} or newer.",
                       "action": "install_omlx", "url": OMLX.url})

    missing = [r for r in TEXT_ROLES
               if roles.get(r, {}).get("endpoint", "local") == "local"
               and roles.get(r, {}).get("model") not in installed]
    if not omlx.running and not installed:
        checks.append({"id": "model", "state": "todo", "title": "Choose a model",
                       "detail": "Wisp will list your models once oMLX is running."})
    elif not chat_models:
        checks.append({"id": "model", "state": "todo", "title": "Download a model",
                       "detail": "No chat model is installed yet.", "action": "download_model"})
    elif missing:
        names = ", ".join(sorted({roles.get(r, {}).get("model") or "(none)" for r in missing}))
        checks.append({"id": "model", "state": "todo", "title": "Pick an installed model",
                       "detail": f"Wisp is set to use {names}, which isn't installed.",
                       "action": "apply_model"})
    else:
        checks.append({"id": "model", "state": "ok", "title": "Models are set",
                       "detail": "Every role points at an installed model."})

    embedder = catalog.EMBEDDING_MODEL
    checks.append({"id": "search", "state": "ok" if embedder.id in installed else "warn",
                   "title": "Smart Search model" if embedder.id in installed else "Smart Search needs an embedding model",
                   "detail": "Installed." if embedder.id in installed
                             else "Optional. Without it, Smart Search falls back to keyword matching."})

    ready = all(c["state"] == "ok" for c in checks if c["id"] in ("engine", "model"))
    return {
        "ready": ready,
        "hardware": {"chip": hardware.chip, "ram_gb": hardware.ram_gb, "tier": hardware.tier},
        "omlx": {"installed": omlx.installed, "running": omlx.running, "model_dir": omlx.model_dir,
                 "min_macos": OMLX_MIN_MACOS, "url": OMLX.url,
                 # oMLX's own admin page has a Model Downloader for Hugging Face repos.
                 "admin_url": omlx.admin_url},
        "models": {"source": models_source, "installed": entries,
                   "recommended": rec["first_choice"], "use": rec["use"], "warning": rec["warning"],
                   "download": download_info(rec["get"], omlx.model_dir),
                   "embedding_download": None if embedder.id in installed
                                         else download_info(embedder.id, omlx.model_dir)},
        "roles": [{"role": r, "label": ROLE_LABELS[r], **roles.get(r, {})} for r in TEXT_ROLES],
        "engines": [
            {"id": OMLX.id, "label": OMLX.label, "port": OMLX.port, "runs_tools": True,
             "summary": OMLX.summary, "how_to_start": OMLX.how_to_start, "url": OMLX.url,
             "running": omlx.running, "models": len(chat_models)},
            *[{"id": e.profile.id, "label": e.profile.label, "port": e.port, "runs_tools": False,
               "summary": e.profile.summary, "how_to_start": e.profile.how_to_start,
               "url": e.profile.url, "running": True, "models": len(e.models),
               "model_ids": e.models} for e in externals],
            *[{"id": p.id, "label": p.label, "port": p.port, "runs_tools": False,
               "summary": p.summary, "how_to_start": p.how_to_start, "url": p.url,
               "running": False, "models": 0, "model_ids": []}
              for p in EXTERNAL_ENGINES
              if p.id not in {e.profile.id for e in externals}],
        ],
        "checks": checks,
    }
