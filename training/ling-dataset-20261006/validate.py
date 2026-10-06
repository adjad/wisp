#!/usr/bin/env python3
"""CPU-only author/reference QA. --public reads train/dev, never final payload."""
import argparse
import copy
import datetime as dt
import difflib
import hashlib
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
import jsonschema

ROOT=Path(__file__).resolve().parent
COUNTS={'train':(1400,600),'dev':(140,60),'final':(210,90)}
ALLOWED={
 ('calendar','overview'):{'query','account','time'},('calendar','records'):{'query','account','time'},('calendar','free_time'):{'query','account','time','minutes'},
 ('reminders','overview'):{'query','scope'},('reminders','records'):{'query','scope'},
 ('email','overview'):{'time','account','unread','count'},('email','records'):{'time','account','unread','count','query'},
 ('messages','overview'):{'time','conversation','count'},('messages','records'):{'time','query','count'},
 ('notes','overview'):{'time','query','count'},('notes','records'):{'time','query','count'}}
DOMAINS={
 'agenda_week':['calendar','reminders'],'agenda_informal':['calendar','reminders'],'agenda_day':['calendar','reminders'],'calendar_day':['calendar'],'calendar_month':['calendar'],'calendar_literal_date':['calendar'],'calendar_range':['calendar'],
 'email_overview':['email'],'email_literal':['email'],'messages_overview':['messages'],'messages_search':['messages'],'notes_records':['notes'],'reminders_scope':['reminders'],'calendar_free':['calendar'],
 'agenda_exclusion':['calendar'],'multi_source':['calendar','email','messages'],'same_email':['email'],'switch_email_calendar':['calendar'],'switch_calendar_messages':['messages'],'calendar_location':['calendar'],
 'email_constraint':['email'],'reminder_time':['reminders'],'messages_unread':['messages'],'same_messages':['messages'],'same_calendar':['calendar'],'agenda_typos':['calendar','reminders']}
RECORDS={'calendar_literal_date','email_literal','messages_search','notes_records','reminders_scope','same_calendar'}


def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p):return [json.loads(l) for l in p.read_text().splitlines() if l]
def require(ok,why):
    if not ok: raise ValueError(why)
def last_user(ex): return [m['content'] for m in ex['messages'] if m['role']=='user'][-1]
def users(ex):return '\n'.join(m['content'] for m in ex['messages'] if m['role']=='user')

def time_check(t):
    require(isinstance(t,dict) and len(t)>0,'empty time')
    require(set(t) in [{'named'},{'date'},{'month'},{'start','end'},{'last_n_days'},{'rolling_days'}],'competing/unpaired time forms')
    for key in ['date','start','end']:
        if key in t: dt.date.fromisoformat(t[key])
    if 'month' in t:dt.date.fromisoformat(t['month']+'-01')
    if 'start' in t:require(t['start']<=t['end'],'reversed range')

def semantic_intent(intent):
    require(intent['kind']=='read' or not intent['sources'],'non-read has sources')
    require(not (set(intent['excluded_sources']) & {s['domain'] for s in intent['sources']}),'excluded source present')
    require(len({s['domain'] for s in intent['sources']})==len(intent['sources']),'duplicate source')
    for src in intent['sources']:
        allowed=ALLOWED.get((src['domain'],src['operation']))
        require(allowed is not None,'unsupported domain/operation')
        require(set(src)-{'domain','operation'}<=allowed,'unsupported domain field')
        if 'time' in src:time_check(src['time'])
        if 'query' in src:require(not src['query'].startswith(('is:unread','from:')),'invented query syntax')


