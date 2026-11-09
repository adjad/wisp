import AVFoundation
import SwiftUI

extension ReadAloudVoice {
    /// The Mac's installed speech voices, as Candidates.
    static func installed() -> [Candidate] {
        AVSpeechSynthesisVoice.speechVoices().map { voice in
            let quality: Int
            switch voice.quality {
            case .premium: quality = 2
            case .enhanced: quality = 1
            default: quality = 0
            }
            let gender: Gender
            switch voice.gender {
            case .male: gender = .male
            case .female: gender = .female
            default: gender = .unspecified
            }
            return Candidate(id: voice.identifier, name: voice.name, language: voice.language,
                             quality: quality, gender: gender)
        }
    }

    static var savedChoice: String? { UserDefaults.standard.string(forKey: defaultsKey) }
    static var savedRateMultiple: Double {
        rateMultiple(UserDefaults.standard.object(forKey: rateKey) as? Double)
    }

    /// Sets the chosen voice and rate on an utterance. A saved voice that was
    /// removed falls back to the automatic one, then the system voice.
    static func apply(to utterance: AVSpeechUtterance, choice: String? = savedChoice,
                      rateMultiple: Double = savedRateMultiple) {
        if case .voice(let id) = resolve(saved: choice, among: installed()),
           let voice = AVSpeechSynthesisVoice(identifier: id) {
            utterance.voice = voice
        }
        // Unchanged at 1, as before the picker.
        guard abs(rateMultiple - 1) > 0.001 else { return }
        let rate = Double(AVSpeechUtteranceDefaultSpeechRate) * rateMultiple
        utterance.rate = Float(min(max(rate, Double(AVSpeechUtteranceMinimumSpeechRate)),
                                   Double(AVSpeechUtteranceMaximumSpeechRate)))
    }
}

/// Settings › General › Voice: the read-aloud voice, its rate, and a preview.
struct ReadAloudVoiceSettings: View {
    @AppStorage(ReadAloudVoice.defaultsKey) private var choice = ""
    @AppStorage(ReadAloudVoice.rateKey) private var rate = 1.0
    @StateObject private var preview = ReadAloudPreview()
    @State private var voices: [ReadAloudVoice.Candidate] = []

    private var listed: [ReadAloudVoice.Candidate] {
        ReadAloudVoice.listed(voices, preferredLanguage: Locale.preferredLanguages.first ?? "en")
    }
    private var automatic: ReadAloudVoice.Candidate? { ReadAloudVoice.automatic(voices) }
    /// A saved voice that is no longer installed shows as Automatic, which is what speaks.
    private var selection: Binding<String> {
        Binding(get: {
            choice == ReadAloudVoice.systemChoice || listed.contains(where: { $0.id == choice }) ? choice : ""
        }, set: { choice = $0 })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Voice").font(.title3.weight(.medium))
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Read-aloud voice")
                            .font(.system(size: 14, weight: .medium))
                        Text("The voice Wisp uses when it reads an answer to you. More natural voices "
                             + "can be downloaded in System Settings › Accessibility › Spoken Content "
                             + "› System Voice › Manage Voices.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    Picker("Read-aloud voice", selection: selection) {
                        Text(automatic.map { "Automatic · \($0.name)" } ?? "Automatic").tag("")
                        Text("Use system voice").tag(ReadAloudVoice.systemChoice)
                        Divider()
                        ForEach(listed.filter { !$0.isNovelty }, id: \.id) { voice in
                            Text(label(voice)).tag(voice.id)
                        }
                        let novelty = listed.filter(\.isNovelty)
                        if !novelty.isEmpty {
                            Divider()
                            ForEach(novelty, id: \.id) { voice in Text(label(voice)).tag(voice.id) }
                        }
                    }
                    .pickerStyle(.menu)
                    .labelsHidden()
                    .frame(width: 260)
                }

                HStack(alignment: .center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Speaking rate")
                            .font(.system(size: 14, weight: .medium))
                        Text(rateLabel)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Slider(value: Binding(get: { ReadAloudVoice.rateMultiple(rate) }, set: { rate = $0 }),
                           in: ReadAloudVoice.rateRange, step: 0.05)
                        .frame(width: 180)
                        .accessibilityLabel("Speaking rate")
                    Button(preview.speaking ? "Stop" : "Preview") {
                        if preview.speaking { preview.stop() } else { preview.play(choice: choice, rate: rate) }
                    }
                }
            }
        }
        .onAppear { voices = ReadAloudVoice.installed() }
    }

    private var rateLabel: String { ReadAloudVoice.rateText(ReadAloudVoice.rateMultiple(rate)) }

    private func label(_ voice: ReadAloudVoice.Candidate) -> String {
        let code = voice.language.split(separator: "-").dropFirst().first.map(String.init)
        let region = code.flatMap { Locale.current.localizedString(forRegionCode: $0) } ?? voice.language
        return ([voice.name, voice.qualityLabel, voice.genderLabel].compactMap { $0 } + [region])
            .joined(separator: " · ")
    }
}

/// Speaks a short sample with the voice and rate on screen.
@MainActor
final class ReadAloudPreview: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published private(set) var speaking = false
    private let synthesizer = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func play(choice: String, rate: Double) {
        stop()
        let utterance = AVSpeechUtterance(string: "Hi, I'm Wisp. This is how I'll read your answers.")
        ReadAloudVoice.apply(to: utterance, choice: choice.isEmpty ? nil : choice,
                             rateMultiple: ReadAloudVoice.rateMultiple(rate))
        speaking = true
        synthesizer.speak(utterance)
    }

    func stop() {
        guard synthesizer.isSpeaking || speaking else { return }
        synthesizer.stopSpeaking(at: .immediate)
        speaking = false
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in if !self.synthesizer.isSpeaking { self.speaking = false } }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in if !self.synthesizer.isSpeaking { self.speaking = false } }
    }
}
