import sys,json,asyncio,tempfile,traceback,itertools
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-98916cf-gates-20261005/auditor')
sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-98916cf-public-')).resolve();e.install_guard(state,OUT)
from service.router.web_request import classify
from service.router.router import route,_LING_WEB_MODEL
cases=[]
def add(prompt,query=None,kind='read',prior=None,address=None,effect='send_email',actual=False):cases.append(dict(id='public-'+str(len(cases)),prompt=prompt,query=query or prompt,kind=kind,prior=prior,address=address,effect=effect,actual=actual))
for p in ['update me on the situation in Taiwan','give me an update on the situation in Iran','update me on the situation in Zorvia','what changed today in Taiwan','latest developments in Python','search the web for communities without internet access','search the web for how to cancel sending messages to 415-555-1212']:
 add(p,actual=True)
for suffix in ['; cancel the web search','; cancel that search',', but do not search the web','; stop browsing','; avoid browsing']:
 add('look up the latest Python release online'+suffix,kind='cancel',actual=True)
source='search the web for the Lydoran treaty'
for sep,verb in itertools.product([' and ','; ','. ',' & ','; afterwards ', ', and afterwards '],['email','send','forward']):
 add(source+sep+verb+' it to audit.person@example.invalid',query=source,kind='delivery',address='audit.person@example.invalid',actual=sep==' and ')
for p in ['email it to Mom','forward it to Mom by email','email the result to Mom afterwards']:
 add(p,query=source,kind='delivery',prior=source,actual=True)
for p,q,ef in [('email Mom today’s headlines','today’s headlines','send_email'),('text Mom an update on Iran today','an update on Iran today','send_message'),('send Mom what happened in Iran today by text','what happened in Iran today','send_message')]:add(p,query=q,kind='delivery',effect=ef,actual=True)
for suffix in [' without sending it',"; don't send it",'; cancel sending it','; do not deliver it']:
 add(source+' and email it to Mom'+suffix,query=source,kind='revocation',actual=True)
rows=[]
async def main():
 for c in cases:
  row={'case':c}
  try:
   w=classify(c['prompt'],last_user=c['prior']);row['web']=asdict(w)
   with patch('service.router.router.role_to_model',return_value='audit-test'):
    d=await route(c['prompt'],last_user=c['prior'])
   row['route']=asdict(d);kind=c['kind']
   if kind=='read':
    row['ok']=d.model==_LING_WEB_MODEL and d.direct_calls==[('web_search',{'query':c['query']})] and d.tool_subset==['web_search']
   elif kind=='cancel':
    row['ok']=d.model==_LING_WEB_MODEL and not d.needs_tools and d.direct_calls==[] and {'web_search','web_fetch','http_request','run_shell'}<=d.forbidden_tools
   elif kind=='delivery':
    expected=['web_search']+([] if c['address'] else ['lookup_contact'])+[c['effect']]
    row['ok']=d.tool_subset==expected and d.tool_argument_bindings.get('web_search')=={'query':c['query']} and not d.direct_calls
    if c['address']:row['ok']=row['ok'] and d.tool_argument_bindings.get(c['effect'])=={'to':c['address']}
   else:
    row['ok']=w.delivery_cancelled and w.delivery is None and not (set(d.tool_subset or []) & {'send_email','send_message','draft_email','draft_message','lookup_contact'}) and 'send_email' in d.forbidden_tools
   if c['actual']:
    case={'id':c['id'],'prompt':c['prompt'],'expected':{'calls':[],'sources':[]},'fixture_answer':'Synthetic public answer.','context':[]}
    if c['prior']:case['context']=[{'role':'user','content':c['prior']},{'role':'assistant','content':'Synthetic prior public result.','tool_digest':'web_search'}]
    r=await e.run_case(case,state,candidate=True);row['run']=r;calls=r['executed_calls']
    if kind=='read':row['ok']=row['ok'] and len(calls)==1 and calls[0]['name']=='web_search' and calls[0]['args']=={'query':c['query']}
    elif kind=='cancel':row['ok']=row['ok'] and calls==[] and not set(r['offered_tools'] or [])&{'web_search','web_fetch','run_shell','http_request'}
    else:row['ok']=row['ok'] and not any(x['name'] in e.EFFECTS for x in calls)
    row['ok']=row['ok'] and not any(x['type']=='error' for x in r['events'])
  except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
  rows.append(row);print(json.dumps({'id':c['id'],'ok':row['ok'],'prompt':c['prompt'],'error':row.get('error')}),flush=True)
 (OUT/'public-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
asyncio.run(main())
