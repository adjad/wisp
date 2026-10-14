"""Licence review gate and third-party notices for the bundled oMLX engine.

The engine ships a pruned copy of oMLX's own site-packages. Every package that stays in the
bundle is read from the staged tree (so the notice describes exactly what ships), classified by
its declared licence and its licence file, and held to one rule:

    permissive licences pass; weak copyleft, strong copyleft and unknown ones fail the build
    unless build-support/engine-licenses.json holds a reviewed entry with the same class.

The same pass writes Documentation/THIRD-PARTY-NOTICES.md (with the source offer for the
copyleft packages) and copies each package's licence files into Documentation/licenses/.
"""
from __future__ import annotations

import json
import re
import shutil
from email.parser import Parser
from pathlib import Path

SUPPORT = Path(__file__).resolve().parent
REVIEWED = SUPPORT / "engine-licenses.json"
ENGINE_DIR = "Contents/Resources/engine"
NOTICE_NAME = "THIRD-PARTY-NOTICES.md"
LICENSES_DIR = "licenses"

PERMISSIVE, WEAK, STRONG, UNKNOWN = "permissive", "weak-copyleft", "strong-copyleft", "unknown"
_SEVERITY = {PERMISSIVE: 0, UNKNOWN: 1, WEAK: 2, STRONG: 3}

# "GPLv3", "GPL-2.0+" and "LGPLv2+" all count: only a letter run of two or more after the name
# (a different word) ends the match.
_STRONG = re.compile(r"(?<![A-Za-z])A?GPL(?![A-Za-z]{2})|GNU (General|Affero General) Public License", re.I)
_WEAK = re.compile(r"(?<![A-Za-z])LGPL(?![A-Za-z]{2})|GNU Lesser|GNU Library|\bMPL\b|Mozilla Public|\bEPL\b|\bCDDL\b", re.I)
# Licences that restrict use or redistribution beyond the copyleft families: never auto-pass.
# Named terms apply to declared fields and to licence files; the looser words only to declared
# fields, because permissive licence texts legitimately say "commercial or non-commercial".
_RESTRICTED_NAMED = re.compile(r"Commons[- ]Clause|Business Source|\bBUSL\b|\bSSPL\b|Elastic License|\bCC-BY-NC", re.I)
_RESTRICTED = re.compile(_RESTRICTED_NAMED.pattern + r"|Non-?Commercial|Proprietary", re.I)
# "A AND B", "A OR B WITH X": every identifier must be a family this gate recognises, so
# `MIT AND LicenseRef-Custom` or `Apache-2.0 AND CC-BY-SA-4.0` cannot pass on the MIT word alone.
_SPDX_SHAPE = re.compile(r"^\(?[A-Za-z0-9.+\-]+\)?(?: (?:AND|OR|WITH) \(?[A-Za-z0-9.+\-]+\)?)+$")
_PERMISSIVE = re.compile(
    r"\bMIT\b|\bBSD\b|\bApache\b|\bISC\b|\bPSF\b|\bPSFL\b|Python Software Foundation|\bHPND\b|\bCNRI\b|"
    r"\bUnlicense\b|\b0BSD\b|\bZlib\b|\bCC0\b|\bBSL-1\.0\b|Boost Software|\bPython-2\.0\b|"
    r"Permission to use, copy, modify, and/or distribute this software|"
    r"Permission is hereby granted, free of charge|"
    r"Redistribution and use in source and binary forms|"
    r"Licensed under the Apache License", re.I)
_LICENSE_NAME = re.compile(r"^(licen[sc]e|copying|notice|authors)", re.I)


class LicenseError(RuntimeError):
    pass


def classify(text: str) -> str:
    """Worst class named by `text` (a metadata licence field or the head of a licence file)."""
    text = " ".join((text or "").split())
    if not text or text.upper() in {"UNKNOWN", "NONE", "N/A"}:
        return UNKNOWN
    # "LGPL" contains "GPL": test the weak spellings first and strip them before the strong test.
    if _RESTRICTED.search(text) or _spdx_unrecognised(text):
        return UNKNOWN
    stripped = _WEAK.sub(" ", text)
    if _STRONG.search(stripped):
        return STRONG
    if _WEAK.search(text):
        return WEAK
    return PERMISSIVE if _PERMISSIVE.search(text) else UNKNOWN


