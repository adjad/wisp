import Foundation

/// A node cannot contribute native actions, Markdown, or navigable URLs.
struct NodePresentation {
    let eventID: String
    let title: String
    let body: String
    var literalText: String { title + "\n\n" + body }

    init?(_ payload: [String: Any]) {
        guard let eventID = payload["event_id"] as? String, !eventID.isEmpty, eventID.utf8.count <= 200,
              let title = payload["title"] as? String, !title.isEmpty, title.utf8.count <= 1000,
              let body = payload["text"] as? String, !body.isEmpty, body.utf8.count <= 200_000 else { return nil }
        self.eventID = eventID
        self.title = title
        self.body = body
    }
}
