"""Reproduce CLI checks in a fresh disposable repo; requires installed EVO 0.8.0.
No installation, model/API call, Wisp import, or global config mutation.
"""
import hashlib, json, os, pathlib, platform, shutil, subprocess, sys, tempfile, time
HERE = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path(tempfile.mkdtemp(prefix='evo-cli-pilot-')).resolve()
REPO = ROOT / 'synthetic'
REPO.mkdir()
EVO = shutil.which('evo')
assert EVO, 'Install/approve EVO separately; this runner never installs packages'
# Allowlist excludes inherited credentials, model endpoints and runtime hooks.
env = {'PATH': os.environ['PATH'], 'HOME': os.environ['HOME'],
       'EVO_HOME': str(ROOT / 'evo-home'), 'EVO_TELEMETRY': '0', 'DO_NOT_TRACK': '1',
       'EVO_SKIP_VERSION_CHECK': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'GIT_CONFIG_NOSYSTEM': '1',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_AUTHOR_NAME': 'Synthetic Pilot',
       'GIT_AUTHOR_EMAIL': 'pilot@example.invalid', 'GIT_COMMITTER_NAME': 'Synthetic Pilot',
       'GIT_COMMITTER_EMAIL': 'pilot@example.invalid'}
records = []
def run(args, cwd=REPO, expect=0):
    start = time.monotonic()
    p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=45)
    records.append(dict(args=args, exit=p.returncode, seconds=round(time.monotonic()-start,3),
                        stdout=p.stdout, stderr=p.stderr))
    print('exit', p.returncode, *args, flush=True)
    if expect == 'nonzero':
        assert p.returncode != 0, records[-1]
    elif expect is not None:
        assert p.returncode == expect, records[-1]
    return p.stdout

def evo(*args, **kwargs):
    return run([EVO, *args], **kwargs)

