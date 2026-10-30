// Kept apart from AgentSend.swift so the Chat window's standalone contract
// (scripts/test_chat_contract.sh) compiles it with ChatStore.swift.

/// A source a tool in an Ask reply couldn't read for lack of access. The
/// service attaches these to the tool's own result (`needs_access`, see
/// service/tools/access_needed.py), so the reply's fix button never depends on
/// the model's wording.
enum AskAccess {
    /// The kinds ("calendar_access", "full_disk_access", …) in a tool_result's
    /// `needs_access`, in order, each once. Anything malformed is skipped.
    static func kinds(_ payload: Any?) -> [String] {
        var out: [String] = []
        for item in payload as? [[String: Any]] ?? [] {
            if let kind = item["kind"] as? String, !kind.isEmpty, !out.contains(kind) { out.append(kind) }
        }
        return out
    }
}
