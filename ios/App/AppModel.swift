import Foundation
import Observation
import SwiftUI

@MainActor @Observable
final class AppModel {
    enum MacStatus: Equatable {
        case notPaired
        case checking
        case connected(String)
        case backendDown(String)
        case problem(String)
    }

    // MARK: State

    var engine: EngineKind {
        didSet { defaults.set(engine.rawValue, forKey: Keys.engine) }
    }
    private(set) var conversations: [Conversation] = []
    var currentID: UUID?
    private(set) var isSending = false
    private(set) var statusText = ""
    var pendingApproval: ApprovalRequest?
    var pairingOffer: MacPairing?

    private(set) var pairing: MacPairing?
    private(set) var macStatus: MacStatus = .notPaired
    var cloudSettings: CloudSettings {
        didSet { save(cloudSettings, key: Keys.cloud) }
    }
    private(set) var hasCloudKey: Bool
    let onDevice = OnDeviceModel()

    private let defaults = UserDefaults.standard
    private let store: ConversationStore?
    private var gatewayClient: GatewayClient?
    private var turn: Task<Void, Never>?

    private enum Keys {
        static let engine = "engine"
        static let cloud = "cloud-settings"
        static let pairing = "mac-pairing"
    }

    /// Persisted pairing fields; the token itself lives in the Keychain.
    private struct StoredPairing: Codable {
        var host: String?
        var ip: String?
        var port: Int
        var macName: String
    }

    init() {
        store = try? ConversationStore.standard()
        engine = EngineKind(rawValue: defaults.string(forKey: Keys.engine) ?? "") ?? .mac
        cloudSettings = Self.load(CloudSettings.self, key: Keys.cloud) ?? CloudSettings()
        hasCloudKey = Keychain.read(.cloudKey) != nil
        conversations = (store?.load() ?? []).sorted { $0.updatedAt > $1.updatedAt }
        if let stored = Self.load(StoredPairing.self, key: Keys.pairing), let token = Keychain.read(.macToken) {
            let pairing = MacPairing(host: stored.host, ip: stored.ip, port: stored.port,
                                     macName: stored.macName, token: token)
            self.pairing = pairing
            gatewayClient = GatewayClient(pairing: pairing)
            macStatus = .checking
        }
    }

    var current: Conversation? {
        conversations.first { $0.id == currentID }
    }

    // MARK: Chat

    func newChat() {
        stop()
        currentID = nil
        pendingApproval = nil
    }

    func open(_ conversation: Conversation) {
        stop()
        currentID = conversation.id
        engine = conversation.engine
        pendingApproval = nil
    }

    func delete(_ ids: Set<UUID>) {
        if let currentID, ids.contains(currentID) { newChat() }
        conversations.removeAll { ids.contains($0.id) }
        persist()
    }

    /// Picking another engine mid-conversation starts a fresh chat: a Mac chat's history
    /// lives on the Mac and can't carry over to the phone or the cloud.
    func select(_ kind: EngineKind) {
        guard kind != engine else { return }
        engine = kind
        if let current, current.engine != kind, !current.messages.isEmpty { newChat() }
        if kind == .mac { Task { await refreshMac() } }
    }

    var readiness: String? {
        switch engine {
        case .mac:
            switch macStatus {
            case .notPaired: return "Pair your Mac in Settings to use Wisp's full tools."
            case .backendDown(let name): return "Wisp isn't running on \(name)."
            case .problem(let message): return message
            case .checking, .connected: return nil
            }
        case .cloud:
            return hasCloudKey ? nil : "Add a cloud provider key in Settings."
        case .onDevice:
            if !OnDeviceModel.isSupportedHere { return OnDeviceError.unsupported.localizedDescription }
            return onDevice.installedDirectory == nil ? OnDeviceError.notInstalled.localizedDescription : nil
        }
    }