def independent_route_oracle(meta,ex):
    """Category/slot semantics independently reconstruct expected labels, not target text."""
    cat=meta['category'];s=meta['synthetic_slots'];u=last_user(ex)
    action='inline' if cat=='inline_draft' else 'unsupported' if cat in {'effect','mixed_effect','web_unsupported'} else 'none' if cat in {'prohibition','pure_exclusions'} else 'read'
    domains=DOMAINS.get(cat,[])
    if cat in {'unfiltered_read','daily_read'}:domains=[s['read_domain']]
    if cat in {'email_read_false','email_person'}:domains=['email']
    if cat=='messages_person':domains=['messages']
    if cat in {'calendar_rolling','calendar_account'}:domains=['calendar']
    if cat=='ordinary_none':action='none'
    srcs=[]
    for domain in domains:
        v={'domain':domain,'operation':'records' if cat in RECORDS else 'free_time' if cat=='calendar_free' else 'overview'}
        if cat in {'agenda_week','agenda_informal','agenda_exclusion','multi_source','calendar_location','email_constraint','email_overview','calendar_free','messages_overview','same_email','same_messages'} and domain!='reminders':v['time']={'named':s['named']}
        if cat=='email_person':v.update(operation='records',query=s['person'])
        if cat=='messages_person':v['conversation']=s['person']
        if cat=='calendar_account':v.update(time={'named':s['named']},account=s['account'])
        if cat=='email_read_false':v.update(time={'named':s['named']},unread=False)
        if cat=='calendar_rolling':v['time']={'rolling_days':s['rolling_days']}
        if cat=='daily_read':
            if domain=='reminders':v['scope']=s['agenda_day']
            else:v['time']={'named':s['agenda_day']}
        if cat=='agenda_day':
            if domain=='calendar':v['time']={'named':s['agenda_day']}
            else:v['scope']=s['agenda_day']
        if cat=='calendar_day':v['time']={'named':s['day']}
        if cat in {'calendar_month','notes_records'}:v['time']={'month':s['month']}
        if cat in {'calendar_literal_date','messages_search'}:v['time']={'date':s['date']}
        if cat in {'calendar_range','same_calendar'}:v['time']={'start':s['start'],'end':s['end']}
        if cat=='email_literal':v['time']={'last_n_days':s['days']}
        if cat in RECORDS:v['query']=s['literal']
        if cat in {'email_overview','same_email'}:v.update(unread=True,account=s['account'])
        if cat in {'email_overview','same_email','messages_overview','messages_search','notes_records','same_messages'}:v['count']=s['count']
        if cat in {'messages_overview','same_messages','switch_calendar_messages','messages_unread'}:v['conversation']=s['group']
        if cat=='reminders_scope':v['scope']=s['scope']
        if cat=='calendar_free':v['minutes']=s['minutes']
        if cat=='agenda_typos':
            n='today' if 'today' in u else 'tomorrow'
            if domain=='calendar':v['time']={'named':n}
            else:v['scope']=n
        srcs.append(v)
    exclusions=['reminders'] if cat=='agenda_exclusion' else []
    if cat=='ordinary_none' and u=='Define calendar without opening mine.':exclusions=['calendar']
    if cat in {'prohibition','pure_exclusions'}:
        for domain,stem in [('email','email'),('messages','message'),('calendar','calendar'),('reminders','reminder'),('notes','note'),('web','web')]:
            if stem in u.lower():exclusions.append(domain)
    constraints=[]
    if cat in {'agenda_week','agenda_informal'}:constraints=[s['named']]
    elif cat in {'calendar_location','email_constraint'}:constraints=[s['constraint']]
    elif cat=='reminder_time':constraints=[s['named']]
    elif cat=='messages_unread':
        # Extract the literal unsupported filter span from the actual utterance.
        candidates=['unread messages only','unread messages','unread only','unread-only filter','unread portion']
        constraints=[next(c for c in candidates if c in u)]
    return {'version':1,'kind':action,'sources':srcs,'excluded_sources':exclusions,'unsupported_constraints':constraints}