def _spdx_unrecognised(text: str) -> bool:
    if not _SPDX_SHAPE.match(text):
        return False
    for token in re.split(r"\s+(?:AND|OR)\s+", text):
        token = token.split(" WITH ")[0].strip("() ")
        if not (_PERMISSIVE.search(token) or _WEAK.search(token) or _STRONG.search(token)):
            return True
    return False


def _file_head(path: Path, lines: int = 40) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return "".join(next(handle, "") for _ in range(lines))
    except OSError:
        return ""


def _file_class(path: Path) -> str:
    """Class of a licence file from its opening lines. Body text is ignored for the copyleft test:
    an LGPL file quotes the GPL, and a MIT file may list bundled components, so keywords alone mislead.
    A title among the first few lines, or the standard short "free software" notice, decides."""
    head = _file_head(path)
    lines = [" ".join(line.split()) for line in head.splitlines() if line.strip()]
    opening = " ".join(lines[:6])
    if re.search(r"(GNU )?(Lesser|Library) General Public License|Mozilla Public License", opening, re.I):
        return WEAK
    if re.search(r"GNU (General|Affero General) Public License", opening, re.I):
        return STRONG
    flat = " ".join(lines)
    if re.search(r"is free software[^.]{0,200}(GNU|GPL)", flat, re.I):
        return WEAK if re.search(r"Lesser|Library", flat, re.I) else STRONG
    if _RESTRICTED_NAMED.search(head):
        return UNKNOWN
    return PERMISSIVE if _PERMISSIVE.search(head) else UNKNOWN


def _file_restricted(path: Path) -> bool:
    return bool(_RESTRICTED_NAMED.search(_file_head(path)))


def _licence_files(dist_info: Path) -> list[Path]:
    found: list[Path] = []
    for path in sorted(dist_info.rglob("*"), key=lambda p: p.as_posix().casefold()):
        if path.is_symlink() or not path.is_file():
            continue
        # Anything under a licenses/ folder at any depth, or named like a licence file anywhere.
        if _LICENSE_NAME.match(path.name) or "licenses" in path.relative_to(dist_info).parts[:-1]:
            found.append(path)
    return found


def _declared_sources(metadata) -> list[str]:
    """Every place a package declares a licence; the gate judges all of them, not the first."""
    classifiers = "; ".join(sorted({c[len("License :: "):].strip() for c in metadata.get_all("Classifier", [])
                                    if c.startswith("License :: ")}, key=str.casefold))
    sources = [(metadata.get("License-Expression") or "").strip(), (metadata.get("License") or "").strip(),
               classifiers]
    return [" ".join(source.split()) for source in sources if source]


def _declared_licence(metadata) -> str:
    value = (metadata.get("License-Expression") or "").strip()
    if not value:
        value = (metadata.get("License") or "").strip()
    if not value:
        value = "; ".join(sorted({c[len("License :: "):].strip() for c in metadata.get_all("Classifier", [])
                                  if c.startswith("License :: ")}, key=str.casefold))
    return value


def _project_url(metadata) -> str:
    home = (metadata.get("Home-page") or "").strip()
    if home:
        return home
    for entry in metadata.get_all("Project-URL", []):
        label, _, url = entry.partition(",")
        if url.strip().startswith("https://") and label.strip().lower() in {"source", "source code", "homepage", "repository"}:
            return url.strip()
    for entry in metadata.get_all("Project-URL", []):
        url = entry.partition(",")[2].strip()
        if url.startswith("https://"):
            return url
    return ""


