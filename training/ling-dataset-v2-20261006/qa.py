"""Independent CPU structural, provenance and reference checks; no model imports."""
from __future__ import annotations
import collections,copy,datetime,hashlib,json,re
from pathlib import Path
ROOT=Path(__file__).resolve().parent
COUNTS={'train':{'time':900,'filters':650,'simple':200,'followup':800,'multisource':450,'boundary':400,'grounded':600},'dev':{'time':90,'filters':65,'simple':20,'followup':80,'multisource':45,'boundary':40,'grounded':60}}
ALLOWED={('calendar','overview'):{'time','query','account'},('calendar','records'):{'time','query','account'},('calendar','free_time'):{'time','minutes'},('reminders','overview'):{'scope','query'},('reminders','records'):{'scope','query'},('email','overview'):{'time','account','unread','count'},('email','records'):{'time','account','unread','count','query'},('messages','overview'):{'time','conversation','count'},('messages','records'):{'time','query','count'},('notes','overview'):{'time','query','count'},('notes','records'):{'time','query','count'}}
def require(ok,msg):
 if not ok:raise ValueError(msg)
def canonical(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read_jsonl(p):return [json.loads(x) for x in Path(p).read_text().splitlines() if x]
def schema_errors(value,schema,path='$'):
 errors=[];t=schema.get('type');kind={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),'integer':type(value)is int,'boolean':type(value)is bool}
 if t and not kind.get(t,False):return [path+':type']
 if 'enum' in schema and not any(type(value)==type(x) and value==x for x in schema['enum']):errors.append(path+':enum')
 if isinstance(value,dict):
  errors += [path+'.'+k+':missing' for k in schema.get('required',[]) if k not in value]
  props=schema.get('properties',{})
  if schema.get('additionalProperties') is False:errors += [path+'.'+k+':extra' for k in value if k not in props]
  for k,v in value.items():
   if k in props:errors+=schema_errors(v,props[k],path+'.'+k)
 if isinstance(value,list):
  if len(value)>schema.get('maxItems',float('inf')):errors.append(path+':maxItems')
  if schema.get('uniqueItems') and len({canonical(x) for x in value})!=len(value):errors.append(path+':duplicates')
  for i,v in enumerate(value):errors+=schema_errors(v,schema.get('items',{}),path+'['+str(i)+']')
 if isinstance(value,str):
  if not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',float('inf')):errors.append(path+':length')
  if 'pattern' in schema and not re.search(schema['pattern'],value):errors.append(path+':pattern')
 if type(value)is int and not schema.get('minimum',-float('inf'))<=value<=schema.get('maximum',float('inf')):errors.append(path+':range')
 return errors
def resolve(value,path):
 parts=re.findall(r'([A-Za-z_]+)|\[(\d+)\]',path)
 for key,index in parts:value=value[key] if key else value[int(index)]
 return value
def semantic_label(meta):
 s=meta['semantic_spec']
 return {'version':1,'kind':s['kind'],'sources':[dict(domain=r['domain'],operation=r['operation'],**r['filters']) for r in s['requests']],'excluded_sources':s['excluded'],'unsupported_constraints':s['unsupported']}
def route_checks(row,meta,schema):
 answer=json.loads(row['messages'][-1]['content']);require(not schema_errors(answer,schema),'schema:'+str(schema_errors(answer,schema)))
 require(answer==semantic_label(meta),'semantic specification mismatch')
 if meta['primary_group']=='followup':
  history=meta['history_spec'];state=copy.deepcopy(history['initial'])
  for edit in history['edits']:
   op=edit['op']
   if op=='switch':
    state={'kind':'read','requests':[{'domain':edit['domain'],'operation':edit['operation'],'filters':copy.deepcopy(edit['filters'])}],'excluded':[],'unsupported':[]}
   else:
    matches=[r for r in state['requests'] if r['domain']==edit['domain']];require(len(matches)==1,'ambiguous history domain');request=matches[0]
    if op=='replace':request['filters'].update(copy.deepcopy(edit['fields']))
    elif op=='remove':
     for field in edit['fields']:require(field in request['filters'],'removed absent history field');request['filters'].pop(field)
    else:raise ValueError('unknown history edit '+op)
  require(state==meta['semantic_spec'],'history edits retain/reset wrong arguments')
  initial=semantic_label({'semantic_spec':history['initial']})
  for ev in history.get('initial_evidence',[]):
   msg=row['messages'][ev['message_index']];require(msg['role']=='user' and ev['span'] in msg['content'],'initial user evidence');require(resolve(initial,ev['field'])==ev['value'],'initial evidence value')
 require(answer['kind']=='read' or not answer['sources'],'nonread grants source')
 require(len({x['domain'] for x in answer['sources']})==len(answer['sources']),'duplicate source')
 require(not {x['domain'] for x in answer['sources']}&set(answer['excluded_sources']),'excluded source present')
 users='\n'.join(m['content'] for m in row['messages'] if m['role']=='user')
 for src in answer['sources']:
  require(set(src)-{'domain','operation'}<=ALLOWED.get((src['domain'],src['operation']),set()),'unsupported source fields')
  if 'time' in src:
   t=src['time'];require(set(t) in [{'named'},{'date'},{'month'},{'start','end'},{'last_n_days'},{'rolling_days'}],'competing/empty time forms')
   for key in ['date','start','end']:
    if key in t:datetime.date.fromisoformat(t[key])
   if 'month' in t:datetime.date.fromisoformat(t['month']+'-01')
   if 'start' in t:require(t['start']<=t['end'],'reversed range')
  for key in ['query','account','conversation']:
   if key in src:require(src[key] in users,'literal '+key+' absent from user')
 for constraint in answer['unsupported_constraints']:require(constraint in users,'nonliteral unsupported constraint')
 for ev in meta.get('evidence',[]):
  require(isinstance(ev['message_index'],int),'evidence index')
  m=row['messages'][ev['message_index']];require(m['role']=='user' and ev['span'] and ev['span'] in m['content'],'missing user evidence')
  field=ev['field']
  if field.startswith('removed:'):continue
  require(resolve(answer,field)==ev['value'],'evidence differs from target at '+field)
 return answer
