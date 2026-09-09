#!/usr/bin/env python3
"""Build a detailed, explicitly limited report from saved real-model results."""
from __future__ import annotations
import argparse
import collections
import json
import math
from pathlib import Path
import statistics

ROOT=Path(__file__).resolve().parent.parent


def pct(values,p):
    values=sorted(values)
    return values[max(0,math.ceil(len(values)*p)-1)] if values else 0


def md(value):
    return str(value).replace('|','\\|').replace('\n',' ')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',default='test_results/routing_stress_full')
    args=parser.parse_args()
    run=(ROOT/args.run).resolve()
    docs=ROOT/'docs/routing_stress/results'
    docs.mkdir(parents=True,exist_ok=True)
    suite=json.loads((ROOT/'test_fixtures/routing_stress/suite.json').read_text())
    specs={c['id']:c for c in suite['cases']}
    results=[json.loads(p.read_text()) for p in sorted((run/'cases').glob('WRS-*.json'))]
    permission=json.loads((ROOT/'test_results/routing_stress_permissions/permission_results.json').read_text())
    meta=json.loads((run/'run_metadata.json').read_text())
    strict_path=run/'strict_verdicts.json'
    strict=json.loads(strict_path.read_text()) if strict_path.exists() else {}
    strict_by_id={v['id']:v for v in strict.get('cases',[])}
    retry_manifests=[]
    for path in sorted((run/'retry_history').glob('*/manifest.json')):
        retry_manifests.append(json.loads(path.read_text()))
    prior_attempt_ids={entry['id'] for manifest in retry_manifests for entry in manifest.get('entries',[])}
    n=len(results)
    if not n:
        print('No case results yet.');return
    times=[r['elapsed_s'] for r in results]
    passing=sum(r['grading']['automated_routing_pass'] for r in results)
    issue_keys=['missing_required_emissions','missing_from_router_menu','forbidden_emissions','ordering_violations','permission_violations','argument_findings','clarification_review_flag']
    counts={k:sum(bool(r['grading'].get(k)) for r in results) for k in issue_keys}
    counts.update(timeouts=sum('CASE_TIMEOUT' in r['error'] for r in results),runtime_errors=sum(bool(r['error']) and 'CASE_TIMEOUT' not in r['error'] for r in results),schema_errors=sum(bool(r['schema_errors']) for r in results),fixture_gaps=sum(bool(r['fixture_gaps']) for r in results))
    group_rows=[]
    for gn in range(1,21):
        subset=[r for r in results if specs[r['id']]['group_number']==gn]
        if not subset: continue
        ts=[r['elapsed_s'] for r in subset]
        group_rows.append(dict(category=subset[0]['category'],cases=len(subset),automated_passes=sum(r['grading']['automated_routing_pass'] for r in subset),menu_gap=sum(bool(r['grading']['missing_from_router_menu']) for r in subset),forbidden=sum(bool(r['grading']['forbidden_emissions']) for r in subset),timeout=sum('CASE_TIMEOUT' in r['error'] for r in subset),median_s=statistics.median(ts),p95_s=pct(ts,.95)))
    tool_rows=[]
    alltools=json.loads((ROOT/'docs/WISP_TOOL_ACTIVATION_INVENTORY.json').read_text())['tools']
    for tool in alltools:
        name=tool['name']
        req=[r for r in results if name in specs[r['id']]['required_tools']]
        tool_rows.append(dict(tool=name,required_cases=len(req),offered_cases=sum(name in (r['route'].get('tool_subset') or []) for r in req),emitted_cases=sum(name in r['grading']['raw_model_calls']+r['grading']['direct_calls'] for r in req),dispatched_cases=sum(name in r['grading']['dispatched_tools'] for r in req),forbidden_emission_cases=sum(name in r['grading']['forbidden_emissions'] for r in results),optional_cases=sum(name in specs[r['id']]['optional_tools'] for r in results)))
    summary=dict(completed_cases=n,total_cases=1000,run_complete=n==1000,model=meta['model'],automated_routing_passes=passing,automated_routing_failures=n-passing,prior_attempt_infrastructure_failures=len(prior_attempt_ids),issues=counts,latency=dict(median_s=statistics.median(times),p90_s=pct(times,.9),p95_s=pct(times,.95),p99_s=pct(times,.99),max_s=max(times),over_15s=sum(t>15 for t in times),over_30s=sum(t>30 for t in times),over_60s=sum(t>60 for t in times)),permission_tests={k:v for k,v in permission.items() if k!='results'},strict_counts=strict.get('counts',{}),categories=group_rows,tools=tool_rows)
    (run/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    heading='Completed' if n==1000 else 'IN PROGRESS'
    lines=[f'# Wisp routing stress report — {heading}', '',f'**{n}/1,000 cases completed. Model: `{meta["model"]}`.** Test contact: Mom (fictional phone). Test email: johnstandark@gmail.com. No email, text, calendar change, reminder deletion, shell command, app action, or external HTTP request was executed.', '',f'**{passing}/{n} met the automated routing checks.** This is not an end-to-end task-success rate: the evaluator checks tool emissions, selected argument constraints, ordering, and policy flow, but does not provide a complete semantic grade for every answer or argument. The tools themselves return synthetic results.', '',f'**Permission checks: {permission["passed"]}/{permission["tests"]} passed. Four standing-grant bypasses were reproduced.** These matter regardless of model quality; details and fixes are below.', '', '## What was run', '', '- The reviewed 1,000 prompts, with the requested contact/email substitutions and shared user setup context.', '- Source-tree Wisp router, original tool schemas, real local model, agent loop and policy engine. Expected tools and hidden fixture outcomes were never inserted into the conversation.', '- All 171 real tool bodies were replaced before calls. Subprocess creation and non-model sockets were blocked; writes were restricted to the result directory. The live `/agent` endpoint was not used.', '- The main corpus uses simulated approval after the real confirmation event. Separate tests exercise approval, refusal, and unanswered-card timeout. No real user approval is inferred from those simulation events.', '- The initial sweep and early recovery used two concurrent cases. The split recovery used two disjoint workers with two cases each, for up to four concurrent cases. Every case had a 90-second deadline and the production default of eight agent steps / 3,000 output tokens per step. Reported times are stress-load measurements, not isolated interactive latency estimates.', '- Warm resident inference model; per-step model eviction/reloading was suppressed to avoid concurrent runs evicting each other. Synthetic clock: Monday 2026-08-31 10:00 America/Los_Angeles.', '- The three completed Ornith pilot cases are excluded. The inference server health default initially identified that model, but Wisp\'s live role configuration confirmed Ling for assistant tasks. The fourth pilot was cancelled.', '', '## First-attempt infrastructure outage', '', f'**{len(prior_attempt_ids)} cases had a preserved first-attempt local-model connection failure.** Those attempts remain strict failures even after selective recovery runs. The first saved connection failure occurred at WRS-0263; two neighboring in-flight cases completed before failures became continuous. Both ports 8000 and 8765 were unavailable when checked after the sweep. The evidence establishes an outage, but does not establish its root cause.', '', 'The local oMLX server was restarted, every `ConnectError` trace was archived with its hash, and only those cases were rerun. Current per-case results describe the retry; strict verdicts also include `prior_attempt_infrastructure_error` so recovery cannot erase the reliability failure.', '', f'[Preserved retry history]({run/"retry_history"})', '', '## Permission behavior and failures', '', 'The normal gate worked in all 27 scripted approve/deny/timeout paths covering nine outbound/calendar tools. Approved actions reached only fixtures. Refused and unanswered actions reached no tool implementation. The real InteractiveApprover timeout code was exercised with a 20 ms timeout; its production default is 300 seconds. All 27 view-only/default/full-access policy checks also passed.', '', '| Tool | Without standing grant | After an “always allow” grant | Result |', '| --- | --- | --- | --- |']
    for item in permission['results']:
        if 'standing-grant' in item['test']:
            lines.append(f"| `{item['test'].split('/')[0]}` | {item['before']} | {item['after']} | {'Pass' if item['passed'] else '**FAIL — confirmation bypass**'} |")
    lines += ['', '**Cause:** `policy.decide()` accepts an allow grant before checking the always-confirm outbound/calendar categories. `grants._NEVER_GRANTABLE` omits `forward_email`, `update_event`, `clear_reminders`, and `clear_past_reminders`, so the UI can offer a standing grant that removes their intended confirmation gate.', '', '**Recommended fix:** enforce non-grantable behavior by safety category at both the grant writer and policy reader; reject or ignore stale grants for always-confirm actions. Add regression coverage for every tool in those categories. Do not rely only on a manually maintained name list. No production policy or grant file was changed during this run.', '',f'[Full permission evidence]({ROOT / "test_results/routing_stress_permissions/permission_results.json"})', '', '## Failure counts', '', 'Counts overlap; one case can have several failures. A missing tool in the router menu is an availability failure, while emitting an unavailable or forbidden tool is a model proposal failure. Wisp may correctly block the proposal before dispatch.', '', '| Finding | Cases |', '| --- | ---: |']
    for k,v in counts.items(): lines.append(f'| {k.replace("_"," ")} | {v} |')
    lines += ['', '## Latency', '', '| Measure | Seconds |', '| --- | ---: |']
    for key in ['median_s','p90_s','p95_s','p99_s','max_s']:lines.append(f'| {key.replace("_s","")} | {summary["latency"][key]:.2f} |')
    lines += ['', f"Over 15 seconds: **{summary['latency']['over_15s']}**; over 30 seconds: **{summary['latency']['over_30s']}**; over 60 seconds: **{summary['latency']['over_60s']}**. Timed-out cases are censored at 90 seconds; their actual completion time is unknown.", '', '### Slowest cases', '', '| Case | Seconds | Model steps | Router seconds | Reasoning characters | Failure |','| --- | ---: | ---: | ---: | ---: | --- |']
    for r in sorted(results,key=lambda x:x['elapsed_s'],reverse=True)[:25]:
        lines.append(f"| [{r['id']}]({run/'cases'/(r['id']+'.json')}) | {r['elapsed_s']:.2f} | {len(r['model_steps'])} | {r['route_s']:.2f} | {sum(s['reasoning_chars'] for s in r['model_steps'])} | {md(r['error'] or ', '.join(r['grading']['missing_required_emissions']))} |")
    lines += ['', '## Category results', '', '| Category | Cases | Routing pass | Menu gap | Forbidden proposal | Timeout | Median s | p95 s |','| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for g in group_rows:lines.append(f"| {g['category']} | {g['cases']} | {g['automated_passes']} | {g['menu_gap']} | {g['forbidden']} | {g['timeout']} | {g['median_s']:.2f} | {g['p95_s']:.2f} |")
    lines += ['', '## Findings verified from individual traces', '',
              '**Semantic retrieval is degraded by a server error.** A direct probe of the configured Qwen3-Embedding-0.6B-4bit-DWQ model returned HTTP 400: the local server says it is not an embedding model. `_semantic_core` catches the error and silently returns its static core menu. Several routes describe this as “retrieved tools (19)” even though retrieval failed. This infrastructure defect was reproduced without changing configuration; it is not a model selection error.', '',
              f'[Embedding probe evidence]({run/"embedding_probe.json"})', '',
              '**Missing capabilities are hidden before the model can choose them.** In WRS-0001, `list_contacts` and `search_browser_history` were absent from the offered menu even though both were requested. WRS-0102 explicitly requested reminder deletion, event cancellation, and event creation, but received only `get_upcoming`, `find_free_time`, `join_video_call`, and `get_past_events`. These are router failures, not proof that the model rejected available tools.', '',
              '**False completion is not limited to read requests.** WRS-0107 claimed “All five tasks completed,” but created a Notes note instead of a countdown, appended another note instead of renaming a reminder, and emitted an immediate message to johnstandark@gmail.com instead of a notification in 25 minutes. No timer, reminder-completion, reminder-update, or notification-scheduling call was made. This is a confirmed wrong-action and false-completion failure; the simulated message never reached a real recipient.', '',
              '**Unsupported claims can survive the final-answer verifier.** In WRS-0001, Wisp listed Calendar event/reminder titles under “Saved contact names” and reported no browser history despite making no browser-history call. The claim about contacts is contradicted by the source labels; the browser-history absence claim has no supporting lookup. `_verified_final` primarily checks action outcomes, so unsupported read/absence claims can pass through.', '',
              '**Correct tool coverage does not ensure ordering.** WRS-0041 reached all five required tools but called `get_upcoming` before the requested past-event lookup and Notes read. The prompt explicitly connected its clauses with “then.”', '',
              '**Recovery adds latency.** WRS-0002 called `summarize_messages` with unsupported argument `query`, then continued with additional reads. WRS-0008 returned no parsed tool calls and an inability response after approximately 45 seconds. These are separate from unavailable-menu failures.', '',
              'These examples are from the primary Ling run. Fixture account/date filtering is simplified, so account/date answer errors are not treated here as independently established model defects.', '',
              '## Recommended priorities', '',
              '1. Close the four confirmation bypasses before enabling standing grants for these actions.',
              '2. Restore the embedding endpoint and expose degraded retrieval in route diagnostics. Then build the tool menu from every requested clause and check completeness before starting. Avoid widening indiscriminately to all 171 tools; retain relevant tools for each identified intent.',
              '3. Require source evidence for claims such as “no contacts,” “no messages,” or “no browser history.” If the source was not queried, say it was not checked.',
              '4. Preserve explicit sequencing and data dependencies, and expose partial completion instead of presenting an incomplete response as complete.',
              '5. Add a response-time budget, stop repeated failed calls earlier, and rerun representative slow cases sequentially to separate model behavior from concurrent load.', '',
              '## Representative failures', '', 'Selected for distinct categories and failure types; these are observed traces, not hypothetical examples.', '']
    chosen=[];seen=set()
    for r in results:
        if r['grading']['automated_routing_pass']:continue
        signature=(r['category'], bool(r['grading']['forbidden_emissions']),bool(r['error']))
        if signature in seen:continue
        seen.add(signature);chosen.append(r)
        if len(chosen)>=25:break
    for r in chosen:
        g=r['grading']
        lines += [f"### {r['id']} — {r['category']}", '', '**Prompt:** '+r['prompt'], '', f"**Elapsed:** {r['elapsed_s']:.2f}s. **Required:** {', '.join(specs[r['id']]['required_tools']) or 'none'}.", f"**Offered:** {', '.join(r['route'].get('tool_subset') or []) or 'none'}.", f"**Emitted:** {', '.join(g['raw_model_calls']+g['direct_calls']) or 'none'}.", f"**Missing:** {', '.join(g['missing_required_emissions']) or 'none'}. **Forbidden proposals:** {', '.join(g['forbidden_emissions']) or 'none'}.", f"**Answer/error:** {md((r['error'] or r['answer'] or '(empty)')[:1400])}", f"[Full trace]({run/'cases'/(r['id']+'.json')})", '']
    lines += ['## Scope and interpretation limits', '', '- Synthetic reads validate routing/chaining, not real Mail, Messages, EventKit, Contacts, filesystem, network, skill scripts, document creation, or OS permissions. macOS privacy dialogs and native UI confirmations were not exercised.', '- Fixtures cover the source evidence needed for branches and common lookups. Many non-conditional read fixtures use a fixed sample response rather than fully emulating query filtering or every optional argument. Do not infer factual answer accuracy or real provider reliability from them.', '- Some capabilities have placeholder implementations. Their honest limitation behavior is included, but a placeholder call never proves completion.', '- Automated argument checks cover schema-required fields/unknown keys, selected recipients, and selected preview constraints. The natural-language argument assertions in the original corpus still require broader semantic review. An automated routing pass can contain an answer or argument defect.', '- Previous conversation text is provided for the 50 follow-up cases; a native prior-turn tool digest is not synthesized. This can expose fallback routing but does not fully reproduce a saved app session.', '- No real memories, contacts, mailboxes, grants, or credentials were copied. The test email is a fixture destination only. Original contact lookup uses a synthetic Mom identity.', '- One run per prompt does not measure stochastic repeatability. Concurrent timing includes competition for the model; slower cases should be repeated sequentially before promising interactive performance.', '', '## Files', '', f'- [All 1,000 case outcomes]({docs/"ALL_CASE_RESULTS.md"})', f'- [Per-tool coverage]({docs/"TOOL_RESULTS.md"})', f'- [Machine-readable summary]({run/"summary.json"})', f'- [Execution metadata and source fingerprints]({run/"run_metadata.json"})', f'- [Reviewed prompts]({ROOT/"docs/routing_stress/PROMPTS_ONLY.txt"})', f'- [Runner]({ROOT/"scripts/run_routing_stress_suite.py"})', '']
    strict_counts=strict.get('counts',{})
    strict_summary=f"{strict_counts.get('FAIL',0):,} FAIL, {strict_counts.get('PASS',0):,} PASS, {strict_counts.get('NOT_PASSED_UNVERIFIED',0):,} not-passed/unverified"
    strict_lines=['## Strict grading requested by the user', '', f"Strict verdicts: **{strict_summary}**. Only PASS counts as a pass. NOT_PASSED_UNVERIFIED is a failure to establish correctness, not an assertion of a confirmed model defect.", '', 'A strict pass requires all requested work, correct arguments and scope, no unsolicited/forbidden proposals, valid ordering, matching approvals, supported final claims, and affirmative semantic review. Invalid calls fail even if a later retry fixes them. Completion over 30 seconds or no tool action within 10 seconds also fails. The initial sweep and early recovery used two concurrent cases; split recovery used up to four, so timing is deliberately contention-heavy.', '', 'Strict failure occurrences overlap. These include deliberately fail-closed heuristic checks; use the saved trace and per-case reason when distinguishing a confirmed defect from a conservative failure:', '', '| Strict reason | Occurrences |', '| --- | ---: |']
    for key,value in sorted(strict.get('failure_occurrences',{}).items(),key=lambda x:(-x[1],x[0])):
        strict_lines.append(f'| {key.replace("_"," ")} | {value} |')
    strict_lines += ['', f'[Every strict verdict and reason]({strict_path})', '', 'The older automated-routing metric below is diagnostic only and must not be read as the strict pass rate.', '']
    lines[4:4]=strict_lines
    (docs/'REPORT.md').write_text('\n'.join(lines))
    details=['# Every case result', '', f'{n}/1,000 complete. Status uses strict grading; unverified cases do not count as passes.', '', '| Case | Category | Status | Seconds | Missing emissions | Forbidden emissions | Error |','| --- | --- | --- | ---: | --- | --- | --- |']
    for r in results:
        g=r['grading'];details.append(f"| [{r['id']}]({run/'cases'/(r['id']+'.json')}) | {r['category']} | {strict_by_id.get(r['id'],{}).get('status','NOT_PASSED_UNVERIFIED')} | {r['elapsed_s']:.2f} | {', '.join(g['missing_required_emissions'])} | {', '.join(g['forbidden_emissions'])} | {md(r['error'])} |")
    (docs/'ALL_CASE_RESULTS.md').write_text('\n'.join(details)+'\n')
    lines=['# Per-tool routing results', '', 'Counts apply to cases that require the named primary tool. Equivalent paths may be valid. Optional placeholder cases are shown separately.', '', '| Tool | Required cases | Offered | Emitted | Dispatched into fixture | Forbidden proposals | Optional cases |','| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for t in sorted(tool_rows,key=lambda x:(-(x['required_cases']-x['emitted_cases']),x['tool'])):
        lines.append(f"| `{t['tool']}` | {t['required_cases']} | {t['offered_cases']} | {t['emitted_cases']} | {t['dispatched_cases']} | {t['forbidden_emission_cases']} | {t['optional_cases']} |")
    (docs/'TOOL_RESULTS.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ['categories','tools']},indent=2))


if __name__=='__main__':main()
