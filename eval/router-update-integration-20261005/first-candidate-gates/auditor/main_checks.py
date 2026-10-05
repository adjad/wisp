"""Fresh synthetic actual-orchestration checks; no corpus or real interfaces."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
sys.path.insert(0, str(ROOT))
from scripts import eval_router_update as evaluation
output = Path('/private/tmp/wisp-router-3f86187-gates-20261005/auditor')
state = Path(tempfile.mkdtemp(prefix='wisp-audit-main-synthetic-')).resolve()
evaluation.install_guard(state, output)

def supplied(domain, operation='records', **kwargs):
    return {'version': 1, 'kind': 'read', 'sources': [
        {'domain': domain, 'operation': operation, **kwargs}],
        'excluded_sources': [], 'unsupported_constraints': []}

cases = [
    ('tomorrow-shortcut-calendar-scope', 'what is up tomorrow?', supplied('calendar', time={'named':'tomorrow'})),
    ('explicit-date-omitted', 'Read email for 2026-10-01', supplied('email')),
    ('explicit-month-omitted', 'Read email for October 2026', supplied('email')),
    ('reminder-scope-omitted', 'Read overdue reminders', supplied('reminders')),
    ('duration-omitted', 'Find a 90 minute free slot on my calendar tomorrow',
        supplied('calendar', 'free_time', time={'named':'tomorrow'})),
    ('unquoted-query-truncated', 'Find notes about audit blueprints', supplied('notes', query='audit')),
]
rows = []
for identity, prompt, intent in cases:
    case = {'id': identity, 'prompt': prompt, 'clock': '2026-10-05T12:00:00-07:00',
        'intent_response': intent, 'fixture_answer': 'Synthetic fixture result.'}
    result = asyncio.run(evaluation.run_case(case, state, candidate=True))
    rows.append({'id': identity, 'prompt': prompt, 'supplied_intent': intent, 'run': result})
data = {'exact_sha': '3f86187ccfca1a20423506c883acecc6fec59ae1',
    'mode': 'actual main.agent; every registered tool replaced by synthetic fixture',
    'heldout_read': False, 'actual_model_calls': False, 'native_or_outbound_calls': False,
    'rows': rows}
(output / 'main-results.json').write_text(json.dumps(data, indent=2, default=str)+'\n')
for row in rows:
    print(json.dumps({'id':row['id'], 'calls':row['run']['executed_calls'],
        'routed':[{'source':e.get('source'), 'intent_disposition':e.get('intent_disposition'),
            'direct_calls':e.get('direct_calls')} for e in row['run']['events'] if e.get('type') == 'routed'],
        'errors':[e for e in row['run']['events'] if e.get('type') == 'error']}))
