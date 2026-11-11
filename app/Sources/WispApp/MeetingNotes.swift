import AVFoundation
import AppKit
import SwiftUI

// Meeting notes: Wisp listens to a meeting through the microphone, the Ask model
// writes the words down on this Mac, and when the meeting ends it writes a
// summary, the decisions, the action items and a follow-up (service/meetings.py).
//
// The microphone is open only while a meeting is recording, and macOS shows its
// own orange indicator the whole time. Audio is cut into windows of at most 25 s
// and sent to Wisp's own service on this Mac; a window lives in memory until it
// is sent and is never written to disk. Only the text is kept.
//
// This file never asks macOS for the microphone. That question belongs to the
// mic button in Ask (VoiceInput.start); a meeting starts only once it is allowed.

struct MeetingAction: Equatable, Identifiable {
    let id: Int
    let task: String
    let owner: String
    let due: String
    /// The date a reminder was added for, "" when none was.
    let reminder: String
}

struct MeetingWriteUp: Equatable {
    let summary: String
    let decisions: [String]
    let actions: [MeetingAction]
    let followUp: String
}

struct Meeting: Equatable, Identifiable {
    let id: String
    let title: String
    let status: String
    let startedAt: Date
    let heard: Double
    let missed: Double
    let notes: MeetingWriteUp?
    let error: String
    let transcript: String

    init?(_ json: [String: Any]) {
        guard let id = json["id"] as? String, let status = json["status"] as? String else { return nil }
        self.id = id
        self.status = status
        title = json["title"] as? String ?? ""
        startedAt = Date(timeIntervalSince1970: json["started_at"] as? Double ?? 0)
        heard = json["heard_s"] as? Double ?? 0
        missed = json["missed_s"] as? Double ?? 0
        error = json["error"] as? String ?? ""
        transcript = json["transcript"] as? String ?? ""
        if let raw = json["notes"] as? [String: Any] {
            let items = raw["action_items"] as? [[String: Any]] ?? []
            notes = MeetingWriteUp(
                summary: raw["summary"] as? String ?? "",
                decisions: raw["decisions"] as? [String] ?? [],
                actions: items.enumerated().map { index, item in
                    MeetingAction(id: index, task: item["task"] as? String ?? "",
                                  owner: item["owner"] as? String ?? "", due: item["due"] as? String ?? "",
                                  reminder: item["reminder"] as? String ?? "")
                },
                followUp: raw["follow_up"] as? String ?? "")
        } else {
            notes = nil
        }
    }

    var displayTitle: String { title.isEmpty ? "Meeting" : title }

    /// "12 min" or "40 s": how much of the meeting was heard.
    static func span(_ seconds: Double) -> String {
        let whole = Int(seconds.rounded())
        return whole < 60 ? "\(whole) s" : "\(max(1, (whole + 30) / 60)) min"
    }

    /// Never says the whole meeting was heard when part of it was missed.
    var coverage: String {
        missed >= 1 ? "Heard \(Self.span(heard)); missed about \(Self.span(missed)) while Wisp was busy."
                    : "Heard \(Self.span(heard))."
    }
}

/// The meeting routes on Wisp's own service. JSON only and no Origin header,
/// which is what the service requires of the app (service/meetings_api.py).
enum MeetingClient {
    enum Reply {
        case ok([String: Any])
        case refused(String)
    }

    static func send(_ method: String, _ path: String, body: [String: Any]? = nil,
                     timeout: TimeInterval = 30) async -> Reply {
        var request = URLRequest(url: WispClient.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if method != "GET" && method != "DELETE" {
            request.httpBody = try? JSONSerialization.data(withJSONObject: body ?? [:])
        }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            return .refused("Wisp isn't answering yet. Try again in a moment.")
        }
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        if (200..<300).contains(http.statusCode) { return .ok(json) }
        return .refused(json["detail"] as? String ?? "Wisp couldn't do that. Try again.")
    }
}

/// The microphone for a meeting: 16 kHz mono 16-bit PCM, handed out in windows.
/// Audio arrives on the engine's own thread, so everything it touches is behind one lock.
final class MeetingRecorder: @unchecked Sendable {
    struct Failure: Error { let message: String }

    /// 25 s of 16 kHz 16-bit mono: under the 30 s the model's audio encoder takes.
    static let windowBytes = 25 * WAVFile.sampleRate * 2
    /// A tail shorter than this cannot hold a word.
    static let minimumBytes = WAVFile.sampleRate * 2 / 4

    var onWindow: (Data) -> Void = { _ in }
    var onLevel: (Float) -> Void = { _ in }
    var onFailure: (String) -> Void = { _ in }

