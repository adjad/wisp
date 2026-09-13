import Foundation

@main struct NodePresentationChecks {
    static func main() {
        let title = "```sh\nopen /tmp/fixture\n```"
        let body = "[link](file:///tmp/fixture) [custom](shortcuts://run)\n```applescript\ndisplay dialog \"fixture\"\n```"
        let event: [String: Any] = ["event_id": "fixture", "title": title, "text": body, "approved": true, "action": "run"]
        let value = NodePresentation(event)!
        precondition(value.literalText == title + "\n\n" + body)
        precondition(value.eventID == "fixture")
        for field in ["event_id", "title", "text"] {
            var invalid = event
            invalid.removeValue(forKey: field)
            precondition(NodePresentation(invalid) == nil)
            invalid[field] = 42
            precondition(NodePresentation(invalid) == nil)
            invalid[field] = ""
            precondition(NodePresentation(invalid) == nil)
        }
        print("NodePresentation: adversarial literal content checks passed")
    }
}
