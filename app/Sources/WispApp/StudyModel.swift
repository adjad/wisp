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
    #if WISP_NOTCH_QA
    /// Fixture builds never reach a live service or the browser: every call
    /// fails as cancelled and the reader finds no pages.
    static let shared = StudyModel(
        transport: { (_: URLRequest) async throws -> (Data, URLResponse) in
            throw URLError(.cancelled)
        },
        reader: { () async -> [StudyPage] in [] })
    #else
    static let shared = StudyModel()
    #endif

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

    /// "Use my sources": material made from the topic or an assignment reads
    /// the student's sources too.
    @Published var useSources: Bool = false
    /// What the material being made reads, for the progress line; nil when idle.
    @Published private(set) var making: StudyMakePlan?

    /// A deck's pages, shown full-tab.
    @Published private(set) var openedDeck: StudyOpenDeck?
    /// "Get ready for quizzes automatically"; nil when the service has no
    /// such setting (an older backend), so the toggle is hidden.
    @Published private(set) var autoPrepare: Bool?

    /// The Drive picker.
    @Published var showingDrive: Bool = false
    @Published private(set) var drivePhase: StudyDrivePhase = .idle
    @Published private(set) var driveList: [StudyDriveFile] = []
    /// The search the list answers ("" for the newest files).
    @Published private(set) var driveQuery: String = ""
    /// The file being added, while its text is exported.
    @Published private(set) var addingFile: String?
    /// A failed add, shown inside the picker.
    @Published var driveNotice: String?
    /// Counts list requests, so a slow answer to an older search is dropped.
    private var driveRequest = 0

    private let transport: Transport
    private let reader: (() async -> [StudyPage])?
    private let baseURL: URL

    /// How long an ordinary call may take, and how long writing a material may.
    static let timeout: TimeInterval = 20
    static let makeTimeout: TimeInterval = 120
    /// Characters of one page's text sent to `/study/import`; the backend
    /// rejects more than this with a 422.
    static let maxPageText = 150_000

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
            page.text.count > Self.maxPageText
                ? StudyPage(url: page.url, title: page.title, text: String(page.text.prefix(Self.maxPageText)))
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

    /// Makes material from the topic or assignment, reading the student's
    /// sources when "Use my sources" is on.
    func make(_ kind: StudyKind, course: StudyCourse?, assignment: StudyAssignment?, topic: String?) async {
        let courseID = course?.id ?? assignment.map(\.courseID)
        let ids = useSources ? (state?.sourceIDs(for: courseID) ?? []) : []
        await make(kind, course: course, assignment: assignment, topic: topic, sourceIds: ids, wholeCourse: false)
    }

    /// Makes material from the whole course (every source and assignment of it).
    func makeWholeCourse(_ kind: StudyKind = .guide, course: StudyCourse) async {
        await make(kind, course: course, assignment: nil, topic: nil, sourceIds: [], wholeCourse: true)
    }

    func make(_ kind: StudyKind, course: StudyCourse?, assignment: StudyAssignment?, topic: String?,
              sourceIds: [String], wholeCourse: Bool) async {
        guard busy == nil else { return }
        busy = .making(kind)
        notice = nil
        making = plan(kind, course: course, sourceIds: sourceIds, wholeCourse: wholeCourse)
        defer {
            busy = nil
            making = nil
        }

        var fields: [String: Any] = ["kind": kind.rawValue]
        if let assignment = assignment {
            fields["assignment_id"] = assignment.id
            if !assignment.courseID.isEmpty { fields["course_id"] = assignment.courseID }
        }
        if let course = course { fields["course_id"] = course.id }
        if let topic = topic?.trimmingCharacters(in: .whitespacesAndNewlines), !topic.isEmpty {
            fields["topic"] = String(topic.prefix(300))
        }
        // Only new keys when they say something, so an older backend sees the
        // same request as before.
        if wholeCourse, course != nil {
            fields["whole_course"] = true
        } else if !sourceIds.isEmpty {
            fields["source_ids"] = Array(sourceIds.prefix(StudyState.maxSourceIDs))
        }
        await generate(fields)
    }

    /// Makes material from chosen deck pages (at most 40), the same way `make` does.
    func makeFromPages(_ kind: StudyKind, pages: [StudySlideRef], count: Int? = nil, title: String? = nil) async {
        guard busy == nil, !pages.isEmpty else { return }
        busy = .making(kind)
        notice = nil
        making = StudyMakePlan(kind: kind)
        defer {
            busy = nil
            making = nil
        }
        await generate(StudySlides.generateBody(kind, pages: pages, count: count, title: title))
    }

    /// `POST /study/generate`, then opens the material and refreshes the state.
    private func generate(_ fields: [String: Any]) async {
        guard let body = try? JSONSerialization.data(withJSONObject: fields) else { return }
        let request = makeRequest("POST", "study/generate", body: body, timeout: Self.makeTimeout)
        guard let material = await send(request, as: StudyMaterial.self) else { return }
        opened = material
        await fetchState()
    }

    /// What a request reads, from the state already here.
    private func plan(_ kind: StudyKind, course: StudyCourse?, sourceIds: [String],
                      wholeCourse: Bool) -> StudyMakePlan {
        let current = state ?? .empty
        var plan = StudyMakePlan(kind: kind)
        plan.longContext = current.connector.longContext
        if wholeCourse, let course {
            let ids = current.sources(inCourse: course.id).map(\.id)
            plan.wholeCourse = course.shortName
            plan.sources = ids.count
            plan.chars = current.sourceChars(ids)
            plan.assignments = current.assignments(in: course).count
        } else {
            plan.sources = sourceIds.count
            plan.chars = current.sourceChars(sourceIds)
        }
        return plan
    }

    // MARK: Sources and Google Drive

    /// Whether Google is connected with Drive; nil when the service didn't say
    /// (then `drivePhase` holds why).
    @discardableResult
    func driveStatus() async -> StudyDriveStatus? {
        let request = makeRequest("GET", "study/drive/status")
        switch await call(request) {
        case .ok(let data):
            guard let status = try? JSONDecoder().decode(StudyDriveStatus.self, from: data) else {
                drivePhase = .failed(StudyText.problem(code: ""))
                return nil
            }
            return status
        case .failed(let status, let data):
            drivePhase = status == 404 && (try? JSONDecoder().decode(StudyErrorReply.self, from: data))?.code == nil
                ? .failed(StudyText.driveUnavailable)
                : driveFailure(status: status, data: data)
            return nil
        case .offline:
            drivePhase = .failed(StudyText.problem(code: "offline"))
            return nil
        }
    }

    /// Opens the picker: checks Drive, then lists the newest files.
    func openDrive() async {
        showingDrive = true
        driveNotice = nil
        drivePhase = .loading
        guard let status = await driveStatus() else { return }
        if let blocker = status.blocker {
            drivePhase = .notConnected(blocker)
            return
        }
        await driveFiles(query: driveQuery)
    }

    func closeDrive() {
        showingDrive = false
        driveNotice = nil
        driveRequest += 1
    }

    /// Lists Docs and Slides whose names contain `query` (all of them when empty).
    func driveFiles(query: String) async {
        let trimmed = String(query.trimmingCharacters(in: .whitespacesAndNewlines).prefix(100))
        driveRequest += 1
        let ticket = driveRequest
        driveQuery = trimmed
        if driveList.isEmpty || drivePhase != .ready { drivePhase = .loading }
        var parts = URLComponents()
        parts.queryItems = [URLQueryItem(name: "q", value: trimmed), URLQueryItem(name: "limit", value: "25")]
        let request = makeRequest("GET", "study/drive/files?" + (parts.percentEncodedQuery ?? ""), timeout: 45)
        let reply = await call(request)
        guard ticket == driveRequest else { return }
        switch reply {
        case .ok(let data):
            guard let files = try? JSONDecoder().decode(StudyDriveFiles.self, from: data) else {
                drivePhase = .failed(StudyText.problem(code: ""))
                return
            }
            driveList = files.files
            drivePhase = .ready
        case .failed(let status, let data):
            drivePhase = driveFailure(status: status, data: data)
        case .offline:
            drivePhase = .failed(StudyText.problem(code: "offline"))
        }
    }

    /// Adds one Drive file as a source, tied to `courseId` when given. Returns
    /// whether it was added.
    @discardableResult
    func addDriveFile(id: String, courseId: String?) async -> Bool {
        guard addingFile == nil else { return false }
        addingFile = id
        driveNotice = nil
        defer { addingFile = nil }
        var fields: [String: Any] = ["file_id": id]
        fields["course_id"] = courseId.flatMap { $0.isEmpty ? nil : $0 } ?? NSNull()
        guard let body = try? JSONSerialization.data(withJSONObject: fields) else { return false }
        let request = makeRequest("POST", "study/drive/add", body: body, timeout: Self.makeTimeout)
        switch await call(request) {
        case .ok(let data):
            guard let reply = try? JSONDecoder().decode(StudyDriveAddReply.self, from: data) else {
                driveNotice = StudyText.problem(code: "")
                return false
            }
            state = reply.state
            return true
        case .failed(let status, let data):
            let failure = driveFailure(status: status, data: data)
            if case .notConnected = failure {
                drivePhase = failure
            } else {
                driveNotice = Self.problem(status: status, data: data)
            }
            return false
        case .offline:
            driveNotice = StudyText.problem(code: "offline")
            return false
        }
    }

    /// Removes a source from Study (never from Google Drive).
    func deleteSource(id: String) async {
        guard busy == nil else { return }
        busy = .saving
        defer { busy = nil }
        let request = makeRequest("DELETE", "study/sources/" + Self.pathComponent(id))
        guard let data = await exchange(request) else { return }
        if let next = try? JSONDecoder().decode(StudySourceDeleteReply.self, from: data), let fresh = next.state {
            state = fresh
        } else if var current = state {
            current.sources.removeAll { $0.id == id }
            state = current
            await fetchState()
        }
    }

    /// A Drive failure as the picker shows it: no Google or no Drive scope is
    /// "not connected", anything else an error.
    private func driveFailure(status: Int, data: Data) -> StudyDrivePhase {
        let reply = try? JSONDecoder().decode(StudyErrorReply.self, from: data)
        if StudyRefusal.notConnected(reply?.code) {
            return .notConnected(StudyRefusal.cleaned(reply?.message) ?? StudyText.driveNotConnected)
        }
        return .failed(Self.problem(status: status, data: data))
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

    /// Opens a deck full-tab and fetches its pages.
    func openDeck(_ source: StudySource) async {
        guard source.hasPages else { return }
        openedDeck = StudyOpenDeck(source: source, pages: [], loading: true)
        let request = makeRequest("GET", "study/sources/" + Self.pathComponent(source.id) + "/pages")
        let reply = await send(request, as: StudyDeckPages.self)
        guard openedDeck?.source.id == source.id else { return }
        openedDeck?.pages = reply?.pages ?? []
        openedDeck?.loading = false
    }

    func closeDeck() {
        openedDeck = nil
    }

    /// Shows a fixture deck without the backend (QA scenes and previews).
    func show(deck: StudyOpenDeck?, autoPrepare: Bool? = nil) {
        openedDeck = deck
        self.autoPrepare = autoPrepare
    }

    /// Reads `GET /study/settings`; a missing route (404) hides the toggle.
    func loadSettings() async {
        let request = makeRequest("GET", "study/settings")
        switch await call(request) {
        case .ok(let data):
            autoPrepare = (try? JSONDecoder().decode(StudySettings.self, from: data))?.autoPrepare
        case .failed(let status, _):
            if status == 404 { autoPrepare = nil }
        case .offline:
            break
        }
    }

    /// Turns automatic quiz preparing on or off: shown at once, put back if the save fails.
    func setAutoPrepare(_ on: Bool) async {
        guard let before = autoPrepare, before != on else { return }
        autoPrepare = on
        guard let body = try? JSONSerialization.data(withJSONObject: ["auto_prepare": on]) else {
            autoPrepare = before
            return
        }
        let request = makeRequest("POST", "study/settings", body: body)
        guard let data = await exchange(request) else {
            autoPrepare = before
            return
        }
        if let saved = try? JSONDecoder().decode(StudySettings.self, from: data) {
            autoPrepare = saved.autoPrepare
        }
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
    func record(correct: Int, total: Int, missed: [Int] = []) async {
        guard let material = opened, total > 0 else { return }
        var fields: [String: Any] = ["correct": max(0, min(correct, total)), "total": total]
        // Only when there are misses, so an older backend sees the same request as before.
        let misses = StudyAttempt.missed(missed, total: total)
        if !misses.isEmpty { fields["missed"] = misses }
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
    /// of provider and model. A nil or empty key keeps the saved one. Returns
    /// whether the key (when one was given) reached the Keychain.
    @discardableResult
    func saveConnector(provider: String, key: String?, modelID: String) async -> Bool {
        guard busy == nil else { return false }
        busy = .saving
        notice = nil
        defer { busy = nil }
        if let key = key, !key.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            do {
                try StudyCredentialStore.save(key, provider: provider)
            } catch {
                notice = StudyText.keyNotSaved
                return false
            }
        }
        let fields: [String: Any] = ["provider": provider, "model_id": modelID]
        guard let body = try? JSONSerialization.data(withJSONObject: fields) else { return true }
        let request = makeRequest("POST", "study/connector", body: body)
        guard let connector = await send(request, as: StudyConnectorState.self) else { return true }
        if var current = state {
            current.connector = connector
            state = current
        }
        await fetchState()
        if connector.ready {
            showingConnector = false
            notice = StudyText.connectorReady
        }
        return true
    }

    /// Removes the provider's Study key from the Keychain, then re-posts the
    /// current choice so the backend drops its cached "key exists" answer, and
    /// refreshes.
    func removeKey(provider: String, modelID: String) async {
        guard busy == nil else { return }
        busy = .saving
        notice = nil
        defer { busy = nil }
        StudyCredentialStore.remove(provider: provider)
        let fields: [String: Any] = ["provider": provider, "model_id": modelID]
        if let body = try? JSONSerialization.data(withJSONObject: fields) {
            let request = makeRequest("POST", "study/connector", body: body)
            if let connector = await send(request, as: StudyConnectorState.self),
               var current = state {
                current.connector = connector
                state = current
            }
        }
        await fetchState()
        if notice == nil { notice = StudyText.keyRemoved }
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
    func makeRequest(_ method: String, _ path: String, body: Data? = nil,
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

    private enum Reply {
        case ok(Data)
        case failed(status: Int, data: Data)
        case offline
    }

    /// One round trip, with no side effects.
    private func call(_ request: URLRequest) async -> Reply {
        let result: (Data, URLResponse)
        do {
            result = try await transport(request)
        } catch {
            return .offline
        }
        let status = (result.1 as? HTTPURLResponse)?.statusCode ?? 0
        if (200..<300).contains(status) { return .ok(result.0) }
        return .failed(status: status, data: result.0)
    }

    /// The response body on a 2xx; otherwise sets `notice` and returns nil.
    private func exchange(_ request: URLRequest) async -> Data? {
        let status: Int
        let data: Data
        switch await call(request) {
        case .ok(let body):
            return body
        case .offline:
            notice = StudyText.problem(code: "offline")
            return nil
        case .failed(let code, let body):
            status = code
            data = body
        }
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

    /// The gentle sentence for a failed reply (StudyRefusal).
    static func problem(status: Int, data: Data) -> String {
        let reply = try? JSONDecoder().decode(StudyErrorReply.self, from: data)
        return StudyRefusal.message(code: reply?.code, said: reply?.message, status: status)
    }
}

/// `POST /study/import`'s body.
private struct StudyImportBody: Encodable {
    let pages: [StudyPage]
}

/// `DELETE /study/sources/{id}`'s reply: `{"state": …}`, or anything else.
private struct StudySourceDeleteReply: Decodable {
    let state: StudyState?

    enum CodingKeys: String, CodingKey { case state }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = try? c.decodeIfPresent(StudyState.self, forKey: .state)
    }
}