    func send(_ raw: String) {
        let prompt = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty, !isSending else { return }
        let engineImpl: any ChatEngine
        do { engineImpl = try makeEngine() } catch {
            appendExchange(prompt: prompt, failure: error.localizedDescription)
            return
        }
        var conversation = current ?? Conversation(engine: engine, title: Conversation.title(from: prompt))
        if conversation.engine != engine { conversation = Conversation(engine: engine, title: Conversation.title(from: prompt)) }
        conversation.messages.append(ChatMessage(role: .user, text: prompt))
        let history = conversation.messages
        let answer = ChatMessage(role: .assistant, text: "")
        conversation.messages.append(answer)
        conversation.updatedAt = Date()
        upsert(conversation)
        currentID = conversation.id

        isSending = true
        statusText = ""
        let conversationID = conversation.id
        let stream = engineImpl.run(prompt: prompt, history: history, remoteSessionId: conversation.remoteSessionId)
        turn = Task { [weak self] in
            do {
                for try await event in stream {
                    self?.apply(event, conversation: conversationID, answer: answer.id)
                }
            } catch {
                self?.update(conversationID, answer.id) { $0.failure = error.localizedDescription }
                if error as? GatewayError == .unauthorized { self?.macStatus = .problem(error.localizedDescription) }
            }
            self?.finishTurn(conversationID, answer.id)
        }
    }

    func stop() {
        turn?.cancel()
        turn = nil
    }

    func answer(_ request: ApprovalRequest, approved: Bool, scope: ApprovalScope = .once) {
        pendingApproval = nil
        guard let gatewayClient else { return }
        Task {
            do { try await gatewayClient.approve(request, approved: approved, scope: scope) } catch {
                statusText = "Couldn't send your answer to the Mac: \(error.localizedDescription)"
            }
        }
    }

    private func makeEngine() throws -> any ChatEngine {
        switch engine {
        case .mac:
            guard let gatewayClient else { throw GatewayError.unreachable }
            return MacEngine(gateway: gatewayClient)
        case .cloud:
            guard let key = Keychain.read(.cloudKey) else { throw CloudSettings.Problem.missingKey }
            _ = try cloudSettings.completionsURL()
            return CloudEngine(client: CloudClient(settings: cloudSettings, apiKey: key))
        case .onDevice:
            guard OnDeviceModel.isSupportedHere else { throw OnDeviceError.unsupported }
            guard let directory = onDevice.installedDirectory else { throw OnDeviceError.notInstalled }
            return OnDeviceEngine(modelDirectory: directory)
        }
    }

    private func apply(_ event: ChatEvent, conversation id: UUID, answer: UUID) {
        switch event {
        case .session(let sid):
            guard let index = conversations.firstIndex(where: { $0.id == id }) else { return }
            conversations[index].remoteSessionId = sid
        case .status(let text):
            statusText = text
        case .delta(let text):
            statusText = ""
            update(id, answer) { $0.text += text }
        case .replace(let text):
            statusText = ""
            update(id, answer) { $0.text = text }
        case .clear:
            update(id, answer) { $0.text = "" }
        case .activity(let line):
            update(id, answer) { if $0.activity.last != line { $0.activity.append(line) } }
        case .approval(let request):
            pendingApproval = request
        case .approvalExpired(let actionID):
            if pendingApproval?.id == actionID {
                pendingApproval = nil
                update(id, answer) { $0.activity.append("Approval timed out, so nothing was done") }
            }
        case .failed(let message):
            update(id, answer) { $0.failure = message }
        case .done:
            break
        }
    }

    private func finishTurn(_ id: UUID, _ answer: UUID) {
        isSending = false
        statusText = ""
        pendingApproval = nil
        turn = nil
        update(id, answer) { message in
            if message.text.isEmpty && message.failure == nil { message.failure = "Stopped." }
        }
        persist()
    }

