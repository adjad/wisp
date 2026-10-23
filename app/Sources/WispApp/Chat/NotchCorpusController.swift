import Foundation
import Combine

/// Keeps the notch on one corpus conversation even when the window switches
/// chats. Both surfaces retain the same messages, receipts and source results.
@MainActor
final class NotchCorpusController: ObservableObject {
    let store: ChatStore
    @Published private(set) var conversation: ChatConversation?
    @Published private(set) var isPresented = false
    @Published private(set) var loading = false
    @Published private(set) var notice = ""
    /// The last export's files, shown with Reveal in Finder.
    @Published private(set) var exported: CorpusExportFiles?
    @Published private(set) var exporting = false
    var canStart: () -> Bool = { true }
    var onStateChanged: (_ busy: Bool, _ presented: Bool) -> Void = { _, _ in }
    private var loadTask: Task<Void, Never>?
    private var requestID = UUID()
    private var conversationObserver: AnyCancellable?

    init(store: ChatStore) { self.store = store }

    var busy: Bool { loading || conversation?.corpusRunning == true || conversation?.busy == true }

    func submit(_ command: String) {
        if case .export? = try? AssistantTaskCorpusCommand.resolve(command) {
            isPresented = true
            export()
            return
        }
        if busy { isPresented = true; publishState(); return }
        guard canStart() else { return }
        isPresented = true
        notice = ""
        loading = true
        requestID = UUID()
        let request = requestID
        publishState()
        loadTask = Task { [weak self] in
            guard let self else { return }
            do {
                let result = try await self.store.submitCorpusCommand(command)
                guard !Task.isCancelled, self.requestID == request else { return }
                self.conversation = result
                self.conversationObserver = result.objectWillChange.sink { [weak self] _ in
                    // @Published announces before mutation; read the settled
                    // state on the next main-actor turn.
                    Task { @MainActor [weak self] in self?.publishState() }
                }
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled, self.requestID == request else { return }
                self.notice = error.localizedDescription
            }
            self.loading = false
            self.loadTask = nil
            self.publishState()
        }
    }

    /// Saves the latest run to Downloads as Markdown and CSV.
    func export() {
        guard !exporting else { return }
        exporting = true
        notice = ""
        publishState()
        Task { [weak self] in
            guard let self else { return }
            do {
                self.exported = try await self.store.exportCorpus()
            } catch {
                self.exported = nil
                self.notice = error.localizedDescription
            }
            self.exporting = false
            self.publishState()
        }
    }

    /// The run on screen as Markdown, for Copy results; nil without one.
    var resultsMarkdown: String? { store.corpusMarkdown(conversation ?? store.latestCorpusConversation) }

    func stop() {
        requestID = UUID()
        loadTask?.cancel()
        loadTask = nil
        if loading { notice = "Corpus loading stopped." }
        loading = false
        if let conversation { store.stop(conversation) }
        publishState()
    }

    func hide() { isPresented = false; publishState() }
    func stopAndHide() { stop(); hide() }

    /// An in-memory handoff: corpus runs have no saved backend session ID.
    /// Selecting this object must never start inference or reload a session.
    func selectInWindow() {
        if let conversation { store.select(conversation) }
    }

    private func publishState() {
        objectWillChange.send()
        onStateChanged(busy, isPresented)
    }
}