    private let engine = AVAudioEngine()
    private let lock = NSLock()
    private var pcm = Data()
    private var converter: AVAudioConverter?
    private var running = false
    private var observer: NSObjectProtocol?
    private let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(WAVFile.sampleRate),
                                       channels: 1, interleaved: true)!

    func start() throws {
        try open()
        // Headphones connecting or the input changing stops the engine. A meeting
        // carries on with the new microphone instead of ending.
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            self?.reopen()
        }
    }

    private func open() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw Failure(message: "No microphone is connected.")
        }
        guard let converter = AVAudioConverter(from: format, to: target) else {
            throw Failure(message: "Wisp couldn't read this microphone.")
        }
        lock.withLock { self.converter = converter; running = true }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            self?.receive(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            lock.withLock { running = false }
            input.removeTap(onBus: 0)
            throw Failure(message: "Wisp couldn't start the microphone.")
        }
    }

    private func reopen() {
        guard lock.withLock({ running }) else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        do {
            try open()
        } catch {
            lock.withLock { running = false }
            let send = lock.withLock { onFailure }
            send((error as? Failure)?.message ?? "The microphone stopped.")
        }
    }

    /// Stops the microphone; what was left over as a last window, nil when too short to hold a word.
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
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        let tail = lock.withLock { () -> Data in
            let taken = pcm
            pcm = Data()
            onWindow = { _ in }
            onLevel = { _ in }
            onFailure = { _ in }
            return taken
        }
        return tail.count >= Self.minimumBytes ? WAVFile.encode(pcm: tail) : nil
    }

    private func receive(_ buffer: AVAudioPCMBuffer) {
        guard let converter = lock.withLock({ self.converter }) else { return }
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
        let level = VoiceEndpointer.level(UnsafeBufferPointer(start: samples, count: count))
        let step: (Data?, (Data) -> Void, (Float) -> Void)? = lock.withLock {
            guard running else { return nil }
            pcm.append(UnsafeBufferPointer(start: UnsafeRawPointer(samples).assumingMemoryBound(to: UInt8.self),
                                           count: count * 2))
            var window: Data?
            if pcm.count >= Self.windowBytes {
                window = pcm.prefix(Self.windowBytes)
                pcm = Data(pcm.dropFirst(Self.windowBytes))
            }
            return (window, onWindow, onLevel)
        }
        guard let (window, sendWindow, sendLevel) = step else { return }
        sendLevel(VoiceEndpointer.meter(level))
        if let window { sendWindow(WAVFile.encode(pcm: window)) }
    }
}

@MainActor
final class MeetingModel: ObservableObject {
    enum Phase: Equatable { case idle, recording, writing }

    static let shared = MeetingModel()

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var level: Float = 0
    @Published private(set) var elapsed = 0
    /// The words heard so far in the meeting being recorded.
    @Published private(set) var heardSoFar = ""
    @Published private(set) var missed: Double = 0
    @Published private(set) var message: String?
    /// The meeting whose notes are open.
    @Published private(set) var current: Meeting?
    @Published private(set) var past: [Meeting] = []
    @Published private(set) var busyAction: Int?

    private var recorder: MeetingRecorder?
    private var meetingID: String?
    private var nextWindow = 0
    private var uploads: Task<Void, Never>?
    private var clock: Timer?
    private var startedAt = Date()

    var elapsedText: String { String(format: "%d:%02d", elapsed / 60, elapsed % 60) }

    func refresh() async {
        guard case .ok(let json) = await MeetingClient.send("GET", "meetings") else { return }
        past = (json["meetings"] as? [[String: Any]] ?? []).compactMap(Meeting.init)
            .filter { $0.status != "recording" }
    }

