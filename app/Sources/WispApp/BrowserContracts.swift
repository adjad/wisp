import Foundation
import CoreFoundation

// A01 canonical schema mirror. No browser or bridge implementation.
// BEGIN SCHEMA
private let browserSchemaJSON = #"{"ActionIntent":{"fields":{"action_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"command":{"type":"enum","values":["snapshot","navigate","click","fill","select","scroll","back","wait","open_tab","handoff"]},"consequential":{"type":"boolean"},"private_data":{"type":"boolean"},"snapshot_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"target_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"task_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"text":{"item":{"max":32768,"min":0,"type":"string"},"type":"nullable"},"url":{"item":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"},"type":"nullable"}},"type":"object"},"ActionProposal":{"fields":{"evidence_ids":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"max":256,"min":1,"type":"array"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"intent":{"name":"ActionIntent","type":"ref"},"item_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"item_revision":{"max":9007199254740991,"min":1,"type":"integer"},"rationale":{"max":2048,"min":1,"type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"state":{"type":"enum","values":["proposed","approved","rejected","expired"]}},"type":"object"},"ActionReceipt":{"fields":{"action_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"completes_obligation":{"type":"boolean"},"error":{"item":{"name":"ContractError","type":"ref"},"type":"nullable"},"evidence":{"item":{"name":"Evidence","type":"ref"},"max":256,"min":0,"type":"array"},"external_record_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"proposal_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"recorded_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"schema_version":{"type":"enum","values":["1.0"]},"status":{"type":"enum","values":["verified","uncertain","failed","cancelled"]},"task_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"ActionableItem":{"fields":{"ambiguity":{"item":{"max":2048,"min":1,"type":"string"},"type":"nullable"},"completion_receipt_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"due_at_ms":{"item":{"max":9007199254740991,"min":0,"type":"integer"},"type":"nullable"},"due_timezone":{"item":{"max":128,"min":1,"type":"string"},"type":"nullable"},"evidence":{"item":{"name":"Evidence","type":"ref"},"max":256,"min":1,"type":"array"},"external_record_ids":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"max":256,"min":0,"type":"array"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"kind":{"type":"enum","values":["assignment","exam","scheduling","follow_up"]},"revision":{"max":9007199254740991,"min":1,"type":"integer"},"schema_version":{"type":"enum","values":["1.0"]},"state":{"type":"enum","values":["candidate","needs_clarification","tracked","completed","dismissed"]},"supersedes_revision":{"item":{"max":9007199254740991,"min":1,"type":"integer"},"type":"nullable"},"title":{"max":512,"min":1,"type":"string"}},"type":"object"},"BrowserAction":{"fields":{"approval":{"item":{"name":"ExactApproval","type":"ref"},"type":"nullable"},"intent":{"name":"ActionIntent","type":"ref"},"schema_version":{"type":"enum","values":["1.0"]}},"type":"object"},"BrowserElement":{"fields":{"editable":{"type":"boolean"},"label":{"max":512,"min":0,"type":"string"},"role":{"max":64,"min":1,"type":"string"},"target_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"BrowserSnapshot":{"fields":{"captured_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"content_mode":{"type":"enum","values":["dom_text"]},"elements":{"item":{"name":"BrowserElement","type":"ref"},"max":256,"min":0,"type":"array"},"enabled":{"type":"enum","values":[true]},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"observation_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"origin":{"max":512,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$","type":"string"},"private_context":{"type":"enum","values":[false]},"schema_version":{"type":"enum","values":["1.0"]},"site_permission":{"type":"enum","values":["granted"]},"tab_role":{"type":"enum","values":["background"]},"task_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"url":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"}},"type":"object"},"BrowserTask":{"fields":{"actions_used":{"max":25,"min":0,"type":"integer"},"active_ms":{"max":300000,"min":0,"type":"integer"},"consecutive_no_progress":{"max":3,"min":0,"type":"integer"},"error":{"item":{"name":"ContractError","type":"ref"},"type":"nullable"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"item_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"policy":{"name":"InvestigationPolicy","type":"ref"},"schema_version":{"type":"enum","values":["1.0"]},"state":{"type":"enum","values":["queued","running","waiting_approval","waiting_user","paused_foreground","handed_off","succeeded","failed","cancelled","outcome_unknown"]}},"type":"object"},"ContractError":{"fields":{"code":{"type":"enum","values":["incompatible_version","missing_capability","invalid_payload","disabled","site_permission_denied","private_context","foreground_preempted","budget_exhausted","no_progress","unsupported_control","approval_required","stale_approval","stale_snapshot","uncertain_receipt","bridge_unauthorized","cancelled"]},"message":{"max":1024,"min":1,"type":"string"},"retryable":{"type":"boolean"},"schema_version":{"type":"enum","values":["1.0"]}},"type":"object"},"Evidence":{"fields":{"captured_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"observation_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"quote":{"max":8192,"min":1,"type":"string"},"source_revision":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"ExactApproval":{"fields":{"approved_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"authority":{"type":"enum","values":["app"]},"expires_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"intent":{"name":"ActionIntent","type":"ref"},"proposal_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"}},"type":"object"},"ExternalRecord":{"fields":{"evidence_ids":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"max":256,"min":1,"type":"array"},"external_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"revision":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"system":{"type":"enum","values":["browser","calendar","reminder","mail"]},"url":{"item":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"},"type":"nullable"}},"type":"object"},"Handshake":{"fields":{"capabilities":{"item":{"type":"enum","values":["dom_text","exact_app_approval","verified_receipts","local_discovery"]},"max":16,"min":0,"type":"array"},"credential_role":{"type":"enum","values":["native_bridge","app_approval"]},"peer":{"type":"enum","values":["app","service","extension"]},"required_capabilities":{"item":{"type":"enum","values":["dom_text","exact_app_approval","verified_receipts","local_discovery"]},"max":16,"min":0,"type":"array"},"schema_version":{"type":"enum","values":["1.0"]},"supported_versions":{"item":{"max":16,"min":1,"type":"string"},"max":16,"min":1,"type":"array"}},"type":"object"},"InvestigationPolicy":{"fields":{"approval_authority":{"type":"enum","values":["app"]},"arbitrary_javascript":{"type":"enum","values":[false]},"bridge_credential_role":{"type":"enum","values":["native_bridge"]},"exclude_private_at_capture":{"type":"enum","values":[true]},"exclude_private_at_service":{"type":"enum","values":[true]},"explicit_enablement":{"type":"enum","values":[true]},"foreground_priority":{"type":"enum","values":[true]},"max_actions":{"type":"enum","values":[25]},"max_active_ms":{"type":"enum","values":[300000]},"max_no_progress":{"type":"enum","values":[3]},"max_proactive_tasks":{"type":"enum","values":[1]},"model_location":{"type":"enum","values":["local"]},"model_replaceable":{"type":"enum","values":[true]},"observation_mode":{"type":"enum","values":["dom_text"]},"separate_background_tab":{"type":"enum","values":[true]},"site_permission_required":{"type":"enum","values":[true]},"unsupported_visual":{"type":"enum","values":["handoff"]},"user_wait_counts":{"type":"enum","values":[false]}},"type":"object"},"NegotiatedCapabilities":{"fields":{"capabilities":{"item":{"type":"enum","values":["dom_text","exact_app_approval","verified_receipts","local_discovery"]},"max":16,"min":0,"type":"array"},"schema_version":{"type":"enum","values":["1.0"]}},"type":"object"},"ScheduledBlock":{"fields":{"end_ms":{"max":9007199254740991,"min":0,"type":"integer"},"external_record_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"obligation_id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"start_ms":{"max":9007199254740991,"min":0,"type":"integer"},"state":{"type":"enum","values":["proposed","scheduled","cancelled"]},"timezone":{"max":128,"min":1,"type":"string"}},"type":"object"},"SourceObservation":{"fields":{"id":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"observed_at_ms":{"max":9007199254740991,"min":0,"type":"integer"},"private_context":{"type":"enum","values":[false]},"revision":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"schema_version":{"type":"enum","values":["1.0"]},"source_kind":{"type":"enum","values":["browser","mail","calendar","reminder","manual"]},"source_record_id":{"item":{"max":128,"min":1,"pattern":"^[A-Za-z0-9][A-Za-z0-9._:-]*$","type":"string"},"type":"nullable"},"source_url":{"item":{"max":4096,"min":1,"pattern":"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?(#[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$","type":"string"},"type":"nullable"},"text":{"max":32768,"min":0,"type":"string"},"title":{"max":512,"min":1,"type":"string"}},"type":"object"}}"#
// END SCHEMA

