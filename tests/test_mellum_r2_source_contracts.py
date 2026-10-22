"""Mellum R2 source/data/package controls; no model, GPU, network or VM setup."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "training/mellum-dataset-r2-20261015"
KIT = ROOT / "training/mellum-r2-train"


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses require the defining module
    previous_path = list(sys.path)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
    return module


@pytest.fixture
def builder(monkeypatch):
    families = load_source("mellum_r2_contract_families", DATA / "families.py")
    monkeypatch.setitem(sys.modules, "families", families)
    return load_source("mellum_r2_contract_build", DATA / "build.py")


@pytest.fixture
def corpus_copy(tmp_path, builder, monkeypatch):
    # Read-only source links avoid copying 64 MB for every negative control.
    # Mutations replace the fixture path, never write through into source data.
    root = tmp_path / "repo"
    here = root / DATA.relative_to(ROOT)
    here.mkdir(parents=True)
    for source in DATA.iterdir():
        if source.is_file() and source.suffix != ".zip":
            if source.name in {"validation.json", "upload-zip.json"}:
                (here / source.name).write_bytes(source.read_bytes())
            else:
                (here / source.name).symlink_to(source)
    for name in ("scripts", "service"):
        (root / name).symlink_to(ROOT / name, target_is_directory=True)
    (root / "training/mellum-r2-train").symlink_to(KIT, target_is_directory=True)
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "HERE", here)
    monkeypatch.setattr(builder, "OUT", {k: here / p.name for k, p in builder.OUT.items()})
    return here


def replace_text(path, text):
    staged = path.with_name(path.name + ".replacement")
    staged.write_text(text)
    staged.replace(path)


def first_row(here, family=None):
    with (here / "train.jsonl").open() as fh:
        return next(json.loads(line) for line in fh if family is None or json.loads(line)["family"] == family)


def write_row_fixture(here, row):
    replace_text(here / "train.jsonl", json.dumps(row) + "\n")


def test_full_committed_corpus_acceptance(builder):
    report = builder.validate_rendered()
    assert report["ok"], report["problems"]
    assert (report["rows"], report["items"], report["families"]) == (3662, 1959, 60)
    assert report["rows_by_split"] == {"train": 3270, "dev": 392}
    assert report["max_gate_overlap"] < builder.OVERLAP_MAX
    assert not any(report[k] for k in ("tokenizer_run", "router_replay_run", "training_run"))
    for name, expected in builder.FROZEN_CORPUS_SHA256.items():
        assert builder.file_sha(DATA / name) == expected


def test_missing_generation_inputs_do_not_overwrite_acceptance(builder, corpus_copy):
    before = (corpus_copy / "validation.json").read_bytes()
    with pytest.raises(SystemExit, match="generation inputs are missing/empty"):
        builder.cmd_validate(SimpleNamespace(no_template=True))
    assert (corpus_copy / "validation.json").read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "empty", "invalid-json"])
def test_missing_empty_malformed_corpus(builder, corpus_copy, kind):
    path = corpus_copy / "train.jsonl"
    if kind == "missing":
        path.unlink()
        with pytest.raises(FileNotFoundError):
            builder.validate_rendered()
    else:
        replace_text(path, "" if kind == "empty" else "{bad json}\n")
        report = builder.validate_rendered()
        assert not report["ok"]
        assert "frozen_split_counts" in report["problems"]


@pytest.mark.parametrize("kind,expected", [
    ("tamper", "frozen_corpus_hash"), ("split", "world_split"),
    ("family", "family_category"), ("tool", "call_not_offered"),
    ("menu", "family_menu"), ("privacy", "privacy_regex"),
    ("holdout", "gate_overlap"), ("frame", "tool_menu_frame"),
])
def test_corpus_negative_controls(builder, corpus_copy, kind, expected):
    row = first_row(corpus_copy, "menu_web_search_only" if kind == "menu" else None)
    if kind == "tamper":
        row["completion"] = row["completion"].replace("<|im_end|>", " changed<|im_end|>")
    elif kind == "split":
        row["item"] = f"{row['family']}-1000-0"
        row["id"] = f"{row['item']}-s0"
    elif kind == "family":
        row["family"] = "nonexistent_family"
    elif kind == "tool":
        row["completion"] = '<tool_call>\n{"name":"invented_tool","arguments":{}}\n</tool_call><|im_end|>'
    elif kind == "menu":
        schema = {"type": "function", "function": {"name": "web_fetch", "parameters": {"properties": {}}}}
        row["prompt"] = row["prompt"].replace("\n</tools>", "\n" + json.dumps(schema) + "\n</tools>", 1)
    elif kind == "privacy":
        row["completion"] = "private@gmail.com<|im_end|>"
    elif kind == "holdout":
        from scripts.toolbench.cases import load_cases
        from scripts.toolbench.world import make_facts
        prompt = next(c for c in load_cases() if c.split == "gate").bind(make_facts(None)).prompt
        row["prompt"] = re.sub(r"(<\|im_start\|>user\n).*?(<\|im_end\|>)", lambda m: m[1] + prompt + m[2], row["prompt"], flags=re.S)
    elif kind == "frame":
        row["prompt"] = row["prompt"].replace("\n</tools>", "\n</wrong>", 1)
    write_row_fixture(corpus_copy, row)
    report = builder.validate_rendered()
    assert not report["ok"]
    assert expected in report["problems"]


def test_package_rejects_stale_acceptance(builder, corpus_copy):
    report = builder.validate_rendered()
    assert report["ok"]
    replace_text(corpus_copy / "validation.json", json.dumps(report))
    replace_text(corpus_copy / "BUILD_NOTES.md", "changed acceptance input\n")
    with pytest.raises(SystemExit, match="stale committed acceptance"):
        builder.cmd_package(SimpleNamespace())
    assert not (corpus_copy / "mellum-r2-data.zip").exists()


def test_package_positive_manifest_binding(builder, corpus_copy):
    report = builder.validate_rendered()
    assert report["ok"]
    replace_text(corpus_copy / "validation.json", json.dumps(report))
    builder.cmd_package(SimpleNamespace())
    rec = json.loads((corpus_copy / "upload-zip.json").read_text())
    archive = corpus_copy / rec["zip"]
    assert builder.file_sha(archive) == rec["sha256"]
    with zipfile.ZipFile(archive) as zf:
        assert set(zf.namelist()) == set(rec["manifest"]["files"]) | {"MANIFEST.json"}
        assert json.loads(zf.read("MANIFEST.json")) == rec["manifest"]
        for name, expected in rec["manifest"]["files"].items():
            assert hashlib.sha256(zf.read(name)).hexdigest() == expected


def test_current_required_arguments_are_checked(builder, corpus_copy, monkeypatch):
    keysets = builder.current_tool_keysets()
    row = first_row(corpus_copy)
    call = json.loads(re.search(r"<tool_call>\s*(.*?)\s*</tool_call>", row["completion"], re.S)[1])
    props, required = keysets[call["name"]]
    keysets[call["name"]] = (props | {"new_required_argument"}, required | {"new_required_argument"})
    monkeypatch.setattr(builder, "current_tool_keysets", lambda: keysets)
    write_row_fixture(corpus_copy, row)
    assert "current_call_arguments" in builder.validate_rendered()["problems"]


@pytest.mark.parametrize("mutation", ["trainer", "evidence", "router-source"])
def test_package_rejects_write_boundary_mutations(builder, corpus_copy, monkeypatch, mutation):
    # Unlike other fixtures, copy the kit/service before mutating them.
    for name in ("training/mellum-r2-train", "service"):
        target = builder.ROOT / name
        source = target.resolve()
        target.unlink()
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    report = builder.validate_rendered()
    assert report["ok"]
    replace_text(corpus_copy / "validation.json", json.dumps(report))
    (corpus_copy / "mellum-r2-data.zip").write_bytes(b"previous valid package")
    before = (corpus_copy / "upload-zip.json").read_bytes()
    original = zipfile.ZipFile.write
    changed = False

    def mutate_then_write(archive, filename, *args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            if mutation == "trainer":
                path = builder.ROOT / "training/mellum-r2-train/train.py"
            elif mutation == "evidence":
                path = corpus_copy / "validation.json"
            else:
                path = next((builder.ROOT / "service/router").glob("*.py"))
            replace_text(path, path.read_text() + "\n")
        return original(archive, filename, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "write", mutate_then_write)
    with pytest.raises(SystemExit, match="changed after acceptance"):
        builder.cmd_package(SimpleNamespace())
    assert (corpus_copy / "mellum-r2-data.zip").read_bytes() == b"previous valid package"
    assert (corpus_copy / "upload-zip.json").read_bytes() == before
    assert not list(corpus_copy.glob(".mellum-r2-*"))


@pytest.fixture
def unpack_program():
    source = (KIT / "vm-setup.sh").read_text()
    # Execute only the stdlib verifier heredoc, never the setup shell script.
    return source.split("<<'VERIFY_DATA_ZIP'", 1)[1].split("\n", 1)[1].split("\nVERIFY_DATA_ZIP\n", 1)[0]


def fixture_zip(path, *, mutation=None):
    payloads = {name: b"synthetic fixture\n" for name in
                ("train.jsonl", "dev.jsonl", "stats.json", "validation.json", "DATA_CARD.md", "train/train.py")}
    if mutation == "size":
        payloads["DATA_CARD.md"] = b"x" * (2 * 1024**2 + 1)
    manifest = {"files": {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}}
    if mutation == "empty-manifest":
        manifest["files"] = {}
    if mutation == "hash":
        manifest["files"]["train.jsonl"] = "0" * 64
    entries = list(payloads.items()) + [("MANIFEST.json", json.dumps(manifest).encode())]
    if mutation in {"traversal", "absolute", "extra"}:
        entries[-1] = ({"traversal": "../escape", "absolute": "/escape", "extra": "extra.txt"}[mutation], b"unexpected")
    if mutation == "missing":
        entries.pop(0)
    if mutation == "duplicate":
        entries.append(entries[0])
    with zipfile.ZipFile(path, "w") as zf:
        for name, raw in entries:
            info = zipfile.ZipInfo(name)
            if mutation == "symlink" and name == "train.jsonl":
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            if mutation == "directory" and name == "train.jsonl":
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
            zf.writestr(info, raw)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_unpack(program, monkeypatch, archive, destination, sha):
    monkeypatch.setattr(sys, "argv", ["verify", str(archive), str(destination), sha])
    exec(compile(program, "VM archive verifier", "exec"), {})


def test_archive_verified_before_replacement(unpack_program, tmp_path, monkeypatch):
    archive, destination = tmp_path / "data.zip", tmp_path / "data"
    destination.mkdir()
    (destination / "old").write_text("preserve until success")
    sha = fixture_zip(archive)
    run_unpack(unpack_program, monkeypatch, archive, destination, sha)
    assert (destination / "train.jsonl").read_bytes() == b"synthetic fixture\n"
    assert not (destination / "old").exists()
    assert not list(tmp_path.glob(".mellum-r2-*-*"))


@pytest.mark.parametrize("kind", ["outer-hash", "empty-manifest", "hash", "traversal", "absolute", "extra", "missing", "duplicate", "symlink", "directory", "size"])
def test_archive_negative_controls_keep_existing_data(unpack_program, tmp_path, monkeypatch, kind):
    archive, destination = tmp_path / "data.zip", tmp_path / "data"
    destination.mkdir()
    (destination / "old").write_text("unchanged")
    with pytest.warns(UserWarning) if kind == "duplicate" else nullcontext():
        sha = fixture_zip(archive, mutation=kind)
    if kind == "outer-hash":
        sha = "0" * 64
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        run_unpack(unpack_program, monkeypatch, archive, destination, sha)
    assert [p.name for p in destination.iterdir()] == ["old"]
    assert (destination / "old").read_text() == "unchanged"
    assert not list(tmp_path.glob(".mellum-r2-*-*"))


def test_archive_stage_failure_rolls_back(unpack_program, tmp_path, monkeypatch):
    archive, destination = tmp_path / "data.zip", tmp_path / "data"
    destination.mkdir()
    (destination / "old").write_text("unchanged")
    sha = fixture_zip(archive)
    original = os.replace

    def fail_install(source, target):
        if Path(source).name.startswith(".mellum-r2-stage-"):
            raise OSError("synthetic install failure")
        return original(source, target)

    monkeypatch.setattr(os, "replace", fail_install)
    with pytest.raises(OSError, match="synthetic install failure"):
        run_unpack(unpack_program, monkeypatch, archive, destination, sha)
    assert (destination / "old").read_text() == "unchanged"
    assert not list(tmp_path.glob(".mellum-r2-*-*"))


@pytest.mark.parametrize("template", ["missing", "changed"])
def test_template_binding_fails_before_tokenizer(tmp_path, monkeypatch, template):
    trainer = load_source("mellum_r2_contract_trainer", KIT / "train.py")
    if template == "changed":
        (tmp_path / "chat_template.jinja").write_text("changed")
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace())
    assert trainer.cmd_preflight(SimpleNamespace(base=str(tmp_path), template_sha="0" * 64)) == 1


def test_preflight_tokenizer_is_local_only(tmp_path, monkeypatch):
    trainer = load_source("mellum_r2_contract_trainer", KIT / "train.py")
    raw = b"synthetic template"
    (tmp_path / "chat_template.jinja").write_bytes(raw)

    class StopAfterCheckingLocalFlag(Exception):
        pass

    def from_pretrained(base, **kwargs):
        assert base == str(tmp_path)
        assert kwargs == {"local_files_only": True}
        raise StopAfterCheckingLocalFlag

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=from_pretrained)))
    with pytest.raises(StopAfterCheckingLocalFlag):
        trainer.cmd_preflight(SimpleNamespace(base=str(tmp_path), template_sha=hashlib.sha256(raw).hexdigest()))


@pytest.mark.parametrize("changed", [None, "train.jsonl", "dev.jsonl", "train/train.py", "template"])
def test_preflight_marker_binds_current_inputs(tmp_path, changed):
    source = (KIT / "vm-setup.sh").read_text()
    helpers = source.split('die() {', 1)[1].split('\nT() {', 1)[0]
    helpers = 'die() {' + helpers
    # Model the Linux checksum primitive with stdlib Python so this source
    # control also runs on macOS hosts without GNU coreutils installed.
    helpers += '''
sha256sum() {
  "$SOURCE_TEST_PYTHON" -c 'import hashlib,sys; from pathlib import Path; [print(hashlib.sha256(Path(p).read_bytes()).hexdigest()+"  "+p) for p in sys.argv[1:]]' "$@"
}
'''
    data, base = tmp_path / "data", tmp_path / "base"
    (data / "train").mkdir(parents=True)
    base.mkdir()
    (tmp_path / "base_path").write_text(str(base))
    for name in ("train.jsonl", "dev.jsonl", "train/train.py"):
        (data / name).write_text("synthetic")
    (base / "chat_template.jinja").write_text("synthetic")
    env = dict(os.environ, WORK=str(tmp_path), DATA=str(data), SOURCE_TEST_PYTHON=sys.executable)
    make_marker = subprocess.run(["bash", "-c", helpers + '\npreflight_stamp > "$DATA/PREFLIGHT_OK"'], env=env, capture_output=True, text=True)
    assert make_marker.returncode == 0, make_marker.stderr
    if changed:
        path = base / "chat_template.jinja" if changed == "template" else data / changed
        path.write_text("changed")
    check = subprocess.run(["bash", "-c", helpers + "\nchecked_preflight"], env=env, capture_output=True, text=True)
    assert (check.returncode == 0) == (changed is None)


@pytest.mark.parametrize("mutation", [None, "train", "dev", "trainer", "template", "trainer-failure", "missing-dev", "missing-train"])
def test_preflight_rejects_changes_during_validation(tmp_path, mutation):
    # Copy only the generated run.sh, never execute VM setup. Its preflight
    # branch invokes a disposable stdlib stub instead of the real trainer.
    source = (KIT / "vm-setup.sh").read_text()
    script = source.split('cat > "$WORK/run.sh" <<\'RUN\'\n', 1)[1].split("\nRUN\n", 1)[0]
    checksum = '''
sha256sum() {
  "$SOURCE_TEST_PYTHON" -c 'import hashlib,sys; from pathlib import Path; [print(hashlib.sha256(Path(p).read_bytes()).hexdigest()+"  "+p) for p in sys.argv[1:]]' "$@"
}
'''
    script = script.replace("set -uo pipefail\n", "set -uo pipefail\n" + checksum, 1)
    run = tmp_path / "run.sh"
    run.write_text(script)
    data, base = tmp_path / "mellum-r2", tmp_path / "base"
    (data / "train").mkdir(parents=True)
    (tmp_path / "venv/bin").mkdir(parents=True)
    (tmp_path / "venv/bin/python").symlink_to(sys.executable)
    base.mkdir()
    (tmp_path / "base_path").write_text(str(base))
    (data / "train.jsonl").write_text("synthetic train")
    (data / "dev.jsonl").write_text("synthetic dev")
    (base / "chat_template.jinja").write_text("synthetic template")
    (data / "train/train.py").write_text('''
import os, sys
from pathlib import Path
assert sys.argv[1] == "preflight"
data = Path(sys.argv[sys.argv.index("--data") + 1])
base = Path(sys.argv[sys.argv.index("--base") + 1])
(data / "stub-ran").write_text("yes")
kind = os.environ.get("PREFLIGHT_TEST_MUTATION")
if kind == "trainer-failure":
    (data / "PREFLIGHT_OK").write_text("invalid marker from failed validation")
    sys.exit(7)
paths = {"train": data / "train.jsonl", "dev": data / "dev.jsonl",
         "trainer": data / "train/train.py", "template": base / "chat_template.jinja"}
if kind in paths:
    path = paths[kind]
    path.write_text(path.read_text() + " changed during preflight")
''')
    marker = data / "PREFLIGHT_OK"
    marker.write_text("old marker must be removed")
    if mutation in {"missing-train", "missing-dev"}:
        (data / ("train.jsonl" if mutation == "missing-train" else "dev.jsonl")).unlink()
    env = dict(os.environ, SOURCE_TEST_PYTHON=sys.executable, PREFLIGHT_TEST_MUTATION=mutation or "")
    result = subprocess.run(["bash", str(run), "preflight"], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) == (mutation is None), result.stdout + result.stderr
    assert marker.exists() == (mutation is None)
    assert not list(data.glob(".PREFLIGHT_OK.*"))
    if mutation is None:
        expected = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                    (data / "train.jsonl", data / "dev.jsonl", data / "train/train.py", base / "chat_template.jinja")}
        actual = {path: digest for digest, path in (line.split("  ", 1) for line in marker.read_text().splitlines())}
        assert actual == expected
    elif mutation in {"missing-train", "missing-dev"}:
        assert not (data / "stub-ran").exists()
    elif mutation != "trainer-failure":
        assert "inputs changed during validation" in result.stdout


@pytest.mark.parametrize("scenario,expected", [
    ("unchanged", ["preflight", "smoke", "train", "merge", "package"]),
    ("retry-smoke", ["preflight", "smoke", "smoke", "train", "merge", "package"]),
    ("retry-train", ["preflight", "smoke", "train", "train", "merge", "package"]),
    ("after-preflight", ["preflight"]),
    ("between-smoke-retries", ["preflight", "smoke"]),
    ("before-train", ["preflight", "smoke"]),
    ("between-train-retries", ["preflight", "smoke", "train"]),
])
def test_unattended_attempts_check_preflight_binding(tmp_path, scenario, expected):
    source = (KIT / "vm-setup.sh").read_text()
    script = source.split('cat > "$WORK/run.sh" <<\'RUN\'\n', 1)[1].split("\nRUN\n", 1)[0]
    # Substitute only infrastructure primitives and terminal fixture stages.
    # The actual preflight, _auto loops and checked_preflight remain intact.
    script = script.replace("set -uo pipefail\n", '''set -uo pipefail
sha256sum() {
  "$SOURCE_TEST_PYTHON" -c 'import hashlib,sys; from pathlib import Path; [print(hashlib.sha256(Path(p).read_bytes()).hexdigest()+"  "+p) for p in sys.argv[1:]]' "$@"
}
date() { if [ "${1:-}" = "-d" ]; then printf 'synthetic deadline\\n'; else command date "$@"; fi; }
''', 1)
    preflight_line = '  "$0" preflight || { say "STOP: preflight failed"; exit 1; }'
    assert script.count(preflight_line) == 1
    script = script.replace(preflight_line, preflight_line + '''
  if [ "$AUTO_TEST_SCENARIO" = "after-preflight" ]; then printf 'changed after preflight' >> "$DATA/train.jsonl"; fi
''', 1)
    for stage in ("merge", "package"):
        case_line = f"\n{stage})\n"
        assert script.count(case_line) == 1
        script = script.replace(case_line, f'''\n{stage})
  printf '{stage}\\n' >> "$WORK/stage-calls"; exit 0 ;;
unused_fixture_{stage})
''', 1)
    run = tmp_path / "run.sh"
    run.write_text(script)
    run.chmod(0o755)
    data, base = tmp_path / "mellum-r2", tmp_path / "base"
    (data / "train").mkdir(parents=True)
    (tmp_path / "venv/bin").mkdir(parents=True)
    (tmp_path / "venv/bin/python").symlink_to(sys.executable)
    base.mkdir()
    (tmp_path / "base_path").write_text(str(base))
    (data / "train.jsonl").write_text("synthetic train")
    (data / "dev.jsonl").write_text("synthetic dev")
    (base / "chat_template.jinja").write_text("synthetic template")
    (data / "train/train.py").write_text('''
import json, os, sys
from pathlib import Path
data = Path(sys.argv[sys.argv.index("--data") + 1])
base = Path(sys.argv[sys.argv.index("--base") + 1])
work = data.parent
stage = sys.argv[1]
calls = work / "stage-calls"
with calls.open("a") as fh:
    fh.write(stage + "\\n")
number = calls.read_text().splitlines().count(stage)
scenario = os.environ["AUTO_TEST_SCENARIO"]
if stage == "smoke":
    out = Path(sys.argv[sys.argv.index("--out") + 1])
    out.mkdir(exist_ok=True)
    (out / "smoke.json").write_text(json.dumps({"epoch_min": 1, "eval_min_per_epoch": 1}))
    if scenario == "between-smoke-retries" and number == 1:
        (data / "dev.jsonl").write_text("changed between smoke attempts")
        sys.exit(3)
    if scenario == "retry-smoke" and number == 1:
        sys.exit(3)
    if scenario == "before-train":
        (base / "chat_template.jinja").write_text("changed after smoke")
elif stage == "train":
    out = Path(sys.argv[sys.argv.index("--out") + 1])
    if scenario == "between-train-retries" and number == 1:
        (data / "dev.jsonl").write_text("changed between train attempts")
        sys.exit(9)
    if scenario == "retry-train" and number == 1:
        sys.exit(9)
    (out / "train.log.jsonl").write_text('{"event": "done"}\\n')
elif stage != "preflight":
    raise AssertionError("unexpected fixture stage")
''')
    env = dict(os.environ, SOURCE_TEST_PYTHON=sys.executable, AUTO_TEST_SCENARIO=scenario)
    result = subprocess.run(["bash", str(run), "_auto", "8"], env=env, capture_output=True, text=True)
    assert (tmp_path / "stage-calls").read_text().splitlines() == expected, result.stdout + result.stderr
    positive = scenario in {"unchanged", "retry-smoke", "retry-train"}
    assert (result.returncode == 0) == positive, result.stdout + result.stderr
    if not positive:
        assert "preflight inputs changed" in result.stdout
