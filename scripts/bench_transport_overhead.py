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

``--spawn`` times the spawn scheduling of ``DesktopOmlx._listener`` (T0-spawn) against the
serial baseline named by ``--baseline-ref`` (use the commit before T0-spawn) on the live
oMLX: ``binding()``, ``connected_peer()``, ``RuntimeAuthority().load()`` and the
request-shaped total (load, binding, process_identity, three peer checks).  It reads the
same things Wisp reads on every connection (lsof, ps, csops, lstat) and holds two idle
TCP connections to 127.0.0.1:8000 that never send a byte.  It starts no model, signals no
process, and ends by shutting the private pool down and checking that no thread or child
process of its own is left.

    python scripts/bench_transport_overhead.py --spawn --baseline-ref <sha> --runs 11 --json out.json

``--executable`` (T0-path) times the executable-path lookup alone, ``lsof -d txt`` (baseline) against
``proc_pidpath`` (this tree), for the live server and its parent, and checks both return the same
path.  Use ``--baseline-ref`` of the commit before T0-path; ``--spawn`` with that baseline also
measures the whole effect on binding(), connected_peer() and the request-shaped total.  Both are
read-only: ``lsof -p <pid> -d txt`` and ``proc_pidpath`` on the live processes, nothing else.

    python scripts/bench_transport_overhead.py --executable --baseline-ref <sha> --runs 11 --json out.json
"""
import argparse
import importlib
import json
import math
import os
import socket
import statistics
import subprocess
import sys
import threading
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


def own_children():
    """Child processes of this process other than the ``ps`` that lists them."""
    probe = subprocess.Popen(['/bin/ps', '-axo', 'pid=,ppid='], stdout=subprocess.PIPE, text=True)
    out = probe.communicate()[0]
    pairs = [line.split() for line in out.splitlines() if len(line.split()) == 2]
    return sorted(int(pid) for pid, ppid in pairs if int(ppid) == os.getpid() and int(pid) != probe.pid)


def spawn_benchmark(old, new, runs, process_identity):
    """Old (serial) against new (scheduled) _listener on the live desktop oMLX, alternating."""
    sockets = []
    try:
        def connect():
            sock = socket.create_connection(('127.0.0.1', 8000), timeout=10)   # idle: never sends a byte
            sockets.append(sock)
            return sock

        warm = {}
        for label, module in (('old', old), ('new', new)):
            authority = module.RuntimeAuthority().load()
            pid = authority.binding()
            warm[label] = (module, authority, pid, process_identity(pid, authority.uid), connect())

        def binding(label):
            return lambda: warm[label][1].binding()

        def peer(label):
            module, authority, pid, identity, sock = warm[label]
            return lambda: authority.connected_peer(sock, pid, identity)

        def load(label):
            return lambda: warm[label][0].RuntimeAuthority().load()

        def request(label):
            module = warm[label][0]

            def call():
                sock = connect()
                authority = module.RuntimeAuthority().load()
                pid = authority.binding()
                identity = process_identity(pid, authority.uid)
                for _ in range(3):
                    module.connected_peer_with_retry(authority, sock, pid, identity)
                sock.close()
            return call

        rows = {}
        for name, make in (('binding', binding), ('connected_peer', peer), ('load', load), ('request_shaped_total', request)):
            samples = alternate(runs, make('old'), make('new'))
            row = {'old': summarize(samples[0]), 'new': summarize(samples[1])}
            row['speedup_p50'] = round(row['old']['p50'] / row['new']['p50'], 2)
            row['speedup_p95'] = round(row['old']['p95'] / row['new']['p95'], 2)
            rows[name] = row
            print(f"{name}: old p50 {row['old']['p50']} p95 {row['old']['p95']} min {row['old']['min']} max {row['old']['max']}"
                  f" | new p50 {row['new']['p50']} p95 {row['new']['p95']} min {row['new']['min']} max {row['new']['max']}"
                  f" | p50 x{row['speedup_p50']} p95 x{row['speedup_p95']}", flush=True)
        return rows
    finally:
        for sock in sockets:
            sock.close()
        if hasattr(new, '_POOL'):
            new._POOL.shutdown()


def executable_benchmark(old, new, runs):
    """``_executable`` alone (old: lsof -d txt, new: proc_pidpath) for the live server and its parent."""
    authority = new.RuntimeAuthority().load()
    server, _executable, parent, _parent_executable = authority._identity
    rows = {}
    for label, pid in (('server', server), ('parent', parent)):
        old_path = old.DesktopOmlx._executable(fresh(old), pid)
        new_path = new.DesktopOmlx._executable(fresh(new), pid)
        samples = alternate(runs, lambda pid=pid: old.DesktopOmlx._executable(fresh(old), pid),
                            lambda pid=pid: new.DesktopOmlx._executable(fresh(new), pid))
        row = {'old': summarize(samples[0]), 'new': summarize(samples[1]),
               'same_path': old_path == new_path, 'path': os.fspath(new_path)}
        row['speedup_p50'] = round(row['old']['p50'] / row['new']['p50'], 1)
        rows[label] = row
        print(f"_executable[{label}]: old p50 {row['old']['p50']} p95 {row['old']['p95']} | new p50 {row['new']['p50']} "
              f"p95 {row['new']['p95']} | same path {row['same_path']} | p50 x{row['speedup_p50']}", flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--runs', type=int, default=11)
    parser.add_argument('--baseline-ref', default=DEFAULT_BASELINE)
    parser.add_argument('--load', action='store_true', help='also time RuntimeAuthority().load() (live, read-only)')
    parser.add_argument('--verify', action='store_true', help='compare old and new inventories on the real trees')
    parser.add_argument('--spawn', action='store_true',
                        help='time binding/connected_peer/load/request total, serial baseline vs scheduled (live, read-only)')
    parser.add_argument('--executable', action='store_true',
                        help='time the executable-path lookup alone, lsof -d txt vs proc_pidpath (live, read-only)')
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

    if args.executable:
        result['executable'] = executable_benchmark(old, new, args.runs)
        result['load_after'], result['swap_after'] = load_average(), swap_usage()
        print('loadavg', result['load_before'], '->', result['load_after'], '| swap', result['swap_after'])
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=1))
        if hasattr(new, '_POOL'):
            new._POOL.shutdown()
        return

    if args.spawn:
        from service.inference.local_peer import process_identity
        result['spawn'] = spawn_benchmark(old, new, args.runs, process_identity)
        result['load_after'], result['swap_after'] = load_average(), swap_usage()
        result['leftover_threads'] = sorted(t.name for t in threading.enumerate() if t.name.startswith('wisp-inspect'))
        result['leftover_children'] = own_children()
        print('leftover wisp-inspect threads', result['leftover_threads'], '| leftover child processes', result['leftover_children'])
        print('loadavg', result['load_before'], '->', result['load_after'], '| swap', result['swap_after'])
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=1))
        return

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