protocol BrowserWireRecord: Codable { static var contractName: String { get } }

struct BrowserContractViolation: Error {
    let message: String
    let code: String
}

enum WispBrowserContracts {
    // This schema describes payloads; it does not authenticate transport or consent.
    static let schema = try! JSONSerialization.jsonObject(with: Data(browserSchemaJSON.utf8)) as! [String: Any]

    static func require(_ condition: Bool, _ message: String, _ code: String = "invalid_payload") throws {
        if !condition { throw BrowserContractViolation(message: message, code: code) }
    }
    static func isBool(_ value: Any) -> Bool {
        guard let n = value as? NSNumber else { return false }
        return CFGetTypeID(n) == CFBooleanGetTypeID()
    }
    static func equal(_ a: Any, _ b: Any) -> Bool {
        if isBool(a) != isBool(b) { return false }
        if a is NSNull || b is NSNull { return a is NSNull && b is NSNull }
        if let a = a as? [String: Any], let b = b as? [String: Any] {
            return Set(a.keys) == Set(b.keys) && a.allSatisfy { equal($0.value, b[$0.key]!) }
        }
        if let a = a as? [Any], let b = b as? [Any] {
            return a.count == b.count && zip(a, b).allSatisfy { equal($0, $1) }
        }
        return (a as? NSObject)?.isEqual(b) ?? false
    }
    static func check(_ spec: [String: Any], _ value: Any, _ path: String) throws {
        let kind = spec["type"] as! String
        if kind == "ref" { _ = try validate(spec["name"] as! String, value); return }
        if kind == "nullable" {
            if !(value is NSNull) { try check(spec["item"] as! [String: Any], value, path) }
            return
        }
        var valid = false
        switch kind {
        case "enum": valid = (spec["values"] as! [Any]).contains { equal(value, $0) }
        case "string":
            if let s = value as? String {
                valid = s.unicodeScalars.count >= spec["min"] as! Int && s.unicodeScalars.count <= spec["max"] as! Int
                if let pattern = spec["pattern"] as? String {
                    let regex = try NSRegularExpression(pattern: pattern)
                    let range = NSRange(s.startIndex..<s.endIndex, in: s)
                    valid = valid && regex.firstMatch(in: s, range: range)?.range == range
                }
            }
        case "boolean": valid = isBool(value)
        case "integer":
            if let n = value as? NSNumber, !isBool(value) {
                let d = n.doubleValue
                valid = d.isFinite && d.rounded() == d && d >= (spec["min"] as! NSNumber).doubleValue && d <= (spec["max"] as! NSNumber).doubleValue
            }
        case "array":
            if let a = value as? [Any] {
                valid = a.count >= spec["min"] as! Int && a.count <= spec["max"] as! Int
                if valid { for child in a { try check(spec["item"] as! [String: Any], child, path + "[]") } }
            }
        case "object":
            if let p = value as? [String: Any] {
                let fields = spec["fields"] as! [String: Any]
                valid = Set(p.keys) == Set(fields.keys)
                if valid { for (key, child) in fields { try check(child as! [String: Any], p[key]!, path + "." + key) } }
            }
        default: break
        }
        try require(valid, "Invalid \(path)")
    }
    @discardableResult
    static func validate(_ name: String, _ value: Any) throws -> [String: Any] {
        try require(schema[name] != nil, "Unknown contract")
        if let p = value as? [String: Any], let version = p["schema_version"] {
            try require(equal(version, "1.0"), "Unsupported schema version", "incompatible_version")
        }
        try check(schema[name] as! [String: Any], value, name)
        let p = value as! [String: Any]
        func null(_ key: String) -> Bool { p[key] is NSNull }
        func number(_ key: String) -> Int64 { (p[key] as! NSNumber).int64Value }
        func string(_ key: String) -> String { p[key] as! String }
        switch name {
        case "Handshake":
            try require((p["supported_versions"] as! [String]).contains("1.0"), "No compatible version", "incompatible_version")
            for key in ["supported_versions", "capabilities", "required_capabilities"] {
                let values = p[key] as! [String]
                try require(Set(values).count == values.count, "Duplicate negotiation entry")
            }
            try require(string("peer") == "app" || string("credential_role") == "native_bridge", "Bridge cannot grant app authority")
        case "SourceObservation":
            try require(string("source_kind") != "browser" || !null("source_url"), "Browser source needs URL")
        case "BrowserSnapshot":
            let url = string("url"), origin = string("origin")
            let prefix = url.range(of: "^https?://[^/\\?#]+", options: .regularExpression).map { String(url[$0]) }
            try require(prefix == origin, "Snapshot origin mismatch")
            let ids = (p["elements"] as! [[String: Any]]).map { $0["target_id"] as! String }
            try require(Set(ids).count == ids.count, "Duplicate target")
        case "ActionIntent":
            let command = string("command")
            try require(!null("target_id") == ["click", "fill", "select"].contains(command), "Target/command mismatch")
            try require(!null("url") == ["navigate", "open_tab"].contains(command), "URL/command mismatch")
            try require(!null("text") == ["fill", "select"].contains(command), "Text/command mismatch")
            try require(!(p["private_data"] as! Bool) || command == "fill", "Private data requires typing")
        case "ExactApproval":
            try require(number("expires_at_ms") > number("approved_at_ms"), "Invalid approval lifetime")
        case "BrowserAction":
            let i = p["intent"] as! [String: Any], a = p["approval"] as? [String: Any]
            let needs = i["consequential"] as! Bool || i["private_data"] as! Bool || ["click", "fill", "select"].contains(i["command"] as! String)
            try require(!needs || a != nil, "Exact app approval required", "approval_required")
            try require(a == nil || equal(a!["intent"]!, i), "Approval intent mismatch", "stale_approval")
        case "BrowserTask":
            let exhausted = number("actions_used") == 25 || number("active_ms") == 300000 || number("consecutive_no_progress") == 3
            try require(!exhausted || !["queued", "running"].contains(string("state")), "Exhausted task cannot run")
        case "ScheduledBlock": try require(number("end_ms") > number("start_ms"), "Invalid block interval")
        case "ActionableItem":
            try require(null("due_at_ms") == null("due_timezone"), "Deadline timezone required")
            try require(null("supersedes_revision") || number("supersedes_revision") < number("revision"), "Invalid revision chain")
            try require(string("state") != "needs_clarification" || !null("ambiguity"), "Missing ambiguity")
            try require(null("ambiguity") || ["candidate", "needs_clarification", "dismissed"].contains(string("state")), "Unresolved item")
            try require(!null("completion_receipt_id") == (string("state") == "completed"), "Completion needs receipt")
        case "ActionReceipt":
            try require(string("status") != "verified" || !(p["evidence"] as! [Any]).isEmpty, "Verification needs evidence")
            try require(!(p["completes_obligation"] as! Bool) || string("status") == "verified", "Uncertain receipt cannot complete obligation")
            try require(string("status") != "verified" || null("error"), "Verified receipt cannot carry error")
        default: break
        }
        return p
    }
    static func decode<T: BrowserWireRecord>(_ type: T.Type, from data: Data) throws -> T {
        try validate(T.contractName, JSONSerialization.jsonObject(with: data))
        return try JSONDecoder().decode(type, from: data)
    }
    static func negotiate(_ local: Any, _ remote: Any) throws -> [String: Any] {
        let a = try validate("Handshake", local), b = try validate("Handshake", remote)
        let common = Set(a["capabilities"] as! [String]).intersection(b["capabilities"] as! [String])
        let required = Set(a["required_capabilities"] as! [String]).union(b["required_capabilities"] as! [String])
        try require(required.isSubset(of: common), "Required capability unavailable", "missing_capability")
        return ["schema_version": "1.0", "capabilities": common.sorted()]
    }
    static func validateProposal(_ item: Any, _ proposal: Any) throws {
        let i = try validate("ActionableItem", item), p = try validate("ActionProposal", proposal)
        try require(equal(p["item_id"]!, i["id"]!) && equal(p["item_revision"]!, i["revision"]!), "Stale proposal")
        let ids = Set((i["evidence"] as! [[String: Any]]).map { $0["id"] as! String })
        try require(Set(p["evidence_ids"] as! [String]).isSubset(of: ids), "Ungrounded proposal")
        try require(equal(i["state"]!, "tracked"), "Proposal requires a resolved tracked obligation")
    }
    static func validateCompletion(_ item: Any, _ receipt: Any, _ proposal: Any) throws {
        let i = try validate("ActionableItem", item), r = try validate("ActionReceipt", receipt), p = try validate("ActionProposal", proposal)
        let intent = p["intent"] as! [String: Any]
        try require(equal(i["state"]!, "completed") && equal(i["completion_receipt_id"]!, r["id"]!), "Receipt link mismatch")
        try require(equal(r["status"]!, "verified") && equal(r["completes_obligation"]!, true), "Receipt does not prove completion")
        try require(equal(r["proposal_id"]!, p["id"]!) && equal(r["action_id"]!, intent["action_id"]!) && equal(r["task_id"]!, intent["task_id"]!), "Receipt action mismatch")
        try require(equal(p["item_id"]!, i["id"]!) && equal(p["item_revision"]!, i["revision"]!), "Receipt item mismatch")
        try require(equal(p["state"]!, "approved"), "Completion requires approved proposal")
        let ids = Set((i["evidence"] as! [[String: Any]]).map { $0["id"] as! String })
        try require(Set(p["evidence_ids"] as! [String]).isSubset(of: ids), "Ungrounded completion proposal")
    }
}