try:
    assert evo('--version').strip() == 'evo-hq-cli 0.8.0'
    run(['git', 'init', '-b', 'pilot-base'])
    (REPO / '.gitignore').write_text('.evo/\n__pycache__/\n*.pyc\n')
    (REPO / 'README.md').write_text('Synthetic CLI acceptance fixture. No Wisp data.\n')
    run(['git','add','.'])
    run(['git','commit','-m','Synthetic fixture base'])
    base = run(['git','rev-parse','HEAD']).strip()
    bench = f'{sys.executable} {{worktree}}/benchmark.py'
    evo('init','--name','Synthetic CLI pilot','--target','target.py','--benchmark',bench,
        '--metric','max','--host','codex','--instrumentation-mode','inline',
        '--per-exp-timeout','15','--port','18880',expect=None)
    if records[-1]['exit']:
        assert 'no free port' in records[-1]['stderr'], records[-1]
        assert (REPO/'.evo/run_0000/config.json').exists(), 'No usable initialized state'
    (REPO / '.evo/project.md').write_text('Sole writer: EVO CLI pilot. Synthetic parser; no Wisp inputs.\n'
        'Deterministic by construction; CPU only, no inference or network.\n'
        'Bound: one baseline, finite CLI acceptance checks; no optimization loop.\n'
        '12 scoring cases and 6 independent validation cases. Gate requires 1.0.\n'
        'Fixtures/gates fixed; memorization remains possible because held-out cases are visible.\n')
    evo('gate','add','root','--name','held-out-correctness','--command',bench+' --gate')
    evo('new','--parent','root','-m','Synthetic baseline for CLI acceptance')
    worktree = pathlib.Path(json.loads(evo('get','exp_0000'))['worktree'])
    for f in (HERE / 'fixture').glob('*.py'):
        shutil.copy2(f, worktree / f.name)
    run(['git','add','.'],cwd=worktree)
    run(['git','commit','-m','Synthetic parser and fixed evaluation contract'],cwd=worktree)
    evo('run','exp_0000','--check')
    evo('run','exp_0000','--check')
    evo('gate','check','exp_0000')
    before = json.loads(evo('show','exp_0000'))
    original = (worktree/'target.py').read_text()
    (worktree/'target.py').write_text('def parse(text):\n    return []\n')
    assert 'GATE_CHECK_FAILED' in evo('gate','check','exp_0000',expect='nonzero')
    assert 'gate_failed:held-out-correctness' in evo('run','exp_0000','--check',expect='nonzero')
    (worktree/'target.py').write_text(original)
    benchmark = (worktree/'benchmark.py').read_text()
    for name, bad in [('missing-result','pass\n'),
        ('nonnumeric-score','import os,pathlib\npathlib.Path(os.environ["EVO_RESULT_PATH"]).write_text(\'{"score":"wrong"}\')\n'),
        ('duplicate-writer','from inline_instrumentation import write_result\nwrite_result(1)\nwrite_result(1)\n')]:
        (worktree/'benchmark.py').write_text(bad)
        output = evo('run','exp_0000','--check',expect='nonzero')
        expected_error = {'missing-result':'missing_result_json', 'nonnumeric-score':'could not convert string to float', 'duplicate-writer':'benchmark_exit_1'}[name]
        assert expected_error in output, output
    (worktree/'benchmark.py').write_text(benchmark)
    after = json.loads(evo('show','exp_0000'))
    assert before['status'] == after['status'] == 'pending'
    assert before['score'] is after['score'] is None
    assert before['attempts'] == after['attempts'] == []
    evo('run','exp_0000')
    final = json.loads(evo('show','exp_0000'))
    assert final['status'] == 'committed' and final['score'] == 1.0
    evo('status')
    evo('tree')
    assert run(['git','rev-parse','HEAD']).strip() == base
    assert not run(['git','status','--porcelain']).strip()
    assert not run(['git','status','--porcelain'],cwd=worktree).strip()
    artifacts = []
    for path in sorted((REPO/'.evo').rglob('result.json')):
        try:
            value = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        artifacts.append({'path': str(path.relative_to(REPO)), 'result':value})
    expdir = REPO/'.evo/run_0000/experiments/exp_0000'
    repeats = [json.loads((expdir / part / 'result.json').read_text()) for part in ['checks/001','checks/002','attempts/001']]
    assert all(r['score'] == 1.0 and len(r['tasks']) == 12 for r in repeats)
    assert repeats[0]['tasks'] == repeats[1]['tasks'] == repeats[2]['tasks']
    traces = [json.loads(p.read_text()) for p in sorted((expdir/'attempts/001/traces').glob('task_*.json'))]
    assert len(traces) == 12 and all(t['score'] == 1 for t in traces)
    snapshot = {str(p.relative_to(expdir)): json.loads(p.read_text()) for p in expdir.rglob('gate_check.json')}
    assert snapshot['checks/003/gate_check.json']['status'] == 'passed'
    assert snapshot['checks/004/gate_check.json']['error'] == 'gate_failed:held-out-correctness'
    gate_output = run([sys.executable, str(worktree/'benchmark.py'), '--gate'])
    gate_result = json.loads(gate_output)
    assert gate_result['score'] == 1 and len(gate_result['tasks']) == 6
    summary = {'gate_result':gate_result, 'python':platform.python_version(), 'platform':platform.platform(),
               'fixture_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (HERE/'fixture').glob('*.py')},
               'traces':traces,'gate_checks':snapshot,'final_node':json.loads(evo('get','exp_0000')), 'root':str(ROOT),'synthetic_base':base,'before':before,'after_checks':after,
               'final':final,'artifacts':artifacts, 'completed':True}
finally:
    # Internal cleanup helper stops only this fresh repo's dashboard, preserving all experiment state.
    if (REPO/'.evo').exists():
        interpreter = pathlib.Path(EVO).resolve().read_text().splitlines()[0][2:]
        run([interpreter,'-c','from pathlib import Path; from evo.cli import _stop_dashboard; _stop_dashboard(Path.cwd())'],expect=None)
    (HERE/'evidence.json').write_text(json.dumps({'commands':records,'summary':locals().get('summary',{'root':str(ROOT),'completed':False})},indent=2)+'\n')
    print('Evidence:',HERE/'evidence.json','Temporary repo:',ROOT)