def validate_route(ex,meta,schema):
    label=json.loads(ex['messages'][-1]['content']);jsonschema.validate(label,schema);semantic_intent(label)
    oracle=independent_route_oracle(meta,ex)
    require(label==oracle,'routing label differs from category/slot oracle')
    sem=meta['semantic_spec']
    canonical={'version':1,'kind':sem['action'],'sources':[dict(domain=r['domain'],operation=r['operation'],**r['filters']) for r in sem['requests']],'excluded_sources':sem['excluded'],'unsupported_constraints':sem['unsupported']}
    require(canonical==oracle,'semantic provenance differs from independent oracle')
    text=users(ex)
    for src in label['sources']:
        for k in ['query','account','conversation']:
            if k in src:require(src[k] in text,f'{k} absent from user evidence')
        for k in ['count','minutes']:
            if k in src:require(str(src[k]) in text,f'{k} has no explicit user limit')
        if 'time' in src:
            t=src['time']
            for k in ['date','start','end']:
                if k in t:require(t[k] in text,'explicit date missing')
            if 'month' in t:require(meta['synthetic_slots']['monthword'] in text,'month not explicit')
            if 'named' in t and meta['category']!='agenda_typos':require(t['named'] in text,'named time missing')
    for constraint in label['unsupported_constraints']:require(constraint in last_user(ex),'constraint span not literal')
    if meta['category'].startswith('switch_'):
        require(not any(k in src for src in label['sources'] for k in ['time','query','account','unread','count']),'old-source filter leak')
    if meta['category'] in {'effect','mixed_effect'}:require(label['kind']=='unsupported' and not label['sources'],'effect implied read authorization')


CAVEAT_WORDS={'empty_scoped':['no matching','work account'],'denied':['denied'],'timeout':['timed out'],'partial':['partial','archived folder'],'truncated':['truncated','matching messages'],'missing':['no email result','unavailable'],'proposal_unconfirmed':['unconfirmed'],'overlap':['overlap'],'mixed_timezones':['different time zones'],'no_deadline':['no deadline'],'draft_notes':['draft'],'draft_sent_distinction':['draft:','sent:']}

