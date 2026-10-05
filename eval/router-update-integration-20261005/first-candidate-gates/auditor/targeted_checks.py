"""Independent fresh synthetic checks; no heldout, model or native execution."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from datetime import datetime

ROOT = Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
sys.path.insert(0, str(ROOT))
from scripts import eval_router_update as evaluation
output = Path('/private/tmp/wisp-router-3f86187-gates-20261005/auditor')
state = Path(tempfile.mkdtemp(prefix='wisp-audit-router-synthetic-')).resolve()
evaluation.install_guard(state, output)
from service.router.intent import validate_intent, compile_intent, InvalidIntent
from service.router.intent import planner
from tests.test_router_intent_core import FakeClient, CONFIG, TARGET

NOW = datetime(2026, 10, 5, 12)
planner.role_target = lambda role: TARGET

def intent(domain, **args):
    return {'version': 1, 'kind': 'read', 'sources': [
        {'domain': domain, 'operation': 'records', **args}],
        'excluded_sources': [], 'unsupported_constraints': []}

cases = [
    ('explicit-date-omitted', 'Read email for 2026-10-01', intent('email')),
    ('explicit-month-omitted', 'Read email for October 2026', intent('email')),
    ('reminder-scope-omitted', 'Read overdue reminders', intent('reminders')),
    ('reminder-scope-changed', 'Read overdue reminders', intent('reminders', scope='all')),
    ('duration-omitted', 'Find a 90 minute free slot on my calendar tomorrow', {
        **intent('calendar', time={'named':'tomorrow'}),
        'sources':[{'domain':'calendar','operation':'free_time','time':{'named':'tomorrow'}}]}),
    ('unquoted-query-truncated', 'Find notes about audit blueprints', intent('notes', query='audit')),
    ('current-filter-regresses-to-prior', 'Actually email from Mira', intent('email', query='Nora')),
]
records = []
for identity, prompt, value in cases:
    context = [{'role':'user','content':'Read email from Nora'}] if identity == 'current-filter-regresses-to-prior' else []
    client = FakeClient(value, value)
    result = asyncio.run(planner.plan_read(prompt, client=client, config=CONFIG,
        context=context, now=NOW))
    records.append({'id':identity, 'prompt':prompt, 'supplied_intent':value,
        'disposition':result.disposition, 'calls':result.calls,
        'response':result.response, 'synthetic_client_calls':client.calls})

# Pure scorer checks: no model, no executor, no data corpus reads.
scorer = []
for count in (True, 1.0):
    case = {'expected':{'calls':[{'name':'view_emails','args':{'count':1}}], 'sources':['email']}}
    run = {'executed_calls':[{'name':'view_emails','args':{'count':count}}],
           'events':[{'type':'done'}], 'answer':'synthetic'}
    scored = evaluation.score(case, run)
    scorer.append({'actual_count':count,'actual_type':type(count).__name__,
                   'metrics':scored['metrics']})
data={'exact_sha':'3f86187ccfca1a20423506c883acecc6fec59ae1',
      'mode':'fresh synthetic; fake status/chat only', 'scope_checks':records,
      'scorer_checks':scorer, 'heldout_read':False, 'actual_model_calls':False,
      'native_or_outbound_calls':False}
(output/'targeted-results.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps(data,indent=2))
