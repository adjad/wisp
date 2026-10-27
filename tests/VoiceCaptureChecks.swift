import Foundation

// Pure checks for voice input (app/Sources/WispApp/VoiceCapture.swift): when a
// spoken question ends, the WAV the service receives, and what of a reply is
// read aloud. No microphone, speech engine or service.
@main
struct VoiceCaptureChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    /// Feed `seconds` of one level in 0.1 s chunks; the first outcome that isn't .listening.
    static func run(_ e: inout VoiceEndpointer, level: Float, seconds: Double) -> VoiceEndpointer.Outcome {
        var t = 0.0
        while t < seconds - 1e-9 {
            let outcome = e.feed(level: level, seconds: 0.1)
            if outcome != .listening { return outcome }
            t += 0.1
        }
        return .listening
    }

    static func main() {
        // A question, then a pause, ends the clip.
        var e = VoiceEndpointer()
        check(run(&e, level: -58, seconds: 0.3) == .listening, "Learning the room")
        check(run(&e, level: -22, seconds: 1.5) == .listening, "Talking keeps listening")
        check(run(&e, level: -57, seconds: 1.0) == .listening, "A short pause is not the end")
        check(run(&e, level: -24, seconds: 1.0) == .listening, "Talking again")
        check(run(&e, level: -57, seconds: 1.5) == .finished, "A pause after speech ends the question")

        // Silence from the start gives up; nothing is sent.
        var quiet = VoiceEndpointer()
        check(run(&quiet, level: -60, seconds: 10) == .noSpeech, "Nothing said: no speech")
        check(quiet.elapsed <= VoiceEndpointer.noSpeechTimeout + 0.15, "Gives up at the timeout")

        // A click or cough isn't speech, so it doesn't start the end-of-question pause.
        var click = VoiceEndpointer()
        _ = run(&click, level: -58, seconds: 0.3)
        _ = run(&click, level: -15, seconds: 0.1)
        check(!click.heardSpeech, "A 0.1 s click is not speech")
        check(run(&click, level: -58, seconds: 3) == .listening, "After a click, still waiting for speech")

        // A noisy room raises the bar: its own hum is not speech.
        var cafe = VoiceEndpointer()
        _ = run(&cafe, level: -36, seconds: 0.3)
        check(run(&cafe, level: -35, seconds: 3) == .listening && !cafe.heardSpeech, "Room noise is not speech")
        check(run(&cafe, level: -12, seconds: 1) == .listening && cafe.heardSpeech, "Speech over the noise is")
        check(run(&cafe, level: -35, seconds: 2) == .finished, "Back to the room's level ends it")

        // The longest clip the model takes.
        var long = VoiceEndpointer()
        _ = run(&long, level: -58, seconds: 0.3)
        check(run(&long, level: -20, seconds: 31) == .limit, "30 s of talking stops at the limit")

        check(VoiceEndpointer.meter(-80) == 0 && VoiceEndpointer.meter(0) == 1 && VoiceEndpointer.meter(-35) == 0.5,
              "Meter maps -60…-10 dB to 0…1")
        let silent = [Int16](repeating: 0, count: 160)
        let loud = [Int16](repeating: 16384, count: 160)
        check(silent.withUnsafeBufferPointer(VoiceEndpointer.level) == -80, "Silence is the floor")
        check(abs(loud.withUnsafeBufferPointer(VoiceEndpointer.level) - (-6.02)) < 0.1, "Half scale is -6 dB")

        // The WAV the service reads (service/voice.py wav_seconds).
        let pcm = Data(repeating: 0, count: 32_000)
        let wav = WAVFile.encode(pcm: pcm)
        check(wav.count == 44 + 32_000, "44-byte header")
        check(String(decoding: wav[0..<4], as: UTF8.self) == "RIFF" && String(decoding: wav[8..<12], as: UTF8.self) == "WAVE"
              && String(decoding: wav[36..<40], as: UTF8.self) == "data", "RIFF/WAVE/data")
        func u32(_ at: Int) -> UInt32 { wav[at..<at + 4].enumerated().reduce(0) { $0 | UInt32($1.element) << (8 * $1.offset) } }
        func u16(_ at: Int) -> UInt16 { UInt16(wav[at]) | UInt16(wav[at + 1]) << 8 }
        check(u16(20) == 1 && u16(22) == 1 && u32(24) == 16_000 && u16(34) == 16 && u32(40) == 32_000,
              "PCM, mono, 16 kHz, 16-bit")
        check(WAVFile.seconds(pcmBytes: 32_000) == 1.0, "One second")

        // When answers are read aloud.
        check(VoiceReplyMode.afterVoice.speaks(askedByVoice: true) && !VoiceReplyMode.afterVoice.speaks(askedByVoice: false),
              "Default: only answers to spoken questions")
        check(VoiceReplyMode.always.speaks(askedByVoice: false) && !VoiceReplyMode.off.speaks(askedByVoice: true), "Always / Off")

        // What is said: words, not symbols.
        let reply = """
        ## Your afternoon 🌤️

        - **2:00 PM** — Design review with [Priya](https://cal.example/1)
        - 5:30 PM: Gym 💪
        ```
        let x = 1
        ```
        You're free after 7! 🎉
        """
        let spoken = SpokenText.plain(reply)
        check(spoken == "Your afternoon. 2:00 PM — Design review with Priya. 5:30 PM: Gym. You're free after 7!",
              "Spoken: " + spoken)
        check(!SpokenText.plain("Call 555-0100 #2 *now*").contains("*"), "Emphasis marks go")
        check(SpokenText.plain("Call 555-0100 #2").contains("#2"), "Keycap characters in text stay")
        let longReply = String(repeating: "This is a sentence about your day. ", count: 60)
        let cut = SpokenText.plain(longReply)
        check(cut.count <= SpokenText.maxCharacters + 30 && cut.hasSuffix("The rest is on screen."), "Long replies stop at a sentence")
        check(SpokenText.plain("| a | b |\n|---|---|\n| 1 | 2 |").contains("1"), "Tables read their cells")

        print("PASS: voice capture — end of speech, WAV for the service, spoken replies")
    }
}
