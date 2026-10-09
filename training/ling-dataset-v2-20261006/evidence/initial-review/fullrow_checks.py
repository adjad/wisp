import json,collections,hashlib,datetime,re,copy
from pathlib import Path
R=Path('/private/tmp/ling-dataset-v2-review-input-v1'); O=Path('/private/tmp/ling-dataset-v2-review')
def jl(name):return [json.loads(x) for x in (R/name).read_text().splitlines()]
schema=json.loads((R/'intent.schema.v1.json').read_text()); system=(R/'router-system.txt').read_text()
allowed={('calendar','overview'):{'time','query','account'},('calendar','records'):{'time','query','account'},('calendar','free_time'):{'time','minutes'},('reminders','overview'):{'query','scope'},('reminders','records'):{'query','scope'},('email','overview'):{'time','account','unread','count'},('email','records'):{'time','account','unread','count','query'},('messages','overview'):{'time','conversation','count'},('messages','records'):{'time','query','count'},('notes','overview'):{'time','query','count'},('notes','records'):{'time','query','count'}}
def shape(x,s):
 types={'object':dict,'array':list,'string':str,'integer':int,'boolean':bool}
 if 'type' in s: assert type(x)==types[s['type']]
 if 'enum' in s: assert any(type(x)==type(v) and x==v for v in s['enum'])
 if type(x)==dict:
  assert set(s.get('required',[]))<=set(x)
  if s.get('additionalProperties') is False:assert set(x)<=set(s['properties'])
  for k,v in x.items():
   if k in s.get('properties',{}):shape(v,s['properties'][k])
 if type(x)==list:
  assert len(x)<=s.get('maxItems',999999)
  if s.get('uniqueItems'): assert len({json.dumps(v,sort_keys=True) for v in x})==len(x)
  for v in x:shape(v,s.get('items',{}))
 if type(x)==str:
  assert s.get('minLength',0)<=len(x)<=s.get('maxLength',999999)
  if 'pattern' in s:assert re.search(s['pattern'],x)
 if type(x)==int:assert s.get('minimum',-999999)<=x<=s.get('maximum',999999)
def out(s):return dict(version=1,kind=s['kind'],sources=[dict(domain=q['domain'],operation=q['operation'],**q['filters']) for q in s['requests']],excluded_sources=s['excluded'],unsupported_constraints=s['unsupported'])
def resolve(x,path):
 for key in re.findall(r'[A-Za-z_]+|\d+',path):x=x[int(key)] if key.isdigit() else x[key]
 return x
def normalize(row,meta):
 text='\n'.join(m['content'] for m in row['messages'] if m['role']=='user').casefold()
 strings=[]
 def collect(x):
  if type(x)==dict:
   for v in x.values():collect(v)
  elif type(x)==list:
   for v in x:collect(v)
  elif type(x)==str and len(x)>2:strings.append(x)
 collect(meta['synthetic_slots'])
 for s in sorted(set(strings),key=len,reverse=True):
  if s.casefold() not in {'today','tomorrow','yesterday','this week','next week','last week','this month','next month','last month','calendar','email','messages','notes','reminders','overview','records','true','false','all','upcoming','overdue'}:text=text.replace(s.casefold(),' SLOT ')
 text=re.sub(r'\d+',' NUM ',text)
 return ' '.join(re.findall(r'\w+',text))
