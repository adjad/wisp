"""READ-ONLY: inspect the live oMLX listener's TCP sockets with libproc and lsof. No connect, no signal."""
import json, subprocess, sys, time
sys.path.insert(0, '.')
from probe_sockets_lib import tcp_table, lsof_rows

ENV = {'PATH': '/usr/bin:/bin:/usr/sbin', 'LC_ALL': 'C'}
raw = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-iTCP:8000', '-sTCP:LISTEN', '-Fp'], capture_output=True, env=ENV).stdout.decode()
pid = int(raw.split()[0][1:])
out = {'pid': pid, 'rounds': []}
seen_est = 0
for round_ in range(12):
    native = [(fd, i) for fd, i in tcp_table(pid)]
    nat = sorted((fd, f"{i['laddr']}:{i['lport']}" + (f"->{i['faddr']}:{i['fport']}" if i['state'] != 1 else ''), i['state'], i['fi_status'], i['vflag'], i['family']) for fd, i in native)
    ls = sorted((r['fd'], r.get('n', '').rstrip(';'), r.get('T', '')) for r in lsof_rows(pid))
    est = [n for n in nat if n[2] == 4]
    seen_est += len(est)
    out['rounds'].append({'native': nat, 'lsof': ls})
    if seen_est >= 2 and round_ >= 2:
        break
    time.sleep(1.0)
print(json.dumps(out, indent=1))