    private func appendExchange(prompt: String, failure: String) {
        var conversation = current?.engine == engine ? current! : Conversation(engine: engine, title: Conversation.title(from: prompt))
        conversation.messages.append(ChatMessage(role: .user, text: prompt))
        conversation.messages.append(ChatMessage(role: .assistant, text: "", failure: failure))
        conversation.updatedAt = Date()
        upsert(conversation)
        currentID = conversation.id
        persist()
    }

    private func update(_ conversationID: UUID, _ messageID: UUID, _ change: (inout ChatMessage) -> Void) {
        guard let c = conversations.firstIndex(where: { $0.id == conversationID }),
              let m = conversations[c].messages.firstIndex(where: { $0.id == messageID }) else { return }
        change(&conversations[c].messages[m])
    }

    private func upsert(_ conversation: Conversation) {
        conversations.removeAll { $0.id == conversation.id }
        conversations.insert(conversation, at: 0)
    }

    private func persist() {
        try? store?.save(conversations)
    }

    // MARK: Mac

    func offerPairing(from text: String) throws {
        pairingOffer = try MacPairing.parse(text)
    }

    func confirmPairing() {
        guard let offer = pairingOffer else { return }
        pairingOffer = nil
        guard Keychain.write(offer.token, for: .macToken) else {
            macStatus = .problem("Couldn't save the pairing in the Keychain.")
            return
        }
        save(StoredPairing(host: offer.host, ip: offer.ip, port: offer.port, macName: offer.macName), key: Keys.pairing)
        pairing = offer
        gatewayClient = GatewayClient(pairing: offer)
        engine = .mac
        Task { await refreshMac() }
    }

    func unpair() {
        Keychain.delete(.macToken)
        defaults.removeObject(forKey: Keys.pairing)
        pairing = nil
        gatewayClient = nil
        macStatus = .notPaired
    }

    var gateway: GatewayClient? { gatewayClient }

    func refreshMac() async {
        guard let gatewayClient else { macStatus = .notPaired; return }
        macStatus = .checking
        do {
            let hello = try await gatewayClient.hello()
            macStatus = hello.backend.reachable ? .connected(hello.macName) : .backendDown(hello.macName)
        } catch {
            macStatus = .problem(error.localizedDescription)
        }
    }

    // MARK: Cloud

    func saveCloudKey(_ key: String) {
        let trimmed = key.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            Keychain.delete(.cloudKey)
        } else {
            Keychain.write(trimmed, for: .cloudKey)
        }
        hasCloudKey = Keychain.read(.cloudKey) != nil
    }

    /// A one-line round trip, so Settings can say whether the provider, model and key work.
    func testCloud() async -> String {
        guard let key = Keychain.read(.cloudKey) else { return CloudSettings.Problem.missingKey.localizedDescription }
        let client = CloudClient(settings: cloudSettings, apiKey: key)
        var reply = ""
        do {
            for try await event in client.stream(messages: [["role": "user", "content": "Reply with just: OK"]]) {
                if case .delta(let text) = event { reply += text }
            }
            return reply.isEmpty ? "Connected, but the model sent an empty reply." : "Connected. \(cloudSettings.model) replied."
        } catch {
            return error.localizedDescription
        }
    }

    // MARK: Lifecycle

    /// iOS forbids GPU work in the background, so an on-device turn stops when the app leaves.
    func didEnterBackground() {
        if engine == .onDevice { stop() }
    }

    func didReceiveMemoryWarning() {
        #if canImport(WispLing)
        guard !isSending else { return }
        Task { await LingHost.shared.unload() }
        #endif
    }

    // MARK: Defaults helpers

    private func save<T: Encodable>(_ value: T, key: String) {
        if let data = try? JSONEncoder().encode(value) { defaults.set(data, forKey: key) }
    }

    private static func load<T: Decodable>(_ type: T.Type, key: String) -> T? {
        UserDefaults.standard.data(forKey: key).flatMap { try? JSONDecoder().decode(type, from: $0) }
    }
}
