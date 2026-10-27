import AVFoundation
import AppKit

// Voice input for Ask: hold a short spoken question, then hand its words to the
// Ask box like typed text. The microphone is open only while the meter shows;
// the clip is sent to Wisp's own service on this Mac (POST /voice/transcribe),
// which lets the Ask model hear it there or refuses (service/voice.py). Nothing
// is saved: the clip lives in memory until its words come back.

@MainActor
final class VoiceInput: ObservableObject {
    enum State: Equatable {
        case idle
        case listening
        case transcribing
        case failed(String)
    }

    @Published private(set) var state: State = .idle
    /// 0...1 while listening, for the meter.
    @Published private(set) var level: Float = 0
    /// The microphone was refused; the failure offers System Settings.
    @Published private(set) var needsMicrophoneAccess = false

    /// The words of a finished question. Empty never arrives here.
    var onTranscript: (String) -> Void = { _ in }

    private var recorder: VoiceRecorder?
    private var request: Task<Void, Never>?
    private var clearFailure: Task<Void, Never>?

    var isActive: Bool { state == .listening || state == .transcribing }

    var failure: String? {
        if case .failed(let message) = state { return message }
        return nil
    }

    /// The mic button and ⌥⇧Space: start listening, or finish the question early.
    func toggle() {
        switch state {
        case .listening: finish()
        case .transcribing: break
        case .idle, .failed: start()
        }
    }

    func start() {
        guard !isActive else { return }
        clearFailure?.cancel()
        needsMicrophoneAccess = false
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            beginRecording()
        case .notDetermined:
            // The person pressed the mic, so this is the one moment Wisp asks.
            state = .listening
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                Task { @MainActor [weak self] in
                    guard let self, self.state == .listening, self.recorder == nil else { return }
                    if granted { self.beginRecording() } else { self.refused() }
                }
            }
        default:
            refused()
        }
    }

    /// Stop listening and send what was said.
    func finish() {
        guard state == .listening, let recorder else { return }
        self.recorder = nil
        let clip = recorder.stop()
        guard recorder.heardSpeech, let clip else {
            fail("Didn't catch that. Tap the mic and try again.")
            return
        }
        transcribe(clip)
    }

    /// Stop without sending anything (Esc, closing the panel, typing).
    func cancel() {
        recorder?.stop()
        recorder = nil
        request?.cancel()
        request = nil
        level = 0
        if isActive { state = .idle }
    }

    func dismissFailure() {
        if failure != nil { state = .idle; needsMicrophoneAccess = false }
    }

    func openMicrophoneSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
    }

    private func refused() {
        needsMicrophoneAccess = true
        fail("Wisp can't use the microphone. Turn on Wisp in System Settings › Privacy & Security › Microphone.",
             clearAfter: nil)
    }

    private func beginRecording() {
        let recorder = VoiceRecorder()
        recorder.onLevel = { [weak self] level in
            Task { @MainActor in if self?.state == .listening { self?.level = level } }
        }
        // Weak: the recorder keeps this closure, and holding the recorder here
        // would keep it, its engine and the clip alive for the life of the app.
        recorder.onOutcome = { [weak self, weak recorder] outcome in
            Task { @MainActor in
                guard let self, let recorder else { return }
                self.ended(outcome, from: recorder)
            }
        }
        do {
            try recorder.start()
        } catch {
            fail((error as? VoiceRecorder.Failure)?.message ?? "Wisp couldn't start the microphone.")
            return
        }
        self.recorder = recorder
        state = .listening
        level = 0
    }

    private func ended(_ outcome: VoiceEndpointer.Outcome, from recorder: VoiceRecorder) {
        guard self.recorder === recorder, state == .listening else { return }
        switch outcome {
        case .listening: return
        case .noSpeech:
            self.recorder = nil
            recorder.stop()
            fail(recorder.deviceChanged
                 ? "Your microphone changed. Tap the mic and ask again."
                 : "Didn't hear anything. Tap the mic and try again.")
        case .finished, .limit:
            finish()
        }
    }

    private func transcribe(_ clip: Data) {
        state = .transcribing
        level = 0
        request = Task { [weak self] in
            let result = await WispClient().transcribe(wav: clip)
            guard let self, !Task.isCancelled, self.state == .transcribing else { return }
            self.request = nil
            switch result {
            case .success(let text) where !text.isEmpty:
                self.state = .idle
                self.onTranscript(text)
            case .success:
                self.fail("Didn't catch any words. Tap the mic and try again.")
            case .failure(let message):
                self.fail(message, clearAfter: 12)
            }
        }
    }

    private func fail(_ message: String, clearAfter seconds: Double? = 6) {
        level = 0
        state = .failed(message)
        clearFailure?.cancel()
        guard let seconds else { return }
        clearFailure = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            guard !Task.isCancelled, let self, self.failure == message else { return }
            self.state = .idle
        }
    }
}

