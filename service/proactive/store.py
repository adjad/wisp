"""Persistent state for the proactive engine, under $WISP_HOME/proactive.

Three private JSON files, each written atomically with 0600 permissions:

- ``triage.json``: the per-item checkpoint. Keyed by item id, holding the
  content hash it was read at and the validated triage result, so an item is
  never sent to the model twice for the same content.
- ``state.json``: the last cycle's output (cards, model to-dos, threads, day
  summary) plus the judge's input key and when it ran.
- ``decisions.json``: what the user did — dismissed cards, done to-dos, fired
  cards — keyed so that nothing they finished or waved away resurfaces.

Nothing here calls a model or a native source.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

_DONE_SHOWN_S = 24 * 3600
_KEEP_S = 14 * 86400


def base_dir() -> Path:
    from service.paths import moe_dir
    return moe_dir() / "proactive"


class ProactiveStore:
    def __init__(self, base: Path | None = None):
        self.base = Path(base) if base is not None else base_dir()
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}

    # --- files ---------------------------------------------------------------

    def _read(self, name: str) -> dict:
        with self._lock:
            if name not in self._cache:
                try:
                    data = json.loads((self.base / name).read_text())
                    self._cache[name] = data if isinstance(data, dict) else {}
                except (OSError, ValueError):
                    self._cache[name] = {}
            return self._cache[name]

    def _write(self, name: str) -> None:
        with self._lock:
            data = self._read(name)
            try:
                self.base.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = self.base / name
                tmp = path.with_suffix(".tmp")
                # Private from the first byte: these files describe mail and texts.
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(json.dumps(data, default=str))
                os.chmod(tmp, 0o600)
                tmp.replace(path)
            except OSError:
                traceback.print_exc()

    # --- triage checkpoint ---------------------------------------------------

    def triage_get(self, item_id: str, content_hash: str) -> dict | None:
        """The checkpointed entry for this exact content, or None."""
        entry = self._read("triage.json").get(item_id)
        if isinstance(entry, dict) and entry.get("hash") == content_hash:
            return entry
        return None

    def triage_put(self, item_id: str, content_hash: str, result: dict | None, *,
                   status: str, account: str = "", now: float | None = None) -> dict:
        with self._lock:
            data = self._read("triage.json")
            old = data.get(item_id) if isinstance(data.get(item_id), dict) else {}
            attempts = (old.get("attempts", 0) + 1) if old.get("hash") == content_hash and status == "failed" else (
                1 if status == "failed" else 0)
            entry = {"hash": content_hash, "status": status, "result": result, "account": account,
                     "at": time.time() if now is None else now, "attempts": attempts}
            data[item_id] = entry
            # Nothing older than 14 days is kept: sources are read 7 days back.
            cutoff = entry["at"] - _KEEP_S
            for key in [k for k, v in data.items() if float((v or {}).get("at") or 0) < cutoff]:
                data.pop(key, None)
            # Bounded: the oldest checkpoints go first.
            if len(data) > 2000:
                for key in sorted(data, key=lambda k: data[k].get("at", 0))[:len(data) - 2000]:
                    data.pop(key, None)
            self._write("triage.json")
            return entry

    # --- cycle output ---------------------------------------------------------

    def state(self) -> dict:
        return self._read("state.json")

    def save_state(self, **values) -> None:
        with self._lock:
            self._read("state.json").update(values)
            self._write("state.json")

    def card(self, card_id: str) -> dict | None:
        for card in self.state().get("cards") or []:
            if card.get("id") == card_id:
                return card
        return None

    def update_card(self, card: dict) -> None:
        with self._lock:
            state = self.state()
            state["cards"] = [card if c.get("id") == card["id"] else c for c in state.get("cards") or []]
            self._write("state.json")

    def remove_card(self, card_id: str) -> None:
        with self._lock:
            state = self.state()
            state["cards"] = [c for c in state.get("cards") or [] if c.get("id") != card_id]
            self._write("state.json")

    # --- decisions --------------------------------------------------------------

    def decisions(self) -> dict:
        data = self._read("decisions.json")
        for key in ("dismissed", "done", "fired", "suppressed", "unknown", "unknown_threads"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def prune(self, now: float | None = None) -> None:
        """Forget decisions older than 14 days; their sources are long out of the window."""
        now = time.time() if now is None else now
        with self._lock:
            data = self.decisions()
            def stamp(v):
                return float(v.get("at") or 0) if isinstance(v, dict) else float(v or 0)
            for key in ("dismissed", "fired", "suppressed", "done", "unknown", "unknown_threads"):
                data[key] = {k: v for k, v in data[key].items() if now - stamp(v) < _KEEP_S}
            self._write("decisions.json")

    def suppressed_fingerprints(self) -> set[str]:
        """Source fingerprints (item id + content hash) the user already handled:
        a dismissed or fired card, a done to-do. An item built only from these
        never comes back."""
        return set(self.decisions()["suppressed"])

    def _suppress(self, fingerprints, now: float) -> None:
        data = self.decisions()
        for fp in fingerprints or []:
            if isinstance(fp, str) and fp:
                data["suppressed"][fp] = now

    def mark_unknown(self, card: dict, now: float | None = None) -> None:
        """A send whose outcome is unknown: the card stays locked, across cycles
        and restarts, until it is dismissed or its to-do marked handled."""
        with self._lock:
            at = time.time() if now is None else now
            data = self.decisions()
            data["unknown"][card["id"]] = {"at": at, "todo_id": card.get("todo_id")}
            # The whole conversation too, for an hour: a fresh card for the same
            # chat must not offer the send again while it may have gone out.
            for ref in _card_refs(card):
                data["unknown_threads"][ref] = at
            self._write("decisions.json")

    def unknown_cards(self) -> set[str]:
        return set(self.decisions()["unknown"])

    def locked(self, card: dict, now: float | None = None) -> bool:
        """The card, or its conversation within the last hour, had an unknown send."""
        now = time.time() if now is None else now
        data = self.decisions()
        if card.get("id") in data["unknown"]:
            return True
        return any(now - float(data["unknown_threads"].get(ref) or 0) < THREAD_LOCK_S for ref in _card_refs(card)
                   if ref in data["unknown_threads"])

    def dismiss_card(self, card: dict, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            data = self.decisions()
            data["unknown"].pop(card["id"], None)
            for ref in _card_refs(card):
                data["unknown_threads"].pop(ref, None)
            data["dismissed"][card["id"]] = now
            self._suppress(card.get("fingerprints"), now)
            self._write("decisions.json")
        self.remove_card(card["id"])

    def record_fire(self, card: dict, result: dict, now: float | None = None) -> None:
        """Called only after a verified success: the card goes, its to-do is done."""
        now = time.time() if now is None else now
        with self._lock:
            data = self.decisions()
            data["fired"][card["id"]] = {"at": now, "method": (card.get("action") or {}).get("method"),
                                         "status": result.get("status", "succeeded")}
            self._suppress(card.get("fingerprints"), now)
            if card.get("todo_id") and (card.get("action") or {}).get("method") == "block_time":
                # Time is held for it; the work itself isn't done.
                data.setdefault("added_to_day", {})[card["todo_id"]] = {"at": now,
                                                                       "task_id": result.get("task_id") or ""}
            elif card.get("todo_id"):
                data["done"][card["todo_id"]] = {"at": now, "todo": _todo_snapshot(card)}
            self._write("decisions.json")
        self.remove_card(card["id"])

    def set_todo_done(self, todo: dict, done: bool, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            data = self.decisions()
            if done:
                data["done"][todo["id"]] = {"at": now, "todo": {**todo, "done": True}}
                data["unknown"] = {k: v for k, v in data["unknown"].items()
                                   if not (isinstance(v, dict) and v.get("todo_id") == todo["id"])}
                self._suppress(todo.get("fingerprints"), now)
            else:
                data["done"].pop(todo["id"], None)
                for fp in todo.get("fingerprints") or []:
                    data["suppressed"].pop(fp, None)
            self._write("decisions.json")

    def done_todos(self, now: float | None = None) -> dict[str, dict]:
        """To-dos marked done in the last day, shown checked; older ones drop off."""
        now = time.time() if now is None else now
        out = {}
        for todo_id, entry in self.decisions()["done"].items():
            if isinstance(entry, dict) and now - float(entry.get("at") or 0) < _DONE_SHOWN_S:
                out[todo_id] = entry
        return out

    def is_done(self, todo_id: str) -> bool:
        return todo_id in self.decisions()["done"]

    def mark_added_to_day(self, todo_id: str, task_id: str, now: float | None = None) -> None:
        with self._lock:
            data = self._read("decisions.json")
            data.setdefault("added_to_day", {})[todo_id] = {"at": time.time() if now is None else now,
                                                            "task_id": task_id}
            self._write("decisions.json")

    def added_to_day(self) -> dict:
        value = self._read("decisions.json").get("added_to_day")
        return value if isinstance(value, dict) else {}

    def erase_account(self, account: str) -> None:
        """Forget every proactive record for one account (a linked account's
        Disconnect & erase): cards, to-dos, threads, summaries, triage
        checkpoints and done/added records. A cycle already running drops
        that account's results instead of saving them (erased_at)."""
        account = (account or "").strip().casefold()
        if not account or account == OWNER:
            return                       # the owner's own data is never erased through a linked account
        mine = lambda x: isinstance(x, dict) and str(x.get("account") or "").casefold() == account  # noqa: E731
        with self._lock:
            state = self.state()
            for key in ("cards", "todos", "threads", "notes"):
                state[key] = [x for x in state.get(key) or [] if not mine(x)]
            state["accounts"] = [a for a in state.get("accounts") or [] if str(a).casefold() != account]
            for key in ("summaries", "judged"):
                if isinstance(state.get(key), dict):
                    state[key] = {k: v for k, v in state[key].items() if str(k).casefold() != account}
            self._write("state.json")
            triage = self._read("triage.json")
            for key in [k for k, v in triage.items() if mine(v)]:
                triage.pop(key, None)
            self._write("triage.json")
            data = self.decisions()
            data["done"] = {k: v for k, v in data["done"].items() if not mine((v or {}).get("todo"))}
            data.setdefault("erased_at", {})[account] = time.time()
            self._write("decisions.json")

    def erased_accounts(self) -> set[str]:
        return {str(a).casefold() for a in (self._read("decisions.json").get("erased_at") or {})}

    def erased_since(self, account: str, started: float) -> bool:
        value = (self._read("decisions.json").get("erased_at") or {}).get((account or "").casefold())
        return isinstance(value, (int, float)) and value >= started


THREAD_LOCK_S = 3600
OWNER = "me"                             # sources.OWN: the owner's bucket


def _card_refs(card: dict) -> list[str]:
    return [str(s.get("ref")) for s in card.get("sources") or [] if isinstance(s, dict) and s.get("ref")]


def _todo_snapshot(card: dict) -> dict:
    source = (card.get("sources") or [{}])[0]
    return {"id": card.get("todo_id"), "title": card.get("title"), "source": source.get("kind"),
            "account": card.get("account"), "who": source.get("who", ""), "quote": source.get("quote", ""),
            "due_ts": None, "due_label": "", "kind": "task", "done": True, "card_id": None}


_store: ProactiveStore | None = None


def store() -> ProactiveStore:
    global _store
    if _store is None:
        _store = ProactiveStore()
    return _store


def erase_account(account: str) -> None:
    """Sync entry point for W4's Disconnect & erase."""
    store().erase_account(account)


def set_store(value: ProactiveStore | None) -> None:
    """Tests point the engine at a scratch directory."""
    global _store
    _store = value