SOURCE_LABELS={'calendar':r'\bcalendar\b','reminders':r'\breminders?\b','email':r'\bemails?\b','messages':r'\bmessages?\b','notes':r'\bnotes?\b'}
def source_labels(text):
 return {source for source,pattern in SOURCE_LABELS.items() if re.search(pattern,text,re.IGNORECASE)}
def attribution_checks(answer,sources):
 # Single-source context is unambiguous. Mixed sources need natural visible labels.
 if len(sources)<2:return
 require(set(sources)<=source_labels(answer),'missing mixed-source attribution '+str(sorted(set(sources)-source_labels(answer))))
 # Bounded association check: in a clause naming exactly one source, a distinctive
 # supplied content/time anchor from another source cannot be assigned to it.
 # Clauses naming several sources still need independent reference review.
 anchors=collections.defaultdict(set)
 for domain,source in sources.items():
  for item in source['items']:
   for field,value in item.items():
    if field in {'title','subject','body','text','visible_text','start','due_time'} and isinstance(value,str) and len(value)>=4:
     anchors[value].add(domain)
 for clause in re.split(r'''[;\n]|(?<=[.!?])[”’"']?\s+''',answer):
  labels=source_labels(clause)
  if len(labels)!=1:continue
  label=next(iter(labels))
  for phrase,owners in anchors.items():
   if len(owners)==1 and phrase in clause:
    require(label in owners,'wrong source association for '+repr(phrase))
def grounded_checks(row,meta):
 u=[m['content'] for m in row['messages'] if m['role']=='user'][-1]
 marker='Synthetic source results:\n';require(marker in u,'missing fixture marker')
 fixture=json.loads(u.split(marker,1)[1]);require(fixture==meta['fixture'],'fixture/provenance mismatch');require(fixture['synthetic'] is True,'not synthetic')
 results=fixture['results'];sources={s['source']:s for s in results}
 require(len(sources)==len(results),'ambiguous duplicated fixture source')
 rubric=meta['rubric'];answer=row['messages'][-1]['content']
 require(rubric['source_statuses']=={k:v['status'] for k,v in sources.items()},'source status mismatch')
 for fact in rubric['required_facts']:
  source=sources[fact['source']];items={i['id']:i for i in source['items']};require(items[fact['item_id']][fact['field']]==fact['value'],'required fact not supplied')
 for phrase in rubric.get('required_phrases',[]):require(str(phrase) in answer,'required phrase missing: '+str(phrase))
 for phrase in rubric.get('forbidden_phrases',[]):require(str(phrase).casefold() not in answer.casefold(),'forbidden claim')
 attribution_checks(answer,sources)
 for domain,source in sources.items():
  require(source['status'] in {'ok','empty','denied','timeout','missing','partial','truncated'},'bad fixture status')
  if source['status']!='ok':require(source['status']!='empty' or not source['items'],'empty has items')
 require(not re.search(r'(?:^|\n)\s*(?:I|We) (?:sent|deleted|created|saved)\b',answer),'false first-person effect')
 return fixture
def row_checks(row,meta,schema,system):
 require(set(row)=={'id','messages'},'unexpected model-input field');require(row['id']==meta['id'],'provenance id')
 for key in ['split','task','primary_group','category','family_id','scenario_id','template_id','synthetic_slots']:require(key in meta,'missing provenance '+key)
 ms=row['messages'];require(len(ms)>=3 and ms[0]['role']=='system' and ms[-2]['role']=='user' and ms[-1]['role']=='assistant','message sequence')
 require(all(isinstance(m.get('content'),str) and m['content'] for m in ms),'empty message')
 require(ms[-1].get('training') is True,'final not supervised')
 for m in ms[:-1]:
  require(m['role'] in {'system','user','assistant'},'unexpected role')
  if m['role']=='assistant':require(m.get('training') is False,'earlier assistant supervised')
  else:require('training' not in m,'nonassistant training flag')
 require(sum(len(m['content']) for m in ms)<14000,'oversized character budget (not token measurement)')
 if meta['task']=='routing':require(ms[0]['content']==system,'system drift');route_checks(row,meta,schema)
 else:require(meta['task']=='grounded' and meta['primary_group']=='grounded','unknown task');grounded_checks(row,meta)