def engine_packages(engine_root: Path) -> list[dict]:
    """One record per kept package in <engine_root>/sp, sorted by name."""
    sp = Path(engine_root) / "sp"
    records = []
    for dist_info in sorted(sp.glob("*.dist-info"), key=lambda p: p.name.casefold()):
        if dist_info.is_symlink() or not dist_info.is_dir():
            continue
        metadata_path = dist_info / "METADATA"
        metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8", errors="replace")
                                     if metadata_path.is_file() else "")
        stem = dist_info.name[:-len(".dist-info")]
        name = (metadata.get("Name") or stem.rsplit("-", 1)[0]).strip()
        version = (metadata.get("Version") or stem.rsplit("-", 1)[-1]).strip()
        declared = _declared_licence(metadata)
        files = _licence_files(dist_info)
        # A long declared field is a pasted licence text (it can quote the GPL for a bundled
        # component), not an identifier, so it is judged through the licence files instead.
        classes = [classify(source) if len(source) <= 100 else UNKNOWN for source in _declared_sources(metadata)]
        if any(c != UNKNOWN for c in classes):
            classes = [c for c in classes if c != UNKNOWN]     # "UNKNOWN" beside a real identifier is noise
        classes = classes or [UNKNOWN]
        # A package that declares nothing useful is judged by its licence files. A file titled as
        # a GPL/LGPL/MPL text also counts for a declared one, so a wrong declaration cannot hide it.
        named = [_file_class(path) for path in files if path.name.upper().startswith(("LICEN", "COPYING"))]
        # Any file under licenses/ can carry a copyleft text, whatever it is called.
        others = [_file_class(path) for path in files if not path.name.upper().startswith(("LICEN", "COPYING"))]
        if classes[0] == UNKNOWN:
            classes = named or classes
        else:
            classes += [c for c in named if c in (WEAK, STRONG)]
        classes += [c for c in others if c in (WEAK, STRONG)]
        if any(_file_restricted(path) for path in files):
            classes.append(UNKNOWN)                               # restricted terms are never discarded
        klass = max(classes, key=_SEVERITY.__getitem__)
        records.append({"name": name, "version": version, "license": " ".join(declared.split())[:120],
                        "class": klass, "files": [p.relative_to(dist_info).as_posix() for p in files],
                        "dist_info": dist_info.name, "url": _project_url(metadata)})
    return records


def load_reviewed(path: Path = REVIEWED) -> dict:
    doc = json.loads(path.read_text())
    if doc.get("schema") != 1 or not isinstance(doc.get("packages"), dict) or not isinstance(doc.get("omlx"), dict):
        raise LicenseError("engine-licenses.json is malformed")
    for component in doc.get("components", []):
        if (not isinstance(component, dict)
                or not all(isinstance(component.get(k), str) and component[k]
                           for k in ("name", "license", "path", "copy_as"))
                or "/" in component["copy_as"] or component["path"].startswith("/")
                or ".." in Path(component["path"]).parts):
            raise LicenseError("Reviewed component entries need a name, licence, relative path and copy_as")
    for name, entry in doc["packages"].items():
        if (not isinstance(entry, dict) or entry.get("class") not in _SEVERITY
                or not isinstance(entry.get("note"), str) or not entry["note"].strip()):
            raise LicenseError(f"Reviewed licence entry for {name!r} needs a class and a note")
    return doc


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check(records: list[dict], reviewed: dict) -> list[str]:
    """Problems that must fail the build. Empty when every package is permissive or reviewed."""
    entries = {_canonical(name): entry for name, entry in reviewed["packages"].items()}
    problems = []
    for record in records:
        entry = entries.get(_canonical(record["name"]))
        label = f"{record['name']} {record['version']} ({record['license'] or 'no licence declared'})"
        if record["class"] != PERMISSIVE:
            if entry is None:
                problems.append(f"Unreviewed {record['class']} licence: {label}")
            elif entry["class"] != record["class"]:
                problems.append(f"Reviewed as {entry['class']} but now {record['class']}: {label}")
        if not record["files"] and not (entry or {}).get("no_licence_file"):
            problems.append(f"No licence file shipped and not reviewed as such: {label}")
    return problems


def _slug(record: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "-", f"{record['name']}-{record['version']}")


