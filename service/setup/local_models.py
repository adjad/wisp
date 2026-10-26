"""Which of Wisp's catalogue models are already on disk where the engine loads models.

When the engine isn't answering (oMLX.app's server is off, or Wisp's built-in engine
has not started yet), setup can't ask it for its model list. A model folder already
on disk is still installed: offering to download it again would waste gigabytes and,
on a first run, hide the oMLX the person already has.

Read-only and bounded. Only catalogue ids are looked for, using the same layout the
engine scans (oMLX's two-level discovery): `<dir>/<model id>/` or
`<dir>/<organisation>/<model id>/`, holding a config.json and complete weights.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from . import catalog

# Organisation folders scanned per model directory. A model folder sits one or two
# levels down; anything wider is not a model directory and is not walked.
_MAX_ENTRIES = 512
_SHARD = re.compile(r"-(\d+)-of-(\d+)\.safetensors$")


def catalogue_ids() -> tuple[str, ...]:
    return tuple(m.id for m in (*catalog.PRIMARY_MODELS, catalog.EMBEDDING_MODEL))


def weights_complete(folder: Path) -> bool:
    """A config.json plus every weight shard the checkpoint promises.

    An interrupted download leaves config.json (fetched first) and only some
    shards; the engine would then fail to load it, so it does not count.
    """
    try:
        if not (folder / "config.json").is_file() or (folder / "adapter_config.json").exists():
            return False
        weights = [p for p in folder.iterdir() if p.name.endswith(".safetensors") and p.is_file()]
        if not weights or any(p.stat().st_size == 0 for p in weights):
            return False
    except OSError:
        return False
    names = {p.name for p in weights}
    promised: set[str] = set()
    index = folder / "model.safetensors.index.json"
    if index.is_file():
        try:
            weight_map = json.loads(index.read_text()).get("weight_map")
        except (OSError, ValueError, AttributeError, RecursionError):
            weight_map = None
        if isinstance(weight_map, dict):
            # Only numbered shards are completeness promises, as in the engine's own
            # check; an index may also list auxiliary files the loader can skip.
            promised = {v for v in weight_map.values()
                        if isinstance(v, str) and "/" not in v and _SHARD.search(v)}
    for name in names:
        match = _SHARD.search(name)
        if match:
            width, total = len(match.group(1)), int(match.group(2))
            promised |= {_SHARD.sub(f"-{n:0{width}d}-of-{match.group(2)}.safetensors", name)
                         for n in range(1, min(total, 999) + 1)}
    return promised <= names


def _children(folder: Path) -> list[Path]:
    try:
        entries = []
        for entry in folder.iterdir():
            if len(entries) >= _MAX_ENTRIES:
                break
            if not entry.name.startswith(".") and entry.is_dir():
                entries.append(entry)
        return entries
    except OSError:
        return []


def catalogue_models_on_disk(model_dirs: Iterable[Path | str],
                             wanted: Iterable[str] | None = None) -> list[str]:
    """Catalogue model ids whose complete folders are in `model_dirs`, sorted."""
    targets = set(wanted if wanted is not None else catalogue_ids())
    found: set[str] = set()
    for raw in model_dirs:
        root = Path(raw).expanduser()
        if not root.is_dir():
            continue
        for entry in _children(root):
            if entry.name in targets:
                if weights_complete(entry):
                    found.add(entry.name)
                continue
            if (entry / "config.json").exists():
                continue  # a model itself, not an organisation folder
            for child in _children(entry):
                if child.name in targets and weights_complete(child):
                    found.add(child.name)
    return sorted(found)