def validate_overview(ex,meta):
    fixture=json.loads(last_user(ex).split('Synthetic source results:\n',1)[1])
    require(fixture['synthetic'] is True,'non-synthetic fixture')
    require(fixture['results']==meta['fixture'],'fixture/provenance mismatch')
    answer=ex['messages'][-1]['content']; low=answer.lower();rubric=meta['rubric']
    sources={r['source']:r for r in fixture['results']}
    require(rubric['source_statuses']=={d:r['status'] for d,r in sources.items()},'status oracle mismatch')
    for fact in rubric['required_facts']:
        src=sources[fact['source']];items={it['id']:it for it in src['items']}
        require(fact['item_id'] in items and items[fact['item_id']][fact['field']]==fact['value'],'fact absent from fixture')
        if not (fact['source']=='messages' and fact['field']=='body'):require(str(fact['value']) in answer,'required supplied fact omitted')
    for domain,source in sources.items():
        require(domain.capitalize()+':' in answer or domain in low,'source attribution omitted')
        for item in source['items']:
            if domain=='messages':
                b=item['body'];speaker=item['speaker']
                if b.startswith('Could we move the '):expected=speaker+' proposed moving the '+b.removeprefix('Could we move the ').split('?')[0]+'.'
                elif b=='I have not agreed to a new time.':expected=speaker+' has not agreed to a new time.'
                elif b.startswith('I can '):expected=speaker+' offered to '+b.removeprefix('I can ')
                elif b.startswith('I will '):expected=speaker+' will '+b.removeprefix('I will ')
                elif b.startswith('I need '):expected=speaker+' needs '+b.removeprefix('I need ')
                else:raise ValueError('unrecognized assertion fixture')
                require(expected in answer,'speaker/assertion association mismatch')
            if domain=='email':require(item['body'] in answer,'email body attribution mismatch')
            if domain=='calendar':
                require(f'{item["title"]} at {item["start"]} ({item["timezone"]})' in answer,'calendar fact association mismatch')
                require(item['date'] in answer,'calendar date omitted')
            if 'delivery_state' in item:require(item['delivery_state'].capitalize()+': '+item['subject'] in answer,'draft/sent state mismatch')
            if 'requested_by' in item:require(item['requested_by'] in answer,'explicit deadline omitted')
            if 'due_date' in item and item['due_date']:require(item['due_date'] in answer,'reminder due date omitted')
        if source['status'] in ['denied','timeout','missing']:require(not source['items'],'unavailable source contains data')
        if source['status'] in ['partial','truncated']:require(source['complete'] is False,'partial source marked complete')
        if source['status']=='truncated':require(f'{source["returned_count"]} of {source["total_matching"]}' in answer,'truncation cardinality wrong')
    require(rubric['required_coverage']=={d:r['coverage'] for d,r in sources.items()},'coverage oracle mismatch')
    for domain,source in sources.items():
        if domain in {'email','messages'} and source['items']:
            for field in ['account','date','conversation']:
                if field in source['coverage']:require(source['coverage'][field] in answer,'source scope omitted')
            if source['coverage'].get('unread'):require('unread' in low,'unread coverage omitted')
    for caveat in rubric['required_caveats']:
        require(all(w in low for w in CAVEAT_WORDS[caveat]),'required caveat omitted')
    require(not re.search(r'\b(I sent|I posted|I created|I deleted|I saved|I forwarded)\b',answer,re.I),'claimed outbound effect')
    # Reference QA permits exact supplied facts and authored connecting prose only.
    allowed=set()
    for source in sources.values():
        for item in source['items']:
            for value in item.values():allowed.update(re.findall(r'\d+(?:[:.-]\d+)*',str(value)))
        for value in source['coverage'].values():allowed.update(re.findall(r'\d+(?:[:.-]\d+)*',str(value)))
        allowed.add(str(len(source['items'])))
        for field in ['returned_count','total_matching']:
            if field in source:allowed.add(str(source[field]))
    require(set(re.findall(r'\d+(?:[:.-]\d+)*',answer))<=allowed,'invented numeric fact')


def normalize(text,meta):
    # Entity/date normalization catches only changing synthetic slots across splits.
    for value in sorted([v for v in meta['synthetic_slots'].values() if isinstance(v,str)],key=len,reverse=True):
        text=re.sub(re.escape(value),'<slot>',text,flags=re.I)
    text=re.sub(r'\b\d{4}-\d{2}(?:-\d{2})?\b','<date>',text)
    text=re.sub(r'\b\d+\b','<number>',text)
    return ' '.join(re.findall(r'[a-z]+|<[^>]+>',text.lower()))

def dedup(examples,metadata):
    exact={};normalized={};by_cat={};surface_counts={};comparisons=0;maximum=0.0
    for split,es in examples.items():
        for ex,meta in zip(es,metadata[split]):
            text=users(ex) if meta['task']=='routing' else last_user(ex).split('\n\nSynthetic source results:')[0]
            if meta['task']=='overview':
                # Prompt surface plus composition identity; within-split slot variation is disclosed.
                text+=' '+json.dumps(meta['fixture'],sort_keys=True)
            exact_key=text
            require(exact_key not in exact, f'exact duplicate {ex["id"]} / {exact.get(exact_key)}')
            exact[exact_key]=ex['id']
            surface=users(ex) if meta['task']=='routing' else last_user(ex).split('\n\nSynthetic source results:')[0]
            norm=normalize(surface,meta)
            if norm in normalized:require(normalized[norm][0]==split,f'normalized cross-split duplicate {ex["id"]} / {normalized[norm][1]}')
            normalized[norm]=(split,ex['id'])
            by_cat.setdefault(meta['task'],{}).setdefault(split,set()).add(norm)
            surface_counts.setdefault(split+'/'+meta['task'],set()).add(norm)
    # Compare all distinct normalized forms across every behavioral category in a task.
    # This conservative lexical gate is a contamination screen, not proof of independence.
    for group in by_cat.values():
        splits=list(group)
        for a_i,a in enumerate(splits):
            for b in splits[a_i+1:]:
                for left in group[a]:
                    for right in group[b]:
                        comparisons+=1
                        ratio=difflib.SequenceMatcher(None,left,right,autojunk=False).ratio();maximum=max(maximum,ratio)
                        require(ratio<0.88,f'normalized near-duplicate ratio={ratio:.3f}: {left!r} / {right!r}')
    return {'exact_duplicate_count':0,'cross_split_normalized_duplicate_count':0,'normalized_near_duplicate_threshold':0.88,'normalized_cross_split_pairs_compared':comparisons,'maximum_cross_split_similarity':round(maximum,6),'unique_normalized_surfaces':len(normalized),'unique_normalized_surfaces_by_split_task':{k:len(v) for k,v in surface_counts.items()},'limitations':'Lexical/task screen only, not model embedding or human independence proof; shared behavioral labels and within-split templates are intentional.'}


