'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const A = require('../../browser-extension/shared/action-executor.js');
const E = require('../../browser-extension/shared/page-extractor.js');
const json = JSON.stringify;
function snapshot() {
  return {schema_version:'1.0', id:'s1', task_id:'t1', observation_id:'o1',
    url:'https://school.invalid/course', origin:'https://school.invalid', captured_at_ms:100,
    enabled:true, site_permission:'granted', private_context:false, tab_role:'background', content_mode:'dom_text',
    elements:[{target_id:'button1',role:'button',label:'Save',editable:false},
      {target_id:'text1',role:'textbox',label:'Name',editable:true},
      {target_id:'select1',role:'combobox',label:'Choice',editable:true}]};
}
function action(s, command, id='a1') {
  const intent = {action_id:id, task_id:s.task_id, snapshot_id:s.id, command,
    target_id:({click:'button1',fill:'text1',select:'select1'})[command] || null,
    url:['navigate','open_tab'].includes(command)?'https://school.invalid/next':null,
    text:['fill','select'].includes(command)?'SYNTHETIC_SECRET':null, private_data:false, consequential:false};
  return {schema_version:'1.0',intent,approval:{proposal_id:'p1',authority:'app',approved_at_ms:100,expires_at_ms:10000,intent:{...intent}}};
}
function run(w, command, id='a1') {
  const a=action(w.snapshotForTest(),command,id), raw=json(a);
  return w.execute(raw,w.approveForTest(raw,9000));
}
for(const verb of A.VERBS) test(`fixed ${verb} has structured pre/post checks`,()=>{
  const w=A.createSyntheticExecutor(json(snapshot()));
  if(verb==='back') assert.equal(run(w,'navigate','first').status,'simulated');
  const r=run(w,verb);
  assert.equal(r.status,'simulated'); assert.equal(r.post.steps,r.pre.steps+1);
  assert.equal(r.mode,'synthetic_only'); assert.equal(r.completesObligation,false);
  assert.equal(r.retryAllowed,false); assert.ok(!json(r).includes('SYNTHETIC_SECRET'));
});
test('wire approval alone never authorizes an effect; autosave is gated despite false flags',()=>{
  for(const verb of ['click','fill','select','navigate','back','open_tab']) {
    const w=A.createSyntheticExecutor(json(snapshot())), a=action(snapshot(),verb);
    assert.equal(A.prepare(json(a),json(snapshot())).approvalRequired,true);
    assert.equal(w.execute(json(a),{}).code,'approval_required');
    assert.equal(w.snapshotForTest().id,'s1');
  }
});
test('scroll/wait require no approval unless effect flag raises the gate',()=>{
  for(const verb of ['scroll','wait']) {
    const w=A.createSyntheticExecutor(json(snapshot())), a=action(snapshot(),verb); a.approval=null;
    assert.equal(w.execute(json(a)).status,'simulated');
    const b=action(w.snapshotForTest(),verb,'second'); b.intent.consequential=true; b.approval.intent={...b.intent};
    assert.equal(w.execute(json(b)).code,'approval_required');
  }
});
test('exact approval binds proposal, text, snapshot and target; handles are instance local',()=>{
  const w=A.createSyntheticExecutor(json(snapshot())), a=action(snapshot(),'fill'), raw=json(a);
  const grant=w.approveForTest(raw,9000);
  const other=A.createSyntheticExecutor(json(snapshot()));
  assert.equal(other.execute(raw,grant).code,'approval_required');
  const b=structuredClone(a); b.approval.proposal_id='replacement';
  assert.equal(w.execute(json(b),grant).code,'stale_approval');
  b.approval.proposal_id='p1'; b.intent.text='changed'; b.approval.intent={...b.intent};
  assert.equal(w.execute(json(b),grant).code,'stale_approval');
  assert.equal(w.execute(raw,grant).status,'simulated');
  assert.equal(w.execute(raw,grant).status,'rejected');
  const fresh=action(w.snapshotForTest(),'fill');
  assert.equal(w.execute(json(fresh),w.approveForTest(json(fresh),9000)).code,'uncertain_receipt');
});
test('stale snapshots, wrong task, missing target, wrong roles and readonly controls fail',()=>{
  for(const change of [a=>a.intent.snapshot_id='old',a=>a.intent.task_id='other',a=>a.intent.target_id='missing',a=>a.intent.target_id='button1']) {
    const w=A.createSyntheticExecutor(json(snapshot())), a=action(snapshot(),'fill'); change(a); a.approval.intent={...a.intent};
    assert.throws(()=>w.approveForTest(json(a),9000));
    assert.equal(w.execute(json(a)).status,'rejected');
  }
  const s=snapshot(); s.elements[1].editable=false;
  assert.throws(()=>A.prepare(json(action(s,'fill')),json(s)),/unsupported_control/);
});
test('expired/future approvals and lifetime inversion fail',()=>{
  for(const [start,end] of [[101,9000],[0,100],[200,150]]) {
    const w=A.createSyntheticExecutor(json(snapshot())), a=action(snapshot(),'click');
    a.approval.approved_at_ms=start; a.approval.expires_at_ms=end;
    assert.throws(()=>w.approveForTest(json(a),end));
  }
});
test('runtime revocation, private context, foreground and cancellation deny before mutation',()=>{
  for(const [key,value,code] of [['enabled',false,'disabled'],['permission',false,'site_permission_denied'],['privateContext',true,'private_context'],['foreground',true,'foreground_preempted'],['cancelled',true,'cancelled']]) {
    const w=A.createSyntheticExecutor(json(snapshot())), raw=json(action(snapshot(),'click')), h=w.approveForTest(raw,9000);
    const c={enabled:true,permission:true,privateContext:false,foreground:false,cancelled:false,now:100}; c[key]=value;
    w.setContextForTest(json(c)); assert.equal(w.execute(raw,h).code,code);
    c[key]=!value; w.setContextForTest(json(c)); assert.equal(w.execute(raw,h).code,'stale_approval');
  }
});
test('all other approvals invalidate after even a wait; clock cannot move backward',()=>{
  const w=A.createSyntheticExecutor(json(snapshot())), raw=json(action(snapshot(),'click')), h=w.approveForTest(raw,9000);
  assert.equal(run(w,'wait','wait1').status,'simulated');
  assert.equal(w.execute(raw,h).code,'stale_snapshot');
  assert.throws(()=>w.setContextForTest(json({enabled:true,permission:true,privateContext:false,foreground:false,cancelled:false,now:0})));
});
test('navigation discards old targets and snapshots; back requires history',()=>{
  const w=A.createSyntheticExecutor(json(snapshot()));
  assert.equal(run(w,'back').code,'unsupported_control');
  assert.equal(run(w,'navigate','nav').status,'simulated');
  assert.deepEqual(w.snapshotForTest().elements,[]);
  assert.throws(()=>w.approveForTest(json(action(w.snapshotForTest(),'click')),9000),/stale_snapshot/);
  assert.equal(run(w,'back').status,'simulated');
  assert.equal(w.snapshotForTest().url,snapshot().url);
});
test('25-action budget enforced and no timers needed',()=>{
  const w=A.createSyntheticExecutor(json(snapshot()));
  for(let n=0;n<25;n++) { const a=action(w.snapshotForTest(),'scroll','a'+n);a.approval=null; assert.equal(w.execute(json(a)).status,'simulated'); }
  assert.equal(w.execute(json(action(w.snapshotForTest(),'scroll','last'))).code,'budget_exhausted');
});
test('closed payloads reject scripts, malformed command fields, arbitrary objects and live URLs',()=>{
  for(const mutate of [a=>a.intent.command='eval',a=>a.script='evil',a=>a.intent.text='javascript:evil',a=>a.intent.url='https://example.com/',a=>a.intent.url='javascript:evil']) {
    const w=A.createSyntheticExecutor(json(snapshot())), a=action(snapshot(),'navigate');mutate(a);a.approval.intent={...a.intent};
    assert.equal(w.execute(json(a)).status,'rejected');
  }
  let touched=false; const object={get intent(){touched=true;throw Error('secret');}};
  assert.throws(()=>A.prepare(object,json(snapshot()))); assert.equal(touched,false);
  assert.throws(()=>A.prepare('x'.repeat(262145),json(snapshot())));
  const s=snapshot();s.url='https://example.com/course';s.origin='https://example.com';
  assert.throws(()=>A.createSyntheticExecutor(json(s)),/site_permission_denied/);
});
test('A05 actual capture remains descriptive and cannot authorize fill',()=>{
  const e=E.createPublicExtractor(json({texts:[{id:'title',value:'Demo'},{id:'name',value:'Name'}],urls:[{id:'url',value:'https://demo.invalid/'}]}));
  const doc=e.createDocument(json({title:'title',url:'url',nodes:[{kind:'control',label:'name',role:'textbox'}]}));
  const s=e.capture(doc,json({task_id:'t1',captured_at_ms:100,enabled:true,site_permission:'granted',private_context:false,tab_role:'background'})).snapshot;
  const a=action(s,'fill'); a.intent.target_id=s.elements[0].target_id;a.approval.intent={...a.intent};
  assert.throws(()=>A.prepare(json(a),json(s)),/unsupported_control/);
});
test('generated snapshots never collide with initial or previously issued IDs',()=>{
  for(const initial of ['synthetic.s1','synthetic.s2','synthetic.s24']) {
    const s=snapshot(); s.id=initial; const w=A.createSyntheticExecutor(json(s));
    const seen=new Set([s.id]);
    for(let n=0;n<24;n++) {
      const previous=w.snapshotForTest(), a=action(previous,'scroll','step'+n); a.approval=null;
      assert.equal(w.execute(json(a)).status,'simulated');
      const current=w.snapshotForTest(); assert.ok(!seen.has(current.id)); seen.add(current.id);
      const stale=action(previous,'scroll','stale'+n); stale.approval=null;
      assert.equal(w.execute(json(stale)).code,'stale_snapshot');
    }
  }
});
test('navigation preserves A01 exact wire origins including default ports and uppercase hosts',()=>{
  const C=require('../../browser-extension/shared/contracts.js');
  for(const url of ['https://school.invalid:443/next','http://school.invalid:80/next','https://SCHOOL.invalid/next']) {
    const w=A.createSyntheticExecutor(json(snapshot())), a=action(snapshot(),'navigate');
    a.intent.url=url;a.approval.intent={...a.intent}; const raw=json(a);
    assert.equal(w.execute(raw,w.approveForTest(raw,9000)).status,'simulated');
    const s=w.snapshotForTest(); assert.equal(s.url,url); assert.doesNotThrow(()=>C.validate('BrowserSnapshot',s));
    assert.equal(run(w,'scroll','after').status,'simulated');
  }
});
