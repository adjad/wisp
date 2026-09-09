import Foundation

/// Explicitly opt-in live Mail preparation checks. No generated script may
/// contain the send command, and no message content or addresses are logged.
@main
enum MailReplyLiveChecks {
    struct Failure: Error, CustomStringConvertible {
        let description: String
    }

    static func run(_ source: String) throws -> NSAppleEventDescriptor {
        guard !source.contains("send theReply") else {
            throw Failure(description: "Live preparation check refused a send script")
        }
        guard let script = NSAppleScript(source: source) else {
            throw Failure(description: "Could not construct AppleScript")
        }
        var error: NSDictionary?
        let result = script.executeAndReturnError(&error)
        if let error {
            throw Failure(description: "Mail error: \(error[NSAppleScript.errorMessage] ?? error)")
        }
        return result
    }

    static func outgoingIDs() throws -> [String] {
        let result = try run("tell application \"Mail\" to return id of every outgoing message")
        return (0..<result.numberOfItems).map { result.atIndex($0 + 1)?.stringValue ?? "?" }.sorted()
    }

    static func main() {
        setbuf(stdout, nil)
        do {
            guard CommandLine.arguments.contains("--live-prepare") else {
                throw Failure(description: "Requires --live-prepare and explicit user authorization")
            }
            // Use an already-read inbox message from each account so the test
            // cannot mark an unread message as read. Identity stays in memory.
            let sources = try run("""
                tell application "Mail"
                    set sources to {}
                    repeat with acct in accounts
                        try
                            set mbx to mailbox "INBOX" of acct
                            set candidate to first message of mbx whose read status is true
                        on error
                            try
                                set mbx to mailbox "Inbox" of acct
                                set candidate to first message of mbx whose read status is true
                            on error
                                set candidate to missing value
                            end try
                        end try
                        if candidate is not missing value then
                            set mid to message id of candidate
                            if mid is not "" then set end of sources to {mid as text, (name of acct) as text}
                        end if
                    end repeat
                    return sources
                end tell
                """)
            guard sources.numberOfItems > 0 else {
                throw Failure(description: "No already-read inbox message available for preparation checks")
            }
            print("Mail access OK; testing \(sources.numberOfItems) account(s).")
            for index in 1...sources.numberOfItems {
                guard let source = sources.atIndex(index),
                      let messageID = source.atIndex(1)?.stringValue,
                      let account = source.atIndex(2)?.stringValue else {
                    throw Failure(description: "Invalid source identity descriptor")
                }
                for replyAll in [false, true] {
                    let before = try outgoingIDs()
                    let testBody = "Wisp preparation smoke test — discard without sending."
                    var previous: [String: Any]?
                    for _ in 0..<2 {
                        let script = MailReplyScript.build(messageID: messageID, account: account,
                            body: testBody, replyAll: replyAll)!
                        let decoded = MailReplyScript.decode(try run(script))
                        guard let envelope = decoded,
                              envelope["message_id"] as? String == messageID,
                              envelope["account"] as? String == account,
                              let sender = envelope["from"] as? String, sender.contains("@"),
                              let to = envelope["to"] as? [String], !to.isEmpty,
                              let content = envelope["content"] as? String,
                              content.contains("Wisp preparation smoke test") else {
                            throw Failure(description: "Account \(index): invalid outgoing envelope")
                        }
                        if let previous, !NSDictionary(dictionary: previous).isEqual(to: envelope) {
                            let changed = MailReplyScript.fields.filter {
                                !NSDictionary(dictionary: [$0: previous[$0]!]).isEqual(to: [$0: envelope[$0]!])
                            }
                            throw Failure(description: "Account \(index): envelope changed between preparations: \(changed)")
                        }
                        previous = envelope
                    }
                    if index == 1 && !replyAll {
                        // Exercise the production pre-send comparison, replacing
                        // its sole send command with draft disposal FIRST.
                        let sendLine = "if (send theReply) is not true then error \"Mail did not accept the reply for sending; check Mail before retrying.\""
                        func validationOnly(_ expected: [String: Any]) -> String {
                            MailReplyScript.build(messageID: messageID, account: account,
                                body: testBody, replyAll: replyAll, expected: expected)!
                                .replacingOccurrences(of: sendLine, with: "close theReply saving no")
                        }
                        let approved = previous!
                        guard let verified = MailReplyScript.decode(try run(validationOnly(approved))),
                              NSDictionary(dictionary: approved).isEqual(to: verified) else {
                            throw Failure(description: "Native approved-envelope comparison failed")
                        }
                        var changed = approved
                        changed["bcc"] = ["wisp-smoke-never-send@example.invalid"]
                        var rejected = false
                        do { _ = try run(validationOnly(changed)) }
                        catch { rejected = String(describing: error).contains("changed after approval") }
                        guard rejected else {
                            throw Failure(description: "Native approval mismatch was not rejected")
                        }
                        let emptyBodyScript = MailReplyScript.build(messageID: messageID, account: account,
                            body: testBody, replyAll: replyAll)!
                            .replacingOccurrences(of: "set content to desiredContent", with: "set content to \"\"")
                        rejected = false
                        do { _ = try run(emptyBodyScript) }
                        catch { rejected = String(describing: error).contains("did not retain") }
                        guard rejected else {
                            throw Failure(description: "Native missing-body guard did not reject empty content")
                        }
                        print("PASS native approval comparison, changed-BCC rejection, missing-body rejection (send replaced with discard)")
                    }
                    guard try outgoingIDs() == before else {
                        throw Failure(description: "Account \(index): outgoing draft IDs changed; inspect Mail")
                    }
                    print("PASS account \(index), replyAll=\(replyAll): identity, recipients, stable full envelope, draft cleanup")
                }
            }
            print("Live Mail preparation checks passed. No send command was executed.")
        } catch {
            fputs("FAIL: \(error)\n", stderr)
            exit(1)
        }
    }
}
