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
 */
'use strict';
(function () {
  const C = typeof module !== 'undefined' && module.exports
    ? require('./contracts.js') : globalThis.WispBrowserContracts;
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
  function prepare(actionJSON, snapshotJSON) {
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
    const approvalRequired = !['scroll', 'wait'].includes(i.command) || i.consequential || i.private_data;
    return freeze({mode: 'synthetic_only', action, snapshot, target, approvalRequired,
      possibleAutosave: ['click', 'fill', 'select'].includes(i.command)});
  }
  function createSyntheticExecutor(snapshotJSON) {
    let snapshot = checked('BrowserSnapshot', snapshotJSON);
    syntheticURL(snapshot.url);
    snapshot = freeze(snapshot);
    let generation = 0, steps = 0, activeMS = 0, now = snapshot.captured_at_ms;
    let enabled = true, permission = true, privateContext = false, foreground = false, cancelled = false;
    let pageURL = snapshot.url, scrollY = 0, clicks = 0;
    const values = new Map(), history = [], tabs = [], used = new Set(), grants = new WeakMap();
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
    function plan(raw) { return prepare(raw, JSON.stringify(snapshot)); }
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
    function execute(actionJSON, approvalHandle = null) {
      let pre = null;
      try {
        guards();
        const p = plan(actionJSON), i = p.action.intent;
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
    return Object.freeze({execute, approveForTest, setContextForTest,
      snapshotForTest: () => snapshot});
  }
  const api = Object.freeze({prepare, createSyntheticExecutor, ActionError, VERBS});
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else globalThis.WispSyntheticActions = api;
})();
