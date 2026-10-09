"""A frozen, offline view of the sources attention would read.

Everything downstream (labelling, scoring, later detectors) works on a snapshot
directory rather than the live caches, for three reasons:

  * a score is only comparable between two detector versions if both saw the
    same data;
  * the live caches change every five minutes and the labels would drift out
    from under their items;
  * the snapshot holds real personal text, so it lives under the user's own
    ~/.moe (0700/0600) and `freeze_snapshot` refuses to write inside the
    repository. Only synthetic fixtures are ever committed.

A snapshot is plain files: `messages.txt` and `email_headers.txt` copied
byte-for-byte from the cache, `commitments.json` exported from assistant.db, and
`manifest.json` with counts and hashes.

Mail is header-only here (sender, subject, time): the cache keeps full bodies
for only about a week. Bodies are a Slice 1 concern.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path

SCHEMA = 1
REPO_ROOT = Path(__file__).resolve().parents[2]
_V3 = "V3 | "
_SNAPSHOT_FILES = ("messages.txt", "email_headers.txt")

# A sampling prefilter, NOT a detector. It is deliberately loose (high recall,
# low precision) so the labelled sample contains most of the real commitments;
# the "control" stratum exists to measure what this misses.
_CUE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)(?!\w)"
    r"|\b\d{1,2}:\d{2}\b|\bnoon\b|\bmidnight\b"
    r"|\b(?:today|tonight|tomorrow|tmrw|tmr|this\s+(?:morning|afternoon|evening|weekend)|"
    r"next\s+(?:week|weekend)|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tues?|wed|thurs?|fri|sat)\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}/\d{1,2}\b"
    r"|\b(?:meet|meeting|pick\s*(?:you\s*)?up|drop\s*(?:you\s*)?off|dinner|lunch|breakfast|"
    r"brunch|class|exam|quiz|appointment|flight|reservation|come\s+over|see\s+you|"
    r"due|deadline|rsvp|party|game|practice)\b",
    re.I)


def has_cue(text: str) -> bool:
    return bool(_CUE.search(text or ""))


@dataclass(frozen=True)
class Item:
    id: str                 # "msg:<guid>" or "mail:<hash>"; stable across snapshots
    source: str             # "messages" | "mail"
    ts: float               # unix seconds
    direction: str          # "incoming" | "outgoing"; every cached mail header is incoming
    sender: str
    conversation: str
    text: str               # message body, or the mail subject


@dataclass
class Snapshot:
    root: Path
    manifest: dict
    items: list[Item]
    commitments: list[dict]
    _threads: dict[str, list[Item]] = field(default_factory=dict, repr=False)

    @property
    def snapshot_id(self) -> str:
        return self.root.name

    def by_id(self) -> dict[str, Item]:
        return {item.id: item for item in self.items}

    def thread_before(self, item: Item, n: int = 3) -> list[Item]:
        """The n messages that preceded `item` in its conversation, oldest first."""
        if not self._threads:
            for it in self.items:
                self._threads.setdefault(it.conversation, []).append(it)
            for rows in self._threads.values():
                rows.sort(key=lambda r: r.ts)
        earlier = [r for r in self._threads.get(item.conversation, ())
                   if r.ts < item.ts and r.id != item.id]
        return earlier[-n:]

    def commitments_after(self, item: Item, hours: float = 48, limit: int = 6) -> list[dict]:
        """Calendar/Reminders rows starting within `hours` after the item arrived.

        This is what the user could have already had on file when the message
        landed. A row dismissed since then is still shown; it was on file at the
        time, and "known" is judged at arrival.
        """
        end = item.ts + hours * 3600
        rows = [c for c in self.commitments
                if c.get("when_ts") is not None and item.ts <= float(c["when_ts"]) <= end]
        rows.sort(key=lambda c: float(c["when_ts"]))
        return rows[:limit]


# ---------------------------------------------------------------- parsing

def parse_messages(text: str) -> tuple[list[Item], dict]:
    """V3 structured records -> Items, plus {coverage, malformed}.

    Malformed or unrecognised lines are counted, never raised: a snapshot taken
    mid-sync must still load. The V2 free-text lines are ignored; V3 carries the
    same messages with direction and a GUID.
    """
    items: list[Item] = []
    coverage: dict = {}
    malformed = 0
    for line in text.split("\n"):
        if not line.startswith(_V3):
            continue
        try:
            obj = json.loads(line[len(_V3):])
        except ValueError:
            malformed += 1
            continue
        if not isinstance(obj, dict):
            malformed += 1
            continue
        if obj.get("kind") == "coverage":
            coverage = obj
            continue
        rec = obj.get("record")
        if not isinstance(rec, dict):
            malformed += 1
            continue
        try:
            body = rec["text"]
            direction = rec["direction"]
            ts = float(rec["timestamp"])
            guid = str(rec["guid"])
            sender = str(rec.get("sender") or "")
            conversation = str(rec.get("conversation") or "")
        except (KeyError, TypeError, ValueError):
            malformed += 1
            continue
        if direction not in ("incoming", "outgoing") or not isinstance(body, str):
            malformed += 1
            continue
        if not body.strip():
            continue                      # attachment-only
        items.append(Item(f"msg:{guid}", "messages", ts, direction, sender, conversation, body))
    return items, {"coverage": coverage, "malformed": malformed}


def _mail_rows(text: str):
    """Yield (ts, account, name, address, message_id, subject) per distinct header line.

    Understands the two cache formats `email_tools._parse_header_records` does:
    the current "H2\\x01..." rows and the older "epoch | R/U | account | sender |
    subject" rows, and collapses byte-identical lines the way it does.

    Deliberately NOT a call into email_tools: importing that module imports the
    whole service.tools package, which constructs the live AssistantStore (and
    its migrations) against ~/.moe/assistant.db. An offline measurement tool must
    not open the live database. tests/test_attention_slice0.py pins this parser
    to email_tools' output so the two cannot drift apart.
    """
    seen: set[str] = set()
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line or line in seen:
            continue
        seen.add(line)
        if line.startswith("H2\x01"):
            f = line.split("\x01")
            if len(f) not in (9, 10) or f[2] not in ("R", "U"):
                continue
            try:
                ts = float(f[1])
            except ValueError:
                continue
            name, address = f[5], parseaddr(f[6] or f[5])[1].strip().casefold()
            yield ts, f[3], name, address, f[7].strip(), f[8]
            continue
        parts = line.split(" | ", 4)
        if len(parts) == 5 and parts[1] in ("R", "U"):
            parts = [parts[0], parts[2], parts[3], parts[4]]
        elif len(parts) == 5:
            parts = line.split(" | ", 3)
        if len(parts) != 4:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        name, address = parseaddr(parts[2])
        yield ts, parts[1], name or parts[2], address.strip().casefold(), "", parts[3]


def parse_mail(text: str) -> list[Item]:
    """Mail header lines -> Items (inbox only; the Sent mailbox is not cached)."""
    items: list[Item] = []
    for ts, account, name, address, message_id, subject in _mail_rows(text):
        key = message_id or "|".join((account, str(ts), address or name, subject))
        digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
        items.append(Item(f"mail:{digest}", "mail", ts, "incoming",
                          name or address or "Unknown sender", address or name, subject))
    return items


# ---------------------------------------------------------------- snapshots

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def ensure_private_tree(moe_dir: Path) -> Path:
    """Create ~/.moe/attention and its snapshots/ at 0700 and return the former."""
    base = moe_dir / "attention"
    for path in (base, base / "snapshots"):
        _private_dir(path)
    return base


def _private_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def freeze_snapshot(moe_dir: Path, out_dir: Path, *, now: float | None = None) -> Path:
    """Copy the caches and export commitments into `out_dir`. Read-only on sources."""
    if _under(out_dir, REPO_ROOT):
        raise ValueError("Refusing to write a snapshot inside the repository: it holds "
                         "real personal text. Use a directory under ~/.moe.")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty; snapshots are immutable")
    cache = moe_dir / "cache"
    _private_dir(out_dir)
    files: dict[str, str] = {}
    for name in _SNAPSHOT_FILES:
        src = cache / name
        data = src.read_bytes() if src.exists() else b""
        _private_write(out_dir / name, data)
        files[name] = _sha256(out_dir / name)

    commitments: list[dict] = []
    db_path = moe_dir / "assistant.db"
    if db_path.exists():
        # Read-only URI: freezing must never touch the live store.
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            db.row_factory = sqlite3.Row
            commitments = [dict(r) for r in db.execute("SELECT * FROM commitments")]
        finally:
            db.close()
    _private_write(out_dir / "commitments.json",
                   json.dumps(commitments, sort_keys=True).encode())
    files["commitments.json"] = _sha256(out_dir / "commitments.json")

    snap = load_snapshot(out_dir, _skip_manifest=True)
    stamps = [i.ts for i in snap.items]
    manifest = {
        "schema": SCHEMA,
        "created_at": now if now is not None else time.time(),
        "counts": {"messages": sum(i.source == "messages" for i in snap.items),
                   "mail": sum(i.source == "mail" for i in snap.items),
                   "commitments": len(commitments)},
        "range": [min(stamps), max(stamps)] if stamps else None,
        "files": files,
    }
    _private_write(out_dir / "manifest.json", json.dumps(manifest, indent=1, sort_keys=True).encode())
    return out_dir


def load_snapshot(root: Path, *, _skip_manifest: bool = False) -> Snapshot:
    root = Path(root)
    manifest: dict = {}
    if not _skip_manifest:
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest.get("schema") != SCHEMA:
            raise ValueError(f"Unsupported snapshot schema {manifest.get('schema')!r}")
        for name, digest in manifest["files"].items():
            if _sha256(root / name) != digest:
                raise ValueError(f"Snapshot file {name} changed since it was frozen")
    msgs, meta = parse_messages((root / "messages.txt").read_text(encoding="utf-8"))
    mail = parse_mail((root / "email_headers.txt").read_text(encoding="utf-8"))
    commitments = json.loads((root / "commitments.json").read_text())
    items = sorted(msgs + mail, key=lambda i: i.ts)
    manifest = {**manifest, "parse": meta}
    return Snapshot(root, manifest, items, commitments)


def latest_snapshot(moe_dir: Path) -> Path:
    base = moe_dir / "attention" / "snapshots"
    found = sorted(p for p in base.iterdir() if (p / "manifest.json").exists()) if base.exists() else []
    if not found:
        raise FileNotFoundError("No snapshot yet; run: scripts/attention_snapshot.py")
    return found[-1]


# ---------------------------------------------------------------- sampling

def sample(snapshot: Snapshot, *, seed: int = 0, n_cue: int = 120, n_control: int = 60,
           per_conversation: int = 30, n_mail: int = 30) -> list[dict]:
    """Choose what the user will label: [{"id", "stratum"}], shuffled.

    Strata: "cue" (incoming text matching the loose cue prefilter), "control"
    (incoming text that does not; labelling a few measures what the prefilter
    misses), and "mail" (cue-matching subjects). Outgoing messages are context,
    never candidates. A per-conversation cap stops one busy group chat filling
    the "cue" stratum, the same failure the message summaries had. The control
    stratum is deliberately uncapped: it is a plain random draw from everything
    the prefilter rejected, and capping it would bias the miss-rate estimate
    that evaluate() scales up to the whole rejected population.
    """
    rng = random.Random(seed)

    def pick(pool: list[Item], n: int, cap: int | None) -> list[Item]:
        pool = sorted(pool, key=lambda i: i.id)       # order-independent determinism
        rng.shuffle(pool)
        out: list[Item] = []
        counts: dict[str, int] = {}
        for it in pool:
            if cap is not None and counts.get(it.conversation, 0) >= cap:
                continue
            counts[it.conversation] = counts.get(it.conversation, 0) + 1
            out.append(it)
            if len(out) == n:
                break
        return out

    texts = [i for i in snapshot.items if i.source == "messages" and i.direction == "incoming"]
    mails = [i for i in snapshot.items if i.source == "mail"]
    chosen = (
        [(i, "cue") for i in pick([i for i in texts if has_cue(i.text)], n_cue, per_conversation)]
        + [(i, "control") for i in pick([i for i in texts if not has_cue(i.text)], n_control, None)]
        + [(i, "mail") for i in pick([i for i in mails if has_cue(i.text)], n_mail, None)])
    rng.shuffle(chosen)
    return [{"id": i.id, "stratum": s} for i, s in chosen]
