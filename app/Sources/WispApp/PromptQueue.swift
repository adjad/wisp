import Foundation

struct QueuedPrompt: Equatable {
    let text: String
    let image: String?
    let imageName: String?
    let researchMode: Bool
    /// A sampled clip for this prompt (memory only, never persisted), and the
    /// tag the service uses for a screen capture ("screen"). Both default to
    /// nothing so a plain prompt builds as before.
    var video: QueuedVideo? = nil
    var imageSource: String? = nil
    /// A document for this prompt (memory only, never persisted).
    var document: QueuedDocument? = nil
}

extension QueuedPrompt {
    /// The marker shown in the transcript instead of the attachment.
    var historyMarker: String? {
        if video != nil { return "[clip]" }
        if document != nil { return "[document]" }
        guard image != nil else { return nil }
        return imageSource == "screen" ? "[screen]" : "[image]"
    }
}

/// The `/agent` `video` field: 1-8 frame data URLs, the length, optional WAV (base64).
struct QueuedVideo: Equatable {
    let frames: [String]
    let seconds: Double
    let audio: String?

    var wire: [String: Any] {
        ["frames": frames, "seconds": seconds, "audio": audio.map { $0 as Any } ?? NSNull()]
    }
}

/// The `/agent` `document` field: the file name plus either its raw bytes
/// (base64) or, for a PDF with no text layer, 1-4 page data URLs.
struct QueuedDocument: Equatable {
    let name: String
    let data: String?
    let pages: [String]

    var wire: [String: Any] {
        pages.isEmpty ? ["name": name, "data": data ?? ""] : ["name": name, "pages": pages]
    }
}

enum PromptWork: Equatable {
    case prompt(QueuedPrompt)
    case dailySummary
}

struct PromptDispatch: Equatable {
    let id: UInt64
    let work: PromptWork
}

/// Dropped prompts the server may have started on, set aside because the user
/// had typed something else meanwhile. Never sent by itself: restoring one only
/// hands it back to be put in the box. A later drop doesn't displace an earlier
/// one; Return on an empty box brings back the most recent first.
struct SetAsidePrompts {
    private var stack: [QueuedPrompt] = []

    var count: Int { stack.count }

    mutating func push(_ prompt: QueuedPrompt?) {
        if let prompt { stack.append(prompt) }
    }

    mutating func restore() -> QueuedPrompt? {
        stack.popLast()
    }

    mutating func reset() {
        stack.removeAll(keepingCapacity: false)
    }
}

struct PromptSubmissionState {
    private(set) var active: PromptDispatch?
    private var queued: [QueuedPrompt] = []
    private var nextID: UInt64 = 0
    /// Queued prompts wait for the dropped one they followed: it is back in the
    /// box, and they go after it, in order, once it is sent again (or Return is
    /// pressed on an empty box).
    private(set) var held = false

    var queuedCount: Int { queued.count }
    var isActive: Bool { active != nil }

    /// What queued prompts are waiting on, for the line beside the box.
    enum Waiting: Equatable { case reply, summary, research, resend }
    var waiting: Waiting? {
        guard !queued.isEmpty else { return nil }
        switch active?.work {
        case .dailySummary: return .summary
        case .prompt(let prompt): return prompt.researchMode ? .research : .reply
        case nil: return held ? .resend : nil
        }
    }

    func isCurrent(_ id: UInt64) -> Bool {
        active?.id == id
    }

    mutating func submit(_ prompt: QueuedPrompt) -> PromptDispatch? {
        guard active == nil else {
            queued.append(prompt)
            return nil
        }
        held = false
        return begin(.prompt(prompt))
    }

    /// Sends the held queue on: Return on an empty box after a drop.
    mutating func resume() -> PromptDispatch? {
        guard active == nil, !queued.isEmpty else { return nil }
        held = false
        return begin(.prompt(queued.removeFirst()))
    }

    mutating func beginDailySummary() -> PromptDispatch? {
        guard active == nil else { return nil }
        return begin(.dailySummary)
    }

    mutating func complete(_ id: UInt64) -> PromptDispatch? {
        guard active?.id == id else { return nil }
        active = nil
        // Held prompts wait for the dropped one, even past a summary run meanwhile.
        guard !queued.isEmpty, !held else { return nil }
        return begin(.prompt(queued.removeFirst()))
    }

    mutating func dropForRetry(_ id: UInt64) -> QueuedPrompt? {
        guard active?.id == id, case .prompt(let prompt) = active?.work else { return nil }
        active = nil
        held = !queued.isEmpty
        return prompt
    }

    /// The dropped prompt goes first in the held queue instead of back in the
    /// box, when the box already holds something typed meanwhile.
    mutating func dropToQueue(_ id: UInt64) -> Bool {
        guard active?.id == id, case .prompt(let prompt) = active?.work else { return false }
        active = nil
        queued.insert(prompt, at: 0)
        held = true
        return true
    }

    mutating func reset() {
        active = nil
        held = false
        queued.removeAll(keepingCapacity: false)
    }

    private mutating func begin(_ work: PromptWork) -> PromptDispatch {
        nextID &+= 1
        if nextID == 0 { nextID &+= 1 }
        let dispatch = PromptDispatch(id: nextID, work: work)
        active = dispatch
        return dispatch
    }
}
