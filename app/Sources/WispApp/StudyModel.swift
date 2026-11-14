import Combine
import Foundation

/// The Study tab's state and its calls to the backend's `/study` routes
/// (docs/STUDY_TAB.md). The transport and the page reader are injectable so
/// fixtures and checks never touch the network or the browser. Nothing is
/// fetched at init; the view calls `refresh()` when the tab appears.
///
/// The model never resizes the panel: after a change that alters height, the
/// view does.
@MainActor
final class StudyModel: ObservableObject {
    static let shared = StudyModel()

    typealias Transport = (URLRequest) async throws -> (Data, URLResponse)

    @Published private(set) var state: StudyState?
    @Published private(set) var busy: StudyBusy?
    /// A gentle one-line error or result.
    @Published var notice: String?
    /// The material shown full-tab.
    @Published var opened: StudyMaterial?
    @Published var showingConnector: Bool = false
    /// Set by the views while a text field has focus, so the panel stays open.
    @Published var editing: Bool = false

    private let transport: Transport
    private let reader: (() async -> [StudyPage])?
    private let baseURL: URL

    /// How long an ordinary call may take, and how long writing a material may.
    static let timeout: TimeInterval = 20
    static let makeTimeout: TimeInterval = 120

    init(transport: Transport? = nil, reader: (() async -> [StudyPage])? = nil) {
        if let transport = transport {
            self.transport = transport
        } else {
            self.transport = { (request: URLRequest) async throws -> (Data, URLResponse) in
                try await URLSession.shared.data(for: request)
            }
        }
        self.reader = reader
        self.baseURL = WispClient.baseURL
    }

    // MARK: Derived

    var hasData: Bool { state?.hasData ?? false }
    var connectorReady: Bool { state?.connector.ready ?? false }
    var isSample: Bool { state?.sample ?? false }

    func material(_ id: String) -> StudyMaterialSummary? {
        state?.materials.first { $0.id == id }
    }

    /// Shows fixture state without the backend (QA scenes and previews).
    func show(state: StudyState?, opened: StudyMaterial? = nil, busy: StudyBusy? = nil, notice: String? = nil) {
        self.state = state
        self.opened = opened
        self.busy = busy
        self.notice = notice
    }

    func dismissNotice() {
        notice = nil
    }

    // MARK: Actions

    func refresh() async {
        guard busy == nil else {
            await fetchState()
            return
        }
        busy = .loading
        await fetchState()
        busy = nil
    }

    func readSchoolPages() async {
        guard busy == nil else { return }
        busy = .reading
        notice = nil
        defer { busy = nil }

        var pages: [StudyPage]
        if let reader = reader {
            pages = await reader()
        } else {
            #if WISP_NOTCH_QA
            pages = []
            #else
            guard StudyPageReader.hasAccessibility else {
                notice = StudyText.needsAccessibility
                return
            }
            pages = await StudyPageReader.read()
            #endif
        }
        pages = Array(pages.prefix(StudyPageReader.maxPages)).map { page in
            page.text.count > 200_000
                ? StudyPage(url: page.url, title: page.title, text: String(page.text.prefix(200_000)))
                : page
        }
        guard !pages.isEmpty else {
            notice = StudyText.noSchoolTabs
            return
        }
        guard let body = try? JSONEncoder().encode(StudyImportBody(pages: pages)) else {
            notice = StudyText.problem(code: "bad_request")
            return
        }
        let request = makeRequest("POST", "study/import", body: body, timeout: Self.makeTimeout)
        guard let reply = await send(request, as: StudyImportReply.self) else { return }
        state = reply.state
        notice = StudyText.readResult(reply)
    }

    func make(_ kind: StudyKind, course: StudyCourse?, assignment: StudyAssignment?, topic: String?) async {
        guard busy == nil else { return }
        busy = .making(kind)
        notice = nil
        defer { busy = nil }

        var fields: [String: Any] = ["kind": kind.rawValue]
        if let assignment = assignment {
            fields["assignment_id"] = assignment.id
            if !assignment.courseID.isEmpty { fields["course_id"] = assignment.courseID }
        }
        if let course = course { fields["course_id"] = course.id }
        if let topic = topic?.trimmingCharacters(in: .whitespacesAndNewlines), !topic.isEmpty {
            fields["topic"] = String(topic.prefix(300))
        }
        guard let body = try? JSONSerialization.data(withJSONObject: fields) else { return }
        let request = makeRequest("POST", "study/generate", body: body, timeout: Self.makeTimeout)
        guard let material = await send(request, as: StudyMaterial.self) else { return }
        opened = material
        await fetchState()
    }

    func open(_ id: String) async {
        guard busy == nil else { return }
        busy = .loading
        defer { busy = nil }
        let request = makeRequest("GET", "study/materials/" + Self.pathComponent(id))
        guard let material = await send(request, as: StudyMaterial.self) else { return }
        opened = material
    }

    func close() {
        opened = nil
    }

