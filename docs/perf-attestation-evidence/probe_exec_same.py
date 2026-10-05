import json, sys
sys.argv = ['x']
src = open('probe_exec.py').read().split("out = {'python'")[0]
exec(src)
r = exec_case('bash -> exec /bin/bash (same binary, new argv)', 'read x; exec /bin/bash -c "sleep 30; true"')
ps_before = None
print(json.dumps({k: r[k] for k in ('label', 'changed', 'bsd_changed')}), json.dumps(r['before']), json.dumps(r['after']), sep='\n')
