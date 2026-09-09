import Foundation

// Standalone so the native contract checks also run with Command Line Tools
// installations that do not include XCTest. Mail scripts are compiled only.
// Only the pure literal-value comparator is executed; it cannot contact Mail.
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
                         "exactReplyValue(text 1 thru"] {
            require(prepare.contains(required), "Missing contract: \(required)")
        }
        let send = MailReplyScript.build(messageID: "<fixture@example.com>", account: "Work",
                                         body: "Thanks", replyAll: true, expected: envelope)!
        require(prepare.range(of: "set content to desiredContent")!.lowerBound <
                prepare.range(of: "(content of theReply)")!.lowerBound,
                "Reading reply content before assigning it can cache an empty native body")
        require(send.range(of: "exactReplyValue(envelope,")!.lowerBound < send.range(of: "send theReply")!.lowerBound,
                "Send happens before validation")
        require(send.contains("every account whose id is"), "Account is not pinned by ID")
        require(send.contains("if (send theReply) is not true"), "Mail's false result is ignored")
        require(send.contains("id of actualValue"), "Exact character comparison required")
        let pinned = MailReplyScript.build(messageID: "<fixture@example.com>", account: "Work",
            body: "Thanks", replyAll: false, sourceAccountID: "native-source-id")!
        require(pinned.contains("every account whose id is \"native-source-id\""),
                "Preparation must use the source snapshot's account ID")
        require(!pinned.contains("send theReply"), "Pinned preparation can send")
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

        func vector(_ value: [String: Any]) -> String {
            "{" + MailReplyScript.fields.map { MailReplyScript.literal(value[$0]!)! }.joined(separator: ", ") + "}"
        }
        func compare(_ actual: String, _ approved: String, matches: Bool) {
            let source = MailReplyScript.exactValueHandler
                + "\nreturn my exactReplyValue(\(actual), \(approved))"
            require(!source.contains("tell application"), "Comparator probe can contact an application")
            var error: NSDictionary?
            let result = NSAppleScript(source: source)!.executeAndReturnError(&error)
            require(error == nil, "Pure comparator failed: \(error ?? [:])")
            require(result.booleanValue == matches, "Exact comparison returned an incorrect result")
        }
        var populated = envelope
        populated["cc"] = ["first@example.com", "second@example.com"]
        populated["bcc"] = ["private@example.com"]
        compare(vector(populated), vector(populated), matches: true)
        for field in MailReplyScript.fields {
            var changed = populated
            if let values = changed[field] as? [String] {
                changed[field] = values.map { $0.uppercased() }
            } else {
                changed[field] = (changed[field] as! String).uppercased()
            }
            compare(vector(changed), vector(populated), matches: false)
        }
        for cc in [["first@example.com"], ["second@example.com", "first@example.com"]] {
            var changed = populated
            changed["cc"] = cc
            compare(vector(changed), vector(populated), matches: false)
        }
        for (a, b) in [("x\ry", "x\ny"), ("x\\ny", "x\ny"), ("é", "e\u{301}"), ("A", "a")] {
            compare(MailReplyScript.literal([a])!, MailReplyScript.literal([b])!, matches: false)
        }
        compare("{}", "{}", matches: true)
        compare("\"\"", "\"\"", matches: true)
        compare("{}", "\"\"", matches: false)

        // Compile the actual raw-reader templates with their Swift-only
        // substitutions supplied. No reader instance or Mail action runs.
        let readerURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("app/Sources/WispApp/MailReader.swift")
        let reader = try! String(contentsOf: readerURL, encoding: .utf8)
        func template(after marker: String) -> String {
            let markerEnd = reader.range(of: marker)!.upperBound
            let start = reader[markerEnd...].range(of: "\"\"\"")!.upperBound
            let end = reader[start...].range(of: "\"\"\"")!.lowerBound
            return String(reader[start..<end])
        }
        let inbox = template(after: "let n = esc(account)")
            .replacingOccurrences(of: "\\(n)", with: "Work")
        let accounts = template(after: "private let accountsScript")
        let raw = template(after: "private func rawScript")
            .replacingOccurrences(of: "\\(refDateSetup)", with: template(after: "private let refDateSetup"))
            .replacingOccurrences(of: "\\(inboxSource(account))", with: inbox)
            .replacingOccurrences(of: "\\(acctNameFragment(account))", with: "set acctName to \"Work\"")
            .replacingOccurrences(of: "\\(limit)", with: "2")
        require(raw.contains("fieldText contains FS") && raw.contains("fieldText contains RS"),
                "Raw fields may corrupt record boundaries")
        require(raw.contains("acctID") && raw.contains("item 9 of rowFields"), "Raw account identity missing")
        require(!accounts.contains("try"), "Account enumeration can hide partial failures")
        for source in [accounts, raw, "tell application \"Mail\"\n" + inbox + "\nend tell"] {
            var error: NSDictionary?
            require(NSAppleScript(source: source)!.compileAndReturnError(&error),
                    "Mail reader script failed to compile: \(error ?? [:])")
        }
        print("Mail reply native contract checks passed (scripts compiled, no Mail actions executed).")
    }
}
