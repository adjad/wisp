import SwiftUI

// The Ask bar's microphone and the line under it while Wisp listens.

extension VoiceInput {
    /// What the Ask box says in place of "Ask anything…".
    var placeholder: String {
        switch state {
        case .listening: return "Listening…"
        case .transcribing: return "Writing down what you said…"
        default: return "Ask anything…"
        }
    }
}

/// Mic: tap to talk, tap again to finish early. While an answer is read aloud it
/// becomes a speaker that stops the voice.
struct VoiceMicButton: View {
    @ObservedObject var voice: VoiceInput
    @ObservedObject var speaker: AnswerSpeaker
    var action: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: action) {
            ZStack {
                if voice.state == .listening {
                    // The ring follows the voice, so it's plain the mic is open.
                    Circle()
                        .fill(Color.white.opacity(0.14))
                        .frame(width: 30, height: 30)
                        .scaleEffect(reduceMotion ? 1 : 0.8 + CGFloat(voice.level) * 0.45)
                        .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: voice.level)
                }
                if voice.state == .transcribing {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: icon)
                        .font(.system(size: 17, weight: .medium))
                        .foregroundStyle(voice.state == .listening ? Theme.textPrimary : Theme.textSecondary)
                }
            }
            .frame(width: 26, height: 26)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(voice.state == .transcribing)
        .help(label + " (⌥⇧Space)")
        .accessibilityLabel(label)
    }

    private var icon: String {
        if voice.state == .listening { return "stop.circle.fill" }
        if speaker.speaking { return "speaker.wave.2.fill" }
        return "mic"
    }

    private var label: String {
        switch voice.state {
        case .listening: return "Stop listening and send"
        case .transcribing: return "Writing down what you said"
        default: return speaker.speaking ? "Stop reading aloud" : "Ask by voice"
        }
    }
}

/// Under the Ask bar: what voice is doing, or why it couldn't.
struct VoiceStatusLine: View {
    @ObservedObject var voice: VoiceInput

    var body: some View {
        if let failure = voice.failure {
            HStack(spacing: 8) {
                Image(systemName: "mic.slash").font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
                    .accessibilityHidden(true)
                Text(failure).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                if voice.needsMicrophoneAccess {
                    NotchChrome.small("Open Settings") { voice.openMicrophoneSettings() }
                }
                NotchChrome.small("OK") { voice.dismissFailure() }
            }
            .padding(.top, 6)
        } else if voice.state == .listening {
            Text("Ask your question. Wisp sends it when you pause; tap the stop button to send now. Esc cancels.")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                .padding(.top, 6)
        }
    }
}
