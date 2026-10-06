"""Bounded pure-helper diagnosis; fixture guard precedes all service imports."""
import json
from dataclasses import asdict,is_dataclass
from pathlib import Path
import sys
import tempfile

ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project')
OUT=Path('/private/tmp/wisp-router-27490ae-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
state=Path(tempfile.mkdtemp(prefix='wisp-27490ae-supplement-')).resolve()
e.install_guard(state,OUT)
from service.router import router
from service.workflows.compiler import outbound_verb,compile_new
from service.tools import imessage_tools as M

classification=[]
for identity,prompt in [
    ('coordinated-source-noun','Read email from Selene and email from Dorian for yesterday'),
    ('explicit-second-read','Read email from Selene and read email from Dorian for yesterday'),
    ('genuine-compound-delivery','Read email from Selene and email Mom the summary'),
    ('genuine-standalone-delivery','Email Mom the summary'),
    ('negated-compound-delivery','Read email from Selene and do not email Mom the summary'),
    ('quoted-delivery-description','Explain the phrase "email Mom the summary"'),
]:
    request=router._classify_web_request(prompt,None)
    workflow=compile_new(prompt)
    classification.append({'id':identity,'prompt':prompt,
        'delivery':None if request.delivery is None else {
            'text':request.delivery.text,'channel':request.delivery.channel,
            'recipient':request.delivery.recipient,'target_missing':request.delivery.target_missing},
        'authorized_effects':sorted(request.authorized_effects),
        'outbound_verb':outbound_verb(prompt),'workflow_compiled':workflow is not None,
        'workflow':asdict(workflow) if is_dataclass(workflow) else repr(workflow)})

redaction=[]
for now in (1791234560.0,1790000000.0):
    source=[(now-2,'Alex','Alex: Please review the report by Friday. Verification code is 1234.'),
        (now-1,'Alex','Alex: Please review the proposal by Friday. Verification code is 5678.')]
    rows=M.filter_summary_message_rows(source)
    secrets=('1234','5678','report','proposal')
    bodies=[r[2] for r in rows]
    redaction.append({'fixed_now':now,'rows':[list(r) for r in rows],
        'two_source_occurrences':len(rows)==2,'redacted_bodies_equal':bodies[0]==bodies[1],
        'source_timestamps_retained':[r[0] for r in rows]==[r[0] for r in source],
        'bodies_and_context_secret_free':all(secret not in str([(r[1],r[2]) for r in rows]) for secret in secrets),
        'whole_rows_secret_free':all(secret not in str(rows) for secret in secrets),
        'second_pass_retains_two':len(M.filter_summary_message_rows(rows))==2})

data={'exact_sha':'27490ae5055502423f187e84ca52d0064b794396',
    'classification':classification,'redaction':redaction,
    'mode':'pure helpers under installed fixture guard; fixed synthetic timestamps',
    'heldout_payload_opened':False,'actual_model_calls':False,'native_or_outbound_execution':False}
(OUT/'supplemental-results.json').write_text(json.dumps(data,indent=2)+'\n')
print(json.dumps(data,indent=2))
