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
        entries[model.id] = {**entry, **model.engine_profile}
        record[model.id] = mark
        written.append(model.id)
    if written:
        doc["version"] = doc.get("version", FILE_VERSION)
        doc["models"] = entries
        _write_private(settings_path, doc)
        _write_private(record_path, record)
    return written


def push_body(entry: dict, allowed: set[str] | None) -> dict:
    """The admin API's PUT replaces the whole entry and rejects unknown keys:
    send the full entry, filtered to what its schema accepts when known."""
    return {k: v for k, v in entry.items() if allowed is None or k in allowed}


def entry_for(settings_path: Path, model_id: str) -> dict | None:
    entries = _read(settings_path).get("models")
    entry = entries.get(model_id) if isinstance(entries, dict) else None
    return entry if isinstance(entry, dict) else None
