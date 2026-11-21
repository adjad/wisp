"""Ideate projects on disk: folders, meta, the event log, the file list, path confinement.

Layout, under Wisp's data folder (service.paths.MOE_DIR, so WISP_HOME moves it):

    ideate/projects/<id>/work/          the harness's cwd; it edits only here
    ideate/projects/<id>/.ideate/       Wisp's own record: meta.json, events.jsonl,
                                        harness.log, the gate extension, the system
                                        prompt and the sandbox profile (read-only to
                                        the harness)
    ideate/projects/<id>/.ideate/agent/ the harness's private, writable state:
                                        pi/ (its config dir), sessions/, home/, tmp/, xdg/

The sandbox lets the harness write only work/ and .ideate/agent/, so it can never rewrite
the gate extension or the events the tab reads.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from service import paths
from service.ideate import config

STATUSES = ("idle", "starting", "running", "waiting", "done", "failed", "cancelled")
ACTIVE = frozenset({"starting", "running", "waiting"})
KINDS = ("general", "ui", "debug", "learn", "script")
EVENT_KINDS = ("text", "thinking", "tool", "approval", "file", "done", "error")

_ID = re.compile(r"[a-z0-9]{8}")
# Folders the file list never walks into: dependencies and tool caches, not the user's files.
_SKIP_DIRS = frozenset({"node_modules", ".git", "__pycache__", ".venv", "venv", ".mypy_cache",
                        ".pytest_cache", "dist", "build", ".next", ".cache"})


class ProjectNotFound(LookupError):
    pass


def projects_root() -> Path:
    """Read at call time, so a test (or WISP_HOME) moving MOE_DIR is followed."""
    return Path(paths.MOE_DIR) / "ideate" / "projects"


def valid_id(project_id: str) -> bool:
    return isinstance(project_id, str) and bool(_ID.fullmatch(project_id))


def new_id() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


_FILLER = frozenset({"a", "an", "the", "please", "can", "you", "could", "would", "i", "want", "me",
                     "build", "make", "create", "write", "scaffold", "for", "to", "of", "with", "my",
                     "some", "simple", "as", "in", "small", "little", "quick", "that", "which", "let's", "lets"})


def display_name(prompt: str, limit: int = 28) -> str:
    """A short, safe title from the prompt: letters, digits, spaces and a little punctuation,
    the first few meaningful words, at most `limit` characters. Never empty."""
    text = unicodedata.normalize("NFKC", str(prompt or ""))
    text = "".join(ch if (ch.isalnum() or ch in " -'") else " " for ch in text)
    words = [w.strip("-'") for w in text.split()]
    words = [w for w in words if w]
    meaningful = [w for w in words if w.lower() not in _FILLER] or words
    name = ""
    for word in meaningful[:5]:
        candidate = (name + " " + word).strip()
        if len(candidate) > limit:
            break
        name = candidate
    if not name and meaningful:
        name = meaningful[0][:limit]
    name = name.strip() or "Untitled idea"
    return name[0].upper() + name[1:]


def slug(name: str, limit: int = 32) -> str:
    """A file-name-safe slug (lowercase ascii letters, digits and dashes)."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:limit].strip("-") or "idea"


