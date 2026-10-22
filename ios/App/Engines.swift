import Foundation
import os
#if canImport(WispLing)
import WispLing
#endif

/// One running place for chat turns. Each returns the engine-neutral event stream.
protocol ChatEngine: Sendable {
    /// `history` already ends with the new user message.
    func run(prompt: String, history: [ChatMessage], remoteSessionId: String?) -> AsyncThrowingStream<ChatEvent, Error>
}

struct MacEngine: ChatEngine {
    let gateway: GatewayClient

    func run(prompt: String, history: [ChatMessage], remoteSessionId: String?) -> AsyncThrowingStream<ChatEvent, Error> {
        gateway.agent(prompt: prompt, sessionId: remoteSessionId)
    }
}

struct CloudEngine: ChatEngine {
    let client: CloudClient

    func run(prompt: String, history: [ChatMessage], remoteSessionId: String?) -> AsyncThrowingStream<ChatEvent, Error> {
        let messages = PhonePrompt.messages(system: PhonePrompt.system(engine: .cloud), history: history)
        return client.stream(messages: messages)
    }
}

enum OnDeviceError: Error, LocalizedError {
    case unsupported
    case notInstalled
    case notEnoughMemory(Int)

    var errorDescription: String? {
        switch self {
        case .unsupported:
            #if targetEnvironment(simulator)
            "Ling needs a real iPhone GPU. The Simulator can't run it, so use Mac or Cloud here."
            #else
            "This build doesn't include the on-device Ling runtime."
            #endif
        case .notInstalled:
            "Ling isn't on this iPhone yet. Copy it from your Mac in Settings → iPhone."
        case .notEnoughMemory(let bytes):
            "Ling needs about 5.5 GB of memory but iOS is only giving Wisp \(ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .memory)). Close other apps, or check that the Increased Memory Limit entitlement is signed."
        }
    }
}

#if canImport(WispLing)
/// Keeps one Ling runtime alive across turns so the 4 GB of weights load once.
actor LingHost {
    static let shared = LingHost()
    private var runtime: LingRuntime?
    private var directory: URL?
    private var registered = false

    func runtime(for directory: URL) async -> LingRuntime {
        if !registered {
            await LingRuntime.registerModelTypes()
            registered = true
        }
        if let runtime, self.directory == directory { return runtime }
        await runtime?.unload()
        let fresh = LingRuntime(modelDirectory: directory)
        runtime = fresh
        self.directory = directory
        return fresh
    }

    func unload() async {
        await runtime?.unload()
    }
}
#endif

struct OnDeviceEngine: ChatEngine {
    let modelDirectory: URL?
    var maxTokens = 1024
    static let requiredMemory = 5_800_000_000

    func run(prompt: String, history: [ChatMessage], remoteSessionId: String?) -> AsyncThrowingStream<ChatEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    guard OnDeviceModel.isSupportedHere else { throw OnDeviceError.unsupported }
                    guard let modelDirectory else { throw OnDeviceError.notInstalled }
                    #if canImport(WispLing)
                    let runtime = await LingHost.shared.runtime(for: modelDirectory)
                    if !(await runtime.isLoaded) {
                        // Ling's footprint measured ~5.1–5.6 GB; loading without the headroom gets the app killed.
                        let available = os_proc_available_memory()
                        guard available == 0 || available >= Self.requiredMemory else {
                            throw OnDeviceError.notEnoughMemory(available)
                        }
                        continuation.yield(.status("Loading Ling…"))
                        try await runtime.load()
                    }
                    continuation.yield(.status("Thinking…"))
                    // A smaller window than Cloud: the KV cache is the phone's real memory cost.
                    let window = PhonePrompt.messages(system: PhonePrompt.system(engine: .onDevice),
                                                      history: history, characterBudget: 12_000)
                    let messages = window.map { LingMessage(role: $0["role"] ?? "user", content: $0["content"] ?? "") }
                    for try await piece in await runtime.stream(messages, maxTokens: maxTokens, temperature: 0.6) {
                        continuation.yield(.delta(piece))
                    }
                    continuation.yield(.done)
                    continuation.finish()
                    #else
                    throw OnDeviceError.unsupported
                    #endif
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