stats={}; splitsets={}; notes=[]; bad=[]; n_history=0; n_route=0; n_ground=0
for split in ['train','dev']:
 rows=jl(split+'.jsonl'); metas=jl(split+'.provenance.jsonl'); md={m['id']:m for m in metas}
 assert len(rows)==len(metas)==len(md)
 fam=set();sc=set();prompts=set();normal=set();templates=set();groups=collections.Counter();statuses=collections.Counter()
 for row in rows:
  m=md[row['id']]; ms=row['messages'];ans=ms[-1]['content'];users='\n'.join(x['content'] for x in ms if x['role']=='user')
  try:
   assert set(row)=={'id','messages'} and ms[0]['role']=='system' and ms[-2]['role']=='user' and ms[-1]['role']=='assistant'
   assert ms[-1]['training'] is True and all(x.get('training') is False for x in ms[:-1] if x['role']=='assistant')
   assert all('training' not in x for x in ms if x['role']!='assistant')
   if m['task']=='routing':
    n_route+=1; assert ms[0]['content']==system
    target=json.loads(ans);shape(target,schema);assert target==out(m['semantic_spec'])
    assert target['kind']=='read' or not target['sources']
    assert not set(target['excluded_sources'])&{x['domain'] for x in target['sources']}
    for src in target['sources']:
     assert set(src)-{'domain','operation'}<=allowed[(src['domain'],src['operation'])]
     for field in ['query','account','conversation']:
      if field in src:assert src[field] in users
     if 'time' in src:
      t=src['time'];assert set(t) in [{'named'},{'date'},{'month'},{'start','end'},{'last_n_days'},{'rolling_days'}]
      for k in ['date','start','end']:
       if k in t:datetime.date.fromisoformat(t[k])
      if 'month' in t:datetime.date.fromisoformat(t['month']+'-01')
      if 'start' in t:assert t['start']<=t['end']
    assert all(x in users for x in target['unsupported_constraints'])
    for ev in m['evidence']:
     msg=ms[ev['message_index']];assert msg['role']=='user' and ev['span'] in msg['content']
     if not ev['field'].startswith('removed:'):assert resolve(target,ev['field'])==ev['value']
    if 'history_spec' in m:
     n_history+=1; h=m['history_spec'];state=copy.deepcopy(h['initial'])
     for e in h['edits']:
      if e['op']=='switch':state=dict(kind='read',requests=[dict(domain=e['domain'],operation=e['operation'],filters=copy.deepcopy(e['filters']))],excluded=[],unsupported=[])
      else:
       q=next(q for q in state['requests'] if q['domain']==e['domain'])
       if e['op']=='replace':q['filters'].update(e['fields'])
       elif e['op']=='remove':
        for field in e['fields']:q['filters'].pop(field)
     assert state==m['semantic_spec']
   else:
    n_ground+=1; fixture=json.loads(ms[-2]['content'].split('Synthetic source results:\n')[1]);assert fixture==m['fixture'] and fixture['synthetic'] is True
    src={x['source']:x for x in fixture['results']};rub=m['rubric'];assert {k:v['status'] for k,v in src.items()}==rub['source_statuses']
    for result in src.values():
     assert len(result['items'])==result['coverage']['returned_count']
     assert result['coverage']['complete']==(result['status']=='ok')
     statuses[result['status']]+=1
    for f in rub['required_facts']:
     item=next(x for x in src[f['source']]['items'] if x['id']==f['item_id']);assert item[f['field']]==f['value']
    assert all(str(x) in ans for x in rub['required_phrases'])
    assert all(str(x).casefold() not in ans.casefold() for x in rub['forbidden_phrases'])
    assert not re.search(r'(?:^|\n)\s*(I|We) (sent|deleted|saved|created)\b',ans)
   fam.add(m['family_id']);sc.add(m['scenario_id']);templates.add(m['template_id']);prompts.add(users);normal.add(normalize(row,m));groups[m['primary_group']]+=1
  except Exception as e:bad.append({'id':row['id'],'error':repr(e)})
 assert len(prompts)==len(rows)
 splitsets[split]={'families':fam,'scenarios':sc,'prompts':prompts,'normalized':normal,'ids':{r['id'] for r in rows}}
 stats[split]=dict(rows=len(rows),families=len(fam),expanded_template_ids=len(templates),groups=dict(groups),source_statuses=dict(statuses))
for key in splitsets['train']:assert not splitsets['train'][key]&splitsets['dev'][key],key
pilot=jl('pilot-train.jsonl');train={r['id']:r for r in jl('train.jsonl')};assert len(pilot)==2000 and len({r['id'] for r in pilot})==2000 and all(train[r['id']]==r for r in pilot)
result=dict(status='PASS_INDEPENDENT_STRUCTURAL_AND_SIDECAR_CHECKS_ONLY',bad=bad,stats=stats,routing_rows=n_route,grounded_rows=n_ground,history_replays=n_history,strict_source_presence_check_separately_fails=477,semantic_review_required=True)
assert not bad,bad
(O/'fullrow-checks.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n');print(json.dumps(result,sort_keys=True))
