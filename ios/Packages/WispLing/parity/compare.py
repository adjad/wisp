"""Compare parity/reference.jsonl (Python mlx-lm) with parity/swift.jsonl (ling-parity).

    python3 parity/compare.py parity/reference.jsonl parity/swift.jsonl [--tol 0.05]

Exit status 0 when, for every prompt, the prompt token ids are identical, the
32 greedy ids are identical, and the top-5 ids match with |logit diff| <= tol.
"""

import argparse
import json
import sys


def load(path):
    rows, summary = {}, None
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            obj = json.loads(line)
            if obj.get("summary"):
                summary = obj
            else:
                rows[obj["prompt"]] = obj
    return rows, summary


def logit_diff(a, b):
    """Max |logit difference| over token ids present in both top-5 lists."""
    da, db = dict(map(tuple, a)), dict(map(tuple, b))
    common = set(da) & set(db)
    return max((abs(da[i] - db[i]) for i in common), default=float("nan"))


def teacher_report(ref, path):
    """Per-position comparison with the Swift side fed the reference tokens."""
    rows, _ = load(path)
    total = agree = 0
    worst = 0.0
    for name, r in ref.items():
        s = rows.get(name)
        if not s or not s.get("teacher_forced"):
            continue
        flips = []
        for pos, (rt, st) in enumerate(zip(r["step_top5"], s["step_top5"])):
            total += 1
            worst = max(worst, logit_diff(rt, st))
            if rt[0][0] == st[0][0]:
                agree += 1
            else:
                flips.append((pos, rt[0][0], st[0][0], round(rt[0][1] - rt[1][1], 4)))
        print(f"[{name}] teacher-forced top-1 agreement {32 - len(flips)}/32"
              + (f"; flips (pos, ref, swift, ref top1-top2 margin): {flips}" if flips else ""))
    print(f"teacher-forced: top-1 agree {agree}/{total}, max |logit diff| (shared top-5 ids) {worst:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference")
    ap.add_argument("swift")
    ap.add_argument("--tol", type=float, default=0.05)
    ap.add_argument("--teacher", help="ling-parity --teacher output (swift_teacher.jsonl)")
    args = ap.parse_args()

    ref, ref_sum = load(args.reference)
    swf, swf_sum = load(args.swift)
    ok = True
    worst = 0.0
    for name, r in ref.items():
        s = swf.get(name)
        if s is None:
            print(f"[{name}] missing from swift output")
            ok = False
            continue
        prompt_ok = (
            r["prompt_tokens"] == s["prompt_tokens"]
            and r["prompt_ids_sha256"] == s["prompt_ids_sha256"]
        )
        r_ids = [i for i, _ in r["top5"]]
        s_ids = [i for i, _ in s["top5"]]
        max_diff = logit_diff(r["top5"], s["top5"])
        worst = max(worst, max_diff)
        top_ok = r_ids == s_ids and max_diff <= args.tol
        g_r, g_s = r["greedy_ids"], s["greedy_ids"]
        first_div = next((i for i, (a, b) in enumerate(zip(g_r, g_s)) if a != b), None)
        greedy_ok = g_r == g_s
        ok &= prompt_ok and top_ok and greedy_ok
        print(
            f"[{name}] prompt_tokens={r['prompt_tokens']}/{s['prompt_tokens']} "
            f"prompt_ids={'same' if prompt_ok else 'DIFFERENT'} "
            f"top5_ids={'same' if r_ids == s_ids else 'DIFFERENT'} "
            f"max|dlogit|={max_diff:.4f} greedy32={'identical' if greedy_ok else f'DIVERGE@{first_div}'}"
        )
        if r_ids != s_ids:
            print(f"    ref  top5 {r['top5']}\n    swift top5 {s['top5']}")
        if not greedy_ok:
            print(f"    ref   {g_r}\n    swift {g_s}")
        print(f"    text: {s['text']!r}")
    print(f"max |logit diff| over all top-5 entries: {worst:.4f}")
    if ref_sum and swf_sum:
        print(f"reference summary: {ref_sum}")
        print(f"swift summary:     {swf_sum}")
    if args.teacher:
        teacher_report(ref, args.teacher)
    print("PARITY: PASS" if ok else "PARITY: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
