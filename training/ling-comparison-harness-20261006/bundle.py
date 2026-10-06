#!/usr/bin/env python3
"""Package source/fixtures only; never include models, credentials or results."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = ('runner.py', 'scoring.py', 'backends.py', 'test_harness.py', 'bundle.py',
         'cases.jsonl', 'router-system.txt', 'intent.schema.v1.json', 'README.md', 'CASES.md', 'HANDOFF.md', '.gitignore')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists; choose a new archive path rather than overwrite')
    payloads = {name: (ROOT / name).read_bytes() for name in FILES}
    manifest = {'format': 1, 'tools_executed': False, 'models_or_personal_data_bundled': False,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}}
    payloads['BUNDLE.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    with zipfile.ZipFile(args.output, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payloads.items()):
            entry = zipfile.ZipInfo(ROOT.name + '/' + name, (2026, 10, 6, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    checksum = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(json.dumps({'archive': str(args.output.resolve()), 'sha256': checksum, 'files': len(payloads)}))


if __name__ == '__main__':
    main()