/// The microphone itself: 16 kHz mono 16-bit PCM in memory, and the endpointer
/// that decides when the question ended. Audio arrives on the engine's own
/// thread, so everything it touches is behind one lock.
final class VoiceRecorder: @unchecked Sendable {
    struct Failure: Error { let message: String }

    var onLevel: (Float) -> Void = { _ in }
    var onOutcome: (VoiceEndpointer.Outcome) -> Void = { _ in }

    private let engine = AVAudioEngine()
    private let lock = NSLock()
    private var pcm = Data()
    private var endpointer = VoiceEndpointer()
    private var converter: AVAudioConverter?
    private var running = false
    private var reported = false
    private var configurationObserver: NSObjectProtocol?
    private var changed = false

    /// The input device changed before anything was said (AirPods connecting).
    var deviceChanged: Bool { lock.withLock { changed && !endpointer.heardSpeech } }
    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(WAVFile.sampleRate),
                                       channels: 1, interleaved: true)!

    var heardSpeech: Bool { lock.withLock { endpointer.heardSpeech } }

    func start() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw Failure(message: "No microphone is connected.")
        }
        guard let converter = AVAudioConverter(from: format, to: target) else {
            throw Failure(message: "Wisp couldn't read this microphone.")
        }
        self.converter = converter
        pcm.reserveCapacity(WAVFile.sampleRate * 2 * 8)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.receive(buffer)
        }
        // Headphones connecting or the input device changing stops the engine,
        // and no more audio arrives: end the question with what was heard.
        // Watched before start, so a change in the first moment isn't missed.
        configurationObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            self?.interrupted()
        }
        lock.withLock { running = true }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            lock.withLock { running = false }
            input.removeTap(onBus: 0)
            if let configurationObserver { NotificationCenter.default.removeObserver(configurationObserver) }
            configurationObserver = nil
            throw Failure(message: "Wisp couldn't start the microphone.")
        }
    }

    private func interrupted() {
        let report: (VoiceEndpointer.Outcome, (VoiceEndpointer.Outcome) -> Void)? = lock.withLock {
            guard running, !reported else { return nil }
            reported = true
            changed = true
            return (endpointer.heardSpeech ? .finished : .noSpeech, onOutcome)
        }
        if let (outcome, send) = report { send(outcome) }
    }

    /// Stops the microphone; the clip as a WAV, nil when nothing was recorded.
    @discardableResult
    func stop() -> Data? {
        let wasRunning = lock.withLock { () -> Bool in
            let was = running
            running = false
            return was
        }
        if wasRunning {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        if let configurationObserver {
            NotificationCenter.default.removeObserver(configurationObserver)
            self.configurationObserver = nil
        }
        // The clip leaves here as the WAV; nothing of it stays in the recorder.
        let clip = lock.withLock { () -> Data in
            let taken = pcm
            pcm = Data()
            return taken
        }
        lock.withLock {
            onLevel = { _ in }
            onOutcome = { _ in }
        }
        return clip.isEmpty ? nil : WAVFile.encode(pcm: clip)
    }

    private func receive(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 64)
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var fed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        guard error == nil, out.frameLength > 0, let samples = out.int16ChannelData?[0] else { return }
        let count = Int(out.frameLength)
        let chunk = UnsafeBufferPointer(start: samples, count: count)
        let level = VoiceEndpointer.level(chunk)
        let seconds = Double(count) / target.sampleRate
        // The callbacks are read under the lock: stop() clears them from another thread.
        let step: (VoiceEndpointer.Outcome, (Float) -> Void, (VoiceEndpointer.Outcome) -> Void)? = lock.withLock {
            guard running, !reported else { return nil }
            pcm.append(UnsafeBufferPointer(start: UnsafeRawPointer(samples).assumingMemoryBound(to: UInt8.self),
                                           count: count * 2))
            let outcome = endpointer.feed(level: level, seconds: seconds)
            if outcome != .listening { reported = true }
            return (outcome, onLevel, onOutcome)
        }
        guard let (outcome, sendLevel, sendOutcome) = step else { return }
        sendLevel(VoiceEndpointer.meter(level))
        if outcome != .listening { sendOutcome(outcome) }
    }
}

/// Reads an answer aloud with the Mac's own speech voice, on this Mac.
@MainActor
final class AnswerSpeaker: NSObject, ObservableObject, AVSpeechSynthesizerDelegate {
    @Published private(set) var speaking = false
    private let synthesizer = AVSpeechSynthesizer()

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ markdown: String) {
        let text = SpokenText.plain(markdown)
        guard !text.isEmpty else { return }
        stop()
        // No voice or rate set: the ones chosen in System Settings › Accessibility › Spoken Content.
        let utterance = AVSpeechUtterance(string: text)
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