def cap(text, limit: int) -> str:
    text = "" if text is None else str(text)
    text = text.replace("\x00", "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# -------------------------------------------------------------------- confinement ----------

def inside(root: Path, raw: str, cwd: Path | None = None) -> Path | None:
    """`raw` as a path inside `root`, or None when it is not provably inside.

    Relative paths are taken from `cwd` (default `root`). Refused: NUL, `~`, any `..`
    component, a path whose resolved form leaves `root`, and a path through a symlink
    (an existing component that is a link, the target included)."""
    if not isinstance(raw, str) or not raw or "\x00" in raw or raw.startswith("~"):
        return None
    candidate = Path(raw)
    if ".." in candidate.parts:
        return None
    base = Path(os.path.abspath(cwd or root))
    full = Path(os.path.abspath(candidate if candidate.is_absolute() else base / candidate))
    real_root = Path(os.path.realpath(root))
    lexical_root = Path(os.path.abspath(root))
    # Accept either spelling of the root (/var vs /private/var), then walk the rest.
    for spelled in (lexical_root, real_root):
        try:
            rest = full.relative_to(spelled)
        except ValueError:
            continue
        walk = real_root
        for part in rest.parts:
            walk = walk / part
            try:
                info = os.lstat(walk)
            except FileNotFoundError:
                break                      # the rest does not exist yet: nothing to follow
            except OSError:
                return None
            if stat.S_ISLNK(info.st_mode):
                return None
        resolved = Path(os.path.realpath(real_root / rest))
        if resolved == real_root or real_root in resolved.parents:
            return real_root / rest
        return None
    return None


def relative_display(work: Path, raw: str) -> str:
    """A path for the tab: relative to work/ when inside it, else as given (capped)."""
    found = inside(work, raw) if raw else None
    if found is not None:
        try:
            return str(found.relative_to(Path(os.path.realpath(work)))) or "."
        except ValueError:
            pass
    return cap(raw, 200)


# --------------------------------------------------------------------------- projects ------

@dataclass
class Project:
    id: str
    root: Path

    @property
    def work(self) -> Path:
        return self.root / "work"

    @property
    def private(self) -> Path:
        return self.root / ".ideate"

    @property
    def agent(self) -> Path:
        return self.private / "agent"

    @property
    def meta_path(self) -> Path:
        return self.private / "meta.json"

    @property
    def events_path(self) -> Path:
        return self.private / "events.jsonl"

    @property
    def log_path(self) -> Path:
        return self.private / "harness.log"


class Store:
    """Projects and their event logs. Thread-safe; one lock per project."""

    def __init__(self, root: Path | None = None):
        self._root = root
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._seq: dict[str, int] = {}
        self._recovered: set[str] = set()

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else projects_root()

    def lock(self, project_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(project_id, threading.RLock())

    def project(self, project_id: str) -> Project:
        if not valid_id(project_id):
            raise ProjectNotFound(project_id)
        project = Project(project_id, self.root / project_id)
        if not project.meta_path.is_file():
            raise ProjectNotFound(project_id)
        return project

    def create(self, prompt: str, kind: str, name: str | None = None) -> Project:
        paths.private_mkdirs(self.root)
        for _ in range(16):
            project_id = new_id()
            folder = self.root / project_id
            try:
                folder.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("could not pick a project id")
        project = Project(project_id, folder)
        for sub in (project.work, project.private, project.agent):
            sub.mkdir(mode=0o700, exist_ok=True)
        now = time.time()
        meta = {"id": project_id, "name": display_name(name or prompt), "kind": kind if kind in KINDS else "general",
                "status": "idle", "created_at": now, "updated_at": now, "prompt": cap(prompt, config.PROMPT_CHARS),
                "turns": 0}
        self.write_meta(project, meta)
        project.events_path.touch(mode=0o600)
        return project

    def list(self) -> list[dict]:
        rows = []
        if not self.root.is_dir():
            return rows
        for folder in self.root.iterdir():
            if not valid_id(folder.name) or folder.is_symlink():
                continue
            try:
                meta = self.meta(Project(folder.name, folder))
            except (OSError, ValueError):
                continue
            rows.append(summary(meta))
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return rows

    # meta ---------------------------------------------------------------------------------
    def meta(self, project: Project) -> dict:
        with self.lock(project.id):
            data = json.loads(project.meta_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("bad meta")
            return data

    def write_meta(self, project: Project, meta: dict) -> None:
        with self.lock(project.id):
            paths.private_write_bytes(project.meta_path, json.dumps(meta, sort_keys=True).encode())

    def update(self, project: Project, **fields) -> dict:
        with self.lock(project.id):
            meta = self.meta(project)
            meta.update(fields)
            meta["updated_at"] = time.time()
            self.write_meta(project, meta)
            return meta

    def recover(self, project: Project, running: bool) -> None:
        """A project left active by a service that stopped is failed, once, with a line why."""
        if running or project.id in self._recovered:
            return
        with self.lock(project.id):
            self._recovered.add(project.id)
            meta = self.meta(project)
            if meta.get("status") in ACTIVE:
                self.append(project, "error", text="Wisp stopped while this was running. Your files are kept.")
                self.update(project, status="failed")

    # events -------------------------------------------------------------------------------
    def _last_seq(self, project: Project) -> int:
        if project.id in self._seq:
            return self._seq[project.id]
        last = 0
        try:
            with project.events_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        last = max(last, int(json.loads(line).get("seq", 0)))
                    except (ValueError, TypeError, AttributeError):
                        continue
        except FileNotFoundError:
            pass
        self._seq[project.id] = last
        return last

    def append(self, project: Project, kind: str, *, text: str = "", tool: str = "", path: str = "",
               status: str = "") -> dict:
        if kind not in EVENT_KINDS:
            raise ValueError(kind)
        with self.lock(project.id):
            seq = self._last_seq(project) + 1
            event = {"seq": seq, "kind": kind, "text": cap(text, config.EVENT_TEXT_CHARS),
                     "tool": cap(tool, 40), "path": cap(path, 300), "status": cap(status, 20), "ts": time.time()}
            paths.private_append_text(project.events_path, json.dumps(event, ensure_ascii=False) + "\n")
            self._seq[project.id] = seq
            return event

    def events(self, project: Project, after: int = 0, limit: int = config.EVENTS_PER_REPLY) -> list[dict]:
        out = []
        with self.lock(project.id):
            try:
                with project.events_path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and int(event.get("seq", 0)) > after:
                            out.append(event)
            except FileNotFoundError:
                return []
        return out[:limit]

    # files --------------------------------------------------------------------------------
    def files(self, project: Project, limit: int = config.FILES_LISTED) -> list[dict]:
        """Regular files under work/, relative paths, no links followed, caches skipped."""
        rows: list[dict] = []
        work = project.work
        if not work.is_dir() or work.is_symlink():
            return rows
        for top, dirs, names in os.walk(work, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
                             and not os.path.islink(os.path.join(top, d)))
            for name in sorted(names):
                if name.startswith(".") and name not in (".gitignore", ".env.example"):
                    continue
                full = os.path.join(top, name)
                try:
                    info = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                rows.append({"path": os.path.relpath(full, work), "size": info.st_size})
                if len(rows) >= limit:
                    return rows
        return rows


def summary(meta: dict) -> dict:
    return {key: meta.get(key) for key in ("id", "name", "kind", "status", "created_at", "updated_at")}
