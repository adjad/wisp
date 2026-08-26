#!/usr/bin/env python3
"""Sample/test data for manually exercising Wisp before release.

Two kinds of data, handled differently on purpose:

1. **Wisp's own databases** (~/.moe/assistant.db commitments, ~/.moe/facts.db
   facts) — seeded IN PLACE, safely. Commitments use source="wisp_seed" (a
   value the real Calendar/Mail syncers never write, so they can never step on
   it, and re-running this script just upserts the same rows instead of
   duplicating them — see AssistantStore.sync_source's diff-by-source
   semantics). Facts are prefixed "[Wisp QA] " so they can never fuzzy-collide
   with a real remembered fact and are trivially greppable/deletable.

2. **Synced-content caches** (Messages/Mail/Notes — ~/.moe/cache/*.txt) — these
   are wholesale wholesale-replace snapshots pushed by the real Swift readers
   from the user's REAL chat.db/Mail.app/Notes.app, so this script never
   touches them directly. Instead `build-cache` renders synthetic fixtures to
   test_fixtures/cache/ (plain repo files); scripts/install_cache_fixtures.sh
   is the explicit, backed-up swap-in step for whoever wants to test that
   surface too.

The persona (cast of people, commitments, facts, message/email/note content)
and the wire-format renderers both live in tests/fixtures/ — this script is
now a thin CLI over that shared code (also used by the sandbox), rather than
its own copy of either. See tests/fixtures/wire.py's module docstring for
why a shared renderer matters: two of these formats had drifted from what the
real Swift readers actually send before that module existed.

Usage:
    .venv/bin/python scripts/wisp_testdata.py seed-db       # commitments + facts
    .venv/bin/python scripts/wisp_testdata.py clear-db       # remove exactly what seed-db added
    .venv/bin/python scripts/wisp_testdata.py build-cache    # write test_fixtures/cache/*.txt
    .venv/bin/python scripts/wisp_testdata.py status         # what's currently seeded
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fixtures import persona, wire  # noqa: E402

QA_TAG = persona.QA_TAG
SEED_SOURCE = "wisp_seed"
FIXTURE_DIR = ROOT / "test_fixtures" / "cache"

NOW = time.time()


def build_commitments() -> list[dict]:
    return persona.commitment_rows(NOW)


def build_facts() -> list[tuple[str, str, bool]]:
    """(category, text, pinned)."""
    return persona.fact_rows()


def seed_db() -> None:
    from service.assistant.store import assistant_store
    from service.memory.facts import store as fact_store

    items = build_commitments()
    n = assistant_store.sync_source(SEED_SOURCE, items)
    print(f"✓ seeded/updated {n} commitments (source={SEED_SOURCE!r})")
    # Seed rows are hidden from every store read path unless the backend runs
    # with WISP_QA_SEED=1 (see store._seed_clause). Without that gate a
    # forgotten seed kept surfacing fake events in the daily brief and firing
    # fake reminder notifications, which looked exactly like the model
    # hallucinating today's schedule.
    print("  NOTE: these are INVISIBLE to Wisp unless the backend runs with "
          "WISP_QA_SEED=1 — set it before launching, and run `clear-db` when "
          "you're done testing.")

    for category, text, pinned in build_facts():
        res = fact_store.add(text, category=category)
        if pinned:
            fact_store.set_pinned(res["id"], True)
        verb = "updated" if res["updated"] else "added"
        print(f"✓ {verb} fact [{category}]: {text[:70]}…")


def clear_db() -> None:
    from service.assistant.store import assistant_store
    from service.memory.facts import store as fact_store

    with assistant_store._lock:
        ids = [r["id"] for r in assistant_store._db.execute(
            "SELECT id FROM commitments WHERE source=?", (SEED_SOURCE,)).fetchall()]
        for cid in ids:
            assistant_store._db.execute("DELETE FROM notify_log WHERE commitment_id=?", (cid,))
        cur = assistant_store._db.execute("DELETE FROM commitments WHERE source=?", (SEED_SOURCE,))
        assistant_store._db.commit()
    print(f"✓ removed {cur.rowcount} seeded commitments")

    with fact_store._lock:
        cur = fact_store._db.execute("DELETE FROM facts WHERE text LIKE ?", (QA_TAG + "%",))
        fact_store._db.commit()
    print(f"✓ removed {cur.rowcount} seeded facts")


def status() -> None:
    from service.assistant.store import assistant_store
    from service.memory.facts import store as fact_store

    n_commit = assistant_store._db.execute(
        "SELECT COUNT(*) AS n FROM commitments WHERE source=?", (SEED_SOURCE,)
    ).fetchone()["n"]
    n_facts = fact_store._db.execute(
        "SELECT COUNT(*) AS n FROM facts WHERE text LIKE ?", (QA_TAG + "%",)
    ).fetchone()["n"]
    print(f"seeded commitments: {n_commit}")
    print(f"seeded facts:       {n_facts}")
    print(f"cache fixtures:     {'present' if FIXTURE_DIR.exists() and any(FIXTURE_DIR.iterdir()) else 'not built'} ({FIXTURE_DIR})")


# ---------------------------------------------------------------------------
# Cache fixtures (Messages / Mail / Notes / Contacts) — repo-local files only
# ---------------------------------------------------------------------------

def build_messages() -> str:
    return wire.messages_lines(persona.message_rows(NOW))


def build_email_headers(deep_history: bool = False) -> str:
    return wire.email_headers(persona.email_header_rows(NOW, deep_history=deep_history))


def build_email_raw() -> str:
    return wire.email_raw(persona.email_raw_rows(NOW))


def build_notes() -> str:
    return wire.notes_raw(persona.note_rows(NOW))


def build_cache() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    norm_contacts, raw_contacts = persona.contacts()
    files = {
        "messages.txt": build_messages(),
        "contacts.txt": json.dumps(norm_contacts, indent=2),
        "contact_handles.txt": json.dumps(persona.contact_handles(), indent=2),
        "email_headers.txt": build_email_headers(deep_history=False),
        "email_history.txt": build_email_headers(deep_history=True),
        "email_raw.txt": build_email_raw(),
        "identity_emails.txt": "\n".join(persona.IDENTITY_EMAILS),
        "notes.txt": build_notes(),
    }
    for name, content in files.items():
        (FIXTURE_DIR / name).write_text(content, encoding="utf-8")
    print(f"✓ wrote {len(files)} cache fixture files to {FIXTURE_DIR}")
    print("  (these are repo-local — nothing under ~/.moe was touched; use "
          "scripts/install_cache_fixtures.sh to try them in a real run)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["seed-db", "clear-db", "build-cache", "status"])
    args = ap.parse_args()
    {"seed-db": seed_db, "clear-db": clear_db,
     "build-cache": build_cache, "status": status}[args.cmd]()


if __name__ == "__main__":
    main()
