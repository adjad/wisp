#!/usr/bin/env python3
"""Measure the real memory worker with a synthetic completed-job queue.

Run the same script with each checkout selected by --root. --live-status uses
that checkout's qualified OMLXClient for read-only residency queries; it never
loads a model, generates text, or starts the backend lifespan. Without that
flag, calls are counted offline and CPU does not include real engine checks.
Example: python -B scripts/bench_idle_memory_worker.py --root . --seconds 60 --live-status
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time


def cpu_seconds():
    return sum(resource.getrusage(who).ru_utime + resource.getrusage(who).ru_stime
               for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN))


async def measure(root, seconds, completed, live):
    from service.memory.capture import MemoryWorker
    from service.memory.facts import FactStore
    from service.memory.store import SessionStore
    from service.paths import MOE_DIR

    facts = FactStore(MOE_DIR / 'fixture-facts.db')
    sessions = SessionStore(MOE_DIR / 'fixture-sessions.db')
    sid = sessions.create_session()
    # Match the approximate completed-job count in the original evaluation.
    for _ in range(completed):
        sessions.add_turn(sid, 'user', 'Synthetic completed statement.')
    with sessions._lock, sessions._db:
        sessions._db.execute("UPDATE memory_jobs SET status='done'")
    worker = MemoryWorker(sessions, facts)
    active = None
    if live:
        from service.inference.omlx_client import OMLXClient
        active = OMLXClient()

    class ObservedClient:
        calls = 0
        failures = 0

        async def loaded_models(self):
            self.calls += 1
            if active is None:
                return []
            try:
                return await active.loaded_models()
            except Exception:
                self.failures += 1
                raise

    client = ObservedClient()
    start = time.monotonic()
    cpu = cpu_seconds()
    task = asyncio.create_task(worker.run(client))
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if active:
            await active.aclose()
        facts._db.close()
        sessions._db.close()
    elapsed = time.monotonic() - start
    consumed = cpu_seconds() - cpu
    return {'kind': 'synthetic_empty_memory_worker', 'seconds': elapsed,
            'completed_jobs': completed, 'live_status': live,
            'status_calls': client.calls, 'status_failures': client.failures,
            'worker_state': worker.state, 'cpu_s_including_reaped_children': consumed,
            'cpu_pct_one_core': 100 * consumed / elapsed,
            'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                              for name in ('service/memory/capture.py', 'service/memory/queue.py')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--completed-jobs', type=int, default=535)
    parser.add_argument('--live-status', action='store_true')
    args = parser.parse_args()
    if not 3 < args.seconds <= 600 or not 0 <= args.completed_jobs <= 10000:
        parser.error('seconds must be in (3, 600], completed-jobs in [0, 10000]')
    root = args.root.resolve(strict=True)
    if not (root / 'service/memory/capture.py').is_file():
        parser.error('root must contain the selected Wisp source')
    # No inherited native credentials or remote-inference switches. HOME is unchanged.
    for key in tuple(os.environ):
        if key.startswith('WISP_'):
            os.environ.pop(key)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='wisp-idle-worker-benchmark-') as scratch:
        os.environ['WISP_HOME'] = scratch
        result = asyncio.run(measure(root, args.seconds, args.completed_jobs, args.live_status))
    print(json.dumps(result, sort_keys=True))
    return int(result['status_failures'] != 0)


if __name__ == '__main__':
    raise SystemExit(main())
