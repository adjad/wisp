#!/usr/bin/env python3
"""Rebuild the exact Ling V3.1 source rows that V3.2 migrates (deterministic, stdlib only, read-only on V3/V3.1).

V3.2 does not author new conversations. It migrates every V3.1 training conversation to the current Wisp tool
registry. This script reproduces those conversations byte-for-byte and checks them against the hashes V3.1 pinned:

  * V3.1 targeted-fix conversations: ../ling-dataset-v3.1-20261013/generate.py run with V31_PARTIAL set to all six
    V3.1 categories (builds every category, writes only into our cache, skips the replay append), seed 20261013.
  * V3 replay: ../ling-dataset-v3-20261012/generate.py regenerated into a temp dir (checked against V3's train sha),
    then the V3.1 v3_replay.py stratified sample (same seed / quotas), checked against v3_replay.provenance.jsonl.
  * V2 replay: V3.1's committed v2_replay.{train,dev}.jsonl.

The concatenation (new rows + V3 replay + V2 replay) must equal V3.1's train.jsonl / dev.jsonl as pinned in V3.1's
preflight.json. Nothing in ../ling-dataset-v3.1-20261013 or ../ling-dataset-v3-20261012 is written.

    python3 sources.py          # writes source_cache/ (gitignored); fast no-op when the cache is already verified
"""
import hashlib, json, os, random, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
V31 = HERE.parent / "ling-dataset-v3.1-20261013"
V3 = HERE.parent / "ling-dataset-v3-20261012"
CACHE = HERE / "source_cache"
V31_CATEGORIES = "safety,guardweb,named,outsend,routing,chatcode"


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha(path):
    return sha_bytes(Path(path).read_bytes())


def _v31_pins():
    pf = json.loads((V31 / "preflight.json").read_text())["data_sha256"]
    rp = json.loads((V31 / "v3_replay.provenance.jsonl").read_text().splitlines()[0])
    return pf, rp


def build_v31_new(out):
    env = dict(os.environ, V31_PARTIAL=V31_CATEGORIES)
    subprocess.run([sys.executable, str(V31 / "generate.py"), "--output-dir", str(out)], check=True, env=env,
                   stdout=subprocess.DEVNULL)


def build_v3_replay(out):
    """Same algorithm as ../ling-dataset-v3.1-20261013/v3_replay.py, writing into `out` instead of the V3.1 dir."""
    _, rp = _v31_pins()
    seed, target = rp["seed"], rp["target_rows"]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        subprocess.run([sys.executable, str(V3 / "generate.py"), "--output-dir", str(td)], check=True,
                       stdout=subprocess.DEVNULL)
        if sha(td / "train.jsonl") != rp["v3_train_sha256"]:
            sys.exit("V3 regeneration does not match the V3 release train.jsonl")
        prov_out = []
        for split in ("train", "dev"):
            prov = [json.loads(x) for x in (td / f"{split}.provenance.jsonl").read_text().splitlines() if x]
            lines = [x for x in (td / f"{split}.jsonl").read_text().splitlines() if x]
            by_conv = {}
            for line in lines:
                row = json.loads(line)
                if row["id"].startswith("v2r-"):
                    continue
                by_conv.setdefault(row["conversation_id"], []).append(line)
            total = sum(len(v) for v in by_conv.values())
            cats = {}
            for p in prov:
                cats.setdefault(p["category"], []).append(p["conversation_id"])
            rng = random.Random(f"{seed}-{split}")
            picked = []
            for cat in sorted(cats):
                ids = sorted(cats[cat])
                rng.shuffle(ids)
                share = target[split] * sum(len(by_conv[i]) for i in ids) / total
                got = 0
                for cid in ids:
                    if got >= share:
                        break
                    picked.append(cid)
                    got += len(by_conv[cid])
            picked_set = set(picked)
            outl = []
            for line in lines:
                row = json.loads(line)
                if row.get("conversation_id") in picked_set:
                    outl.append(line.replace(f'"id":"{row["id"]}"', f'"id":"v3r-{row["id"]}"', 1))
            (out / f"v3_replay.{split}.jsonl").write_text("".join(x + "\n" for x in outl))
            pmap = {p["conversation_id"]: p for p in prov}
            for cid in sorted(picked_set):
                prov_out.append({"split": split, "source_conversation_id": cid, **{k: pmap[cid][k] for k in pmap[cid]
                                 if k not in ("split",)}})
        (out / "v3_replay.provenance.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in prov_out))
    for split in ("train", "dev"):
        want = rp[f"{split}_sha256"]
        if sha(out / f"v3_replay.{split}.jsonl") != want:
            sys.exit(f"V3 replay {split} does not match V3.1's pinned sample")


def verify(out):
    pf, _ = _v31_pins()
    for split in ("train", "dev"):
        full = b"".join((out / f).read_bytes() for f in (f"v31_new/{split}.jsonl", f"v3_replay.{split}.jsonl"))
        full += (V31 / f"v2_replay.{split}.jsonl").read_bytes()
        if sha_bytes(full) != pf[f"{split}.jsonl"]:
            return False
    return True


def main():
    CACHE.mkdir(exist_ok=True)
    stamp = CACHE / "VERIFIED"
    if stamp.exists() and verify(CACHE):
        print("source_cache verified against V3.1 pins")
        return
    (CACHE / "v31_new").mkdir(exist_ok=True)
    build_v31_new(CACHE / "v31_new")
    build_v3_replay(CACHE)
    if not verify(CACHE):
        sys.exit("rebuilt sources do not reproduce V3.1 train.jsonl/dev.jsonl (preflight.json data_sha256)")
    stamp.write_text("sources reproduce V3.1 train.jsonl and dev.jsonl byte-for-byte\n")
    print("source_cache rebuilt and verified against V3.1 pins")


if __name__ == "__main__":
    main()