    func start() {
        guard phase == .idle else { return }
        message = nil
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            message = "Wisp needs the microphone first. Press the mic in Ask once and allow it, then start the meeting."
            return
        }
        phase = .recording
        elapsed = 0; heardSoFar = ""; missed = 0; nextWindow = 0; current = nil
        WispClient().prewarmEncoder(.audio)
        Task {
            switch await MeetingClient.send("POST", "meetings", body: ["title": ""]) {
            case .refused(let why):
                phase = .idle
                message = why
            case .ok(let json):
                guard let id = json["id"] as? String, phase == .recording else { phase = .idle; return }
                meetingID = id
                beginRecording()
            }
        }
    }

    private func beginRecording() {
        let recorder = MeetingRecorder()
        recorder.onLevel = { [weak self] level in
            Task { @MainActor in if self?.phase == .recording { self?.level = level } }
        }
        recorder.onWindow = { [weak self] window in
            Task { @MainActor in self?.enqueue(window) }
        }
        recorder.onFailure = { [weak self] why in
            Task { @MainActor in
                self?.message = why + " Wisp kept what it heard."
                self?.stop()
            }
        }
        do {
            try recorder.start()
        } catch {
            message = (error as? MeetingRecorder.Failure)?.message ?? "Wisp couldn't start the microphone."
            phase = .idle
            abandon()
            return
        }
        self.recorder = recorder
        startedAt = Date()
        clock = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.phase == .recording else { return }
                self.elapsed = Int(Date().timeIntervalSince(self.startedAt))
            }
        }
    }

    /// Windows are sent one at a time, in order; the notes wait for the last one.
    private func enqueue(_ window: Data) {
        guard let id = meetingID else { return }
        let seq = nextWindow
        nextWindow += 1
        let previous = uploads
        uploads = Task { [weak self] in
            await previous?.value
            let reply = await MeetingClient.send(
                "POST", "meetings/\(id)/windows",
                body: ["seq": seq, "audio": window.base64EncodedString()], timeout: 75)
            guard let self, case .ok(let json) = reply else { return }
            if let text = json["text"] as? String, !text.isEmpty {
                self.heardSoFar += (self.heardSoFar.isEmpty ? "" : " ") + text
            }
            self.missed = json["missed_s"] as? Double ?? self.missed
        }
    }

    /// Stop listening and write the notes.
    func stop() {
        guard phase == .recording, let id = meetingID else { return }
        clock?.invalidate(); clock = nil
        level = 0
        if let tail = recorder?.stop() { enqueue(tail) }
        recorder = nil
        phase = .writing
        Task {
            await uploads?.value
            uploads = nil
            await finish(id)
            meetingID = nil
            phase = .idle
            await refresh()
        }
    }

    private func finish(_ id: String) async {
        switch await MeetingClient.send("POST", "meetings/\(id)/finish", timeout: 180) {
        case .ok(let json): current = Meeting(json)
        case .refused(let why): message = why
        }
    }

    /// A meeting that never got a microphone leaves nothing behind.
    private func abandon() {
        guard let id = meetingID else { return }
        meetingID = nil
        Task { _ = await MeetingClient.send("DELETE", "meetings/\(id)") }
    }

    func open(_ meeting: Meeting) {
        message = nil
        Task {
            if case .ok(let json) = await MeetingClient.send("GET", "meetings/\(meeting.id)") {
                current = Meeting(json)
            }
        }
    }

    func close() { current = nil; message = nil }

    /// Write the notes again for a meeting whose transcript is saved.
    func writeAgain() {
        guard phase == .idle, let id = current?.id else { return }
        phase = .writing
        message = nil
        Task {
            await finish(id)
            phase = .idle
            await refresh()
        }
    }

    func remind(_ action: MeetingAction) {
        guard busyAction == nil, let id = current?.id else { return }
        busyAction = action.id
        message = nil
        Task {
            switch await MeetingClient.send("POST", "meetings/\(id)/actions/\(action.id)/reminder") {
            case .ok(let json): current = Meeting(json)
            case .refused(let why): message = why
            }
            busyAction = nil
        }
    }

    func delete(_ meeting: Meeting) {
        Task {
            _ = await MeetingClient.send("DELETE", "meetings/\(meeting.id)")
            if current?.id == meeting.id { current = nil }
            await refresh()
        }
    }

    func copyFollowUp() {
        guard let text = current?.notes?.followUp, !text.isEmpty else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        message = "Follow-up copied."
    }
}

/// The Meet tab in the notch.
struct NotchMeetingView: View {
    @ObservedObject var model: MeetingModel
    @State private var showingTranscript = false

