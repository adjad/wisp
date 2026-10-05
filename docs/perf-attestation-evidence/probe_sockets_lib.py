"""Read-only libproc socket-owner probe against DISPOSABLE loopback sockets created here.

Never touches :8000/:8765. Spawns its own server child on an ephemeral port, connects to it,
parses PROC_PIDFDSOCKETINFO with the offsets measured by layout.c, and compares with lsof.
"""
import ctypes, json, os, socket, struct, subprocess, sys, time

LIB = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
LIB.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
LIB.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
SFD = 792


def list_fds(pid):
    buf = ctypes.create_string_buffer(8 * 4096)
    n = LIB.proc_pidinfo(pid, 1, 0, buf, len(buf))
    if n <= 0 or n % 8:
        return None, ctypes.get_errno()
    return [struct.unpack_from('=iI', buf.raw, i) for i in range(0, n, 8)], 0


def sock_info(pid, fd):
    buf = ctypes.create_string_buffer(SFD + 256)
    n = LIB.proc_pidfdinfo(pid, fd, 3, buf, len(buf))
    if n != SFD:
        return {'ret': n, 'errno': ctypes.get_errno()}
    r = buf.raw
    fi_status, = struct.unpack_from('=I', r, 4)
    so, pcb = struct.unpack_from('=QQ', r, 160)
    typ, proto, fam = struct.unpack_from('=iii', r, 176)
    kind, = struct.unpack_from('=i', r, 256)
    out = {'ret': n, 'fi_status': fi_status, 'so': so, 'type': typ, 'proto': proto, 'family': fam, 'kind': kind}
    if kind == 2:
        fport, lport = struct.unpack_from('=ii', r, 264)
        gencnt, = struct.unpack_from('=Q', r, 272)
        vflag = r[288]
        faddr = socket.inet_ntoa(r[308:312]); laddr = socket.inet_ntoa(r[324:328])
        state, = struct.unpack_from('=i', r, 344)
        out.update(fport_raw=fport, lport_raw=lport, fport=socket.ntohs(fport & 0xffff), lport=socket.ntohs(lport & 0xffff),
                   gencnt=gencnt, vflag=vflag, faddr=faddr, laddr=laddr, state=state)
    return out


def tcp_table(pid):
    fds, err = list_fds(pid)
    if fds is None:
        return {'error': err}
    rows = []
    for fd, kind in fds:
        if kind == 2:
            info = sock_info(pid, fd)
            if info.get('kind') == 2:
                rows.append((fd, info))
    return rows


def lsof_rows(pid):
    raw = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(pid), '-iTCP', '-FpufPtTn', '-Ts'],
                         capture_output=True, env={'PATH': '/usr/bin:/bin:/usr/sbin', 'LC_ALL': 'C'}).stdout.decode()
    rows, fd = [], None
    cur = {}
    for line in raw.splitlines():
        k, v = line[0], line[1:]
        if k == 'f':
            if cur: rows.append(cur)
            cur = {'fd': int(v)}
        elif k in 'nT' and cur:
            cur[k] = cur.get(k, '') + v + ';'
    if cur: rows.append(cur)
    return rows


