"""Exec detection and per-use revalidation cost, on DISPOSABLE child processes only."""
import ctypes, json, os, struct, subprocess, sys, time

LP = ctypes.CDLL('/usr/bin/true', use_errno=True)
LP.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
LP.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
SYS = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
SYS.csops.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t]


def bsd(pid):
    b = ctypes.create_string_buffer(136)
    n = LP.proc_pidinfo(pid, 3, 0, b, 136)
    if n != 136:
        return None
    r = b.raw
    flags, status, xstatus, p, ppid, uid, gid, ruid, rgid, svuid, svgid, _ = struct.unpack_from('=12I', r, 0)
    comm = r[48:64].split(b'\0')[0].decode(); name = r[64:96].split(b'\0')[0].decode()
    start_s, start_us = struct.unpack_from('=QQ', r, 120)
    return dict(flags=hex(flags), ppid=ppid, uid=(uid, ruid, svuid), comm=comm, name=name, start=(start_s, start_us))


def path(pid):
    b = ctypes.create_string_buffer(4096)
    n = LP.proc_pidpath(pid, b, 4096)
    return b.raw[:n].decode() if n > 0 else None


def cs(pid, op, size):
    b = ctypes.create_string_buffer(size)
    rc = SYS.csops(pid, op, b, size)
    return b.raw if rc == 0 else ('err', ctypes.get_errno())


def facts(pid):
    st = cs(pid, 0, 4); cd = cs(pid, 5, 20); ident = cs(pid, 11, 256)
    return dict(bsd=bsd(pid), path=path(pid),
                cs_flags=hex(struct.unpack('=I', st)[0]) if isinstance(st, bytes) else st,
                cdhash=cd.hex() if isinstance(cd, bytes) else cd,
                identity=ident[8:].split(b'\0')[0].decode() if isinstance(ident, bytes) else ident)


def exec_case(label, script):
    child = subprocess.Popen(['/bin/sh', '-c', script], stdin=subprocess.PIPE)
    try:
        time.sleep(0.2)
        before = facts(child.pid)
        child.stdin.write(b'go\n'); child.stdin.flush()
        time.sleep(0.3)
        after = facts(child.pid)
        changed = sorted(k for k in before if before[k] != after[k])
        bsd_changed = sorted(k for k in before['bsd'] if before['bsd'][k] != after['bsd'][k]) if before['bsd'] and after['bsd'] else 'n/a'
        return dict(label=label, before=before, after=after, changed=changed, bsd_changed=bsd_changed)
    finally:
        child.kill(); child.wait()


out = {'python': sys.version.split()[0]}
out['exec_other_binary'] = exec_case('sh -> exec /bin/sleep', 'read x; exec /bin/sleep 30')
out['exec_same_binary'] = exec_case('sh -> exec /bin/sh (same binary, new argv)', 'read x; exec /bin/sh -c "sleep 30"')

# Per-use revalidation cost: two processes (child + its parent = us) x (bsdinfo, pidpath, csops status, cdhash)
# plus 12 lstat calls on small fixed paths, plus one manifest-absence open().
child = subprocess.Popen(['/bin/sleep', '30'])
try:
    time.sleep(0.1)
    paths = ['/bin/sleep', '/bin/sh', '/usr/bin', '/bin', '/usr', '/', '/Applications', '/usr/bin/true',
             '/usr/sbin/lsof', '/bin/ps', '/usr/lib', '/usr/sbin']
    samples = []
    for _ in range(300):
        t = time.perf_counter()
        for pid in (child.pid, os.getpid()):
            bsd(pid); path(pid); cs(pid, 0, 4); cs(pid, 5, 20)
        for p in paths:
            os.lstat(p)
        try:
            os.open('/nonexistent-wisp-manifest-probe', os.O_RDONLY)
        except FileNotFoundError:
            pass
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    out['revalidation_ms_p50_p95_max'] = [round(samples[150], 4), round(samples[285], 4), round(samples[-1], 4)]
    # PID reuse: start time of a dead pid after it exits.
finally:
    child.kill(); child.wait()
out['dead_pid_bsd'] = bsd(child.pid)
cd = subprocess.run(['/usr/bin/codesign', '-dvvv', '/bin/sleep'], capture_output=True, text=True).stderr
out['codesign_cdhash_sleep'] = [l for l in cd.splitlines() if l.startswith('CDHash=')]
out['clock'] = {'monotonic_impl': __import__('time').get_clock_info('monotonic').implementation,
                'has_CLOCK_MONOTONIC': hasattr(time, 'CLOCK_MONOTONIC'),
                'CLOCK_MONOTONIC_minus_monotonic_s': round(time.clock_gettime(time.CLOCK_MONOTONIC) - time.monotonic(), 1)}
print(json.dumps(out, indent=1))
