// A01 canonical schema mirror. Regenerate with scripts/check_browser_contracts.py --sync-schema.
// BEGIN SCHEMA
const SCHEMA = {"ActionIntent":{"fields":{"action_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"command":{"type":"enum","values":["snapshot","navigate","click","fill","select","scroll","back","wait","open_tab","handoff"]},"consequential":{"type":"boolean"},"private_data":{"type":"boolean"},"snapshot_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"target_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"task_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"text":{"item":{"max":32768,"min":0,"type":"string"},"type":"nullable"},"url":{"item":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"},"type":"nullable"}},"type":"object"},"ActionProposal":{"fields":{"evidence_ids":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"max":256,"min":1,"type":"array"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"intent":{"name":"ActionIntent","type":"ref"},"item_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"item_revision":{"max":9007199254740991,"min":1,"type":"integer"},"rationale":{"max":2048,"min":1,"type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"state":{"type":"enum","values":["proposed","approved","rejected","expired"]}},"type":"object"},"ActionReceipt":{"fields":{"action_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"completes_obligation":{"type":"boolean"},"error":{"item":{"name":"ContractError","type":"ref"},"type":"nullable"},"evidence":{"item":{"name":"Evidence","type":"ref"},"max":256,"min":0,"type":"array"},"external_record_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"proposal_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"recorded_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"schema_version":{"type":"enum","values":["1.0"]},"status":{"type":"enum","values":["verified","uncertain","failed","cancelled"]},"task_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"ActionableItem":{"fields":{"ambiguity":{"item":{"max":2048,"min":1,"type":"string"},"type":"nullable"},"completion_receipt_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"due_at_ms":{"item":{"max":9007199254740991,"min":0,"type":"integer"},"type":"nullable"},"due_timezone":{"item":{"max":128,"min":1,"type":"string"},"type":"nullable"},"evidence":{"item":{"name":"Evidence","type":"ref"},"max":256,"min":1,"type":"array"},"external_record_ids":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"max":256,"min":0,"type":"array"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"kind":{"type":"enum","values":["assignment","exam","scheduling","follow_up"]},"revision":{"max":9007199254740991,"min":1,"type":"integer"},"schema_version":{"type":"enum","values":["1.0"]},"state":{"type":"enum","values":["candidate","needs_clarification","tracked","completed","dismissed"]},"supersedes_revision":{"item":{"max":9007199254740991,"min":1,"type":"integer"},"type":"nullable"},"title":{"max":512,"min":1,"type":"string"}},"type":"object"},"BrowserAction":{"fields":{"approval":{"item":{"name":"ExactApproval","type":"ref"},"type":"nullable"},"intent":{"name":"ActionIntent","type":"ref"},"schema_version":{"type":"enum","values":["1.0"]}},"type":"object"},"BrowserElement":{"fields":{"editable":{"type":"boolean"},"label":{"max":512,"min":0,"type":"string"},"role":{"max":64,"min":1,"type":"string"},"target_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"BrowserSnapshot":{"fields":{"captured_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"content_mode":{"type":"enum","values":["dom_text"]},"elements":{"item":{"name":"BrowserElement","type":"ref"},"max":256,"min":0,"type":"array"},"enabled":{"type":"enum","values":[true]},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"observation_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"origin":{"max":512,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$","type":"string"},"private_context":{"type":"enum","values":[false]},"schema_version":{"type":"enum","values":["1.0"]},"site_permission":{"type":"enum","values":["granted"]},"tab_role":{"type":"enum","values":["background"]},"task_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"url":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"}},"type":"object"},"BrowserTask":{"fields":{"actions_used":{"max":25,"min":0,"type":"integer"},"active_ms":{"max":300000,"min":0,"type":"integer"},"consecutive_no_progress":{"max":3,"min":0,"type":"integer"},"error":{"item":{"name":"ContractError","type":"ref"},"type":"nullable"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"item_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"policy":{"name":"InvestigationPolicy","type":"ref"},"schema_version":{"type":"enum","values":["1.0"]},"state":{"type":"enum","values":["queued","running","waiting_approval","waiting_user","paused_foreground","handed_off","succeeded","failed","cancelled","outcome_unknown"]}},"type":"object"},"ContractError":{"fields":{"code":{"type":"enum","values":["incompatible_version","missing_capability","invalid_payload","disabled","site_permission_denied","private_context","foreground_preempted","budget_exhausted","no_progress","unsupported_control","approval_required","stale_approval","stale_snapshot","uncertain_receipt","bridge_unauthorized","cancelled"]},"message":{"max":1024,"min":1,"type":"string"},"retryable":{"type":"boolean"},"schema_version":{"type":"enum","values":["1.0"]}},"type":"object"},"Evidence":{"fields":{"captured_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"observation_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"quote":{"max":8192,"min":1,"type":"string"},"source_revision":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"ExactApproval":{"fields":{"approved_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"authority":{"type":"enum","values":["app"]},"expires_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"intent":{"name":"ActionIntent","type":"ref"},"proposal_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"ExternalRecord":{"fields":{"evidence_ids":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"max":256,"min":1,"type":"array"},"external_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"revision":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"system":{"type":"enum","values":["browser","calendar","reminder","mail"]},"url":{"item":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"},"type":"nullable"}},"type":"object"},"Handshake":{"fields":{"capabilities":{"item":{"type":"enum","values":["dom_text","exact_app_approval","verified_receipts","local_discovery"]},"max":16,"min":0,"type":"array"},"credential_role":{"type":"enum","values":["native_bridge","app_approval"]},"peer":{"type":"enum","values":["app","service","extension"]},"required_capabilities":{"item":{"type":"enum","values":["dom_text","exact_app_approval","verified_receipts","local_discovery"]},"max":16,"min":0,"type":"array"},"schema_version":{"type":"enum","values":["1.0"]},"supported_versions":{"item":{"max":16,"min":1,"type":"string"},"max":16,"min":1,"type":"array"}},"type":"object"},"InvestigationPolicy":{"fields":{"approval_authority":{"type":"enum","values":["app"]},"arbitrary_javascript":{"type":"enum","values":[false]},"bridge_credential_role":{"type":"enum","values":["native_bridge"]},"exclude_private_at_capture":{"type":"enum","values":[true]},"exclude_private_at_service":{"type":"enum","values":[true]},"explicit_enablement":{"type":"enum","values":[true]},"foreground_priority":{"type":"enum","values":[true]},"max_actions":{"type":"enum","values":[25]},"max_active_ms":{"type":"enum","values":[300000]},"max_no_progress":{"type":"enum","values":[3]},"max_proactive_tasks":{"type":"enum","values":[1]},"model_location":{"type":"enum","values":["local"]},"model_replaceable":{"type":"enum","values":[true]},"observation_mode":{"type":"enum","values":["dom_text"]},"separate_background_tab":{"type":"enum","values":[true]},"site_permission_required":{"type":"enum","values":[true]},"unsupported_visual":{"type":"enum","values":["handoff"]},"user_wait_counts":{"type":"enum","values":[false]}},"type":"object"},"NegotiatedCapabilities":{"fields":{"capabilities":{"item":{"type":"enum","values":["dom_text","exact_app_approval","verified_receipts","local_discovery"]},"max":16,"min":0,"type":"array"},"schema_version":{"type":"enum","values":["1.0"]}},"type":"object"},"ScheduledBlock":{"fields":{"end_ms":{"max":9007199254740991,"min":0,"type":"integer"},"external_record_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"obligation_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"start_ms":{"max":9007199254740991,"min":0,"type":"integer"},"state":{"type":"enum","values":["proposed","scheduled","cancelled"]},"timezone":{"max":128,"min":1,"type":"string"}},"type":"object"},"SourceObservation":{"fields":{"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"observed_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"private_context":{"type":"enum","values":[false]},"revision":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"source_kind":{"type":"enum","values":["browser","mail","calendar","reminder","manual"]},"source_record_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"source_url":{"item":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"},"type":"nullable"},"text":{"max":32768,"min":0,"type":"string"},"title":{"max":512,"min":1,"type":"string"}},"type":"object"}};
// END SCHEMA

// BEGIN RECORDS
/**
 * @typedef {Object} NegotiatedCapabilities
 * @property {("1.0")} schema_version
 * @property {Array<("dom_text"|"exact_app_approval"|"verified_receipts"|"local_discovery")>} capabilities
 */
/**
 * @typedef {Object} Handshake
 * @property {("1.0")} schema_version
 * @property {("app"|"service"|"extension")} peer
 * @property {Array<string>} supported_versions
 * @property {Array<("dom_text"|"exact_app_approval"|"verified_receipts"|"local_discovery")>} capabilities
 * @property {Array<("dom_text"|"exact_app_approval"|"verified_receipts"|"local_discovery")>} required_capabilities
 * @property {("native_bridge"|"app_approval")} credential_role
 */
/**
 * @typedef {Object} ContractError
 * @property {("1.0")} schema_version
 * @property {("incompatible_version"|"missing_capability"|"invalid_payload"|"disabled"|"site_permission_denied"|"private_context"|"foreground_preempted"|"budget_exhausted"|"no_progress"|"unsupported_control"|"approval_required"|"stale_approval"|"stale_snapshot"|"uncertain_receipt"|"bridge_unauthorized"|"cancelled")} code
 * @property {string} message
 * @property {boolean} retryable
 */
/**
 * @typedef {Object} SourceObservation
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {("browser"|"mail"|"calendar"|"reminder"|"manual")} source_kind
 * @property {(string|null)} source_url
 * @property {(string|null)} source_record_id
 * @property {string} revision
 * @property {number} observed_at_ms
 * @property {string} title
 * @property {string} text
 * @property {(false)} private_context
 */
/**
 * @typedef {Object} Evidence
 * @property {string} id
 * @property {string} observation_id
 * @property {string} source_revision
 * @property {string} quote
 * @property {number} captured_at_ms
 */
/**
 * @typedef {Object} BrowserElement
 * @property {string} target_id
 * @property {string} role
 * @property {string} label
 * @property {boolean} editable
 */
/**
 * @typedef {Object} BrowserSnapshot
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {string} task_id
 * @property {string} observation_id
 * @property {string} url
 * @property {string} origin
 * @property {number} captured_at_ms
 * @property {(true)} enabled
 * @property {("granted")} site_permission
 * @property {(false)} private_context
 * @property {("background")} tab_role
 * @property {("dom_text")} content_mode
 * @property {Array<BrowserElement>} elements
 */
/**
 * @typedef {Object} ActionIntent
 * @property {string} action_id
 * @property {string} task_id
 * @property {string} snapshot_id
 * @property {("snapshot"|"navigate"|"click"|"fill"|"select"|"scroll"|"back"|"wait"|"open_tab"|"handoff")} command
 * @property {(string|null)} target_id
 * @property {(string|null)} url
 * @property {(string|null)} text
 * @property {boolean} private_data
 * @property {boolean} consequential
 */
/**
 * @typedef {Object} ExactApproval
 * @property {string} proposal_id
 * @property {("app")} authority
 * @property {number} approved_at_ms
 * @property {number} expires_at_ms
 * @property {ActionIntent} intent
 */
/**
 * @typedef {Object} BrowserAction
 * @property {("1.0")} schema_version
 * @property {ActionIntent} intent
 * @property {(ExactApproval|null)} approval
 */
/**
 * @typedef {Object} InvestigationPolicy
 * @property {(true)} explicit_enablement
 * @property {(true)} site_permission_required
 * @property {(true)} exclude_private_at_capture
 * @property {(true)} exclude_private_at_service
 * @property {(true)} separate_background_tab
 * @property {(1)} max_proactive_tasks
 * @property {(true)} foreground_priority
 * @property {(25)} max_actions
 * @property {(300000)} max_active_ms
 * @property {(false)} user_wait_counts
 * @property {(3)} max_no_progress
 * @property {("local")} model_location
 * @property {(true)} model_replaceable
 * @property {("dom_text")} observation_mode
 * @property {("handoff")} unsupported_visual
 * @property {(false)} arbitrary_javascript
 * @property {("native_bridge")} bridge_credential_role
 * @property {("app")} approval_authority
 */
/**
 * @typedef {Object} BrowserTask
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {(string|null)} item_id
 * @property {("queued"|"running"|"waiting_approval"|"waiting_user"|"paused_foreground"|"handed_off"|"succeeded"|"failed"|"cancelled"|"outcome_unknown")} state
 * @property {InvestigationPolicy} policy
 * @property {number} actions_used
 * @property {number} active_ms
 * @property {number} consecutive_no_progress
 * @property {(ContractError|null)} error
 */
/**
 * @typedef {Object} ExternalRecord
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {("browser"|"calendar"|"reminder"|"mail")} system
 * @property {string} external_id
 * @property {(string|null)} url
 * @property {string} revision
 * @property {Array<string>} evidence_ids
 */
/**
 * @typedef {Object} ScheduledBlock
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {string} obligation_id
 * @property {number} start_ms
 * @property {number} end_ms
 * @property {string} timezone
 * @property {("proposed"|"scheduled"|"cancelled")} state
 * @property {(string|null)} external_record_id
 */
/**
 * @typedef {Object} ActionableItem
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {("assignment"|"exam"|"scheduling"|"follow_up")} kind
 * @property {string} title
 * @property {("candidate"|"needs_clarification"|"tracked"|"completed"|"dismissed")} state
 * @property {number} revision
 * @property {(number|null)} supersedes_revision
 * @property {(number|null)} due_at_ms
 * @property {(string|null)} due_timezone
 * @property {(string|null)} ambiguity
 * @property {Array<Evidence>} evidence
 * @property {Array<string>} external_record_ids
 * @property {(string|null)} completion_receipt_id
 */
/**
 * @typedef {Object} ActionProposal
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {string} item_id
 * @property {number} item_revision
 * @property {ActionIntent} intent
 * @property {string} rationale
 * @property {Array<string>} evidence_ids
 * @property {("proposed"|"approved"|"rejected"|"expired")} state
 */
/**
 * @typedef {Object} ActionReceipt
 * @property {("1.0")} schema_version
 * @property {string} id
 * @property {string} action_id
 * @property {string} task_id
 * @property {(string|null)} proposal_id
 * @property {("verified"|"uncertain"|"failed"|"cancelled")} status
 * @property {number} recorded_at_ms
 * @property {Array<Evidence>} evidence
 * @property {(string|null)} external_record_id
 * @property {boolean} completes_obligation
 * @property {(ContractError|null)} error
 */
// END RECORDS

class ContractViolation extends Error {
  constructor(message, code = 'invalid_payload') { super(message); this.code = code; }
}
function requireContract(condition, message, code = 'invalid_payload') {
  if (!condition) throw new ContractViolation(message, code);
}
function equal(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object') return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(k => Object.hasOwn(b, k) && equal(a[k], b[k]));
}
function check(spec, value, path) {
  const kind = spec.type;
  if (kind === 'ref') { validate(spec.name, value); return; }
  if (kind === 'nullable') { if (value !== null) check(spec.item, value, path); return; }
  let valid = false;
  if (kind === 'enum') valid = spec.values.some(v => v === value);
  if (kind === 'string') valid = typeof value === 'string' && [...value].length >= spec.min &&
    [...value].length <= spec.max && ![...value].some(c => c.codePointAt(0) >= 0xD800 && c.codePointAt(0) <= 0xDFFF) && (!spec.pattern || new RegExp(spec.pattern).exec(value)?.[0] === value);
  if (kind === 'boolean') valid = typeof value === 'boolean';
  if (kind === 'integer') valid = Number.isSafeInteger(value) && value >= spec.min && value <= spec.max;
  if (kind === 'array') {
    valid = Array.isArray(value) && value.length >= spec.min && value.length <= spec.max;
    if (valid) {
      for (let index = 0; index < value.length; index++) {
        requireContract(Object.hasOwn(value, index), `Missing ${path}[${index}]`);
        check(spec.item, value[index], path + '[]');
      }
    }
  }
  if (kind === 'object') {
    valid = value !== null && typeof value === 'object' && !Array.isArray(value) &&
      equal(Object.keys(value).sort(), Object.keys(spec.fields).sort());
    if (valid) Object.entries(spec.fields).forEach(([k, s]) => check(s, value[k], path + '.' + k));
  }
  requireContract(valid, `Invalid ${path}`);
}
function validate(name, p) {
  requireContract(Object.hasOwn(SCHEMA, name), 'Unknown contract');
  if (p && Object.hasOwn(p, 'schema_version'))
    requireContract(p.schema_version === '1.0', 'Unsupported schema version', 'incompatible_version');
  check(SCHEMA[name], p, name);
  if (name === 'Handshake') {
    requireContract(p.supported_versions.includes('1.0'), 'No compatible version', 'incompatible_version');
    for (const k of ['supported_versions', 'capabilities', 'required_capabilities'])
      requireContract(new Set(p[k]).size === p[k].length, 'Duplicate negotiation entry');
    requireContract(p.peer === 'app' || p.credential_role === 'native_bridge', 'Bridge cannot grant app authority');
  } else if (name === 'SourceObservation') {
    requireContract(p.source_kind !== 'browser' || p.source_url !== null, 'Browser source needs URL');
  } else if (name === 'BrowserSnapshot') {
    // Match the wire origin exactly; URL normalization must not change approval bindings.
    requireContract(p.origin === p.url.match(/^https?:\/\/[^/\?#]+/)[0], 'Snapshot origin mismatch');
    requireContract(new Set(p.elements.map(e => e.target_id)).size === p.elements.length, 'Duplicate target');
  } else if (name === 'ActionIntent') {
    requireContract((p.target_id !== null) === ['click', 'fill', 'select'].includes(p.command), 'Target/command mismatch');
    requireContract((p.url !== null) === ['navigate', 'open_tab'].includes(p.command), 'URL/command mismatch');
    requireContract((p.text !== null) === ['fill', 'select'].includes(p.command), 'Text/command mismatch');
    requireContract(!p.private_data || p.command === 'fill', 'Private data requires typing');
  } else if (name === 'ExactApproval') {
    requireContract(p.expires_at_ms > p.approved_at_ms, 'Invalid approval lifetime');
  } else if (name === 'BrowserAction') {
    const i = p.intent, a = p.approval;
    requireContract(!(i.consequential || i.private_data || ['click','fill','select'].includes(i.command)) || a !== null,
      'Exact app approval required', 'approval_required');
    requireContract(a === null || equal(a.intent, i), 'Approval intent mismatch', 'stale_approval');
  } else if (name === 'BrowserTask') {
    const exhausted = p.actions_used === 25 || p.active_ms === 300000 || p.consecutive_no_progress === 3;
    requireContract(!exhausted || !['queued', 'running'].includes(p.state), 'Exhausted task cannot run');
  } else if (name === 'ScheduledBlock') {
    requireContract(p.end_ms > p.start_ms, 'Invalid block interval');
  } else if (name === 'ActionableItem') {
    requireContract((p.due_at_ms === null) === (p.due_timezone === null), 'Deadline timezone required');
    requireContract(p.supersedes_revision === null || p.supersedes_revision < p.revision, 'Invalid revision chain');
    requireContract(p.state !== 'needs_clarification' || p.ambiguity !== null, 'Missing ambiguity');
    requireContract(p.ambiguity === null || ['candidate', 'needs_clarification', 'dismissed'].includes(p.state), 'Unresolved item');
    requireContract((p.completion_receipt_id !== null) === (p.state === 'completed'), 'Completion needs receipt');
  } else if (name === 'ActionReceipt') {
    requireContract(p.status !== 'verified' || p.evidence.length > 0, 'Verification needs evidence');
    requireContract(!p.completes_obligation || p.status === 'verified', 'Uncertain receipt cannot complete obligation');
    requireContract(p.status !== 'verified' || p.error === null, 'Verified receipt cannot carry error');
  }
  return JSON.parse(JSON.stringify(p));
}
function negotiate(local, remote) {
  const a = validate('Handshake', local), b = validate('Handshake', remote);
  const common = a.capabilities.filter(c => b.capabilities.includes(c)).sort();
  requireContract([...a.required_capabilities, ...b.required_capabilities].every(c => common.includes(c)),
    'Required capability unavailable', 'missing_capability');
  return {schema_version: '1.0', capabilities: common};
}
function validateProposal(item, proposal) {
  const i = validate('ActionableItem', item), p = validate('ActionProposal', proposal);
  requireContract(p.item_id === i.id && p.item_revision === i.revision, 'Stale proposal');
  requireContract(p.evidence_ids.every(id => i.evidence.some(e => e.id === id)), 'Ungrounded proposal');
  requireContract(i.state === 'tracked', 'Proposal requires a resolved tracked obligation');
}
function validateCompletion(item, receipt, proposal) {
  const i = validate('ActionableItem', item), r = validate('ActionReceipt', receipt), p = validate('ActionProposal', proposal);
  requireContract(i.state === 'completed' && i.completion_receipt_id === r.id, 'Receipt link mismatch');
  requireContract(r.status === 'verified' && r.completes_obligation, 'Receipt does not prove completion');
  requireContract(r.proposal_id === p.id && r.action_id === p.intent.action_id && r.task_id === p.intent.task_id, 'Receipt action mismatch');
  requireContract(p.item_id === i.id && p.item_revision === i.revision, 'Receipt item mismatch');
  requireContract(p.state === 'approved', 'Completion requires approved proposal');
  requireContract(p.evidence_ids.every(id => i.evidence.some(e => e.id === id)), 'Ungrounded completion proposal');
}
// Pure service interface: adapters implement negotiate/snapshot/execute/cancel;
// discovery implements extract/reconcile/propose/reconcileReceipt. No adapter is installed here.
const BrowserContracts = Object.freeze({SCHEMA, validate, negotiate, validateProposal, validateCompletion, ContractViolation});
if (typeof module !== 'undefined' && module.exports) module.exports = BrowserContracts;
if (typeof globalThis !== 'undefined') globalThis.WispBrowserContracts = BrowserContracts;

/**
 * @typedef {Object} BrowserService
 * @property {function(Handshake): Promise<NegotiatedCapabilities>} negotiate
 * @property {function(string): Promise<BrowserSnapshot>} snapshot
 * @property {function(BrowserAction): Promise<ActionReceipt>} execute
 * @property {function(string): Promise<BrowserTask>} cancel
 */
/**
 * @typedef {Object} DiscoveryService
 * @property {function(SourceObservation): Promise<Array<ActionableItem>>} extract
 * @property {function(Array<ActionableItem>): Promise<Array<ActionableItem>>} reconcile
 * @property {function(string, number): Promise<ActionProposal>} propose
 * @property {function(ActionReceipt): Promise<ActionableItem>} reconcileReceipt
 */