def prompt(row):return '\n'.join(m['content'] for m in row['messages'] if m['role']=='user')
def normalized_prompt(row,meta):
 text=prompt(row).casefold()
 # Substitute synthetic values but preserve language and semantic distinctions.
 values=[]
 def collect(v):
  if isinstance(v,dict):
   for x in v.values():collect(x)
  elif isinstance(v,list):
   for x in v:collect(x)
  elif isinstance(v,str) and len(v)>2:values.append(v)
 collect(meta['synthetic_slots'])
 for value in sorted(set(values),key=len,reverse=True):
  if value.casefold() not in {'today','tomorrow','yesterday','this week','next week','last week','this month','next month','last month','calendar','email','messages','notes','reminders','overview','records','true','false','all','upcoming','overdue'}:text=text.replace(value.casefold(),' SLOT ')
 text=re.sub(r'\d+',' NUM ',text)
 return ' '.join(re.findall(r'\w+',text))
def validate(root):
 root=Path(root);schema=json.loads((ROOT/'intent.schema.v1.json').read_text());system=(ROOT/'router-system.txt').read_text();all_ids=set();families={};scenarios={};exact={};norm={};stats={};group_rows={}
 for split in ['train','dev']:
  rows=read_jsonl(root/(split+'.jsonl'));metas=read_jsonl(root/(split+'.provenance.jsonl'));require(len(rows)==len(metas),'row/meta count');md={m['id']:m for m in metas};require(len(md)==len(metas),'duplicate metaid');groups=collections.Counter();tasks=collections.Counter();category=collections.Counter();seen_prompts=set();lengths=[]
  for row in rows:
   rid=row['id'];require(rid not in all_ids,'duplicate id');all_ids.add(rid);meta=md[rid];require(meta['split']==split,'split drift');row_checks(row,meta,schema,system)
   groups[meta['primary_group']]+=1;tasks[meta['task']]+=1;category[meta['category']]+=1
   for field,registry in [('family_id',families),('scenario_id',scenarios)]:
    value=meta[field];require(registry.get(value,split)==split,'crosssplit '+field);registry[value]=split
   p=prompt(row);require(p not in seen_prompts,'duplicate user prompt within split: '+rid);seen_prompts.add(p)
   require(exact.get(p,split)==split,'exact crosssplit prompt');exact[p]=split
   n=normalized_prompt(row,meta);require(norm.get(n,split)==split,'normalized crosssplit prompt: '+rid);norm[n]=split
   lengths.append(sum(len(m['content']) for m in row['messages']))
  require(dict(groups)==COUNTS[split],'wrong groupcounts '+str(groups));group_rows[split]=rows
  stats[split]={'rows':len(rows),'tasks':dict(tasks),'groups':dict(groups),'categories':dict(category),'families':len({m['family_id'] for m in metas}),'scenarios':len({m['scenario_id'] for m in metas}),'expanded_template_ids':len({m['template_id'] for m in metas}),'max_characters':max(lengths),'median_characters':sorted(lengths)[len(lengths)//2]}
 pilot=read_jsonl(root/'pilot-train.jsonl');train_by_id={r['id']:r for r in group_rows['train']};require(len(pilot)==2000 and len({r['id'] for r in pilot})==2000,'pilotcount');require(all(r==train_by_id.get(r['id']) for r in pilot),'pilot not exact train subset');pmd={m['id']:m for m in read_jsonl(root/'train.provenance.jsonl')};pg=collections.Counter(pmd[r['id']]['primary_group'] for r in pilot);require(dict(pg)=={k:v//2 for k,v in COUNTS['train'].items()},'pilotbalance')
 return {'validated_file_sha256':{name:digest(root/name) for name in ['train.jsonl','dev.jsonl','pilot-train.jsonl','train.provenance.jsonl','dev.provenance.jsonl','manifest.json']},'validator_sha256':{name:digest(ROOT/name) for name in ['qa.py','validate.py','intent.schema.v1.json','router-system.txt']},'authored_surface_units':{'arguments':{'train_request_surfaces':141,'dev_request_surfaces':78},'context':{'train_opening_prefixes':20,'dev_opening_prefixes':5,'assistant_context_phrasings':5,'contrast_branches_per_family':2},'grounded':{'train_surface_ids':72,'dev_surface_ids':30,'request_and_answer_surfaces_per_family':3},'note':'Units differ by shard and are not summed as full authored templates.'},'status':'PASS_STATIC_DATA_QA','splits':stats,'pilot':dict(pg),'no_final_read':True,'tokenizer_length_or_actual_axolotl_masking_qualified':False,'semantic_limit':'Oracle/evidence/capability checks plus independent sample review; no universal natural-language entailment proof.'}
