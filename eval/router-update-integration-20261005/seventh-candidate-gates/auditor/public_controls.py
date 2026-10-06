import sys,json,asyncio,tempfile,traceback,itertools
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project');OUT=Path('/private/tmp/wisp-router-8dfd5c9-gates-20261005/auditor');sys.path.insert(0,str(ROOT))
from scripts import eval_router_update as e
HEAD=e.git_revision();state=Path(tempfile.mkdtemp(prefix='wisp-8dfd5c9-public-')).resolve();e.install_guard(state,OUT)
from service.router.web_request import classify
from service.router.router import route,_LING_WEB_MODEL
cases=[]
def add(prompt,kind='read',query=None,prior=None,address=None,effect='send_email',actual=False):cases.append(dict(id='public-'+str(len(cases)),prompt=prompt,kind=kind,query=query or prompt,prior=prior,address=address,effect=effect,actual=actual))
for p in ['update me on the situation in Taiwan','update me on the situation in Iran','give me an update on the situation in Sudan','what changed today in Taiwan','search the web for cities without internet access','search the web for how to cancel sending messages to 415-555-1212']:
 add(p,actual=True)
for suffix in ['; cancel the web search','; cancel that search',', but do not search the web','; stop browsing','; don’t browse']:
 add('look up the latest Python release online'+suffix,'cancel',actual=True)
source='search the web for the Neravian accord'
for separator,verb in itertools.product([' and ','; ','. ',' & ','; afterwards ', ', and afterwards '],['email','send','forward']):
 add(source+separator+verb+' it to audit.route@example.invalid','delivery',query=source,address='audit.route@example.invalid',actual=separator==' and ')
for p in ['email it to Mom','forward it to Mom by email','email the result to Mom afterwards']:add(p,'delivery',query=source,prior=source,actual=True)
for p,q,ef in [('email Mom today’s headlines','today’s headlines','send_email'),('text Mom an update on Iran today','an update on Iran today','send_message'),('send Mom what happened in Iran today by text','what happened in Iran today','send_message')]:add(p,'delivery',query=q,effect=ef,actual=True)
for suffix in [' without sending it',"; don't send it",'; cancel sending it','; do not deliver it']:
 add(source+' and email it to Mom'+suffix,'revocation',query=source,actual=True)
rows=[]
async def run():
 for c in cases:
  row={'case':c}
  try:
   w=classify(c['prompt'],last_user=c['prior']);row['web']=asdict(w)
   with patch('service.router.router.role_to_model',return_value='audit-fake'):
    d=await route(c['prompt'],last_user=c['prior'])
   row['route']=asdict(d)
   if c['kind']=='read':row['ok']=d.model==_LING_WEB_MODEL and d.direct_calls==[('web_search',{'query':c['query']})] and d.tool_subset==['web_search']
   elif c['kind']=='cancel':row['ok']=d.model==_LING_WEB_MODEL and not d.needs_tools and not d.direct_calls and {'web_search','web_fetch','http_request','run_shell'}<=d.forbidden_tools
   elif c['kind']=='delivery':
    expected=['web_search']+([] if c['address'] else ['lookup_contact'])+[c['effect']]
    row['ok']=d.tool_subset==expected and d.tool_argument_bindings.get('web_search')=={'query':c['query']} and not d.direct_calls
    if c['address']:row['ok']=row['ok'] and d.tool_argument_bindings.get(c['effect'])=={'to':c['address']}
   else:row['ok']=w.delivery_cancelled and w.delivery is None and not(set(d.tool_subset or [])&{'send_email','send_message','draft_email','draft_message','lookup_contact'}) and 'send_email' in d.forbidden_tools
   if c['actual']:
    case={'id':c['id'],'prompt':c['prompt'],'expected':{'calls':[],'sources':[]},'fixture_answer':'Synthetic public result.','context':[]}
    if c['prior']:case['context']=[dict(role='user',content=c['prior']),dict(role='assistant',content='Synthetic prior result.',tool_digest='web_search')]
    result=await e.run_case(case,state,candidate=True);row['run']=result;calls=result['executed_calls']
    if c['kind']=='read':row['ok']=row['ok'] and len(calls)==1 and calls[0]['name']=='web_search' and calls[0]['args']=={'query':c['query']}
    elif c['kind']=='cancel':row['ok']=row['ok'] and not calls and not(set(result['offered_tools'] or [])&{'web_search','web_fetch','run_shell','http_request'})
    else:row['ok']=row['ok'] and not any(x['name'] in e.EFFECTS for x in calls)
    row['ok']=row['ok'] and not any(x['type']=='error' for x in result['events'])
  except Exception as exc:row.update(ok=False,error=repr(exc),traceback=traceback.format_exc())
  rows.append(row);print(json.dumps({'id':c['id'],'ok':row['ok'],'error':row.get('error')}),flush=True)
 (OUT/'public-results.json').write_text(json.dumps({'head':HEAD,'cases':len(rows),'passed':sum(x['ok'] for x in rows),'rows':rows},indent=2,default=str))
asyncio.run(run())
