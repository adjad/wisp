"""Explicit long-term memory — facts Wisp was TOLD to remember.

Distinct from the two memory layers that already exist, and deliberately so:

- `memory/store.py` persists conversations, but each session's rolling summary
  is scoped to that session. Nothing carries across.
- `memory/profile.py` is a nightly LLM *digest* of synced Mail/Messages/Notes.
  It's inferred, lossy, rebuilt from scratch, and can't hold anything the user
  states directly in conversation ("my sister's wedding is in October", "always
  deploy from the release branch") because none of that lives in a synced
  source.

This is the third layer: durable, verbatim, user-attributable facts, written on
purpose (by the model calling `remember`, or by the user asking it to) and
recalled in every later session regardless of which conversation created them.
A fact written in one chat is visible in the next one, next week.

Stored in its own SQLite file at ~/.moe/facts.db so it survives a sessions.db
reset and can be inspected/backed up on its own. Text stays on disk — only the
compact `memory_context_block` slice is ever resident.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

from service.paths import STATE_DIR

DB_PATH = STATE_DIR / "facts.db"

# Buckets exist so the context block can stay balanced when there are more
# facts than fit — see _select_for_context. Free-text categories are accepted
# too; these are just the ones the tool description steers toward.
CATEGORIES = ("person", "preference", "project", "routine", "fact")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    category    TEXT DEFAULT 'fact',
    session_id  TEXT,
    created_at  REAL,
    updated_at  REAL,
    last_used   REAL DEFAULT 0,
    use_count   INTEGER DEFAULT 0,
    pinned      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS facts_updated ON facts(updated_at DESC);
"""


def _norm(text: str) -> str:
    """Normalized form used for near-duplicate detection."""
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


class FactStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()

    def add(self, text: str, category: str = "fact",
            session_id: str | None = None, pinned: bool = False) -> dict:
        """Store a fact. Updates in place when one already says the same thing.

        The de-dup pass is not a nicety: the model calls `remember` whenever it
        notices something durable, so without it a fact the user repeats across
        three conversations becomes three near-identical rows that then crowd
        out everything else in the context block.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("cannot remember an empty fact")
        now = time.time()
        key = _norm(text)
        with self._lock:
            for row in self._db.execute("SELECT id, text FROM facts").fetchall():
                other = _norm(row["text"])
                if other == key or (len(key) > 12 and (key in other or other in key)):
                    self._db.execute(
                        "UPDATE facts SET text=?, category=?, updated_at=? WHERE id=?",
                        (text, category, now, row["id"]))
                    self._db.commit()
                    return {"id": row["id"], "text": text, "updated": True}
            cur = self._db.execute(
                "INSERT INTO facts (text, category, session_id, created_at, "
                "updated_at, pinned) VALUES (?,?,?,?,?,?)",
                (text, category, session_id, now, now, int(pinned)))
            self._db.commit()
            return {"id": cur.lastrowid, "text": text, "updated": False}

    def all(self, limit: int = 500) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM facts ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Word-overlap search. No embeddings on purpose — the fact corpus is
        small (hundreds of short lines), so scoring them all in Python costs
        microseconds and avoids making recall depend on an embedding server
        being up."""
        terms = [t for t in _norm(query).split() if len(t) > 2]
        rows = [dict(r) for r in self._db.execute("SELECT * FROM facts").fetchall()]
        if not terms:
            return sorted(rows, key=lambda r: r["updated_at"], reverse=True)[:limit]
        scored = []
        for r in rows:
            hay = _norm(r["text"] + " " + (r["category"] or ""))
            score = sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, r["updated_at"], r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [r for _, _, r in scored[:limit]]

    def touch(self, ids: list[int]) -> None:
        """Record that these facts were actually surfaced — feeds the recency
        ordering so facts that keep proving useful stay in the context block."""
        if not ids:
            return
        now = time.time()
        with self._lock:
            self._db.executemany(
                "UPDATE facts SET last_used=?, use_count=use_count+1 WHERE id=?",
                [(now, i) for i in ids])
            self._db.commit()

    def delete(self, fact_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self._db.commit()
            return cur.rowcount > 0

    def delete_matching(self, query: str) -> int:
        hits = self.search(query, limit=10)
        # Only delete unambiguous matches: a single hit, or hits that all
        # contain every search term. `forget` is destructive and the model is
        # the one calling it, so a fuzzy match must not take out a neighbor.
        terms = [t for t in _norm(query).split() if len(t) > 2]
        exact = [h for h in hits
                 if all(t in _norm(h["text"]) for t in terms)] if terms else []
        targets = exact or (hits[:1] if len(hits) == 1 else [])
        for h in targets:
            self.delete(h["id"])
        return len(targets)

    def set_pinned(self, fact_id: int, pinned: bool) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE facts SET pinned=? WHERE id=?",
                                   (int(pinned), fact_id))
            self._db.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"])


store = FactStore()

# Ceiling on the per-turn injection. Small on purpose: this rides on EVERY
# turn alongside the profile block (see profile._DIGEST_CHARS), and the two
# together have to leave room for the actual conversation inside
# context.MAX_CONTEXT_TOKENS.
_CONTEXT_CHARS = 2000


def _select_for_context(limit_chars: int) -> list[dict]:
    """Pinned first, then most-recently-updated, round-robin across categories.

    Round-robin rather than a flat recency sort so one busy category (a project
    the user has been dumping notes into all week) can't push every remembered
    person and preference out of the block.
    """
    rows = store.all(limit=300)
    pinned = [r for r in rows if r["pinned"]]
    rest = [r for r in rows if not r["pinned"]]

    by_cat: dict[str, list[dict]] = {}
    for r in rest:
        by_cat.setdefault(r["category"] or "fact", []).append(r)

    interleaved: list[dict] = []
    while any(by_cat.values()):
        for cat in list(by_cat):
            if by_cat[cat]:
                interleaved.append(by_cat[cat].pop(0))

    selected: list[dict] = []
    used = 0
    for r in pinned + interleaved:
        cost = len(r["text"]) + 4
        if used + cost > limit_chars and selected:
            break
        selected.append(r)
        used += cost
    return selected


def memory_context_block(max_chars: int = _CONTEXT_CHARS) -> str:
    """The "what you've been told to remember" prompt block, or "".

    Injected next to profile_context_block on every turn. The explicit
    precedence note matters: these facts were stated by the user, so unlike the
    model-derived profile they should WIN against it — otherwise a stale
    inference ("works at X") silently overrides a correction the user made on
    purpose ("I left X in March, remember that").
    """
    rows = _select_for_context(max_chars)
    if not rows:
        return ""
    store.touch([r["id"] for r in rows])
    lines = "\n".join(f"- [{r['category']}] {r['text']}" for r in rows)
    return ("\nThings you were explicitly asked to remember about this user, "
            "carried over from earlier conversations. These came from the user "
            "directly, so they take precedence over the model-derived profile "
            "above when the two disagree. Use them silently — don't recite them "
            "unless asked:\n" + lines)
