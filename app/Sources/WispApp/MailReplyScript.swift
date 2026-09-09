import Foundation

/// One native envelope contract used by both preparation and execution.
/// No source-message sender/subject is mislabeled as outgoing metadata.
enum MailReplyScript {
    static let fields = ["message_id", "account", "account_id", "from", "to", "cc", "bcc", "subject", "content"]

    private static func quote(_ value: String) -> String {
        "\"" + value.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
            .replacingOccurrences(of: "\r", with: "\\r")
            .replacingOccurrences(of: "\n", with: "\\n") + "\""
    }

    private static func literal(_ value: Any) -> String? {
        if let s = value as? String { return quote(s) }
        if let a = value as? [String] { return "{" + a.map(quote).joined(separator: ", ") + "}" }
        return nil
    }

    static func build(messageID: String, account: String, body: String,
                      replyAll: Bool, expected: [String: Any]? = nil) -> String? {
        let accountFilter: String
        if let id = expected?["account_id"] as? String, !id.isEmpty {
            accountFilter = "(every account whose id is \(quote(id)))"
        } else if !account.isEmpty {
            accountFilter = "(every account whose name is \(quote(account)))"
        } else {
            accountFilter = "accounts"
        }
        var finish = "close theReply saving no"
        if let expected {
            let values = fields.compactMap { expected[$0].flatMap(literal) }
            guard values.count == fields.count else { return nil }
            // Compare inside the same script immediately before send, including
            // actual From, full recipient sets and the entire rendered content.
            finish = """
            considering case
                if envelope is not {\(values.joined(separator: ", "))} then
                    close theReply saving no
                    error "The reply changed after approval. Nothing sent; prepare it again."
                end if
            end considering
            if (send theReply) is not true then error "Mail did not accept the reply for sending; check Mail before retrying."
            """
        }
        return """
        tell application "Mail"
            set theMsg to missing value
            set theAcct to ""
            set theAcctID to ""
            set sourceAddresses to {}
            set hitCount to 0
            set matchingAccounts to \(accountFilter)
            repeat with acct in matchingAccounts
                -- Use the same mailbox vocabulary as MailReader. A failure to
                -- read any searched account prevents an ungrounded unique hit.
                try
                    set mbx to mailbox "INBOX" of acct
                    set hits to (every message of mbx whose message id is \(quote(messageID)))
                on error
                    set mbx to mailbox "Inbox" of acct
                    set hits to (every message of mbx whose message id is \(quote(messageID)))
                end try
                set hitCount to hitCount + (count of hits)
                if (count of hits) > 0 then
                    set theMsg to item 1 of hits
                    set theAcct to name of acct
                    set theAcctID to id of acct
                    set sourceAddresses to email addresses of acct
                end if
            end repeat
            if hitCount is not 1 then error "Choose one current email in one account; it was moved, missing, or ambiguous."
            set theReply to reply theMsg opening window true reply to all \(replyAll ? "true" : "false")
            try
                -- Mail can return a reply backend whose content stays empty
                -- and silently ignores writes. Explicit visibility initializes
                -- it; the reply command's opening-window flag alone does not.
                set visible of theReply to true
                delay 1
                -- Do not read content before setting it. Mail's rich-text
                -- getter can retain that first (empty) value for this reply.
                -- The supplied body is complete; threading belongs to Mail's
                -- reply metadata, not to a manually copied quote.
                set desiredContent to \(quote(body))
                tell theReply
                    set content to desiredContent
                end tell
                set renderedContent to (content of theReply) as text
                considering case
                    if renderedContent does not start with \(quote(body)) then
                        error "Mail did not retain the requested reply text. Nothing sent."
                    end if
                end considering
                set fromAddress to extract address from (sender of theReply)
                ignoring case
                    if fromAddress is not in sourceAddresses then error "Mail selected a sending identity outside the chosen account. Nothing sent."
                end ignoring
                set toAddresses to address of every to recipient of theReply
                set ccAddresses to address of every cc recipient of theReply
                set bccAddresses to address of every bcc recipient of theReply
                set envelope to {(message id of theMsg) as text, theAcct as text, theAcctID as text, fromAddress as text, toAddresses, ccAddresses, bccAddresses, (subject of theReply) as text, renderedContent}
                \(finish)
                return envelope
            on error problem number code
                try
                    close theReply saving no
                end try
                error problem number code
            end try
        end tell
        """
    }

    static func decode(_ result: NSAppleEventDescriptor) -> [String: Any]? {
        guard result.numberOfItems == fields.count else { return nil }
        var envelope: [String: Any] = [:]
        for (index, field) in fields.enumerated() {
            guard let item = result.atIndex(index + 1) else { return nil }
            if ["to", "cc", "bcc"].contains(field) {
                guard item.descriptorType == NSAppleEventDescriptor.list().descriptorType else { return nil }
                var addresses: [String] = []
                if item.numberOfItems > 0 {
                    for i in 1...item.numberOfItems {
                        guard let value = item.atIndex(i)?.stringValue else { return nil }
                        addresses.append(value)
                    }
                }
                envelope[field] = addresses
            } else {
                guard let value = item.stringValue else { return nil }
                envelope[field] = value
            }
        }
        return envelope
    }
}
