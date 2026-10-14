"""Licence gate and third-party notices for the bundled engine (build-support/engine_licenses.py).

Synthetic bundles only: no oMLX app, no network, nothing executed.

    python tests/test_engine_licenses.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build-support"))

import engine_licenses as lic  # noqa: E402

PASS = FAIL = 0
MIT = "Permission is hereby granted, free of charge, to any person obtaining a copy"
GPL3 = "                    GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007\n"
LGPL21 = "GNU LESSER GENERAL PUBLIC LICENSE\nVersion 2.1\nthe ordinary GNU General Public License applies\n"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def package(sp: Path, name: str, version: str, declared: str | None, files: dict[str, str] | None = None,
            *, field: str = "License-Expression") -> None:
    dist_info = sp / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    meta = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    if declared is not None:
        meta += f"{field}: {declared}\n"
    meta += f"Project-URL: Source, https://example.test/{name}\n"
    (dist_info / "METADATA").write_text(meta)
    for relative, text in (files or {}).items():
        target = dist_info / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def bundle(tmp: Path, packages: list[tuple]) -> Path:
    root = tmp / "Wisp.app"
    engine = root / lic.ENGINE_DIR
    (engine / "sp").mkdir(parents=True)
    (engine / "ENGINE.json").write_text(json.dumps({"omlx_version": "0.7.1"}))
    for args in packages:
        package(engine / "sp", *args[:4], **(args[4] if len(args) > 4 else {}))
    (root / "Contents/Resources/Documentation").mkdir(parents=True)
    return root


def reviewed(**packages) -> dict:
    return {"schema": 1, "omlx": {"url": "https://example.test/omlx", "license": "Apache-2.0",
                                   "licence_text": "engine-licenses/oMLX-LICENSE.txt"},
            "packages": {name: {"note": "reviewed in test", **entry} for name, entry in packages.items()}}


def test_classification() -> None:
    for text, want in (("MIT", lic.PERMISSIVE), ("Apache-2.0 OR BSD-3-Clause", lic.PERMISSIVE),
                       ("OSI Approved :: BSD License", lic.PERMISSIVE), ("PSF-2.0", lic.PERMISSIVE),
                       ("MPL-2.0 AND MIT", lic.WEAK), ("LGPL-2.1-or-later", lic.WEAK),
                       ("GNU Lesser General Public License v3", lic.WEAK),
                       ("GNU General Public License v3", lic.STRONG), ("GPL-3.0-only", lic.STRONG),
                       ("AGPL-3.0", lic.STRONG), ("GPL-2.0 OR MIT", lic.STRONG),
                       ("UNKNOWN", lic.UNKNOWN), ("", lic.UNKNOWN), ("Dual License", lic.UNKNOWN),
                       ("Proprietary", lic.UNKNOWN)):
        check(f"{text or '(empty)'!r} -> {want}", lic.classify(text) == want, lic.classify(text))


def test_packages_and_gate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        root = bundle(tmp, [
            ("plain", "1.0", "MIT", {"LICENSE": MIT}),
            ("gplpkg", "2.0", "GPL-3.0-only", {"licenses/LICENSE": GPL3}),
            ("lgplpkg", "3.0", "LGPL-2.1-only", {"licenses/COPYING.LGPL": LGPL21}),
            ("hidden", "4.0", "MIT", {"LICENSE": GPL3}),
            ("undeclared", "5.0", None, {"LICENSE": MIT}),
            ("mystery", "6.0", "UNKNOWN", {"LICENSE": "all rights reserved"}),
            ("nofile", "7.0", "MIT", {}),
            ("quoted", "8.0", MIT + " " + "x" * 120 + " GNU General Public License exception", {"LICENSE": MIT}),
        ])
        records = {r["name"]: r for r in lic.engine_packages(root / lic.ENGINE_DIR)}
        check("every package is listed", len(records) == 8)
        check("permissive package", records["plain"]["class"] == lic.PERMISSIVE)
        check("declared GPL", records["gplpkg"]["class"] == lic.STRONG)
        check("an LGPL file that quotes the GPL stays weak, not strong", records["lgplpkg"]["class"] == lic.WEAK)
        check("a GPL file under a permissive declaration is caught", records["hidden"]["class"] == lic.STRONG)
        check("an undeclared package takes its class from its licence file", records["undeclared"]["class"] == lic.PERMISSIVE)
        check("an unrecognisable licence is unknown", records["mystery"]["class"] == lic.UNKNOWN)
        check("a pasted licence text in the field is judged by the files",
              records["quoted"]["class"] == lic.PERMISSIVE)
        check("project address comes from the metadata", records["plain"]["url"] == "https://example.test/plain")

        problems = lic.check(list(records.values()), reviewed())
        text = "\n".join(problems)
        for name in ("gplpkg", "lgplpkg", "hidden", "mystery"):
            check(f"unreviewed {name} fails the gate", name in text)
        check("a missing licence file fails the gate", "nofile" in text and "No licence file" in text)
        check("permissive packages with files pass", "plain" not in text and "quoted" not in text and "undeclared" not in text)

        ok = reviewed(gplpkg={"class": "strong-copyleft"}, lgplpkg={"class": "weak-copyleft"},
                      hidden={"class": "strong-copyleft"}, mystery={"class": "unknown"},
                      nofile={"class": "permissive", "no_licence_file": True})
        check("reviewed entries clear every problem", lic.check(list(records.values()), ok) == [])
        stale = reviewed(gplpkg={"class": "weak-copyleft"}, lgplpkg={"class": "weak-copyleft"},
                         hidden={"class": "strong-copyleft"}, mystery={"class": "unknown"},
                         nofile={"class": "permissive", "no_licence_file": True})
        check("a reviewed class that no longer matches fails", any("Reviewed as weak-copyleft" in p
              for p in lic.check(list(records.values()), stale)))
        check("matching ignores case and punctuation in the package name",
              lic.check([records["gplpkg"]], reviewed(**{"GPLPKG": {"class": "strong-copyleft"}})) == [])


def test_notices_written() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        root = bundle(tmp, [
            ("plain", "1.0", "MIT", {"LICENSE": MIT}),
            ("gplpkg", "2.0", "GPL-3.0-only", {"licenses/LICENSE": GPL3, "licenses/AUTHORS.rst": "authors"}),
            ("nofile", "7.0", "Apache-2.0", {}),
        ])
        ok = reviewed(gplpkg={"class": "strong-copyleft", "source": "https://example.test/gplpkg-src"},
                      nofile={"class": "permissive", "no_licence_file": True})
        records = lic.write_notices(root, ok)
        docs = root / "Contents/Resources/Documentation"
        notice = (docs / lic.NOTICE_NAME).read_text()
        check("notice written for every package", len(records) == 3 and all(f"| {n} |" in notice for n in ("plain", "gplpkg", "nofile")))
        check("notice names oMLX, its licence and its version",
              "## oMLX" in notice and "Apache-2.0" in notice and "0.7.1" in notice)
        check("copyleft section carries the source address and note",
              "https://example.test/gplpkg-src" in notice and "reviewed in test" in notice and "Source offer" in notice)
        check("permissive package is not in the copyleft section", "### plain" not in notice)
        check("licence files are copied per package",
              (docs / "licenses/gplpkg-2.0/LICENSE").read_text() == GPL3
              and (docs / "licenses/gplpkg-2.0/AUTHORS.rst").is_file() and (docs / "licenses/plain-1.0/LICENSE").is_file())
        check("a package without upstream files says so", "not shipped by upstream" in notice
              and not (docs / "licenses/nofile-7.0").exists())
        check("the oMLX Apache text is copied", "Apache License" in (docs / "licenses/oMLX-LICENSE.txt").read_text())
        lic.validate(root, ok)
        check("validate accepts what write_notices produced", True)
        lic.write_notices(root, ok)
        check("writing twice is idempotent", (docs / "licenses/gplpkg-2.0/LICENSE").is_file())

        shutil.rmtree(docs / "licenses/gplpkg-2.0")
        try:
            lic.validate(root, ok)
            check("validate refuses missing licence files", False)
        except lic.LicenseError as exc:
            check("validate refuses missing licence files", "gplpkg" in str(exc))
        (docs / lic.NOTICE_NAME).unlink()
        try:
            lic.validate(root, ok)
            check("validate refuses a missing notice", False)
        except lic.LicenseError as exc:
            check("validate refuses a missing notice", lic.NOTICE_NAME in str(exc))

        try:
            lic.write_notices(root, reviewed())
            check("write_notices refuses an unreviewed copyleft package", False)
        except lic.LicenseError as exc:
            check("write_notices refuses an unreviewed copyleft package", "gplpkg" in str(exc))


def test_reviewed_file() -> None:
    doc = lic.load_reviewed()
    check("reviewed file loads", doc["schema"] == 1)
    check("oMLX licence text exists and is Apache-2.0",
          "Apache License" in (lic.SUPPORT / doc["omlx"]["licence_text"]).read_text())
    check("mlx-embeddings is reviewed as strong copyleft with a source address",
          doc["packages"]["mlx-embeddings"]["class"] == "strong-copyleft"
          and doc["packages"]["mlx-embeddings"]["source"].startswith("https://"))
    check("no reviewed entry waives a permissive package's licence file without saying why",
          all(entry["note"] for entry in doc["packages"].values()))
    with tempfile.TemporaryDirectory() as raw:
        bad = Path(raw) / "bad.json"
        bad.write_text(json.dumps({"schema": 1, "omlx": {}, "packages": {"x": {"class": "bogus", "note": "n"}}}))
        try:
            lic.load_reviewed(bad)
            check("malformed reviewed entry is refused", False)
        except lic.LicenseError:
            check("malformed reviewed entry is refused", True)
        bad.write_text(json.dumps({"schema": 1, "omlx": {}, "packages": {"x": {"class": "unknown", "note": ""}}}))
        try:
            lic.load_reviewed(bad)
            check("a reviewed entry needs a written reason", False)
        except lic.LicenseError:
            check("a reviewed entry needs a written reason", True)


def test_bypass_cases() -> None:
    """Each of these passed the first version of the gate (found by the release audit)."""
    for text, want in (("MIT AND GPLv3", lic.STRONG), ("BSD-3-Clause AND LGPLv2+", lic.WEAK),
                       ("GPL-2.0+", lic.STRONG), ("LGPLv3", lic.WEAK), ("Apache-2.0 WITH Commons-Clause", lic.UNKNOWN),
                       ("Business Source License 1.1", lic.UNKNOWN), ("BSL-1.0", lic.PERMISSIVE),
                       ("ISC", lic.PERMISSIVE), ("MIT", lic.PERMISSIVE)):
        check(f"{text!r} -> {want}", lic.classify(text) == want, lic.classify(text))
    notice = ("Copyright (c) 2020 Someone\n\nThis program is free software: you can redistribute it and/or modify "
              "it under the terms of the GNU General Public License as published by the Free Software Foundation.\n")
    with tempfile.TemporaryDirectory() as raw:
        root = bundle(Path(raw), [
            ("shortnotice", "1.0", "MIT", {"LICENSE": notice}),
            ("oddname", "2.0", "MIT", {"LICENSE": MIT, "licenses/NOTES.txt": GPL3}),
            ("lateTitle", "3.0", "MIT", {"LICENSE": "Some Project\nCopyright 2020\n\n" + GPL3}),
            ("isc", "4.0", "ISC", {"LICENSE": "Permission to use, copy, modify, and/or distribute this software for any purpose"}),
        ])
        records = {r["name"]: r for r in lic.engine_packages(root / lic.ENGINE_DIR)}
        check("a short GPL notice under a permissive declaration is caught", records["shortnotice"]["class"] == lic.STRONG)
        check("a GPL text in an oddly named file under licenses/ is caught", records["oddname"]["class"] == lic.STRONG)
        check("a GPL title a few lines down is caught", records["lateTitle"]["class"] == lic.STRONG)
        check("ISC text passes", records["isc"]["class"] == lic.PERMISSIVE)


def test_components_listed() -> None:
    doc = lic.load_reviewed()
    check("interpreter and patch licences are shipped as components",
          {c["copy_as"] for c in doc["components"]} >= {"CPython-LICENSE.txt", "oMLX-deepseek_v41-LICENSE.txt"})
    with tempfile.TemporaryDirectory() as raw:
        root = bundle(Path(raw), [("plain", "1.0", "MIT", {"LICENSE": MIT})])
        engine = root / lic.ENGINE_DIR
        (engine / "py/lib/python3.11").mkdir(parents=True)
        (engine / "py/lib/python3.11/LICENSE.txt").write_text("PSF text")
        (engine / "omlx/patches/deepseek_v41").mkdir(parents=True)
        (engine / "omlx/patches/deepseek_v41/LICENSE").write_text("MIT text")
        lic.write_notices(root, doc)
        docs = root / "Contents/Resources/Documentation"
        notice = (docs / lic.NOTICE_NAME).read_text()
        check("components are listed and copied",
              "CPython 3.11" in notice and "Other bundled components" in notice and "JavaScript" in notice
              and (docs / "licenses/CPython-LICENSE.txt").read_text() == "PSF text")
        (engine / "py/lib/python3.11/LICENSE.txt").unlink()
        try:
            lic.write_notices(root, doc)
            check("a missing component licence fails the build", False)
        except lic.LicenseError as exc:
            check("a missing component licence fails the build", "py/lib/python3.11/LICENSE.txt" in str(exc))


def test_second_audit_bypasses() -> None:
    """Gaps the second audit found in the first hardening."""
    for text, want in (("MIT AND EUPL-1.2", lic.UNKNOWN), ("MIT AND LicenseRef-Custom", lic.UNKNOWN),
                       ("Apache-2.0 AND CC-BY-SA-4.0", lic.UNKNOWN), ("Apache-2.0 OR BSD-3-Clause", lic.PERMISSIVE),
                       ("Apache-2.0 AND CNRI-Python", lic.PERMISSIVE), ("MPL-2.0 AND MIT", lic.WEAK),
                       ("GPL-2.0-only WITH Classpath-exception-2.0", lic.STRONG), ("PSF-2.0", lic.PERMISSIVE),
                       ("MIT-0", lic.PERMISSIVE)):
        check(f"{text!r} -> {want}", lic.classify(text) == want, lic.classify(text))
    commons = "Apache License 2.0 with Commons Clause\n\nThe Software is provided under the Commons Clause condition."
    with tempfile.TemporaryDirectory() as raw:
        root = bundle(Path(raw), [
            ("clause", "1.0", "Apache-2.0", {"LICENSE": commons}),
            ("sidefile", "2.0", "MIT", {"LICENSE": MIT, "licenses/TERMS.txt": commons}),
            ("nested", "3.0", "MIT", {"LICENSE": MIT, "licenses/third_party/gpl-3.0.txt": GPL3}),
            ("noisy", "4.0", "MIT", {"LICENSE": MIT}, {"field": "License"}),
            ("classified", "5.0", "MIT", {"LICENSE": MIT}),
        ])
        # extra declared sources the helper does not write
        sp = root / lic.ENGINE_DIR / "sp"
        meta = sp / "classified_package-5.0.dist-info" / "METADATA"
        meta.parent.mkdir(parents=True)
        meta.write_text("Metadata-Version: 2.4\nName: classified-package\nVersion: 5.0\nLicense-Expression: MIT\n"
                        "Classifier: License :: OSI Approved :: GNU General Public License v3 (GPLv3)\n")
        (meta.parent / "LICENSE").write_text(MIT)
        unknown_noise = sp / "noise_pkg-6.0.dist-info" / "METADATA"
        unknown_noise.parent.mkdir(parents=True)
        unknown_noise.write_text("Metadata-Version: 2.4\nName: noise-pkg\nVersion: 6.0\nLicense: UNKNOWN\n"
                                 "License-Expression: MIT\n")
        (unknown_noise.parent / "LICENSE").write_text(MIT)
        records = {r["name"]: r for r in lic.engine_packages(root / lic.ENGINE_DIR)}
        check("a Commons Clause LICENSE under an Apache declaration is not auto-passed", records["clause"]["class"] == lic.UNKNOWN)
        check("restricted terms in a side file under licenses/ are not discarded", records["sidefile"]["class"] == lic.UNKNOWN)
        check("a GPL text in a nested licenses/ folder is collected and caught", records["nested"]["class"] == lic.STRONG)
        check("a GPL classifier beside an MIT expression is caught", records["classified-package"]["class"] == lic.STRONG)
        check("'UNKNOWN' beside a real identifier is ignored", records["noise-pkg"]["class"] == lic.PERMISSIVE)
    with tempfile.TemporaryDirectory() as raw:
        bad = Path(raw) / "bad.json"
        bad.write_text(json.dumps({"schema": 1, "omlx": {}, "packages": {}, "components": [
            {"name": "x", "license": "MIT", "path": "py/../../x", "copy_as": "x.txt"}]}))
        try:
            lic.load_reviewed(bad)
            check("a component path may not climb out of the engine", False)
        except lic.LicenseError:
            check("a component path may not climb out of the engine", True)


def main() -> int:
    for fn in (test_classification, test_packages_and_gate, test_notices_written, test_reviewed_file,
               test_bypass_cases, test_components_listed, test_second_audit_bypasses):
        print(fn.__name__)
        fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
