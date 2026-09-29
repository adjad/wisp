/* A13 preparatory, inert action executor. No DOM, browser, network, timer, or
 * adapter callbacks are used. This is NOT a production approval authority or an
 * ActionReceipt producer. All URLs must use reserved .invalid hosts.
 *
 * prepare() checks A01 payloads but cannot authenticate their provenance.
 * createSyntheticExecutor() owns isolated state and test-only approval handles.
 * Neither function may be wired to live browser effects. Production requires
 * A03 durable atomic approval consumption/revision checks, A06/A07 live target
 * resolution and runtime permissions, and A14 durable budgets/recovery. A05's
 * descriptive, noneditable manifest controls are intentionally not fill targets.
 *
 * A13 R2 additions: mapModelViewChoice() turns a ModelView choice number into a
 * navigate/scroll/back/wait/handoff action, rejecting out-of-view references.
 * The link navigation rule (in-scope HTTP(S) anchors need no approval) applies
 * only to an action bound to such a mapping. The consequential-pattern list is
 * NOT duplicated here: it is imported from model-view.js (single source of
 * truth shared with the A05/WP2 capture-side exclusion). The 'a14c' executor
 * profile is read-only: navigate/scroll/back/wait only.
 */
'use strict';
(function () {
  const C = typeof module !== 'undefined' && module.exports
    ? require('./contracts.js') : globalThis.WispBrowserContracts;
  const M = typeof module !== 'undefined' && module.exports
    ? require('./model-view.js') : globalThis.WispModelView;
  const A14C_VERBS = Object.freeze(['navigate', 'scroll', 'back', 'wait', 'handoff']);
  const MAX_SNAPSHOT_AGE_MS = 60000;
  const mappings = new WeakMap();
  const VERBS = Object.freeze(['click', 'scroll', 'fill', 'select', 'navigate', 'back', 'wait', 'open_tab']);
  const MAX_JSON = 262144;
  class ActionError extends Error {
    constructor(code) { super(code); this.name = 'ActionError'; this.code = code; }
  }
  function demand(ok, code = 'invalid_payload') { if (!ok) throw new ActionError(code); }
  function parse(raw) {
    demand(typeof raw === 'string' && raw.length <= MAX_JSON);
    try { return JSON.parse(raw); } catch (_) { throw new ActionError('invalid_payload'); }
  }
  function checked(name, raw) {
    try { return C.validate(name, parse(raw)); }
    catch (error) { throw new ActionError(error.code || 'invalid_payload'); }
  }
  function freeze(value) {
    if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
    return value;
  }
  function syntheticURL(raw) {
    const u = new URL(raw);
    demand(u.hostname.endsWith('.invalid') && !u.username && !u.password, 'site_permission_denied');
  }
  function httpScoped(rawURL, scope) {
    let u;
    try { u = new URL(rawURL); } catch (_) { return false; }
    return (u.protocol === 'http:' || u.protocol === 'https:') && scope.includes(u.origin);
  }
  // The consequential list is owned by model-view.js; recheck at the point of use.
  function consequential(url, label, site) {
    return M.consequentialReasons(url, label, site).length > 0;
  }
  /* Maps a ModelView choice to a fixed action. `choice` is a 1-based element
   * number from THIS view, or 'scroll' | 'back' | 'wait' | 'handoff'. Anything
   * else, including out-of-view numbers and forged views, is rejected. */
  function mapModelViewChoice(view, choice, ctx) {
    demand(ctx && typeof ctx === 'object' && !Array.isArray(ctx));
    const snapshot = C.validate('BrowserSnapshot', ctx.snapshot);
    const id = ctx.action_id;
    demand(typeof id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(id));
    demand(view && view.snapshot_id !== null && view.snapshot_id === snapshot.id &&
      view.observation_id === snapshot.observation_id, 'stale_snapshot');
    if (ctx.now_ms !== undefined) {
      demand(Number.isSafeInteger(ctx.now_ms) && ctx.now_ms >= snapshot.captured_at_ms &&
        ctx.now_ms - snapshot.captured_at_ms <= MAX_SNAPSHOT_AGE_MS, 'stale_snapshot');
    }
    const base = {action_id: id, task_id: snapshot.task_id, snapshot_id: snapshot.id,
      target_id: null, url: null, text: null, private_data: false, consequential: false};
    const wrap = intent => C.validate('BrowserAction',
      {schema_version: '1.0', intent, approval: null});
    if (choice === 'handoff') return freeze({kind: 'handoff', action: null, target_id: null});
    if (['scroll', 'back', 'wait'].includes(choice)) {
      const action = wrap({...base, command: choice});
      return freeze({kind: choice, action, target_id: null});
    }
    demand(Number.isSafeInteger(choice), 'invalid_payload');
    const resolved = M.resolveChoice(view, choice);
    demand(resolved !== null, 'out_of_view');
    const element = view.elements[choice - 1];
    const scope = ctx.scope_origins === undefined ? [new URL(snapshot.url).origin] : ctx.scope_origins;
    demand(Array.isArray(scope) && scope.length >= 1 && scope.every(o => typeof o === 'string'));
    demand(httpScoped(resolved.url, scope), 'out_of_scope');
    demand(!consequential(resolved.url, element.label, view.site), 'consequential_link');
    demand(snapshot.elements.some(e => e.target_id === resolved.target_id && e.role === 'link'), 'stale_snapshot');
    const action = wrap({...base, command: 'navigate', url: resolved.url});
    const mapping = {kind: 'navigate', action, target_id: resolved.target_id};
    mappings.set(mapping, {intent: action.intent, target_id: resolved.target_id, label: element.label,
      site: view.site, scope: scope.slice(), snapshot_id: snapshot.id, observation_id: snapshot.observation_id});
    return freeze(mapping);
  }
  // True only for a navigate bound to a live mapping that still passes every recheck.
  function linkRuleHolds(mapping, i, snapshot) {
    const m = mapping === null ? undefined : mappings.get(mapping);
    demand(mapping === null || m !== undefined);
    if (m === undefined || i.command !== 'navigate') return false;
    demand(m.snapshot_id === snapshot.id && m.observation_id === snapshot.observation_id, 'stale_snapshot');
    demand(Object.keys(m.intent).length === Object.keys(i).length &&
      Object.keys(m.intent).every(k => m.intent[k] === i[k]), 'stale_approval');
    demand(httpScoped(i.url, m.scope), 'out_of_scope');
    demand(!consequential(i.url, m.label, m.site), 'consequential_link');
    return !i.consequential && !i.private_data;
  }
  function prepare(actionJSON, snapshotJSON, mapping = null) {
    const action = checked('BrowserAction', actionJSON);
    C.validate('ActionIntent', action.intent);
    if (action.approval !== null) C.validate('ExactApproval', action.approval);
    const snapshot = checked('BrowserSnapshot', snapshotJSON);
    const i = action.intent;
    demand(VERBS.includes(i.command), 'unsupported_control');
    demand(i.snapshot_id === snapshot.id && i.task_id === snapshot.task_id, 'stale_snapshot');
    syntheticURL(snapshot.url);
    if (i.url !== null) syntheticURL(i.url);
    let target = null;
    if (i.target_id !== null) {
      target = snapshot.elements.find(e => e.target_id === i.target_id);
      demand(target, 'stale_snapshot');
      const roles = {click: ['button', 'link', 'checkbox', 'radio'], fill: ['textbox'], select: ['combobox']};
      demand(roles[i.command].includes(target.role), 'unsupported_control');
      demand(!['fill', 'select'].includes(i.command) || target.editable, 'unsupported_control');
    }
    // Even navigation/back/open-tab can initiate server effects. Unknown
    // semantics are gated; action-supplied effect flags cannot lower the gate.
    const linkRule = linkRuleHolds(mapping, i, snapshot);
    const approvalRequired = !linkRule &&
      (!['scroll', 'wait'].includes(i.command) || i.consequential || i.private_data);
    return freeze({mode: 'synthetic_only', action, snapshot, target, approvalRequired, linkRule,
      possibleAutosave: ['click', 'fill', 'select'].includes(i.command)});
  }
  function createSyntheticExecutor(snapshotJSON, options = {}) {
    const readOnly = options !== null && typeof options === 'object' && options.profile === 'a14c';
    if (!readOnly) demand(options !== null && typeof options === 'object' && options.profile === undefined);
    let snapshot = checked('BrowserSnapshot', snapshotJSON);
    syntheticURL(snapshot.url);
    snapshot = freeze(snapshot);
    let generation = 0, steps = 0, activeMS = 0, now = snapshot.captured_at_ms;
    let enabled = true, permission = true, privateContext = false, foreground = false, cancelled = false;
    let pageURL = snapshot.url, scrollY = 0, clicks = 0;
    const occluded = new Set(), values = new Map(), history = [], tabs = [], used = new Set(), grants = new WeakMap();
    let epoch = 0, snapshotSequence = 0;
    const snapshotIDs = new Set([snapshot.id]);
    function nextSnapshotID() {
      let id;
      do { id = 'synthetic.s' + (++snapshotSequence); } while (snapshotIDs.has(id));
      snapshotIDs.add(id);
      return id;
    }
    function result(status, code, pre = null, post = null) {
      // Never echo filled text, destinations, labels, approvals or exception data.
      return freeze({mode: 'synthetic_only', status, code, pre, post,
        completesObligation: false, retryAllowed: false});
    }
    function conditions() {
      return {generation, steps, activeMS, scrollY, clicks, tabCount: tabs.length,
        historyDepth: history.length};
    }
    function guards() {
      demand(!cancelled, 'cancelled'); demand(enabled, 'disabled');
      demand(permission, 'site_permission_denied'); demand(!privateContext, 'private_context');
      demand(!foreground, 'foreground_preempted');
      demand(steps < 25 && activeMS < 300000, 'budget_exhausted');
    }
    function plan(raw, mapping = null) { return prepare(raw, JSON.stringify(snapshot), mapping); }
    function fingerprint(p) { return JSON.stringify(p.action); }
    function approveForTest(actionJSON, expiresAtMS) {
      guards();
      const p = plan(actionJSON), a = p.action.approval;
      demand(a !== null, 'approval_required');
      demand(Number.isSafeInteger(expiresAtMS) && expiresAtMS > now &&
        expiresAtMS <= a.expires_at_ms && a.approved_at_ms <= now, 'stale_approval');
      const handle = Object.freeze(Object.create(null));
      grants.set(handle, {intent: fingerprint(p), epoch, expiresAtMS});
      return handle;
    }
    function execute(actionJSON, approvalHandle = null, mapping = null) {
      let pre = null;
      try {
        guards();
        if (readOnly) {
          const raw = parse(actionJSON);
          demand(raw && raw.intent && A14C_VERBS.includes(raw.intent.command), 'unsupported_control');
        }
        const p = plan(actionJSON, mapping), i = p.action.intent;
        if (readOnly) {
          demand(i.command !== 'navigate' || !consequential(i.url, null, 'canvas'), 'consequential_link');
        }
        // Freshness/occlusion recheck immediately before dispatch.
        if (p.linkRule) {
          const tid = mappings.get(mapping).target_id;
          demand(now - snapshot.captured_at_ms <= MAX_SNAPSHOT_AGE_MS, 'stale_snapshot');
          demand(snapshot.elements.some(e => e.target_id === tid), 'stale_snapshot');
          demand(!occluded.has(tid), 'target_occluded');
        }
        if (i.target_id !== null) demand(!occluded.has(i.target_id), 'target_occluded');
        demand(!used.has(i.action_id), 'uncertain_receipt');
        if (p.approvalRequired) {
          const grant = grants.get(approvalHandle);
          demand(grant !== undefined, 'approval_required');
          demand(grant.intent === fingerprint(p) && grant.epoch === epoch &&
            now < grant.expiresAtMS && p.action.approval !== null &&
            now < p.action.approval.expires_at_ms && p.action.approval.approved_at_ms <= now, 'stale_approval');
        }
        if (i.command === 'back') demand(history.length > 0, 'unsupported_control');
        if (i.command === 'wait') demand(activeMS + 1000 <= 300000, 'budget_exhausted');
        pre = conditions();
        // Synchronous, isolated mutation: consume before dispatch, never retry.
        if (p.approvalRequired) grants.delete(approvalHandle);
        used.add(i.action_id); steps += 1;
        switch (i.command) {
          case 'click': clicks += 1; break;
          case 'fill': case 'select': values.set(i.target_id, i.text); break;
          case 'scroll': scrollY += 600; break; // one fixed synthetic viewport
          case 'wait': activeMS += 1000; now += 1000; break; // logical time only
          case 'navigate': history.push(pageURL); pageURL = i.url; break;
          case 'back': pageURL = history.pop(); break;
          case 'open_tab': tabs.push(i.url); break;
        }
        generation += 1; epoch += 1; // every action invalidates all other grants
        const post = conditions();
        const predicates = {
          click: () => post.clicks === pre.clicks + 1,
          fill: () => values.get(i.target_id) === i.text,
          select: () => values.get(i.target_id) === i.text,
          scroll: () => post.scrollY === pre.scrollY + 600,
          wait: () => post.activeMS === pre.activeMS + 1000,
          navigate: () => pageURL === i.url && post.historyDepth === pre.historyDepth + 1,
          back: () => post.historyDepth === pre.historyDepth - 1,
          open_tab: () => tabs.at(-1) === i.url && post.tabCount === pre.tabCount + 1,
        };
        demand(predicates[i.command](), 'uncertain_receipt');
        // Synthetic revision; navigation discards all prior target descriptions.
        const navigated = ['navigate', 'back'].includes(i.command);
        snapshot = freeze({...snapshot, id: nextSnapshotID(),
          observation_id: 'synthetic.o' + generation, captured_at_ms: now,
          url: pageURL, origin: pageURL.match(/^https?:\/\/[^/\?#]+/)[0],
          elements: navigated ? [] : snapshot.elements});
        return result('simulated', null, pre, post);
      } catch (error) {
        return result(pre === null ? 'rejected' : 'uncertain', error instanceof ActionError ? error.code : 'invalid_payload', pre);
      }
    }
    function setContextForTest(raw) {
      const c = parse(raw), keys = ['enabled', 'permission', 'privateContext', 'foreground', 'cancelled', 'now'];
      demand(c && !Array.isArray(c) && Object.keys(c).length === keys.length && keys.every(k => Object.hasOwn(c, k)));
      demand(keys.slice(0, 5).every(k => typeof c[k] === 'boolean'));
      demand(Number.isSafeInteger(c.now) && c.now >= now);
      ({enabled, permission, privateContext, foreground, cancelled, now} = c);
      epoch += 1; // revocation followed by reenablement cannot revive a grant
    }
    function setOccludedForTest(raw) {
      const ids = parse(raw);
      demand(Array.isArray(ids) && ids.every(x => typeof x === 'string'));
      occluded.clear(); ids.forEach(x => occluded.add(x));
    }
    return Object.freeze({execute, approveForTest, setContextForTest, setOccludedForTest,
      snapshotForTest: () => snapshot});
  }
  const api = Object.freeze({prepare, createSyntheticExecutor, mapModelViewChoice, ActionError, VERBS, A14C_VERBS});
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.WispSyntheticActions = api;
})();