// BEGIN RECORDS
extension WispBrowserContracts {
    struct NegotiatedCapabilities: BrowserWireRecord {
        static let contractName = "NegotiatedCapabilities"
        let schema_version: String
        let capabilities: [String]
        enum CodingKeys: String, CodingKey {
            case schema_version, capabilities
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(capabilities, forKey: .capabilities)
        }
    }
    struct Handshake: BrowserWireRecord {
        static let contractName = "Handshake"
        let schema_version: String
        let peer: String
        let supported_versions: [String]
        let capabilities: [String]
        let required_capabilities: [String]
        let credential_role: String
        enum CodingKeys: String, CodingKey {
            case schema_version, peer, supported_versions, capabilities, required_capabilities, credential_role
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(peer, forKey: .peer)
            try c.encode(supported_versions, forKey: .supported_versions)
            try c.encode(capabilities, forKey: .capabilities)
            try c.encode(required_capabilities, forKey: .required_capabilities)
            try c.encode(credential_role, forKey: .credential_role)
        }
    }
    struct ContractError: BrowserWireRecord {
        static let contractName = "ContractError"
        let schema_version: String
        let code: String
        let message: String
        let retryable: Bool
        enum CodingKeys: String, CodingKey {
            case schema_version, code, message, retryable
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(code, forKey: .code)
            try c.encode(message, forKey: .message)
            try c.encode(retryable, forKey: .retryable)
        }
    }
    struct SourceObservation: BrowserWireRecord {
        static let contractName = "SourceObservation"
        let schema_version: String
        let id: String
        let source_kind: String
        let source_url: String?
        let source_record_id: String?
        let revision: String
        let observed_at_ms: Int64
        let title: String
        let text: String
        let private_context: Bool
        enum CodingKeys: String, CodingKey {
            case schema_version, id, source_kind, source_url, source_record_id, revision, observed_at_ms, title, text, private_context
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            try c.encode(source_kind, forKey: .source_kind)
            if let value = source_url { try c.encode(value, forKey: .source_url) } else { try c.encodeNil(forKey: .source_url) }
            if let value = source_record_id { try c.encode(value, forKey: .source_record_id) } else { try c.encodeNil(forKey: .source_record_id) }
            try c.encode(revision, forKey: .revision)
            try c.encode(observed_at_ms, forKey: .observed_at_ms)
            try c.encode(title, forKey: .title)
            try c.encode(text, forKey: .text)
            try c.encode(private_context, forKey: .private_context)
        }
    }
    struct Evidence: BrowserWireRecord {
        static let contractName = "Evidence"
        let id: String
        let observation_id: String
        let source_revision: String
        let quote: String
        let captured_at_ms: Int64
        enum CodingKeys: String, CodingKey {
            case id, observation_id, source_revision, quote, captured_at_ms
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(id, forKey: .id)
            try c.encode(observation_id, forKey: .observation_id)
            try c.encode(source_revision, forKey: .source_revision)
            try c.encode(quote, forKey: .quote)
            try c.encode(captured_at_ms, forKey: .captured_at_ms)
        }
    }
    struct BrowserElement: BrowserWireRecord {
        static let contractName = "BrowserElement"
        let target_id: String
        let role: String
        let label: String
        let editable: Bool
        enum CodingKeys: String, CodingKey {
            case target_id, role, label, editable
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(target_id, forKey: .target_id)
            try c.encode(role, forKey: .role)
            try c.encode(label, forKey: .label)
            try c.encode(editable, forKey: .editable)
        }
    }
    struct BrowserSnapshot: BrowserWireRecord {
        static let contractName = "BrowserSnapshot"
        let schema_version: String
        let id: String
        let task_id: String
        let observation_id: String
        let url: String
        let origin: String
        let captured_at_ms: Int64
        let enabled: Bool
        let site_permission: String
        let private_context: Bool
        let tab_role: String
        let content_mode: String
        let elements: [BrowserElement]
        enum CodingKeys: String, CodingKey {
            case schema_version, id, task_id, observation_id, url, origin, captured_at_ms, enabled, site_permission, private_context, tab_role, content_mode, elements
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            try c.encode(task_id, forKey: .task_id)
            try c.encode(observation_id, forKey: .observation_id)
            try c.encode(url, forKey: .url)
            try c.encode(origin, forKey: .origin)
            try c.encode(captured_at_ms, forKey: .captured_at_ms)
            try c.encode(enabled, forKey: .enabled)
            try c.encode(site_permission, forKey: .site_permission)
            try c.encode(private_context, forKey: .private_context)
            try c.encode(tab_role, forKey: .tab_role)
            try c.encode(content_mode, forKey: .content_mode)
            try c.encode(elements, forKey: .elements)
        }
    }
    struct ActionIntent: BrowserWireRecord {
        static let contractName = "ActionIntent"
        let action_id: String
        let task_id: String
        let snapshot_id: String
        let command: String
        let target_id: String?
        let url: String?
        let text: String?
        let private_data: Bool
        let consequential: Bool
        enum CodingKeys: String, CodingKey {
            case action_id, task_id, snapshot_id, command, target_id, url, text, private_data, consequential
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(action_id, forKey: .action_id)
            try c.encode(task_id, forKey: .task_id)
            try c.encode(snapshot_id, forKey: .snapshot_id)
            try c.encode(command, forKey: .command)
            if let value = target_id { try c.encode(value, forKey: .target_id) } else { try c.encodeNil(forKey: .target_id) }
            if let value = url { try c.encode(value, forKey: .url) } else { try c.encodeNil(forKey: .url) }
            if let value = text { try c.encode(value, forKey: .text) } else { try c.encodeNil(forKey: .text) }
            try c.encode(private_data, forKey: .private_data)
            try c.encode(consequential, forKey: .consequential)
        }
    }
    struct ExactApproval: BrowserWireRecord {
        static let contractName = "ExactApproval"
        let proposal_id: String
        let authority: String
        let approved_at_ms: Int64
        let expires_at_ms: Int64
        let intent: ActionIntent
        enum CodingKeys: String, CodingKey {
            case proposal_id, authority, approved_at_ms, expires_at_ms, intent
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(proposal_id, forKey: .proposal_id)
            try c.encode(authority, forKey: .authority)
            try c.encode(approved_at_ms, forKey: .approved_at_ms)
            try c.encode(expires_at_ms, forKey: .expires_at_ms)
            try c.encode(intent, forKey: .intent)
        }
    }
    struct BrowserAction: BrowserWireRecord {
        static let contractName = "BrowserAction"
        let schema_version: String
        let intent: ActionIntent
        let approval: ExactApproval?
        enum CodingKeys: String, CodingKey {
            case schema_version, intent, approval
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(intent, forKey: .intent)
            if let value = approval { try c.encode(value, forKey: .approval) } else { try c.encodeNil(forKey: .approval) }
        }
    }
    struct InvestigationPolicy: BrowserWireRecord {
        static let contractName = "InvestigationPolicy"
        let explicit_enablement: Bool
        let site_permission_required: Bool
        let exclude_private_at_capture: Bool
        let exclude_private_at_service: Bool
        let separate_background_tab: Bool
        let max_proactive_tasks: Int64
        let foreground_priority: Bool
        let max_actions: Int64
        let max_active_ms: Int64
        let user_wait_counts: Bool
        let max_no_progress: Int64
        let model_location: String
        let model_replaceable: Bool
        let observation_mode: String
        let unsupported_visual: String
        let arbitrary_javascript: Bool
        let bridge_credential_role: String
        let approval_authority: String
        enum CodingKeys: String, CodingKey {
            case explicit_enablement, site_permission_required, exclude_private_at_capture, exclude_private_at_service, separate_background_tab, max_proactive_tasks, foreground_priority, max_actions, max_active_ms, user_wait_counts, max_no_progress, model_location, model_replaceable, observation_mode, unsupported_visual, arbitrary_javascript, bridge_credential_role, approval_authority
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(explicit_enablement, forKey: .explicit_enablement)
            try c.encode(site_permission_required, forKey: .site_permission_required)
            try c.encode(exclude_private_at_capture, forKey: .exclude_private_at_capture)
            try c.encode(exclude_private_at_service, forKey: .exclude_private_at_service)
            try c.encode(separate_background_tab, forKey: .separate_background_tab)
            try c.encode(max_proactive_tasks, forKey: .max_proactive_tasks)
            try c.encode(foreground_priority, forKey: .foreground_priority)
            try c.encode(max_actions, forKey: .max_actions)
            try c.encode(max_active_ms, forKey: .max_active_ms)
            try c.encode(user_wait_counts, forKey: .user_wait_counts)
            try c.encode(max_no_progress, forKey: .max_no_progress)
            try c.encode(model_location, forKey: .model_location)
            try c.encode(model_replaceable, forKey: .model_replaceable)
            try c.encode(observation_mode, forKey: .observation_mode)
            try c.encode(unsupported_visual, forKey: .unsupported_visual)
            try c.encode(arbitrary_javascript, forKey: .arbitrary_javascript)
            try c.encode(bridge_credential_role, forKey: .bridge_credential_role)
            try c.encode(approval_authority, forKey: .approval_authority)
        }
    }
    struct BrowserTask: BrowserWireRecord {
        static let contractName = "BrowserTask"
        let schema_version: String
        let id: String
        let item_id: String?
        let state: String
        let policy: InvestigationPolicy
        let actions_used: Int64
        let active_ms: Int64
        let consecutive_no_progress: Int64
        let error: ContractError?
        enum CodingKeys: String, CodingKey {
            case schema_version, id, item_id, state, policy, actions_used, active_ms, consecutive_no_progress, error
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            if let value = item_id { try c.encode(value, forKey: .item_id) } else { try c.encodeNil(forKey: .item_id) }
            try c.encode(state, forKey: .state)
            try c.encode(policy, forKey: .policy)
            try c.encode(actions_used, forKey: .actions_used)
            try c.encode(active_ms, forKey: .active_ms)
            try c.encode(consecutive_no_progress, forKey: .consecutive_no_progress)
            if let value = error { try c.encode(value, forKey: .error) } else { try c.encodeNil(forKey: .error) }
        }
    }
    struct ExternalRecord: BrowserWireRecord {
        static let contractName = "ExternalRecord"
        let schema_version: String
        let id: String
        let system: String
        let external_id: String
        let url: String?
        let revision: String
        let evidence_ids: [String]
        enum CodingKeys: String, CodingKey {
            case schema_version, id, system, external_id, url, revision, evidence_ids
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            try c.encode(system, forKey: .system)
            try c.encode(external_id, forKey: .external_id)
            if let value = url { try c.encode(value, forKey: .url) } else { try c.encodeNil(forKey: .url) }
            try c.encode(revision, forKey: .revision)
            try c.encode(evidence_ids, forKey: .evidence_ids)
        }
    }
    struct ScheduledBlock: BrowserWireRecord {
        static let contractName = "ScheduledBlock"
        let schema_version: String
        let id: String
        let obligation_id: String
        let start_ms: Int64
        let end_ms: Int64
        let timezone: String
        let state: String
        let external_record_id: String?
        enum CodingKeys: String, CodingKey {
            case schema_version, id, obligation_id, start_ms, end_ms, timezone, state, external_record_id
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            try c.encode(obligation_id, forKey: .obligation_id)
            try c.encode(start_ms, forKey: .start_ms)
            try c.encode(end_ms, forKey: .end_ms)
            try c.encode(timezone, forKey: .timezone)
            try c.encode(state, forKey: .state)
            if let value = external_record_id { try c.encode(value, forKey: .external_record_id) } else { try c.encodeNil(forKey: .external_record_id) }
        }
    }
    struct ActionableItem: BrowserWireRecord {
        static let contractName = "ActionableItem"
        let schema_version: String
        let id: String
        let kind: String
        let title: String
        let state: String
        let revision: Int64
        let supersedes_revision: Int64?
        let due_at_ms: Int64?
        let due_timezone: String?
        let ambiguity: String?
        let evidence: [Evidence]
        let external_record_ids: [String]
        let completion_receipt_id: String?
        enum CodingKeys: String, CodingKey {
            case schema_version, id, kind, title, state, revision, supersedes_revision, due_at_ms, due_timezone, ambiguity, evidence, external_record_ids, completion_receipt_id
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            try c.encode(kind, forKey: .kind)
            try c.encode(title, forKey: .title)
            try c.encode(state, forKey: .state)
            try c.encode(revision, forKey: .revision)
            if let value = supersedes_revision { try c.encode(value, forKey: .supersedes_revision) } else { try c.encodeNil(forKey: .supersedes_revision) }
            if let value = due_at_ms { try c.encode(value, forKey: .due_at_ms) } else { try c.encodeNil(forKey: .due_at_ms) }
            if let value = due_timezone { try c.encode(value, forKey: .due_timezone) } else { try c.encodeNil(forKey: .due_timezone) }
            if let value = ambiguity { try c.encode(value, forKey: .ambiguity) } else { try c.encodeNil(forKey: .ambiguity) }
            try c.encode(evidence, forKey: .evidence)
            try c.encode(external_record_ids, forKey: .external_record_ids)
            if let value = completion_receipt_id { try c.encode(value, forKey: .completion_receipt_id) } else { try c.encodeNil(forKey: .completion_receipt_id) }
        }
    }
    struct ActionProposal: BrowserWireRecord {
        static let contractName = "ActionProposal"
        let schema_version: String
        let id: String
        let item_id: String
        let item_revision: Int64
        let intent: ActionIntent
        let rationale: String
        let evidence_ids: [String]
        let state: String
        enum CodingKeys: String, CodingKey {
            case schema_version, id, item_id, item_revision, intent, rationale, evidence_ids, state
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            try c.encode(item_id, forKey: .item_id)
            try c.encode(item_revision, forKey: .item_revision)
            try c.encode(intent, forKey: .intent)
            try c.encode(rationale, forKey: .rationale)
            try c.encode(evidence_ids, forKey: .evidence_ids)
            try c.encode(state, forKey: .state)
        }
    }
    struct ActionReceipt: BrowserWireRecord {
        static let contractName = "ActionReceipt"
        let schema_version: String
        let id: String
        let action_id: String
        let task_id: String
        let proposal_id: String?
        let status: String
        let recorded_at_ms: Int64
        let evidence: [Evidence]
        let external_record_id: String?
        let completes_obligation: Bool
        let error: ContractError?
        enum CodingKeys: String, CodingKey {
            case schema_version, id, action_id, task_id, proposal_id, status, recorded_at_ms, evidence, external_record_id, completes_obligation, error
        }
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(schema_version, forKey: .schema_version)
            try c.encode(id, forKey: .id)
            try c.encode(action_id, forKey: .action_id)
            try c.encode(task_id, forKey: .task_id)
            if let value = proposal_id { try c.encode(value, forKey: .proposal_id) } else { try c.encodeNil(forKey: .proposal_id) }
            try c.encode(status, forKey: .status)
            try c.encode(recorded_at_ms, forKey: .recorded_at_ms)
            try c.encode(evidence, forKey: .evidence)
            if let value = external_record_id { try c.encode(value, forKey: .external_record_id) } else { try c.encodeNil(forKey: .external_record_id) }
            try c.encode(completes_obligation, forKey: .completes_obligation)
            if let value = error { try c.encode(value, forKey: .error) } else { try c.encodeNil(forKey: .error) }
        }
    }
    static func roundTrip(_ name: String, _ data: Data) throws -> Any {
        let encoded: Data
        switch name {
        case "NegotiatedCapabilities": encoded = try JSONEncoder().encode(decode(NegotiatedCapabilities.self, from: data))
        case "Handshake": encoded = try JSONEncoder().encode(decode(Handshake.self, from: data))
        case "ContractError": encoded = try JSONEncoder().encode(decode(ContractError.self, from: data))
        case "SourceObservation": encoded = try JSONEncoder().encode(decode(SourceObservation.self, from: data))
        case "Evidence": encoded = try JSONEncoder().encode(decode(Evidence.self, from: data))
        case "BrowserElement": encoded = try JSONEncoder().encode(decode(BrowserElement.self, from: data))
        case "BrowserSnapshot": encoded = try JSONEncoder().encode(decode(BrowserSnapshot.self, from: data))
        case "ActionIntent": encoded = try JSONEncoder().encode(decode(ActionIntent.self, from: data))
        case "ExactApproval": encoded = try JSONEncoder().encode(decode(ExactApproval.self, from: data))
        case "BrowserAction": encoded = try JSONEncoder().encode(decode(BrowserAction.self, from: data))
        case "InvestigationPolicy": encoded = try JSONEncoder().encode(decode(InvestigationPolicy.self, from: data))
        case "BrowserTask": encoded = try JSONEncoder().encode(decode(BrowserTask.self, from: data))
        case "ExternalRecord": encoded = try JSONEncoder().encode(decode(ExternalRecord.self, from: data))
        case "ScheduledBlock": encoded = try JSONEncoder().encode(decode(ScheduledBlock.self, from: data))
        case "ActionableItem": encoded = try JSONEncoder().encode(decode(ActionableItem.self, from: data))
        case "ActionProposal": encoded = try JSONEncoder().encode(decode(ActionProposal.self, from: data))
        case "ActionReceipt": encoded = try JSONEncoder().encode(decode(ActionReceipt.self, from: data))
        default: throw BrowserContractViolation(message: "Unknown contract", code: "invalid_payload")
        }
        return try JSONSerialization.jsonObject(with: encoded)
    }
}
// END RECORDS

protocol WispBrowserService {
    func negotiate(_ peer: WispBrowserContracts.Handshake) async throws -> WispBrowserContracts.NegotiatedCapabilities
    func snapshot(taskID: String) async throws -> WispBrowserContracts.BrowserSnapshot
    func execute(_ action: WispBrowserContracts.BrowserAction) async throws -> WispBrowserContracts.ActionReceipt
    func cancel(taskID: String) async throws -> WispBrowserContracts.BrowserTask
}
protocol WispDiscoveryService {
    func extract(_ observation: WispBrowserContracts.SourceObservation) async throws -> [WispBrowserContracts.ActionableItem]
    func reconcile(_ items: [WispBrowserContracts.ActionableItem]) async throws -> [WispBrowserContracts.ActionableItem]
    func propose(itemID: String, revision: Int64) async throws -> WispBrowserContracts.ActionProposal
    func reconcileReceipt(_ receipt: WispBrowserContracts.ActionReceipt) async throws -> WispBrowserContracts.ActionableItem
}
