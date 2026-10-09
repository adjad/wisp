import Foundation

extension WispClient {
    /// GET /chats -> the saved conversations, newest first. Nil when the
    /// service can't be reached or predates the endpoint.
    func chatList(limit: Int = 100) async -> [[String: Any]]? {
        var parts = URLComponents(url: Self.baseURL.appendingPathComponent("chats"), resolvingAgainstBaseURL: false)
        parts?.queryItems = [URLQueryItem(name: "limit", value: String(limit))]
        guard let url = parts?.url else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = 10
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let rows = object["chats"] as? [[String: Any]]
        else { return nil }
        return rows
    }

    /// GET /sessions/{id} -> that chat's saved turns, oldest first.
    func chatTurns(id: String) async -> [[String: Any]]? {
        var request = URLRequest(url: Self.baseURL.appendingPathComponent("sessions").appendingPathComponent(id))
        request.timeoutInterval = 15
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["ok"] as? Bool == true,
              let turns = object["turns"] as? [[String: Any]]
        else { return nil }
        return turns
    }

    /// DELETE /sessions/{id}
    func deleteChat(id: String) async -> Bool {
        var request = URLRequest(url: Self.baseURL.appendingPathComponent("sessions").appendingPathComponent(id))
        request.httpMethod = "DELETE"
        request.timeoutInterval = 10
        guard let (_, response) = try? await URLSession.shared.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }
}

/// The Chat window's connection to the real Wisp service.
@MainActor
final class LiveChatBackend: ChatBackend {
    private let client: WispClient
    init(client: WispClient) { self.client = client }

    func listChats() async -> [ChatSummary]? {
        guard let rows = await client.chatList() else { return nil }
        return rows.compactMap(ChatSummary.parse)
    }

    func loadChat(id: String) async -> [ChatMessage]? {
        guard let turns = await client.chatTurns(id: id) else { return nil }
        return ChatHistory.messages(from: turns)
    }

    func deleteChat(id: String) async -> Bool { await client.deleteChat(id: id) }

    func run(prompt: String, image: String?, sessionId: String, debug: Bool,
             onEvent: @escaping @Sendable (ChatEvent) -> Void) async {
        await client.runAgent(prompt: prompt, image: image, sessionId: sessionId, debug: debug) { event in
            onEvent(ChatEvent(type: event.type, payload: event.payload))
        }
    }

    func approve(sessionId: String, actionId: String, approved: Bool, scope: String, requestId: String) async {
        await client.approve(sessionId: sessionId, actionId: actionId, approved: approved,
                             scope: scope, requestId: requestId)
    }

    func sendDraft(to: String, text: String) async -> (ok: Bool, result: String) {
        await client.sendMessageDraft(to: to, text: text)
    }

    func fullAccess() async -> Bool? { await client.mode().fullAccess }
    func setFullAccess(_ on: Bool) async -> Bool? { await client.setFullAccess(on).fullAccess }
}
