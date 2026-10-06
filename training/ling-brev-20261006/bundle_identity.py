"""Bind manual label review to complete current inputs; no trust in a log alone."""
import hashlib
import json
from pathlib import Path


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
    return digest.hexdigest()


def identity(root):
    root = Path(root)
    files = [p for p in root.iterdir() if p.is_file() and p.suffix in {".py", ".sh", ".yml", ".json", ".txt", ".jinja"} and p.name != "launch-admission.json"]
    files += [p for p in (root / "data").rglob("*") if p.is_file()]
    prepared = [p for p in (root / "artifacts/prepared").rglob("*") if p.is_file()]
    if not prepared: raise ValueError("Missing prepared dataset")
    files += prepared
    files += [root / "artifacts" / f for f in ("runtime.json", "tokenization.json", "preprocess.log")]
    if any(p.is_symlink() for p in files): raise ValueError("No symlinked inputs")
    fingerprints = {str(p.relative_to(root)): file_hash(p) for p in sorted(files)}
    return {"version": 1, "files": fingerprints, "sha256": hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest()}


def check_audit(root):
    root = Path(root)
    audit = json.loads((root / "artifacts/label-audit.json").read_text())
    current = identity(root)
    if audit.get("input_identity") != current:
        raise ValueError("Stale label review: code/config/data/pins/template/runtime/prepared identity changed")
    for field in ("passed", "prompt_and_prior_assistant_masked", "complete_final_assistant_and_eos_supervised"):
        if audit.get(field) is not True: raise ValueError("Unapproved label check: " + field)
    return current


if __name__ == "__main__":
    import sys
    root = Path(__file__).resolve().parent
    print(json.dumps(check_audit(root) if "--check" in sys.argv else identity(root), indent=2))