    private static let day: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEE MMM d, h:mm a"
        return formatter
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            switch model.phase {
            case .recording: recording
            case .writing: writing
            case .idle:
                if let meeting = model.current { notes(meeting) } else { home }
            }
            if let message = model.message {
                Text(message).font(.wisp(.footnote)).foregroundStyle(Theme.warn)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.bottom, 8)
        .task { await model.refresh() }
    }

    private var home: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Meeting notes").font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
            Text("Wisp listens through the microphone and writes the words down on this Mac. "
                 + "When you stop, it writes a summary, the decisions, the action items and a follow-up. "
                 + "No audio is kept and nothing leaves your Mac.")
                .font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            pill("Start a meeting", icon: "record.circle", filled: true) { model.start() }
            if !model.past.isEmpty {
                label("Earlier meetings").padding(.top, 4)
                ForEach(model.past) { meeting in
                    Button { model.open(meeting) } label: {
                        HStack(alignment: .firstTextBaseline) {
                            Text(meeting.displayTitle).font(.wisp(.body)).foregroundStyle(Theme.textPrimary)
                                .lineLimit(1)
                            Spacer(minLength: 12)
                            Text(Self.day.string(from: meeting.startedAt) + " · " + Meeting.span(meeting.heard))
                                .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var recording: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Circle().fill(Theme.bad).frame(width: 9, height: 9).accessibilityHidden(true)
                Text("Listening").font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                Text(model.elapsedText).font(.wisp(.body, design: .monospaced)).foregroundStyle(Theme.textSecondary)
                Spacer()
                meter
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Listening to the meeting, \(model.elapsedText)")
            Text(model.heardSoFar.isEmpty
                 ? "The words appear here about every 25 seconds."
                 : String(model.heardSoFar.suffix(420)))
                .font(.wisp(.reading, design: .serif))
                .foregroundStyle(model.heardSoFar.isEmpty ? Theme.textMuted : Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if model.missed >= 1 {
                Text("Missed about \(Meeting.span(model.missed)) while Wisp was busy.")
                    .font(.wisp(.caption)).foregroundStyle(Theme.warn)
            }
            pill("Stop and write notes", icon: "stop.circle", filled: true) { model.stop() }
        }
    }

    private var meter: some View {
        HStack(spacing: 2) {
            ForEach(0..<8, id: \.self) { bar in
                Capsule().fill(Float(bar) / 8 < model.level ? Theme.textPrimary : Theme.hairline)
                    .frame(width: 3, height: 12)
            }
        }
        .accessibilityHidden(true)
    }

    private var writing: some View {
        HStack(spacing: 10) {
            ProgressView().controlSize(.small)
            Text("Writing the notes…").font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
        }
    }

    @ViewBuilder
    private func notes(_ meeting: Meeting) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Button { model.close(); showingTranscript = false } label: {
                Label("Meetings", systemImage: "chevron.left").font(.wisp(.footnote))
            }
            .buttonStyle(.plain).foregroundStyle(Theme.textSecondary)
            Spacer()
            Button("Delete") { model.delete(meeting) }
                .buttonStyle(.plain).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
        }
        Text(meeting.displayTitle).font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
        Text(Self.day.string(from: meeting.startedAt) + " · " + meeting.coverage)
            .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
        if let notes = meeting.notes {
            Text(notes.summary).font(.wisp(.reading, design: .serif)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            if !notes.decisions.isEmpty {
                label("Decisions")
                ForEach(Array(notes.decisions.enumerated()), id: \.offset) { _, decision in
                    bullet(decision)
                }
            }
            if !notes.actions.isEmpty {
                label("Action items")
                ForEach(notes.actions) { action in actionRow(action) }
            }
            if !notes.followUp.isEmpty {
                HStack {
                    label("Follow-up")
                    Spacer()
                    pill("Copy", icon: "doc.on.doc", filled: false) { model.copyFollowUp() }
                }
                Text(notes.followUp).font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
            }
        } else {
            if !meeting.error.isEmpty {
                Text(meeting.error).font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !meeting.transcript.isEmpty {
                pill("Write the notes again", icon: "arrow.clockwise", filled: true) { model.writeAgain() }
            }
        }
        if !meeting.transcript.isEmpty {
            Button { showingTranscript.toggle() } label: {
                Label(showingTranscript ? "Hide what was said" : "Show what was said",
                      systemImage: showingTranscript ? "chevron.down" : "chevron.right")
                    .font(.wisp(.footnote))
            }
            .buttonStyle(.plain).foregroundStyle(Theme.textSecondary)
            if showingTranscript {
                Text(meeting.transcript).font(.wisp(.body)).foregroundStyle(Theme.textMuted)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func actionRow(_ action: MeetingAction) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(action.task).font(.wisp(.body)).foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                let detail = [action.owner, action.due.isEmpty ? "" : "by " + action.due]
                    .filter { !$0.isEmpty }.joined(separator: " · ")
                if !detail.isEmpty {
                    Text(detail).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                }
            }
            Spacer(minLength: 8)
            if !action.reminder.isEmpty {
                Label("Reminder " + action.reminder, systemImage: "checkmark")
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted).fixedSize()
            } else if model.busyAction == action.id {
                ProgressView().controlSize(.small)
            } else {
                pill("Remind me", icon: "bell", filled: false) { model.remind(action) }
            }
        }
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("•").foregroundStyle(Theme.textMuted).accessibilityHidden(true)
            Text(text).font(.wisp(.body)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func label(_ text: String) -> some View {
        Text(text).font(.wisp(.footnote, weight: .semibold)).foregroundStyle(Theme.textSecondary)
            .accessibilityAddTraits(.isHeader)
    }

    private func pill(_ title: String, icon: String, filled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon).font(.wisp(.footnote, weight: .medium))
                .foregroundStyle(filled ? Color.black : Theme.textPrimary)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(Capsule().fill(filled ? Color.white : Theme.chipFill))
                .overlay(Capsule().stroke(filled ? Color.clear : Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
    }
}