    func delete(_ id: String) async {
        guard busy == nil else { return }
        busy = .saving
        defer { busy = nil }
        let request = makeRequest("DELETE", "study/materials/" + Self.pathComponent(id))
        guard await sendIgnoringBody(request) else { return }
        if opened?.id == id { opened = nil }
        if var current = state {
            current.materials.removeAll { $0.id == id }
            state = current
        }
        await fetchState()
    }

    /// Records a quiz attempt for the opened material and keeps its best score.
    func record(correct: Int, total: Int) async {
        guard let material = opened, total > 0 else { return }
        let fields: [String: Any] = ["correct": max(0, min(correct, total)), "total": total]
        guard let body = try? JSONSerialization.data(withJSONObject: fields) else { return }
        let request = makeRequest("POST", "study/materials/" + Self.pathComponent(material.id) + "/attempt", body: body)
        guard let reply = await send(request, as: StudyAttemptReply.self) else { return }
        let best = reply.bestScore
        if opened?.id == material.id { opened?.bestScore = best }
        if var current = state, let index = current.materials.firstIndex(where: { $0.id == material.id }) {
            current.materials[index].bestScore = best
            state = current
        }
    }

    /// Saves the key (when one is given) to the Keychain first, then the choice
    /// of provider and model. A nil or empty key keeps the saved one.
    func saveConnector(provider: String, key: String?, modelID: String) async {
        guard busy == nil else { return }
        busy = .saving
        notice = nil
        defer { busy = nil }
        if let key = key, !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            do {
                try StudyCredentialStore.save(key, provider: provider)
            } catch {
                notice = StudyText.keyNotSaved
                return
            }
        }
        let fields: [String: Any] = ["provider": provider, "model_id": modelID]
        guard let body = try? JSONSerialization.data(withJSONObject: fields) else { return }
        let request = makeRequest("POST", "study/connector", body: body)
        guard let connector = await send(request, as: StudyConnectorState.self) else { return }
        if var current = state {
            current.connector = connector
            state = current
        }
        await fetchState()
        if connector.ready {
            showingConnector = false
            notice = StudyText.connectorReady
        }
    }

    func loadSample() async {
        guard busy == nil else { return }
        busy = .loading
        notice = nil
        defer { busy = nil }
        let request = makeRequest("POST", "study/sample", body: Data("{}".utf8))
        guard let next = await send(request, as: StudyState.self) else { return }
        state = next
    }

    func clearAll() async {
        guard busy == nil else { return }
        busy = .saving
        notice = nil
        defer { busy = nil }
        let request = makeRequest("DELETE", "study/data")
        guard let next = await send(request, as: StudyState.self) else { return }
        opened = nil
        state = next
        notice = StudyText.cleared
    }

    // MARK: Transport

    private func fetchState() async {
        let request = makeRequest("GET", "study/state")
        guard let next = await send(request, as: StudyState.self) else { return }
        state = next
    }

    private static func pathComponent(_ id: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#%")
        return id.addingPercentEncoding(withAllowedCharacters: allowed) ?? id
    }

    /// Native-only: JSON content type, no Origin header.
    private func makeRequest(_ method: String, _ path: String, body: Data? = nil,
                             timeout: TimeInterval = StudyModel.timeout) -> URLRequest {
        var root = baseURL.absoluteString
        while root.hasSuffix("/") { root.removeLast() }
        let url = URL(string: root + "/" + path) ?? baseURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = body
        return request
    }

    /// The response body on a 2xx; otherwise sets `notice` and returns nil.
    private func exchange(_ request: URLRequest) async -> Data? {
        let result: (Data, URLResponse)
        do {
            result = try await transport(request)
        } catch {
            notice = StudyText.problem(code: "offline")
            return nil
        }
        let data = result.0
        let response = result.1
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if (200..<300).contains(status) { return data }
        notice = Self.problem(status: status, data: data)
        if status == 409, let reply = try? JSONDecoder().decode(StudyErrorReply.self, from: data),
           reply.code == "missing" {
            showingConnector = true
        }
        return nil
    }

    private func send<T: Decodable>(_ request: URLRequest, as type: T.Type) async -> T? {
        guard let data = await exchange(request) else { return nil }
        guard let value = try? JSONDecoder().decode(T.self, from: data) else {
            notice = StudyText.problem(code: "")
            return nil
        }
        return value
    }

    private func sendIgnoringBody(_ request: URLRequest) async -> Bool {
        await exchange(request) != nil
    }

    private static let knownCodes: Set<String> = ["missing", "rejected", "busy", "unreachable", "bad_reply"]

    /// The gentle sentence for a failed reply.
    static func problem(status: Int, data: Data) -> String {
        let reply = try? JSONDecoder().decode(StudyErrorReply.self, from: data)
        if let code = reply?.code, knownCodes.contains(code) { return StudyText.problem(code: code) }
        if let message = reply?.message?.trimmingCharacters(in: .whitespacesAndNewlines), !message.isEmpty {
            return String(message.prefix(300))
        }
        switch status {
        case 404: return StudyText.problem(code: "not_found")
        case 422: return StudyText.problem(code: "bad_request")
        default: return StudyText.problem(code: "")
        }
    }
}

/// `POST /study/import`'s body.
private struct StudyImportBody: Encodable {
    let pages: [StudyPage]
}
