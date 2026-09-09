"""Live resident-model checks with synthetic sources and in-process fake tools.

Usage: .venv/bin/python scripts/verify_tool_calling.py
No Mail, Messages, filesystem action, or external data tool is dispatched.
Only inference requests go to the configured local model server.
"""
import asyncio
import argparse
import json
from pathlib import Path
import sys
import time
import fnmatch
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.agent.loop import run_agent
from service.config import role_to_model
from service.inference.omlx_client import OMLXClient
from service.router.router import route
from service.tools.registry import REGISTRY, Tool
from service.workflows.compiler import compile_new, compile_decision


class LocalClient(OMLXClient):
    async def ensure_only(self, *args, **kwargs):
        # Do not unload any models or alter the user's server configuration.
        pass


class Approver:
    async def confirm(self, action):
        return True  # All offered actions are replaced below with fake functions.


async def main():
    client = LocalClient(timeout=60)
    observations = []
    remaining = {'project-debug-1.txt', 'project-debug-2.json'}
    def fake_organize(args):
        pattern = args['pattern']
        prefix = args['folder'].rstrip('/') + '/'
        if pattern.startswith(prefix):
            pattern = pattern[len(prefix):]
        if args['folder'] != '~/Downloads' or '/' in pattern:
            return '(error: invalid source folder or filename glob)'
        matches = sorted(n for n in remaining if fnmatch.fnmatch(n, pattern))
        if not matches:
            return 'No files match the pattern.'
        if not args.get('confirm'):
            return f'Would move {len(matches)} file(s). Call again with confirm=true.'
        remaining.difference_update(matches)
        return f"Moved {len(matches)} file(s) to {args['destination']}.\n" + '\n'.join(matches)
    def install(name, properties, required, category, result):
        def fake(**args):
            observations.append({'name': name, 'args': args})
            return result(args) if callable(result) else result
        # Preserve production schemas and descriptions: they are part of the
        # model's tool-selection input. Replace only execution.
        REGISTRY[name] = replace(REGISTRY[name], func=fake)
    string = {'type': 'string'}
    original = dict(REGISTRY)
    install('lookup_contact', {'name': string}, ['name'], 'system_read', 'Alex: +15555550123')
    install('send_message', {'to': string, 'text': string}, ['to', 'text'],
            'messages_send', lambda args: f"Message sent to {args['to']}.")
    install('web_search', {'query': string}, ['query'], 'web_read',
            'Synthetic market news today: Example Exchange extended its trading hours. Source: https://example.com/news')
    install('find_files', {'query': string}, ['query'], 'fs_read',
            '~/Downloads/project-debug-1.txt\n~/Downloads/project-debug-2.json')
    install('list_dir', {'path': string}, ['path'], 'fs_read',
            lambda a: '(error: directory does not exist)' if a.get('path', '').startswith('/Users')
            else '~/Downloads/project-debug-1.txt\n~/Downloads/project-debug-2.json')
    install('move_path', {'source': string, 'destination': string}, ['source', 'destination'],
            'fs_write', 'Moved file to ~/Downloads/Debug/.')
    install('organize_files', {'pattern': string, 'folder': string, 'destination': string,
                             'confirm': {'type': 'boolean'}}, ['pattern', 'folder', 'destination'],
            'fs_write', fake_organize)
    report = 'Today: no meetings. Tomorrow: workshop at 14:00.'
    artifact = compile_new('send a message to Alex with my daily summary',
                           last_user='Daily summary', last_assistant=report)
    cases = [
        ('referenced_report', 'send a message to Alex with my daily summary', compile_decision(artifact),
         [{'role': 'user', 'content': 'Daily summary'}, {'role': 'assistant', 'content': report}]),
        ('topic_followup', 'and in the stock market?', await route('and in the stock market?',
             last_user='what is the global news for today', last_tools='web_fetch'),
         [{'role': 'user', 'content': 'what is the global news for today'},
          {'role': 'assistant', 'content': 'Here is a global news report.'}]),
        ('file_discovery', 'organize my project debug files',
             await route('organize my project debug files'), []),
    ]
    results = []
    try:
        for label, prompt, decision, history in cases:
            observations.clear()
            events = []
            async def emit(event):
                events.append(event)
            start = time.monotonic()
            answer = await run_agent(client, role_to_model('agent'),
                history + [{'role': 'user', 'content': decision.resolved_request or prompt}], emit, Approver(),
                tools=decision.tool_subset, direct_calls=decision.direct_calls,
                required_tool_groups=decision.required_tool_groups,
                forbidden_tools=decision.forbidden_tools,
                tool_argument_bindings=decision.tool_argument_bindings,
                multi_round=decision.multi_round, include_memory_context=False,
                style_hint=artifact.prompt_block() if label == 'referenced_report' else '',
                max_steps=8, debug=False)
            names = [c['name'] for c in observations]
            passed = bool(answer.strip())
            if label == 'referenced_report':
                sends = [c for c in observations if c['name'] == 'send_message']
                passed &= len(sends) == 1 and sends[0]['args'] == {'to': 'Alex', 'text': report}
            elif label == 'topic_followup':
                passed &= set(names) == {'web_search'} and any(
                    term in answer.lower() for term in ['trading hours', 'insufficient', 'unable', 'cannot', 'unreliable'])
                passed &= all(c['args']['query'] == decision.direct_calls[0][1]['query'] for c in observations)
                passed &= not any(word in answer.lower() for word in ['normal ranges', 'no significant'])
            else:
                passed &= bool(names) and names[0] in {'find_files', 'list_dir'}
                passed &= any(c['name'] == 'organize_files' and c['args'].get('confirm')
                              for c in observations)
                passed &= 'missing' not in answer.lower()
                passed &= not any(c['args'].get('source', '').startswith('/Users/') for c in observations)
                passed &= not remaining
            result = {'case': label, 'model': role_to_model('agent'), 'passed': bool(passed),
                      'seconds': round(time.monotonic() - start, 2),
                      'calls': list(observations), 'answer': answer}
            results.append(result)
            print(json.dumps(result), flush=True)
    finally:
        REGISTRY.clear()
        REGISTRY.update(original)
        await client._client.aclose()
    return 0 if all(r['passed'] for r in results) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    statuses = [asyncio.run(main()) for _ in range(args.repeat)]
    raise SystemExit(max(statuses))