def negative_controls(examples,metadata,schema):
    controls=[]
    def route_case(cat):
        for ex,meta in zip(examples['train'],metadata['train']):
            if meta['category']==cat:return copy.deepcopy(ex),copy.deepcopy(meta)
        raise KeyError(cat)
    def reject(name,ex,meta,mutate,overview=False):
        if overview:mutate(ex)
        else:
            obj=json.loads(ex['messages'][-1]['content']);mutate(obj);ex['messages'][-1]['content']=json.dumps(obj)
        try:
            (validate_overview if overview else lambda e,m:validate_route(e,m,schema))(ex,meta)
        except (ValueError,jsonschema.ValidationError,StopIteration):controls.append({'control':name,'result':'rejected'});return
        raise ValueError('negative control accepted: '+name)
    for name,cat,fn in [
      ('dropped_agenda_period_constraint','agenda_week',lambda x:x.update(unsupported_constraints=[])),
      ('invented_agenda_reminder_time','agenda_week',lambda x:x['sources'][1].update(time={'named':'next week'})),
      ('invented_unfiltered_defaults','unfiltered_read',lambda x:x['sources'][0].update(count=10)),
      ('read_email_false_inverted','email_read_false',lambda x:x['sources'][0].update(unread=True)),
      ('ordinary_chat_source_access','ordinary_none',lambda x:x['sources'].append({'domain':'notes','operation':'overview'})),
      ('missing_requested_source','multi_source',lambda x:x['sources'].pop()),
      ('literal_plus_address_corruption','email_literal',lambda x:x['sources'][0].update(query='from:someone')),
      ('wrong_date','calendar_literal_date',lambda x:x['sources'][0].update(time={'date':'2027-02-30'})),
      ('competing_time_forms','calendar_day',lambda x:x['sources'][0]['time'].update(date='2027-01-01')),
      ('unpaired_range','calendar_range',lambda x:x['sources'][0]['time'].pop('end')),
      ('messages_unread_field','messages_overview',lambda x:x['sources'][0].update(unread=True)),
      ('reminders_arbitrary_time','reminders_scope',lambda x:x['sources'][0].update(time={'named':'next week'})),
      ('email_overview_query','email_overview',lambda x:x['sources'][0].update(query='invented')),
      ('non_calendar_free_time','messages_overview',lambda x:x['sources'][0].update(operation='free_time')),
      ('excluded_source_present','agenda_exclusion',lambda x:x['sources'].append({'domain':'reminders','operation':'overview'})),
      ('inline_source_access','inline_draft',lambda x:x['sources'].append({'domain':'email','operation':'overview'})),
      ('none_source_access','prohibition',lambda x:x['sources'].append({'domain':'calendar','operation':'overview'})),
      ('effect_read_authorization','effect',lambda x:x.update(kind='read',sources=[{'domain':'email','operation':'overview'}])),
      ('old_source_filter_leak','switch_email_calendar',lambda x:x['sources'][0].update(unread=True)),
      ('same_source_filter_loss','same_email',lambda x:x['sources'][0].pop('account')),
      ('dropped_unsupported_constraint','calendar_location',lambda x:x.update(unsupported_constraints=[])),
      ('nonliteral_constraint','email_constraint',lambda x:x.update(unsupported_constraints=['paraphrased restriction']))]:
        ex,meta=route_case(cat);reject(name,ex,meta,fn)
    for ex,meta in zip(examples['dev'],metadata['dev']):
        if meta['category']=='ordinary_none' and 'without opening mine' in last_user(ex):
            reject('ordinary_definition_prohibition_dropped',copy.deepcopy(ex),copy.deepcopy(meta),lambda x:x.update(excluded_sources=[]));break
    else:raise ValueError('no explicit ordinary definition prohibition fixture')
    for ex,meta in zip(examples['train'],metadata['train']):
        if meta['category']=='email_literal' and meta['synthetic_slots']['literal'].startswith('+'):
            reject('actual_leading_plus_removed',copy.deepcopy(ex),copy.deepcopy(meta),lambda x:x['sources'][0].update(query=x['sources'][0]['query'][1:]));break
    else:raise ValueError('no positive leading-plus address control fixture')
    for name,cat,fn in [
      ('fabricated_number','calendar',lambda e:e['messages'][-1].update(content=e['messages'][-1]['content']+' Cost: 987654 credits.')),
      ('denied_as_empty','denied',lambda e:e['messages'][-1].update(content='Email: no matching emails.')),
      ('missing_partial_honesty','partial',lambda e:e['messages'][-1].update(content=e['messages'][-1]['content'].split('\n')[0])),
      ('proposal_as_confirmed','proposal',lambda e:e['messages'][-1].update(content=e['messages'][-1]['content'].replace('unconfirmed','confirmed'))),
      ('claimed_send','email',lambda e:e['messages'][-1].update(content=e['messages'][-1]['content']+' I sent the recap.')),
      ('speaker_body_swap','messages',lambda e:e['messages'][-1].update(content=e['messages'][-1]['content'].replace(' offered to ',' claimed to ',1)))]:
        for ex,meta in zip(examples['train'],metadata['train']):
            if meta['task']=='overview' and meta['category']==cat:reject(name,copy.deepcopy(ex),copy.deepcopy(meta),fn,True);break
    # Split/near-duplicate controls operate on minimal otherwise valid rows.
    es={'train':[copy.deepcopy(examples['train'][0])],'dev':[copy.deepcopy(examples['train'][0])]}
    ms={'train':[copy.deepcopy(metadata['train'][0])],'dev':[copy.deepcopy(metadata['train'][0])]}
    try:dedup(es,ms)
    except ValueError:controls.append({'control':'exact_cross_split_duplicate','result':'rejected'})
    else:raise ValueError('exact duplicate control accepted')
    ex,meta=route_case('calendar_literal_date');clone=copy.deepcopy(ex);cm=copy.deepcopy(meta)
    old=meta['synthetic_slots']['literal'];new='New substituted entity'
    clone['id']='injected-dev';clone['messages'][-2]['content']=clone['messages'][-2]['content'].replace(old,new);cm['synthetic_slots']['literal']=new
    try:dedup({'train':[ex],'dev':[clone]},{'train':[meta],'dev':[cm]})
    except ValueError:controls.append({'control':'entity_substitution_cross_split_duplicate','result':'rejected'})
    else:raise ValueError('entity-normalized duplicate accepted')
    return controls


