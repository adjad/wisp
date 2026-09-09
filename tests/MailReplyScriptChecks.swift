import Foundation

// Standalone so the native contract checks also run with Command Line Tools
// installations that do not include XCTest. Compiles scripts; never executes them.
@main
enum MailReplyScriptChecks {
    static func require(_ value: @autoclosure () -> Bool, _ message: String) {
        guard value() else { fatalError(message) }
    }

    static func main() {
        let envelope: [String: Any] = [
            "message_id": "<fixture@example.com>", "account": "Work", "account_id": "account-1",
            "from": "me@example.com", "to": ["reply@example.com"], "cc": [], "bcc": [],
            "subject": "Re: A \"quoted\" subject", "content": "Thanks\rOriginal\nline \\ path",
        ]
        let prepare = MailReplyScript.build(messageID: "<fixture@example.com>", account: "Work",
                                            body: "Thanks", replyAll: false)!
        require(!prepare.contains("send theReply"), "Preparation can send")
        for required in ["close theReply saving no", "sender of theReply", "subject of theReply",
                         "bcc recipient of theReply", "hitCount is not 1", "set visible of theReply to true",
                         "if renderedContent does not start with"] {
            require(prepare.contains(required), "Missing contract: \(required)")
        }
        let send = MailReplyScript.build(messageID: "<fixture@example.com>", account: "Work",
                                         body: "Thanks", replyAll: true, expected: envelope)!
        require(prepare.range(of: "set content to desiredContent")!.lowerBound <
                prepare.range(of: "(content of theReply)")!.lowerBound,
                "Reading reply content before assigning it can cache an empty native body")
        require(send.range(of: "if envelope is not")!.lowerBound < send.range(of: "send theReply")!.lowerBound,
                "Send happens before validation")
        require(send.contains("every account whose id is"), "Account is not pinned by ID")
        require(send.contains("if (send theReply) is not true"), "Mail's false result is ignored")
        require(send.contains("considering case"), "Exact content comparison required")
        require(MailReplyScript.build(messageID: "id", account: "Work", body: "Thanks", replyAll: false,
                                      expected: ["message_id": "id"]) == nil, "Incomplete envelope accepted")
        let result = NSAppleEventDescriptor.list()
        for (index, field) in MailReplyScript.fields.enumerated() {
            let value: NSAppleEventDescriptor
            if let addresses = envelope[field] as? [String] {
                value = .list()
                for (i, address) in addresses.enumerated() { value.insert(.init(string: address), at: i + 1) }
            } else { value = .init(string: envelope[field] as! String) }
            result.insert(value, at: index + 1)
        }
        let decoded = MailReplyScript.decode(result)!
        require(decoded["to"] as? [String] == ["reply@example.com"], "Recipient list decode failed")
        require(decoded["content"] as? String == envelope["content"] as? String, "Content decode changed text")
        for expected in [nil, envelope] as [[String: Any]?] {
            let source = MailReplyScript.build(messageID: "<fixture@example.com>", account: "Work",
                body: "A \"quote\"\nBackslash \\ and tab\t.", replyAll: true, expected: expected)!
            let script = NSAppleScript(source: source)!
            var error: NSDictionary?
            require(script.compileAndReturnError(&error), "Mail script failed to compile: \(error ?? [:])")
        }
        print("Mail reply native contract checks passed (scripts compiled, no Mail actions executed).")
    }
}
