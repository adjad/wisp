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


SERVER = r'''
import os, socket, sys, time
s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(4)
print(s.getsockname()[1], flush=True)
c, _ = s.accept()
held = []
for line in sys.stdin:
    cmd = line.strip()
    if cmd == 'dup':
        held.append(os.dup(c.fileno()))
    elif cmd == 'undup':
        os.close(held.pop())
    elif cmd == 'fork':
        pid = os.fork()
        if pid == 0:
            time.sleep(3); os._exit(0)
        held.append(('child', pid))
    print('ok', flush=True)
'''


def main():
    out = {'python': sys.version.split()[0]}
    srv = subprocess.Popen([sys.executable, '-I', '-c', SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        port = int(srv.stdout.readline())
        cli = socket.create_connection(('127.0.0.1', port))
        time.sleep(0.2)
        local = cli.getsockname()
        out['client_local'] = local
        out['server_port'] = port
        # Server side, native vs lsof.
        native = tcp_table(srv.pid)
        out['server_native'] = [(fd, {k: v for k, v in i.items() if k not in ('so',)}) for fd, i in native]
        out['server_lsof'] = lsof_rows(srv.pid)
        # Client side (own fd).
        out['client_native'] = sock_info(os.getpid(), cli.fileno())
        # FP_SHARED behaviour.
        def server_conn_status():
            for fd, i in tcp_table(srv.pid):
                if i.get('state') == 4:
                    return i['fi_status'], i['so'], i['gencnt']
        out['shared_baseline'] = server_conn_status()[0]
        srv.stdin.write('dup\n'); srv.stdin.flush(); srv.stdout.readline()
        out['shared_after_dup'] = server_conn_status()[0]
        srv.stdin.write('undup\n'); srv.stdin.flush(); srv.stdout.readline()
        out['shared_after_undup'] = server_conn_status()[0]
        srv.stdin.write('fork\n'); srv.stdin.flush(); srv.stdout.readline()
        time.sleep(0.1)
        out['shared_after_fork'] = server_conn_status()[0]
        d = os.dup(cli.fileno())
        out['client_shared_after_dup'] = sock_info(os.getpid(), cli.fileno())['fi_status']
        os.close(d)
        out['client_shared_after_close'] = sock_info(os.getpid(), cli.fileno())['fi_status']
        # Timing: one native owner check = list server fds + socket info per socket fd + own fd.
        samples = []
        for _ in range(200):
            t = time.perf_counter()
            tcp_table(srv.pid); sock_info(os.getpid(), cli.fileno())
            samples.append((time.perf_counter() - t) * 1000)
        samples.sort()
        out['native_check_ms_p50_p95_max'] = [round(samples[100], 4), round(samples[190], 4), round(samples[-1], 4)]
        lt = []
        for _ in range(7):
            t = time.perf_counter(); lsof_rows(srv.pid); lt.append((time.perf_counter() - t) * 1000)
        lt.sort(); out['lsof_p_ms_p50'] = round(lt[3], 2)
        # Other-uid visibility: pid 1 (launchd, root).
        fds, err = list_fds(1)
        out['pid1_listfds'] = {'ok': fds is not None, 'errno': err}
        cli.close()
    finally:
        srv.kill(); srv.wait()
    print(json.dumps(out, indent=1, default=str))


main()