def run(public=False):
    schema=json.loads((ROOT/'intent.schema.v1.json').read_text());manifest=json.loads((ROOT/'manifest.json').read_text())
    splits=['train','dev'] if public else list(COUNTS)
    examples={};metadata={};mask_count=0
    for split in splits:
        examples[split]=rows(ROOT/f'{split}.jsonl');metadata[split]=rows(ROOT/f'{split}.provenance.jsonl')
        require(len(examples[split])==sum(COUNTS[split]),'wrong split count')
        require(len(metadata[split])==len(examples[split]),'provenance count mismatch')
        require(Counter(m['task'] for m in metadata[split])=={'routing':COUNTS[split][0],'overview':COUNTS[split][1]},'mixture mismatch')
        for ex,meta in zip(examples[split],metadata[split]):
            require(ex['id']==meta['id'] and meta['split']==split,'row alignment mismatch')
            require(set(ex)=={'id','messages'},'oracle metadata in training record')
            require(ex['messages'][-1]['role']=='assistant' and ex['messages'][-1].get('training') is True,'final mask missing')
            require(all(m.get('training') is False for m in ex['messages'][:-1] if m['role']=='assistant'),'prior assistant supervised')
            require(all('training' not in m for m in ex['messages'] if m['role']!='assistant'),'non-assistant training field')
            require(all(set(m)<={'role','content','training'} and isinstance(m['content'],str) for m in ex['messages']),'bad message envelope')
            require(ex['messages'][0]['role']=='system','system missing')
            mask_count+=sum(m.get('training') is True for m in ex['messages'])
            if meta['task']=='routing':
                require(ex['messages'][0]['content']==(ROOT/'router-system.txt').read_text(),'router contract mismatch');validate_route(ex,meta,schema)
            else:validate_overview(ex,meta)
        for fn in [f'{split}.jsonl',f'{split}.provenance.jsonl']:
            require(digest(ROOT/fn)==manifest['files'][fn]['sha256'],'manifest hash mismatch '+fn)
    # Full author mode sees family definitions; public mode never opens them or final payload.
    if not public:
        families=json.loads((ROOT/'families.json').read_text())['families']
        require(len({f['family'] for f in families})==len(families),'family ID reused')
        require(all('/'+f['split']+'/' in f['family'] for f in families),'family split ownership missing')
        family_set={f['family'] for f in families}
        require(all(m['family'] in family_set for vals in metadata.values() for m in vals),'unknown family')
    for fn in ['intent.schema.v1.json','router-system.txt']:
        require(digest(ROOT/fn)==manifest['source_contract_sha256'][fn],'contract hash changed')
    screening=dedup(examples,metadata);controls=negative_controls(examples,metadata,schema)
    archive=ROOT/'ling-train-dev-20261006.zip';zipmeta=json.loads((ROOT/'upload-zip.json').read_text())
    require(digest(archive)==zipmeta['sha256'],'ZIP hash mismatch')
    with zipfile.ZipFile(archive) as z:
        require(sorted(z.namelist())==zipmeta['members'],'ZIP member mismatch')
        require(set(z.namelist())=={'train.jsonl','dev.jsonl','train.provenance.jsonl','dev.provenance.jsonl','intent.schema.v1.json','router-system.txt','DATA_CARD.md','upload-manifest.json'},'unexpected portable member')
        for fn in z.namelist():require(z.read(fn)==(ROOT/fn).read_bytes(),'ZIP content mismatch')
        require(z.testzip() is None,'ZIP CRC failure')
    report={'mode':'public_train_dev_only' if public else 'author_all_splits','passed':True,'splits_read':splits,'counts':{s:len(es) for s,es in examples.items()},'supervised_assistant_messages':mask_count,'schema_semantic_grounding_checks':'PASS','dedup_screen':screening,'negative_controls':controls,'negative_controls_rejected':len(controls),'zip_members':zipmeta['members'],'limitations':'Deterministic author/reference checks; no model inference, downstream training, embedding-based contamination audit, or measured gain. Final has been read only for initial author validation.'}
    print(json.dumps(report,indent=2))
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--public',action='store_true');args=p.parse_args();run(args.public)