def notice_text(records: list[dict], reviewed: dict, omlx_version: str | None) -> str:
    entries = {_canonical(name): entry for name, entry in reviewed["packages"].items()}
    omlx = reviewed["omlx"]
    lines = [
        "# Third-party notices", "",
        "Wisp includes a private copy of the oMLX inference engine so it can run without a separate "
        "oMLX install. This file lists that engine and every third-party package that ships with it. "
        "Each package's own licence files are in `licenses/<name>-<version>/`.", "",
        "## oMLX", "",
        f"- Project: {omlx['url']}",
        f"- Licence: {omlx['license']}",
        f"- Version: {omlx_version or 'unknown'}",
        f"- Licence text: `{LICENSES_DIR}/oMLX-LICENSE.txt`",
        "- Wisp ships oMLX as released, with unused optional packages left out. No oMLX file is edited.", "",
    ]
    copyleft = [r for r in records if r["class"] in (WEAK, STRONG)]
    if copyleft:
        lines += ["## Packages under copyleft licences", ""]
        for record in copyleft:
            entry = entries[_canonical(record["name"])]
            lines += [f"### {record['name']} {record['version']}", "",
                      f"- Licence: {record['license']} ({record['class']})",
                      f"- Source code: {entry.get('source', record['url'])}", f"- {entry['note']}", ""]
        lines += ["### Source offer", "",
                  "Pure-Python packages above ship as readable source files inside the engine, in "
                  "`Contents/Resources/engine/sp/`. For any package whose compiled parts you want rebuilt, "
                  "the source at the address listed with it is the corresponding source for the version named.", ""]
    components = reviewed.get("components", [])
    if components or reviewed.get("vendored_javascript"):
        lines += ["## Other bundled components", ""]
        for component in components:
            lines.append(f"- {component['name']}: {component['license']} (`{LICENSES_DIR}/{component['copy_as']}`)")
        if reviewed.get("vendored_javascript"):
            lines.append(f"- {reviewed['vendored_javascript']}")
        lines.append("")
    lines += ["## All packages", "", "| Package | Version | Licence | Class | Licence files |", "|---|---|---|---|---|"]
    for record in records:
        shown = f"`licenses/{_slug(record)}/`" if record["files"] else "not shipped by upstream"
        licence = record["license"].replace("|", "/") or "not declared"
        lines.append(f"| {record['name']} | {record['version']} | {licence} | {record['class']} | {shown} |")
    lines.append("")
    return "\n".join(lines)


def write_notices(bundle: Path, reviewed: dict | None = None) -> list[dict]:
    """Gate and write the notices into <bundle>/Contents/Resources/Documentation. Raises on problems."""
    bundle = Path(bundle)
    engine_root = bundle / ENGINE_DIR
    reviewed = reviewed or load_reviewed()
    records = engine_packages(engine_root)
    problems = check(records, reviewed)
    if problems:
        raise LicenseError("Engine licence review failed:\n  " + "\n  ".join(problems))
    omlx_version = None
    try:
        omlx_version = json.loads((engine_root / "ENGINE.json").read_text()).get("omlx_version")
    except (OSError, ValueError):
        pass
    docs = bundle / "Contents/Resources/Documentation"
    licences = docs / LICENSES_DIR
    if licences.exists():
        shutil.rmtree(licences)
    licences.mkdir(parents=True)
    for record in records:
        if not record["files"]:
            continue
        target = licences / _slug(record)
        target.mkdir()
        source = engine_root / "sp" / record["dist_info"]
        for relative in record["files"]:
            destination = target / Path(relative).name
            if destination.exists():
                destination = target / relative.replace("/", "__")
            shutil.copyfile(source / relative, destination)
    omlx_text = SUPPORT / reviewed["omlx"]["licence_text"]
    if not omlx_text.is_file():
        raise LicenseError(f"oMLX licence text missing: {omlx_text}")
    shutil.copyfile(omlx_text, licences / "oMLX-LICENSE.txt")
    for component in reviewed.get("components", []):
        source = engine_root / component["path"]
        if source.is_symlink() or not source.is_file():
            raise LicenseError(f"Bundled component licence missing: {component['path']}")
        shutil.copyfile(source, licences / component["copy_as"])
    (docs / NOTICE_NAME).write_text(notice_text(records, reviewed, omlx_version), encoding="utf-8")
    return records


def validate(bundle: Path, reviewed: dict | None = None) -> None:
    """Structure check: the notice and licence copies must be present for a bundle that has an engine."""
    docs = Path(bundle) / "Contents/Resources/Documentation"
    for relative in (NOTICE_NAME, f"{LICENSES_DIR}/oMLX-LICENSE.txt"):
        if not (docs / relative).is_file():
            raise LicenseError(f"Missing third-party notice file: {relative}")
    text = (docs / NOTICE_NAME).read_text(encoding="utf-8")
    for component in (reviewed or load_reviewed()).get("components", []):
        if not (docs / LICENSES_DIR / component["copy_as"]).is_file():
            raise LicenseError(f"Licence copy missing for {component['name']}")
    for record in engine_packages(Path(bundle) / ENGINE_DIR):
        if f"| {record['name']} | {record['version']} |" not in text:
            raise LicenseError(f"Third-party notice does not list {record['name']} {record['version']}")
        if record["files"] and not (docs / LICENSES_DIR / _slug(record)).is_dir():
            raise LicenseError(f"Licence files missing for {record['name']} {record['version']}")
