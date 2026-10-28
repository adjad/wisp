"""Per-model settings Wisp seeds into its bundled engine.

Wisp omits sampling from its requests on purpose (see OmlxClient._payload), so
a model runs with whatever its entry in the engine's model_settings.json says.
A fresh engine has no entry, which means the engine's generic defaults: for
Gemma 4 E4B that is the wrong temperature, no MTP, and an 8k window. The
catalog (service/setup/catalog.py) carries each model's `engine_profile`; this
module writes it.

Two writes, because the engine caches model_settings.json at startup:

- `seed()` runs in EngineManager.prepare(), before the engine starts, so the
  profile is in the file the engine reads.
- `push()` runs once a download completes, through the engine's admin API, so a
  model that arrived after startup gets it without a restart.

A profile is applied once per version. `seeded_profiles.json` remembers the
fingerprint of what was written; a later change in the engine's admin UI is the
user's tuning and is kept until the catalog profile itself changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Iterable

from service.setup import catalog

FILE_VERSION = 1

# Values an earlier catalog profile wrote that the current one no longer sets.
# The merge in seed() keeps keys outside the profile, so without this they would
# outlive the profile that set them. Removed only while they still hold exactly
# what Wisp wrote, so a value the person chose later in the engine's admin UI
# stays. Each cleanup runs once per install: the record notes it under
# RETIRED_KEY, so a forced reseed never takes back a value the person turned on
# again themselves. TurboQuant 6-bit KV was briefly Gemma's default and was withdrawn.
# A model-level 20 W GPU cap was briefly in Gemma's profile (8bff39bc) and was
# withdrawn: oMLX applies min(request, model), so it would cap every request,
# intensive work too (which must run uncapped).
# Each group is retired on its own: all of its keys must still hold Wisp's values.
RETIRED_KEY = "_retired"
RETIRED = {
    "gemma-4-E4B-it-oQ4e-mtp": (
        {"turboquant_kv_enabled": True, "turboquant_kv_bits": 6},
        {"gpu_power_limit_w": 20},
    ),
}


def fingerprint(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()[:16]


def profiled_models() -> list[catalog.CatalogModel]:
    return [m for m in catalog.PRIMARY_MODELS if m.engine_profile]


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write_private(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def seed(settings_path: Path, record_path: Path,
         models: Iterable[catalog.CatalogModel] | None = None, *, force: Iterable[str] = ()) -> list[str]:
    """Merge each model's profile into its entry when that profile version is new.

    Keys outside the profile are left alone (the engine and its admin UI own
    them). `force` re-applies the named models regardless of the record, for the
    write that follows an admin-API push. Returns the model ids written.
    """
    models = profiled_models() if models is None else list(models)
    forced = set(force)
    doc = _read(settings_path)
    entries = doc.get("models") if isinstance(doc.get("models"), dict) else {}
    record = _read(record_path)
    written = []
    for model in models:
        mark = fingerprint(model.engine_profile)
        if record.get(model.id) == mark and model.id not in forced:
            continue
        entry = entries.get(model.id) if isinstance(entries.get(model.id), dict) else {}
        entry = _retire_once(model, entry, record)
        entries[model.id] = {**entry, **model.engine_profile}
        record[model.id] = mark
        written.append(model.id)
    if written:
        doc["version"] = doc.get("version", FILE_VERSION)
        doc["models"] = entries
        _write_private(settings_path, doc)
        _write_private(record_path, record)
    return written


def _retire_once(model: catalog.CatalogModel, entry: dict, record: dict) -> dict:
    """The entry without the values RETIRED lists for it, each group the first time only."""
    done = record.get(RETIRED_KEY) if isinstance(record.get(RETIRED_KEY), dict) else {}
    marks = done.get(model.id)
    # Records from before groups held one mark as a string.
    marks = [marks] if isinstance(marks, str) else list(marks) if isinstance(marks, list) else []
    for group in RETIRED.get(model.id, ()):
        retired = {k: v for k, v in group.items() if k not in model.engine_profile}
        if not retired:
            continue
        mark = fingerprint(retired)
        if mark in marks:
            continue
        marks.append(mark)
        if all(entry.get(k) == v for k, v in retired.items()):
            entry = {k: v for k, v in entry.items() if k not in retired}
    if marks:
        record[RETIRED_KEY] = {**done, model.id: marks}
    return entry


def push_body(entry: dict, allowed: set[str] | None) -> dict:
    """The admin API's PUT replaces the whole entry and rejects unknown keys:
    send the full entry, filtered to what its schema accepts when known."""
    return {k: v for k, v in entry.items() if allowed is None or k in allowed}


def entry_for(settings_path: Path, model_id: str) -> dict | None:
    entries = _read(settings_path).get("models")
    entry = entries.get(model_id) if isinstance(entries, dict) else None
    return entry if isinstance(entry, dict) else None
