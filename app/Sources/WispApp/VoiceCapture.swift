import Foundation

// The pure half of voice input: when a spoken question has ended, how the clip
// is written down for the service, and what of a reply is worth saying aloud.
// No microphone, engine or network here (tests/VoiceCaptureChecks.swift).

/// Decides, from the clip's loudness, when the person has finished talking.
///
/// Like Siri: talking starts the clip, a pause after it ends the clip, and
/// silence from the start gives up. The quiet level is learned from the room in
/// the first moments, so a fan or a café doesn't count as speech.
struct VoiceEndpointer {
    enum Outcome: Equatable {
        case listening
        /// Speech, then a pause: send the clip.
        case finished
        /// Nothing said: send nothing.
        case noSpeech
        /// The longest clip the model takes: send what there is.
        case limit
    }

    static let maxSeconds: Double = 30
    /// The pause that ends a question. Long enough for "um", short enough to feel quick.
    static let endPause: Double = 1.3
    /// No speech this long after starting: give up.
    static let noSpeechTimeout: Double = 7
    /// Speech must last this long in total to count, so a cough or click doesn't.
    static let minSpeech: Double = 0.18
    /// The first moments only learn the room's level (and skip the click of starting).
    static let settle: Double = 0.25

    private(set) var elapsed: Double = 0
    private(set) var speech: Double = 0
    private(set) var silenceAfterSpeech: Double = 0
    private(set) var noiseFloor: Float = -55

    var heardSpeech: Bool { speech >= Self.minSpeech }

    /// Louder than this counts as speech: well above the room, never below a whisper.
    var threshold: Float { max(noiseFloor + 14, -48) }

    /// Feed one chunk's level (dBFS) and length; returns whether to keep listening.
    mutating func feed(level: Float, seconds: Double) -> Outcome {
        elapsed += seconds
        if elapsed <= Self.settle {
            noiseFloor = min(max(level, -80), -30)
            return .listening
        }
        if level >= threshold {
            speech += seconds
            silenceAfterSpeech = 0
        } else {
            // The room level drifts down to quieter chunks; never up toward speech.
            if level < noiseFloor + 6 { noiseFloor = noiseFloor * 0.95 + max(level, -80) * 0.05 }
            if heardSpeech { silenceAfterSpeech += seconds }
        }
        if elapsed >= Self.maxSeconds { return heardSpeech ? .limit : .noSpeech }
        if heardSpeech && silenceAfterSpeech >= Self.endPause { return .finished }
        if !heardSpeech && elapsed >= Self.noSpeechTimeout { return .noSpeech }
        return .listening
    }

    /// 0...1 for the listening meter.
    static func meter(_ level: Float) -> Float {
        min(1, max(0, (level + 60) / 50))
    }

    /// Level in dBFS of 16-bit samples.
    static func level(_ samples: UnsafeBufferPointer<Int16>) -> Float {
        guard !samples.isEmpty else { return -80 }
        var sum: Double = 0
        for s in samples { let v = Double(s) / 32768; sum += v * v }
        let rms = (sum / Double(samples.count)).squareRoot()
        return rms > 0 ? max(-80, Float(20 * log10(rms))) : -80
    }
}

/// A 16-bit PCM mono WAV, the one format the service takes.
enum WAVFile {
    static let sampleRate = 16_000

    static func encode(pcm: Data, sampleRate: Int = sampleRate) -> Data {
        var out = Data(capacity: 44 + pcm.count)
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { out.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { out.append(contentsOf: $0) } }
        out.append(contentsOf: Array("RIFF".utf8)); u32(UInt32(36 + pcm.count))
        out.append(contentsOf: Array("WAVE".utf8))
        out.append(contentsOf: Array("fmt ".utf8)); u32(16)
        u16(1); u16(1); u32(UInt32(sampleRate)); u32(UInt32(sampleRate * 2)); u16(2); u16(16)
        out.append(contentsOf: Array("data".utf8)); u32(UInt32(pcm.count))
        out.append(pcm)
        return out
    }

    static func seconds(pcmBytes: Int, sampleRate: Int = sampleRate) -> Double {
        Double(pcmBytes) / Double(sampleRate * 2)
    }
}

/// When Wisp reads its answer aloud.
enum VoiceReplyMode: String, CaseIterable {
    /// Only answers to questions asked by voice, like Siri. The default.
    case afterVoice
    case always
    case off

    static let defaultsKey = "WispVoiceReplies"

    static var current: VoiceReplyMode {
        UserDefaults.standard.string(forKey: defaultsKey).flatMap(VoiceReplyMode.init(rawValue:)) ?? .afterVoice
    }

    var title: String {
        switch self {
        case .afterVoice: return "After voice questions"
        case .always: return "Always"
        case .off: return "Off"
        }
    }

    func speaks(askedByVoice: Bool) -> Bool {
        switch self {
        case .afterVoice: return askedByVoice
        case .always: return true
        case .off: return false
        }
    }
}

/// What of a Markdown reply a speech voice should say: the words, without
/// symbols, links, code or emoji, cut at a sentence once it runs long.
enum SpokenText {
    static let maxCharacters = 900

    static func plain(_ markdown: String) -> String {
        var lines: [String] = []
        var inCode = false
        for raw in markdown.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("```") { inCode.toggle(); continue }
            if inCode || line.isEmpty || line.allSatisfy({ "-*_=|:".contains($0) }) { continue }
            var text = line
            // Headings, quotes, bullets and numbered items keep only their words.
            while let first = text.first, "#>".contains(first) { text.removeFirst() }
            for bullet in ["- [ ] ", "- [x] ", "- ", "* ", "+ ", "• "] where text.hasPrefix(bullet) {
                text.removeFirst(bullet.count); break
            }
            text = text.replacingOccurrences(of: #"\[([^\]]*)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
            text = text.replacingOccurrences(of: #"https?://\S+"#, with: "", options: .regularExpression)
            text = text.replacingOccurrences(of: #"`([^`]*)`"#, with: "$1", options: .regularExpression)
            text = text.replacingOccurrences(of: #"(\*\*|__|\*|~~)"#, with: "", options: .regularExpression)
            text = text.replacingOccurrences(of: "|", with: ", ")
            text = String(String.UnicodeScalarView(text.unicodeScalars.filter { !isEmoji($0) }))
            text = text.trimmingCharacters(in: .whitespaces)
            guard !text.isEmpty else { continue }
            // Each line is its own sentence when spoken.
            if let last = text.last, !".!?:;,".contains(last) { text += "." }
            lines.append(text)
        }
        let joined = lines.joined(separator: " ")
            .replacingOccurrences(of: #"\s{2,}"#, with: " ", options: .regularExpression)
        return cut(joined)
    }

    private static func isEmoji(_ scalar: Unicode.Scalar) -> Bool {
        let p = scalar.properties
        // Digits, # and * are "emoji" too, but only as keycaps; keep them.
        if scalar.isASCII { return false }
        return p.isEmojiPresentation || (p.isEmoji && scalar.value > 0x2100)
            || scalar.value == 0xFE0F || scalar.value == 0x200D || (0x1F3FB...0x1F3FF).contains(scalar.value)
    }

    private static func cut(_ text: String) -> String {
        guard text.count > maxCharacters else { return text }
        let head = String(text.prefix(maxCharacters))
        if let end = head.lastIndex(where: { ".!?".contains($0) }), head.distance(from: head.startIndex, to: end) > maxCharacters / 3 {
            return String(head[...end]) + " The rest is on screen."
        }
        return head + "… The rest is on screen."
    }
}
