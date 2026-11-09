#!/usr/bin/env python3
"""Each registered Wisp tool's category and its parameters' Python defaults, read from the pinned service code with ``ast``
(nothing is imported or run). ``choose_traces.py`` uses it for two things only, both from the tool's own code:

- an argument left out on one side matches the other side's value only when that value IS the function's default, because
  the call then runs exactly the same way (``search_reminders(query="")`` is ``search_reminders()``);
- calls in one step may come in any order only when every one of them is a read (category ``*_read`` or ``compute``).

    tool_specs.py --wisp-repo PIN --out tool_specs.json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

READ_CATEGORIES = frozenset({"assistant_read", "browser_history_read", "compute", "email_read", "fs_read", "messages_read",
                             "notes_read", "system_read", "web_read"})


def _literal(node):
    try:
        v = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None, False
    if v is None or isinstance(v, (bool, int, float, str)):
        return v, True
    return None, False                      # a list/dict default is mutable state, not a plain value: no equivalence


def specs_from_source(text: str) -> dict:
    out = {}
    for fn in ast.walk(ast.parse(text)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in fn.decorator_list:
            if not (isinstance(dec, ast.Call) and getattr(dec.func, "id", None) in ("register", "tool")):
                continue
            kw = {k.arg: k.value for k in dec.keywords}
            name, ok = _literal(dec.args[0]) if dec.args else _literal(kw["name"]) if "name" in kw else (None, False)
            if not ok or not isinstance(name, str):
                continue
            cat = next((_literal(k.value)[0] for k in dec.keywords if k.arg == "category"), None)
            if cat is None and len(dec.args) >= 4:
                cat = _literal(dec.args[3])[0]
            a = fn.args
            params = a.posonlyargs + a.args
            defaults = {}
            for p, d in zip(params[len(params) - len(a.defaults):], a.defaults):
                v, ok = _literal(d)
                if ok:
                    defaults[p.arg] = v
            for p, d in zip(a.kwonlyargs, a.kw_defaults):
                if d is not None:
                    v, ok = _literal(d)
                    if ok:
                        defaults[p.arg] = v
            out[name] = {"category": cat, "read": cat in READ_CATEGORIES, "defaults": defaults}
    return out


def build(repo: Path) -> dict:
    specs = {}
    for path in sorted((repo / "service").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "@register(" in text or "@tool(" in text:
            for name, spec in specs_from_source(text).items():
                if name in specs:
                    sys.exit(f"tool_specs: {name} is registered twice ({path})")
                specs[name] = spec
    return dict(sorted(specs.items()))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wisp-repo", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    specs = build(Path(a.wisp_repo))
    Path(a.out).write_text(json.dumps(specs, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(specs)} tools, {sum(s['read'] for s in specs.values())} reads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
