#!/usr/bin/env python3
"""Audit Axolotl's PREPARED dataset labels against the intended Ling V3 mask.

Run AFTER `axolotl preprocess axolotl-a6000.yaml` (see README, "Train on an A6000").
For every prepared row it finds the matching source row by exact input_ids (so a
template/tools/thinking-kwarg mismatch fails loudly as "no matching source row"),
then asserts labels != -100 ONLY on the final assistant span, and equal to input_ids there.

  python check_labels.py PREPARED_DIR [PREPARED_DIR ...] [--tokenizer-dir DIR] [--local-files-only]
                         [--sample N] [--allow-header]

PREPARED_DIR is the `dataset_prepared_path` (e.g. ./last_run_prepared) or any directory inside
it; every HF `datasets` arrow dataset found under it (load_from_disk) is audited. Splits are
matched to source rows by content, so shuffling/ordering does not matter. Exit 0 = all pass.

--allow-header tolerates (only) the labeled generation-prompt header tokens
"<role>ASSISTANT</role>\\n<think>" of the final turn if Axolotl includes them; default is strict.
--sample N audits N random prepared rows per dataset (default: ALL rows).
"""
import argparse, json, os, random, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preflight


def find_datasets(root):
    from datasets import load_from_disk
    found = []
    for dirpath, dirs, files in os.walk(root):
        if "dataset_dict.json" in files or ("state.json" in files and any(f.endswith(".arrow") for f in files)):
            try:
                found.append((dirpath, load_from_disk(dirpath)))
            except Exception as e:
                print("skip", dirpath, repr(e)[:120])
            dirs[:] = []
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prepared", nargs="+", type=Path)
    ap.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--tokenizer-dir", default=preflight.DEFAULT_TOKENIZER_DIR)
    ap.add_argument("--local-files-only", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--allow-header", action="store_true")
    a = ap.parse_args()

    tok = preflight.load_tokenizer(a.tokenizer_dir, a.local_files_only)
    print("building intended masks from source rows (re-rendering with the real template) ...")
    index = {}
    for split in ["train", "dev"]:
        for line in (a.data_dir / (split + ".jsonl")).read_text().splitlines():
            if not line:
                continue
            row = json.loads(line)
            r = preflight.intended(tok, row)
            # V2-replay rows (v2r-*, tools=[]) render with NO tools argument (identical to tools=[]); reported separately
            index[tuple(r["ids"])] = (split + (":v2_replay" if preflight.is_v2r(row) else ":model_led"), row["id"], r)
    header_len = len(tok(preflight.HEADER, add_special_tokens=False)["input_ids"])
    kinds = {}
    for k_, _, _ in index.values():
        kinds[k_] = kinds.get(k_, 0) + 1
    print("indexed", len(index), "source rows", kinds)

    datasets_found = []
    for root in a.prepared:
        datasets_found += find_datasets(root)
    if not datasets_found:
        sys.exit("FAIL: no HF arrow datasets found under " + ", ".join(map(str, a.prepared)))

    failures, audited, per_split = [], 0, {}
    for path, ds in datasets_found:
        parts = ds.items() if hasattr(ds, "keys") else [("", ds)]
        for pname, d in parts:
            if len(d) == 0:
                continue
            assert {"input_ids", "labels"} <= set(d.column_names), "%s lacks input_ids/labels: %s" % (path, d.column_names)
            idxs = list(range(len(d)))
            if a.sample:
                random.Random(0).shuffle(idxs); idxs = idxs[:a.sample]
            print("auditing %s [%s]: %d of %d rows" % (path, pname, len(idxs), len(d)))
            for i in idxs:
                rec = d[i]
                ids, labels = list(rec["input_ids"]), list(rec["labels"])
                hit = index.get(tuple(ids))
                if hit is None:
                    failures.append("%s[%s]#%d: input_ids match no source row (template, tools, thinking kwarg, truncation or packing differs)" % (path, pname, i)); continue
                split, rid, r = hit
                per_split[split] = per_split.get(split, 0) + 1
                audited += 1
                if len(labels) != len(r["labels"]):
                    failures.append("%s: label length %d != %d" % (rid, len(labels), len(r["labels"]))); continue
                labeled = [j for j, x in enumerate(labels) if x != -100]
                if not labeled:
                    failures.append(rid + ": NO labeled tokens"); continue
                bad_value = [j for j in labeled if labels[j] != ids[j]]
                if bad_value:
                    failures.append("%s: labels differ from input_ids at %s" % (rid, bad_value[:5])); continue
                start = r["n_prefix"]
                outside = [j for j in labeled if j < start - (header_len if a.allow_header else 0)]
                if outside:
                    failures.append("%s: %d labeled tokens BEFORE the final assistant span (first at %d, span starts %d) - earlier turns/context are being trained" % (rid, len(outside), outside[0], start)); continue
                missing = [j for j in range(start, len(ids)) if labels[j] == -100]
                if missing:
                    failures.append("%s: %d target tokens masked (first at %d); end-of-turn masked: %s" % (rid, len(missing), missing[0], labels[-1] == -100)); continue
                if not a.allow_header and labeled[0] != start:
                    failures.append("%s: labeled span starts at %d, expected %d" % (rid, labeled[0], start)); continue
    print(json.dumps({"audited_rows": audited, "by_source_split": per_split, "failures": len(failures)}))
    if failures:
        print("FAIL (showing up to 25):")
        for f in failures[:25]:
            print("  -", f)
        sys.exit(1)
    print("PASS: labels != -100 only on the final assistant span, for all %d audited rows" % audited)


if __name__ == "__main__":
    main()
