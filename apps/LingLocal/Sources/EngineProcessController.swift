import AppKit
import Darwin
import Foundation

private final class CapturedData: @unchecked Sendable {
    private let lock = NSLock()
    private var storage = Data()

    func set(_ data: Data) {
        lock.lock()
        storage = data
        lock.unlock()
    }

    func get() -> Data {
        lock.lock()
        defer { lock.unlock() }
        return storage
    }
}

@MainActor
final class EngineProcessController: ObservableObject {
    static let supportedModelID = "Ling-3.0-tiny-oQ4e"
    @Published var checkpointPath = "/Users/adijain/Desktop/OMLX_Model_Files/TheWirelessPhoenix/Ling-3.0-tiny-oQ4e"
    @Published private(set) var status: EngineStatus = .stopped
    @Published private(set) var metadata: CheckpointMetadata?
    @Published private(set) var health: EngineHealth?
    @Published private(set) var chatLines: [ChatLine] = []
    @Published var draft = ""
    @Published var thinking = false
    @Published private(set) var isSending = false
    @Published private(set) var message: String?

    private var child: Process?
    private var stderrPipe: Pipe?
    private var terminationCallback: (() -> Void)?
    private var processOutput = ""
    private var activeCheckpointPath: String?
    private var transcript: [ChatTurn] = []
    private let client = LingEngineClient()
    private let launcher = Bundle.main.bundleURL
        .appendingPathComponent("Contents/Resources/ling_engine/run.sh")

    var isOwnedProcessRunning: Bool { child?.isRunning == true }

    func chooseCheckpoint() {
        guard !isOwnedProcessRunning, status != .starting, status != .inspecting else { return }
        let panel = NSOpenPanel()
        panel.title = "Choose Ling checkpoint"
        panel.message = "Select the local checkpoint directory. No files will be changed."
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        if panel.runModal() == .OK, let url = panel.url {
            checkpointPath = url.path
            metadata = nil
            message = nil
        }
    }

