#!/usr/bin/env python3
"""Which code a step really ran on. Every stats.json records its own step's code; none copies an earlier step's head.

``code``: the training repo's HEAD, and whether training/ had uncommitted code. When it did, ``code_dirty_sha256`` hashes the
diff against HEAD plus each untracked .py/.sh file under training/, so two dirty runs can be told apart, and the head alone
never claims a commit the run didn't match.

``service``: the Wisp code that shaped prompts and tools. When WISP_REPO is the repo itself, it's the last commit touching
service/ plus a dirty flag. When it's a pinned copy such as ``data/pin-7e442f2d``, every file's blob is compared with that
commit: ``service_head`` is set only if service/ matches it file for file, and files that differ are listed by path and blob.

With no git (a node with only the kit), ``CODE_PROVENANCE.json`` beside the scripts is used if present, else the head is
``unknown``. Nothing is inherited.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

CODE_SUFFIXES = (".py", ".sh")
KIT_FILE = "CODE_PROVENANCE.json"


def _git(cwd, *args, stdin: str | None = None) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, input=stdin, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def code(code_dir) -> dict:
    code_dir = Path(code_dir).resolve()
    top = (_git(code_dir, "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        kit = code_dir / KIT_FILE
        if kit.is_file():
            return json.loads(kit.read_text()) | {"code_source": f"kit {KIT_FILE}"}
        return {"code_head": "unknown", "code_source": "no git and no kit provenance"}
    head = (_git(top, "rev-parse", "HEAD") or "").strip() or "unknown"
    changed = [ln[3:] for ln in (_git(top, "status", "--porcelain=v1", "--untracked-files=all", "--", "training") or "").splitlines()
               if ln[:2] != "??" or ln[3:].endswith(CODE_SUFFIXES)]
    out = {"code_head": head, "code_dirty": bool(changed), "code_source": "git"}
    if changed:
        h = hashlib.sha256((_git(top, "diff", "HEAD", "--binary", "--", "training") or "").encode())
        for rel in sorted(p for p in changed if not _git(top, "ls-files", "--error-unmatch", "--", p)):
            f = Path(top) / rel
            if f.is_file():
                h.update(b"\0untracked\0" + rel.encode() + b"\0" + f.read_bytes())
        out["code_dirty_sha256"] = h.hexdigest()
        out["code_dirty_files"] = sorted(changed)
    return out


def _pin_commit(pin: Path, top: str) -> str | None:
    tail = pin.name.rsplit("-", 1)[-1]
    return (_git(top, "rev-parse", "--verify", "--quiet", f"{tail}^{{commit}}") or "").strip() or None


def service(wisp_repo, code_dir) -> dict:
    wisp = Path(wisp_repo).resolve()
    top = (_git(code_dir, "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        return {"service_head": "unknown", "service_source": "no git"}
    if wisp == Path(top).resolve():
        head = (_git(top, "log", "-1", "--format=%H", "--", "service") or "").strip() or "unknown"
        dirty = bool((_git(top, "status", "--porcelain=v1", "--", "service") or "").strip())
        return {"service_head": head, "service_dirty": dirty, "service_source": "repo"}
    files = sorted(str(p.relative_to(wisp)) for p in wisp.rglob("*") if p.is_file())
    blobs = (_git(top, "hash-object", "--stdin-paths", stdin="".join(str(wisp / f) + "\n" for f in files)) or "").split()
    have = dict(zip(files, blobs, strict=True))
    tree = hashlib.sha256("".join(f"{have[f]} {f}\n" for f in files).encode()).hexdigest()
    out = {"service_head": None, "service_source": f"pin {os.path.relpath(wisp, top)}", "pin_files": len(files), "pin_tree_sha256": tree}
    commit = _pin_commit(wisp, top)
    if not commit:
        return out | {"pin_commit": None}
    want = {}
    for ln in (_git(top, "ls-tree", "-r", commit, "--", "service", "scripts") or "").splitlines():
        meta, path = ln.split("\t", 1)
        want[path] = meta.split()[2]
    differ = sorted(p for p in set(have) | set(want) if have.get(p) != want.get(p))
    service_ok = not [p for p in differ if p.startswith("service/")]
    out |= {"pin_commit": commit, "service_head": commit if service_ok else None, "service_matches_pin_commit": service_ok,
            "pin_differs_from_commit": [{"path": p, "pin_blob": have.get(p), "commit_blob": want.get(p)} for p in differ]}
    return out


def describe(code_dir, wisp_repo=None) -> dict:
    out = code(code_dir)
    if wisp_repo:
        out |= service(wisp_repo, code_dir)
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(describe(sys.argv[1] if len(sys.argv) > 1 else ".", sys.argv[2] if len(sys.argv) > 2 else None), indent=1))
