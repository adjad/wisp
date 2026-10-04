#!/usr/bin/env python3
"""Alternating A/B timing of the desktop oMLX tree qualification (read-only).

Compares ``DesktopOmlx._qualified_tree`` at a baseline git ref (default: the commit
that introduced the T0 characterization tests, which still has the ``rglob``
walker) against the working tree, on the real application bundle.  Every run uses a
fresh authority, so the tree cache is cold and both passes of the walk execute.

Only ``lstat``/``scandir``/``resolve`` of ``/Applications/oMLX.app`` are issued, the
same calls Wisp already makes on every connection.  It starts no model, no backend,
touches no port and does not signal any process.  ``--load`` additionally times
``RuntimeAuthority().load()`` for both versions, which runs the read-only ``lsof``,
``ps`` and ``codesign``-style inspection of the live oMLX server that Wisp performs
on every connection.

    python scripts/bench_transport_overhead.py --runs 11
    python scripts/bench_transport_overhead.py --runs 11 --load --json out.json
    python scripts/bench_transport_overhead.py --verify        # inventories identical?
"""
import argparse
import importlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULE = 'service/inference/attributed_transport.py'
DEFAULT_BASELINE = '1550554ae7df21a3c1bc463c644ff71015da406b'


def load_baseline(ref):
    """Import the module as it was at ``ref`` under a private name, inside the real package."""
    source = subprocess.run(['git', '-C', str(ROOT), 'show', f'{ref}:{MODULE}'],
                            capture_output=True, text=True, check=True).stdout
    name = 'service.inference._attributed_transport_baseline'
    module = types.ModuleType(name)
    module.__package__ = 'service.inference'
    module.__file__ = f'<{ref}:{MODULE}>'
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def fresh(module):
    authority = object.__new__(module.DesktopOmlx)
    authority.uid = os.getuid()
    authority._group_cache = {}
    authority._qualified_roots = set()
    return authority


def summarize(samples):
    ordered = sorted(samples)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return {'n': len(ordered), 'p50': round(statistics.median(ordered), 1),
            'p95': round(ordered[rank - 1], 1), 'min': round(ordered[0], 1),
            'max': round(ordered[-1], 1), 'all': [round(v, 1) for v in samples]}


def alternate(runs, old_call, new_call):
    """Interleave the two implementations, swapping who goes first every run."""
    old, new = [], []
    for index in range(runs):
        order = (('old', old_call, old), ('new', new_call, new)) if index % 2 == 0 else \
                (('new', new_call, new), ('old', old_call, old))
        for _label, call, bucket in order:
            started = time.perf_counter()
            call()
            bucket.append((time.perf_counter() - started) * 1000)
    return old, new


def load_average():
    return [round(v, 2) for v in os.getloadavg()]


def swap_usage():
    try:
        return subprocess.run(['/usr/sbin/sysctl', '-n', 'vm.swapusage'], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def capture(module, root):
    """The inventories snapshot() returns, via a profile hook (no seam in the product)."""
    captured = []

    def profiler(frame, event, arg):
        if event == 'return' and frame.f_code.co_name == 'snapshot' and isinstance(arg, dict):
            captured.append({os.fspath(key): value for key, value in arg.items()})
    sys.setprofile(profiler)
    try:
        fresh(module)._qualified_tree(root)
    finally:
        sys.setprofile(None)
    return captured


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runs', type=int, default=11)
    parser.add_argument('--baseline-ref', default=DEFAULT_BASELINE)
    parser.add_argument('--load', action='store_true', help='also time RuntimeAuthority().load() (live, read-only)')
    parser.add_argument('--verify', action='store_true', help='compare old and new inventories on the real trees')
    parser.add_argument('--json', help='write the results here')
    args = parser.parse_args()

    new = importlib.import_module('service.inference.attributed_transport')
    old = load_baseline(args.baseline_ref)
    trees = {'python_root': new.DesktopOmlx.python_root, 'server_entry_parent': new.DesktopOmlx.server_entry.parent}
    result = {'baseline_ref': args.baseline_ref, 'runs': args.runs, 'load_before': load_average(),
              'swap_before': swap_usage(), 'python': sys.version.split()[0]}

    if args.verify:
        verdicts = {}
        for name, root in trees.items():
            before, after = capture(old, root), capture(new, root)
            verdicts[name] = {'passes': [len(before), len(after)], 'entries': len(after[0]) if after else 0,
                              'identical': before == after and len(after) == 2}
        result['verify'] = verdicts
        print(json.dumps(verdicts, indent=1))

    for name, root in trees.items():
        old_samples, new_samples = alternate(args.runs, lambda: fresh(old)._qualified_tree(root),
                                             lambda: fresh(new)._qualified_tree(root))
        row = {'old': summarize(old_samples), 'new': summarize(new_samples)}
        row['speedup_p50'] = round(row['old']['p50'] / row['new']['p50'], 2)
        row['speedup_p95'] = round(row['old']['p95'] / row['new']['p95'], 2)
        row['speedup_min'] = round(row['old']['min'] / row['new']['min'], 2)
        result[name] = row
        print(f"{name}: old p50 {row['old']['p50']} p95 {row['old']['p95']} | new p50 {row['new']['p50']} "
              f"p95 {row['new']['p95']} | speedup p50 {row['speedup_p50']}x p95 {row['speedup_p95']}x")

    if args.load:
        old_samples, new_samples = alternate(args.runs, lambda: old.RuntimeAuthority().load(),
                                             lambda: new.RuntimeAuthority().load())
        row = {'old': summarize(old_samples), 'new': summarize(new_samples)}
        row['speedup_p50'] = round(row['old']['p50'] / row['new']['p50'], 2)
        result['load'] = row
        print(f"RuntimeAuthority.load: old p50 {row['old']['p50']} | new p50 {row['new']['p50']} "
              f"| speedup {row['speedup_p50']}x")

    result['load_after'] = load_average()
    result['swap_after'] = swap_usage()
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1))
    print('loadavg', result['load_before'], '->', result['load_after'], '| swap', result['swap_after'])


if __name__ == '__main__':
    main()