    func inspectCheckpoint() {
        guard !isOwnedProcessRunning, status != .starting, status != .inspecting else { return }
        guard !checkpointPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            message = "Choose a checkpoint directory first."
            return
        }
        status = .inspecting
        message = nil
        let selectedCheckpoint = checkpointPath
        Task.detached(priority: .userInitiated) { [selectedCheckpoint, launcher] in
            do {
                guard FileManager.default.fileExists(atPath: selectedCheckpoint) else {
                    throw AppError.message("Checkpoint folder not found: \(selectedCheckpoint)")
                }
                guard FileManager.default.isExecutableFile(atPath: launcher.path) else {
                    throw AppError.message("The bundled Ling engine is missing. Rebuild the app after tools/ling_engine is available.")
                }
                let result = try Self.runCapture(executable: launcher, arguments: ["--model", selectedCheckpoint, "inspect"])
                guard result.status == 0 else {
                    throw AppError.message(result.stderr.isEmpty ? result.stdout : result.stderr)
                }
                let decoded = try JSONDecoder().decode(CheckpointMetadata.self, from: Data(result.stdout.utf8))
                await MainActor.run {
                    self.metadata = decoded
                    self.status = self.isOwnedProcessRunning ? .ready : .stopped
                }
            } catch {
                await MainActor.run {
                    self.status = self.isOwnedProcessRunning ? .ready : .failed
                    self.message = error.localizedDescription
                }
            }
        }
    }

    func start() {
        guard status != .inspecting, status != .starting, status != .stopping else { return }
        guard child == nil else { message = "This app already owns a Ling engine process."; return }
        guard FileManager.default.isExecutableFile(atPath: launcher.path) else {
            status = .failed
            message = "The bundled Ling engine is missing. Build the app from a workspace containing tools/ling_engine/run.sh, cli.py, and engine.py."
            return
        }
        guard FileManager.default.fileExists(atPath: checkpointPath) else {
            status = .failed
            message = "Checkpoint folder not found: \(checkpointPath)"
            return
        }
        guard !Self.isPortOccupied(8767) else {
            status = .failed
            message = "Port 8767 is already in use. Ling Local will not stop or modify another process. Choose another service port only after changing the engine integration."
            return
        }

        status = .starting
        message = nil
        let selectedCheckpoint = checkpointPath
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = [launcher.path, "--model", selectedCheckpoint, "serve", "--host", "127.0.0.1", "--port", "8767"]
        process.standardOutput = pipe
        process.standardError = pipe
        process.terminationHandler = { [weak self, weak process] ended in
            Task { @MainActor in
                guard let self, self.child === process else { return }
                self.child = nil
                self.activeCheckpointPath = nil
                self.stderrPipe?.fileHandleForReading.readabilityHandler = nil
                self.stderrPipe = nil
                self.health = nil
                if self.status != .stopping {
                    self.status = .failed
                    let detail = self.processOutput.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.message = detail.isEmpty
                        ? "Ling engine exited (code \(ended.terminationStatus)). Check that the oMLX Python runtime and checkpoint are available."
                        : "Ling engine exited (code \(ended.terminationStatus)): \(detail)"
                } else {
                    self.status = .stopped
                }
                let callback = self.terminationCallback
                self.terminationCallback = nil
                callback?()
            }
        }
        do {
            try process.run()
            child = process
            activeCheckpointPath = selectedCheckpoint
            stderrPipe = pipe
            processOutput = ""
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard !data.isEmpty else { return }
                let text = String(decoding: data, as: UTF8.self)
                Task { @MainActor in
                    guard let self else { return }
                    self.processOutput = String((self.processOutput + text).suffix(4000))
                }
            }
            status = .starting
            Task { await waitUntilReady(process: process, checkpointPath: selectedCheckpoint) }
        } catch {
            status = .failed
            message = "Could not launch the Ling engine: \(error.localizedDescription)"
        }
    }

    func stop() {
        guard let process = child else { status = .stopped; return }
        status = .stopping
        process.terminate()
        Task { await reportIfStopTimedOut(process: process, isQuit: false) }
    }

    func shutdownOwnedProcess(completion: @escaping (Bool) -> Void) {
        guard let process = child else { completion(true); return }
        terminationCallback = { completion(true) }
        status = .stopping
        process.terminate()
        Task { await reportIfStopTimedOut(process: process, isQuit: true, onTimeout: { completion(false) }) }
    }

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending, status == .ready else { return }
        draft = ""
        let userTurn = ChatTurn(role: "user", content: text)
        transcript.append(userTurn)
        chatLines.append(ChatLine(role: "You", content: text, metrics: nil))
        isSending = true
        message = nil
        Task {
            do {
                guard let modelID = health?.model else { throw AppError.message("Engine health is unavailable; restart the local engine before chatting.") }
                let response = try await client.chat(messages: transcript, model: modelID, thinking: thinking)
                guard let choice = response.choices.first else { throw AppError.message("The engine returned no chat response.") }
                transcript.append(ChatTurn(role: "assistant", content: choice.message.content))
                chatLines.append(ChatLine(role: "Ling", content: choice.message.content, metrics: response.ling_metrics))
            } catch {
                message = error.localizedDescription
            }
            isSending = false
        }
    }

    private func waitUntilReady(process: Process, checkpointPath: String) async {
        let deadline = Date().addingTimeInterval(90)
        while process.isRunning && Date() < deadline {
            do {
                let result = try await client.health()
                guard child === process, process.isRunning, status != .stopping, status != .stopped,
                      activeCheckpointPath == checkpointPath else { return }
                guard result.model == Self.supportedModelID else {
                    status = .failed
                    message = "Port 8767 answered for model \(result.model), but this app supports \(Self.supportedModelID). The app will not treat another service as ready."
                    return
                }
                health = result
                status = .ready
                return
            } catch {
                guard child === process, process.isRunning, status != .stopping, status != .stopped,
                      activeCheckpointPath == checkpointPath else { return }
                try? await Task.sleep(for: .milliseconds(500))
                guard child === process, process.isRunning, status != .stopping, status != .stopped,
                      activeCheckpointPath == checkpointPath else { return }
            }
        }
        guard child === process, status != .stopping, status != .stopped,
              activeCheckpointPath == checkpointPath else { return }
        if !process.isRunning {
            status = .failed
            message = "Ling engine stopped before becoming ready. Confirm the installed oMLX Python runtime and checkpoint path."
        } else {
            status = .failed
            message = "Ling engine did not become ready within 90 seconds. It is still running; use Stop to end this app-owned process."
        }
    }

    private func reportIfStopTimedOut(process: Process, isQuit: Bool, onTimeout: (() -> Void)? = nil) async {
        try? await Task.sleep(for: .seconds(6))
        guard child === process, process.isRunning else { return }
        status = .failed
        message = isQuit
            ? "The app-owned engine has not exited after a stop request. Quit was canceled; try Stop again or quit after the process exits. No other process was signaled."
            : "The app-owned engine has not exited after a stop request. Try Stop again or quit after it exits. No other process was signaled."
        let callback = terminationCallback
        terminationCallback = nil
        if let onTimeout { onTimeout() } else { callback?() }
    }

    nonisolated private static func runCapture(executable: URL, arguments: [String]) throws -> (status: Int32, stdout: String, stderr: String) {
        let process = Process()
        let output = Pipe()
        let errors = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = [executable.path] + arguments
        process.standardOutput = output
        process.standardError = errors
        try process.run()
        let outData = CapturedData()
        let errData = CapturedData()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global().async {
            outData.set(output.fileHandleForReading.readDataToEndOfFile())
            group.leave()
        }
        group.enter()
        DispatchQueue.global().async {
            errData.set(errors.fileHandleForReading.readDataToEndOfFile())
            group.leave()
        }
        process.waitUntilExit()
        group.wait()
        return (process.terminationStatus, String(decoding: outData.get(), as: UTF8.self), String(decoding: errData.get(), as: UTF8.self))
    }

    private static func isPortOccupied(_ port: UInt16) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return true }
        defer { close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if connected == 0 { return true }
        return errno != ECONNREFUSED
    }

    private enum AppError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case let .message(text) = self { text } else { nil } }
    }
}
