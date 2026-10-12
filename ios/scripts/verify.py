#!/usr/bin/env python3
"""Exact-commit offline foundation verification; never installs apps or runs models."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--expected-sha', required=True)
p.add_argument('--artifacts', type=Path, required=True)
p.add_argument('--fixtures', type=Path, help='Read-only shared tests/workflow20/cases directory')
args = p.parse_args()
artifacts = args.artifacts.resolve()
try:
    artifacts.relative_to(ROOT)
except ValueError:
    pass
else:
    p.error('Artifacts must be outside the source checkout.')
artifacts.mkdir(parents=True, exist_ok=True)
def git(*parts):
    return subprocess.check_output(['git', '-C', str(ROOT), *parts], text=True).strip()
head, tree, dirty = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}'), git('status', '--porcelain')
if head != args.expected_sha or dirty:
    p.error('Expected exact SHA and a clean checkout before verification.')
report = {'schema_version': 1, 'head': head, 'tree': tree, 'clean_before': True,
          'commands': [], 'model_execution': 'NOT_RUN', 'device_install': 'NOT_RUN',
          'simulator_launch': 'NOT_RUN', 'inference_latency': None, 'semantic_completion': 'NOT_VALIDATED'}
def run(name, command):
    log = artifacts / (name + '.log')
    with log.open('w') as output:
        result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    report['commands'].append({'name': name, 'argv': command, 'exit_code': result.returncode,
                               'log': str(log), 'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest()})
    return result.returncode == 0
ok = run('swift-tests', ['swift', 'test', '--package-path', 'ios', '--scratch-path', str(artifacts/'core')])
ok = run('simulator-build', ['xcodebuild', '-quiet', '-project', 'ios/WispPhone.xcodeproj', '-scheme', 'WispPhone',
         '-sdk', 'iphonesimulator', '-destination', 'generic/platform=iOS Simulator', '-derivedDataPath', str(artifacts/'app'),
         'CODE_SIGNING_ALLOWED=NO', 'build']) and ok
if args.fixtures and ok:
    files = sorted(args.fixtures.resolve().glob('*.json'))
    if not files:
        raise SystemExit('No shared fixture files found.')
    report['fixtures'] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    ok = run('contract-consumer', [str(artifacts/'core/debug/workflow20-ios'), *map(str, files)]) and ok
    if ok:
        observations = [json.loads(line) for line in (artifacts/'contract-consumer.log').read_text().splitlines()]
        (artifacts/'observations.json').write_text(json.dumps(observations, indent=2) + '\n')
        report['contract_consumption'] = {'cases': len(observations), 'statuses': sorted({row['status'] for row in observations}),
            'effects': sum(len(row['effects']) for row in observations),
            'inference_executed': any(row['metrics']['inference_executed'] for row in observations),
            'meaning': 'Schema plumbing only; unsupported cases are capability failures, not workflow passes.'}
        ok = all(row['status'] == 'unsupported' and not row['effects'] and not row['facts'] and
                 not row['approval']['approved_effects'] and not row['approval']['granted'] and
                 not row['metrics']['inference_executed'] for row in observations) and ok
report['clean_after'] = not git('status', '--porcelain')
report['head_after'], report['tree_after'] = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
ok = ok and report['clean_after'] and report['head_after'] == head and report['tree_after'] == tree
report['foundation_mechanical_pass'] = ok
(artifacts/'mechanical.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'head': head, 'mechanical_pass': ok, 'report': str(artifacts/'mechanical.json')}))
sys.exit(0 if ok else 1)
