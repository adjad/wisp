import Foundation

/// Which voice reads answers aloud (AnswerSpeaker), and how fast. Pure: the
/// installed voices come in as Candidates, so the checks never need a speech
/// engine (tests/ReadAloudVoiceChecks.swift). Settings › General picks it.
enum ReadAloudVoice {
    /// Absent: the automatic male voice. `systemChoice`: the Spoken Content
    /// voice from System Settings. Anything else: a voice identifier.
    static let defaultsKey = "WispReadAloudVoice"
    static let systemChoice = "system"
    /// A multiple of the normal speaking rate; absent is 1.
    static let rateKey = "WispReadAloudRate"
    static let rateRange: ClosedRange<Double> = 0.75...1.5

    enum Gender: Equatable { case male, female, unspecified }

    struct Candidate: Equatable {
        let id: String
        let name: String
        let language: String
        /// 0 default, 1 enhanced, 2 premium.
        let quality: Int
        let gender: Gender

        var qualityLabel: String { quality >= 2 ? "Premium" : quality == 1 ? "Enhanced" : "Default" }
        var genderLabel: String? {
            switch gender {
            case .male: return "Male"
            case .female: return "Female"
            case .unspecified: return ReadAloudVoice.knownMaleNames.contains(name) ? "Male" : nil
            }
        }
        /// The old speech-synthesis voices (Fred, Zarvox, Bells…), all but Alex:
        /// fine to pick, never the default.
        var isNovelty: Bool {
            id.hasPrefix("com.apple.speech.synthesis.voice.") && name != "Alex"
        }
    }

    /// Male voices macOS ships or offers for download, best first, for when a
    /// voice doesn't report its gender (the Eloquence voices report none).
    static let knownMaleNames = ["Aaron", "Alex", "Evan", "Nathan", "Tom", "Daniel", "Arthur", "Oliver",
                                 "Jamie", "Gordon", "Lee", "Rishi", "Reed", "Eddy"]

    enum Resolution: Equatable {
        case system
        case voice(String)
    }

    /// The voices the picker lists: the person's language and English, best
    /// quality first, novelty voices last.
    static func listed(_ voices: [Candidate], preferredLanguage: String) -> [Candidate] {
        let own = languageCode(preferredLanguage)
        return voices
            .filter { ["en", own].contains(languageCode($0.language)) }
            .sorted { a, b in
                if a.isNovelty != b.isNovelty { return !a.isNovelty }
                if a.quality != b.quality { return a.quality > b.quality }
                if a.name != b.name { return a.name.localizedStandardCompare(b.name) == .orderedAscending }
                return a.language < b.language
            }
    }

    /// The automatic voice: the best male English voice, premium before
    /// enhanced before default, en-US before other English. A voice that
    /// reports no gender counts only by a known male name. Nil when there is
    /// none, which means the system voice.
    static func automatic(_ voices: [Candidate]) -> Candidate? {
        let english = voices.filter { languageCode($0.language) == "en" && !$0.isNovelty }
        func best(_ pool: [Candidate]) -> Candidate? {
            pool.min { a, b in
                if a.quality != b.quality { return a.quality > b.quality }
                let aUS = a.language == "en-US", bUS = b.language == "en-US"
                if aUS != bUS { return aUS }
                return knownRank(a) < knownRank(b)
            }
        }
        if let male = best(english.filter { $0.gender == .male }) { return male }
        return best(english.filter { $0.gender == .unspecified && knownMaleNames.contains($0.name) })
    }

    /// What to speak with: the saved voice while it is still installed,
    /// otherwise the automatic one, otherwise the system voice.
    static func resolve(saved: String?, among voices: [Candidate]) -> Resolution {
        if saved == systemChoice { return .system }
        if let saved, !saved.isEmpty, voices.contains(where: { $0.id == saved }) { return .voice(saved) }
        if let auto = automatic(voices) { return .voice(auto.id) }
        return .system
    }

    /// System Settings › Accessibility › Spoken Content, where Manage Voices is.
    static let downloadPane = "x-apple.systempreferences:com.apple.preference.universalaccess?SpeakableItems"

    /// The notch's speaking rate chips. Settings' slider can land between them.
    static let rateChoices: [Double] = [0.75, 0.9, 1, 1.15, 1.3, 1.5]

    static func rateText(_ multiple: Double) -> String {
        abs(multiple - 1) < 0.001 ? "Normal" : "\(Int((multiple * 100).rounded()))% of normal"
    }

    /// The name on a notch chip, with the region where one name has several, and
    /// the quality when it's better than basic.
    static func shortLabel(_ voice: Candidate, among voices: [Candidate]) -> String {
        var parts = [voice.name]
        if voices.filter({ $0.name == voice.name }).count > 1,
           let region = voice.language.split(whereSeparator: { $0 == "-" || $0 == "_" }).dropFirst().first {
            parts.append(String(region).uppercased())
        }
        if voice.quality > 0 { parts.append(voice.qualityLabel) }
        return parts.joined(separator: " · ")
    }

    /// True when every voice the notch lists is basic quality, so the natural ones
    /// are still to be downloaded.
    static func onlyBasicVoices(_ voices: [Candidate]) -> Bool {
        !voices.contains { $0.quality > 0 && !$0.isNovelty }
    }

    /// The saved rate multiple, within range; 1 when unset or unreadable.
    static func rateMultiple(_ stored: Double?) -> Double {
        guard let stored, stored.isFinite, stored > 0 else { return 1 }
        return min(max(stored, rateRange.lowerBound), rateRange.upperBound)
    }

    private static func knownRank(_ voice: Candidate) -> Int {
        knownMaleNames.firstIndex(of: voice.name) ?? knownMaleNames.count
    }

    private static func languageCode(_ tag: String) -> String {
        String(tag.lowercased().split(whereSeparator: { $0 == "-" || $0 == "_" }).first ?? "")
    }
}
