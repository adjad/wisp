"""Finite, offline EVO 0.8.0 lifecycle evaluation; no Wisp imports.

Reuses the immutable prior pilot's inline helper rather than duplicating it.
All generated repositories and EVO configuration stay in this directory/.runs.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
PRIOR = 'e27fca2aa71eef1f3b6043139493ca86e67b5e4d'
BASE = '014aee2c9b73658e439c820f9e649ff926fabdb9'

# Deterministic operation-count proxy, deliberately not a latency claim.
BASELINE = '''def unique(values):
    out, operations = [], 0
    for value in values:
        found = False
        for existing in out:
            operations += 1
            if existing == value:
                found = True
                break
        if not found:
            out.append(value)
    return out, operations
'''
OPTIMIZED = '''def unique(values):
    seen, out = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out, len(values)
'''
REGRESSION = '''def unique(values):
    return [], 0
'''
BENCHMARK = '''from target import unique
from inline_instrumentation import log_task, write_result
for n in (16, 32, 64):
    result, operations = unique(list(range(n)))
    log_task(str(n), 1 / (1 + operations), summary="synthetic operation proxy")
write_result()
'''
GATE = '''from target import unique
cases = [([], []), ([3, 1, 3, 2], [3, 1, 2]),
         ([-1, 0, -1], [-1, 0]), ([5], [5])]
for values, expected in cases:
    actual, _ = unique(values)
    assert actual == expected, (values, expected, actual)
print("4/4 independent correctness cases passed")
'''


def main():
    (HERE / '.runs').mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='run-', dir=HERE / '.runs'))
    repo = root / 'synthetic'
    repo.mkdir()
    evo_bin = shutil.which('evo')
    assert evo_bin, 'Requires preinstalled EVO; never installs dependencies'
    env = {
        'PATH': os.environ['PATH'],
        'HOME': str(root / 'home'),
        'EVO_HOME': str(root / 'evo-home'),
        'EVO_TELEMETRY': '0', 'EVO_SKIP_VERSION_CHECK': '1', 'DO_NOT_TRACK': '1',
        'PYTHONDONTWRITEBYTECODE': '1', 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_AUTHOR_NAME': 'Synthetic Evaluation',
        'GIT_AUTHOR_EMAIL': 'evaluation@example.invalid',
        'GIT_COMMITTER_NAME': 'Synthetic Evaluation',
        'GIT_COMMITTER_EMAIL': 'evaluation@example.invalid',
    }
    Path(env['HOME']).mkdir()
    commands, nodes = [], {}
    evidence = {'completed': False, 'wisp_base': BASE, 'prior_pilot': PRIOR,
                'python': platform.python_version(), 'root': str(root),
                'commands': commands, 'nodes': nodes}

    def run(args, cwd=repo, expect=0):
        proc = subprocess.run(args, cwd=cwd, env=env, text=True,
                              capture_output=True, timeout=45)
        commands.append({'args': args, 'exit': proc.returncode,
                         'stdout': proc.stdout, 'stderr': proc.stderr})
        print(proc.returncode, *args, flush=True)
        if expect is not None:
            assert proc.returncode == expect, commands[-1]
        return proc.stdout

    def evo(*args, **kwargs):
        return run([evo_bin, *args], **kwargs)

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    try:
        assert evo('--version').strip() == 'evo-hq-cli 0.8.0'
        helper = run(['git', 'show', PRIOR + ':research/evo-cli-pilot/fixture/inline_instrumentation.py'], cwd=HERE)
        commands[-1]['stdout'] = 'Helper content omitted; immutable source commit and SHA-256 recorded.'
        files = {'target.py': BASELINE, 'benchmark.py': BENCHMARK,
                 'gate.py': GATE, 'inline_instrumentation.py': helper,
                 '.gitignore': '.evo/\n__pycache__/\n'}
        for name, content in files.items():
            (repo / name).write_text(content)
        protected = {name: digest(repo / name) for name in files if name != 'target.py'}
        evidence['protected_sha256'] = protected
        run(['git', 'init', '-b', 'synthetic-base'])
        run(['git', 'add', '.'])
        run(['git', 'commit', '-m', 'Synthetic evaluation contract'])
        original = run(['git', 'rev-parse', 'HEAD']).strip()
        python = shlex.quote(sys.executable)
        evo('init', '--name', 'Bounded lifecycle evaluation', '--target', 'target.py',
            '--benchmark', python + ' "{worktree}/benchmark.py"', '--metric', 'max',
            '--host', 'codex', '--instrumentation-mode', 'inline',
            '--per-exp-timeout', '10', '--port', '18880', expect=None)
        if commands[-1]['exit']:
            assert 'no free port' in commands[-1]['stderr'], commands[-1]
            assert (repo / '.evo/run_0000/config.json').exists()
        (repo / '.evo/project.md').write_text('Finite synthetic lifecycle acceptance. No agent search, Wisp imports, or network workloads. Fixed benchmark and gate; only target.py and a deliberate harmless scope probe may change.\n')
        evo('gate', 'add', 'root', '--name', 'correctness',
            '--command', python + ' "{worktree}/gate.py"')
        worktrees = []
        for index, (name, source) in enumerate([
            ('baseline', BASELINE), ('improvement', OPTIMIZED),
            ('regression', REGRESSION),
            ('non_improvement', BASELINE.replace('operations += 1', 'operations += 2')),
            ('tie', BASELINE),
        ]):
            exp = f'exp_{index:04d}'
            evo('new', '--parent', 'root' if index == 0 else 'exp_0000', '-m', name)
            worktree = Path(json.loads(evo('get', exp))['worktree'])
            assert worktree.is_relative_to(root), worktree
            worktrees.append(worktree)
            (worktree / 'target.py').write_text(source)
            if name == 'improvement':
                (worktree / 'scope-probe.txt').write_text('Harmless synthetic untracked file outside --target.\n')
            pre_head = run(['git', 'rev-parse', 'HEAD'], cwd=worktree).strip()
            output = evo('run', exp)
            node = json.loads(evo('get', exp))
            nodes[name] = node
            if name in ('baseline', 'improvement', 'tie'):
                assert node['status'] == 'committed' and node['gate_result'] is True, node
            else:
                # Full-run rejection is exit zero, unlike check-mode gate failure.
                assert node['status'] == 'evaluated' and 'EVALUATED' in output, node
                assert run(['git', 'rev-parse', 'HEAD'], cwd=worktree).strip() == pre_head
            assert {name: digest(worktree / name) for name in protected} == protected
        assert nodes['improvement']['score'] > nodes['baseline']['score']
        assert nodes['regression']['score'] > nodes['improvement']['score']
        assert nodes['regression']['gate_result'] is False
        assert nodes['non_improvement']['gate_result'] is True
        assert nodes['non_improvement']['score'] < nodes['baseline']['score']
        assert nodes['tie']['score'] == nodes['baseline']['score']
        diff = run(['git', 'diff', '--name-only', nodes['baseline']['commit'],
                    nodes['improvement']['commit']])
        assert set(diff.splitlines()) == {'target.py', 'scope-probe.txt'}, diff
        evidence['candidate_files'] = diff.splitlines()
        evidence['status'] = evo('status')
        evidence['frontier'] = evo('frontier', '--strategy', 'argmax')
        assert 'exp_0001' in evidence['frontier'] and 'exp_0002' not in evidence['frontier']
        evidence['report'] = evo('report', '--color', 'never')
        evidence['target_diff'] = evo('diff', 'exp_0000', 'exp_0001')
        assert 'scope-probe.txt' not in evidence['target_diff']
        evidence['attempts'] = {
            str(p.relative_to(repo)): json.loads(p.read_text())
            for p in sorted((repo / '.evo').glob('run_*/experiments/*/attempts/*/outcome.json'))
        }
        assert len(evidence['attempts']) == 5
        assert run(['git', 'rev-parse', 'HEAD']).strip() == original
        assert not run(['git', 'status', '--porcelain']).strip()
        evidence['completed'] = True
    finally:
        if (repo / '.evo').exists():
            interpreter = Path(evo_bin).resolve().read_text().splitlines()[0][2:]
            run([interpreter, '-c', 'from pathlib import Path; from evo.cli import _stop_dashboard; _stop_dashboard(Path.cwd())'], expect=None)
        (HERE / 'evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print('Evidence:', HERE / 'evidence.json')


if __name__ == '__main__':
    main()
