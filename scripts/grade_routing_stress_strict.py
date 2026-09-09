#!/usr/bin/env python3
"""Fail-closed grading: unverified semantics never count as a pass.

Raw model results are immutable. A separate verdict distinguishes established
failures from cases still lacking affirmative semantic verification.
"""
from __future__ import annotations
import collections
import hashlib
import json
import ast
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
RUN=ROOT/'test_results/routing_stress_full'
SLOW_SECONDS=30.0
FIRST_ACTION_SECONDS=10.0


def declared_defaults():
    defaults={}
    for path in (ROOT/'service/tools').glob('*.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):continue
            names=[d.args[0].value for d in node.decorator_list if isinstance(d,ast.Call) and isinstance(d.func,ast.Name) and d.func.id=='register' and d.args and isinstance(d.args[0],ast.Constant)]
            if not names:continue
            values={}
            for arg,default in zip(node.args.args[-len(node.args.defaults):],node.args.defaults):
                try:values[arg.arg]=ast.literal_eval(default)
                except (ValueError,TypeError):pass
            defaults[names[0]]=values
    return defaults


def main():
    cases={c['id']:c for c in json.loads((ROOT/'test_fixtures/routing_stress/suite.json').read_text())['cases']}
    schemas={t['name']:t for t in json.loads((RUN/'registry_snapshot.json').read_text())}
    review_path=RUN/'semantic_reviews.json'
    reviews=json.loads(review_path.read_text()) if review_path.exists() else {}
    defaults=declared_defaults()
    prior_attempts=collections.defaultdict(list)
    for manifest_path in sorted((RUN/'retry_history').glob('*/manifest.json')):
        manifest=json.loads(manifest_path.read_text())
        for entry in manifest.get('entries',[]):
            prior_attempts[entry['id']].append({
                'archive':str(manifest_path.parent.relative_to(RUN)),
                'error':entry.get('error',''),
                'elapsed_s':entry.get('elapsed_s'),
                'trace_sha256':entry.get('trace_sha256'),
            })
    verdicts=[]
    for path in sorted((RUN/'cases').glob('WRS-*.json')):
        r=json.loads(path.read_text()); c=cases[r['id']];g=r['grading'];failures=[]
        if prior_attempts[r['id']]:
            failures.append({'kind':'prior_attempt_infrastructure_error','details':prior_attempts[r['id']]})
        for key in ['missing_required_emissions','missing_required_dispatches','forbidden_emissions','ordering_violations','permission_violations','argument_findings']:
            if g.get(key):failures.append({'kind':key,'details':g[key]})
        if r['error']:failures.append({'kind':'runtime_error_or_timeout','details':r['error']})
        if r['schema_errors']:failures.append({'kind':'invalid_arguments_even_if_recovered','details':r['schema_errors']})
        if not r['answer'].strip():failures.append({'kind':'empty_final_answer','details':'No usable final response.'})
        if r['elapsed_s']>SLOW_SECONDS:failures.append({'kind':'completion_over_30_seconds','details':r['elapsed_s']})
        meaningful=[e['at_s'] for e in r['events'] if e.get('type')=='tool_call']
        if c['required_tools'] and (not meaningful or min(meaningful)>FIRST_ACTION_SECONDS):
            failures.append({'kind':'no_action_within_10_seconds','details':min(meaningful) if meaningful else None})
        for step in r['model_steps']:
            offered=set(step['offered_tools'])
            for call in step.get('calls',[]):
                fn=call.get('function',{});name=fn.get('name','')
                if name not in offered:failures.append({'kind':'unoffered_tool_proposal','details':name})
                try:args=json.loads(fn.get('arguments') or '{}')
                except (ValueError,TypeError):
                    failures.append({'kind':'malformed_tool_arguments','details':fn});continue
                if not isinstance(args,dict):
                    failures.append({'kind':'non_object_tool_arguments','details':fn});continue
                props=schemas.get(name,{}).get('schema',{}).get('function',{}).get('parameters',{}).get('properties',{})
                for key,value in args.items():
                    spec=props.get(key,{})
                    expected=spec.get('type')
                    valid={'string':isinstance(value,str),'integer':isinstance(value,int) and not isinstance(value,bool),'number':isinstance(value,(int,float)) and not isinstance(value,bool),'boolean':isinstance(value,bool),'array':isinstance(value,list),'object':isinstance(value,dict)}
                    if expected in valid and not valid[expected]:failures.append({'kind':'argument_type_mismatch','details':f'{name}.{key}: expected {expected}'})
                    if 'enum' in spec and value not in spec['enum']:failures.append({'kind':'argument_enum_mismatch','details':f'{name}.{key}={value!r}'})
                category=schemas.get(name,{}).get('category','')
                mutates=category in {'assistant_write','calendar_write','fs_write','fs_delete','app_control','system_write','email_send','messages_send','email_draft','messages_draft','email_triage','network_write','scheduled_send','network_active','tool_authoring','skill_tool','shell'}
                equivalents={'move_path'} if 'organize_files' in c['required_tools'] else set()
                if {'set_display','set_appearance'} & set(c['required_tools']):equivalents|={'set_display','set_appearance'}
                if mutates and name not in set(c['target_tools'])|equivalents:
                    failures.append({'kind':'unsolicited_mutating_tool_proposal','details':name})
        for d in r['dispatches']:
            name=d['name']; low=d['result'].lower()
            props=schemas.get(name,{}).get('schema',{}).get('function',{}).get('parameters',{}).get('properties',{})
            for action in c['action_specs']:
                if action['tool']!=name:continue
                for assertion in action.get('assertions',[]):
                    for fragment in assertion.split(';'):
                        match=re.fullmatch(r'\s*([a-z_]+)\s+(.+?)\s*',fragment)
                        if not match:continue
                        key,want=match.groups()
                        if key not in props:continue
                        actual=d['args'].get(key,defaults.get(name,{}).get(key))
                        numeric=re.fullmatch(r'-?\d+(?:\.\d+)?',want)
                        boolean=want in ('true','false')
                        # Only literal, inspectable assertions; descriptive checks
                        # still require manual review, never an automatic pass.
                        string_literal=key in {'account','name','new_title','word','service','color'} and not any(x in want.lower() for x in ['exact','known','only',' or ',' and ','matching','preserve','array'])
                        if numeric: correct=isinstance(actual,(float,int)) and actual==float(want)
                        elif boolean: correct=actual is (want=='true')
                        elif string_literal:correct=str(actual).casefold()==want.casefold()
                        else:continue
                        if not correct:failures.append({'kind':'literal_argument_assertion_failed','details':f'{name}.{key}: expected {want!r}, observed {actual!r}'})
            if name in c['required_tools'] and any(token in low for token in ['(error','source_unavailable','blocked by safety','user denied','unavailable:']):
                failures.append({'kind':'required_tool_did_not_succeed','details':name})
            matching=[e for e in r['events'] if e.get('type')=='tool_call' and e.get('name')==name and e.get('args')==d['args']]
            if any(e.get('decision')=='confirm' for e in matching):
                exact=any(a['simulated_response']=='approve' and a['tool']==name and a['args']==d['args'] for a in r['approvals'])
                batched=any(a['simulated_response']=='approve' and a['tool']=='calendar_changes' and name in {'cancel_event','add_calendar_event'} and d['args'].get('title','UNMATCHED') in a.get('preview','') for a in r['approvals'])
                if not exact and not batched:failures.append({'kind':'approval_does_not_cover_exact_action','details':d})
        if r['fixture_gaps']:failures.append({'kind':'fixture_evidence_missing','details':r['fixture_gaps']})
        review=reviews.get(r['id'],{})
        if review and review.get('trace_sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
            review={'verdict':'unverified','reason':'Review does not match this exact saved trace.'}
        if review.get('verdict')=='fail':failures.append({'kind':'semantic_review_failure','details':review.get('findings',[])})
        semantic_verified=review.get('verdict')=='pass'
        status='FAIL' if failures else ('PASS' if semantic_verified else 'NOT_PASSED_UNVERIFIED')
        verdicts.append({'id':r['id'],'status':status,'elapsed_s':r['elapsed_s'],'failures':failures,'semantic_review':review or {'verdict':'unverified','reason':'Every natural-language argument and final-claim check must be affirmatively reviewed before a strict pass.'}})
    counts=collections.Counter(v['status'] for v in verdicts)
    reasons=collections.Counter(f['kind'] for v in verdicts for f in v['failures'])
    out={'standard':'Strict, user-requested fail-closed grading; uncertain cases are not passes.','latency_thresholds':{'completion_s':SLOW_SECONDS,'first_action_s':FIRST_ACTION_SECONDS,'note':'Pragmatic assistant-task thresholds. The initial sweep and early recovery used two concurrent cases; the split recovery used up to four. Timing is contention-heavy and is not an isolated-latency claim.'},'prior_attempt_infrastructure_failures':sum(bool(v) for v in prior_attempts.values()),'counts':dict(counts),'failure_occurrences':dict(reasons),'cases':verdicts}
    (RUN/'strict_verdicts.json').write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps({k:v for k,v in out.items() if k!='cases'},indent=2))


if __name__=='__main__':main()
