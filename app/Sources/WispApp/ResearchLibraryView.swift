import AppKit
import SwiftUI

struct ResearchLibraryView: View {
    @ObservedObject var model: ResearchLibraryModel
    var onOpen: (String) -> Void
    var onNew: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Research Library").font(.system(size: 23, weight: .semibold))
                    Text("Your 30 most recent jobs and every pinned job.")
                        .font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                }
                Spacer()
                Button("New Research…", action: onNew).buttonStyle(.borderedProminent)
            }
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(Theme.textMuted)
                TextField("Search titles and questions", text: $model.query)
                    .textFieldStyle(.roundedBorder)
                Button(action: model.refresh) {
                    Image(systemName: "arrow.clockwise")
                }.help("Refresh saved research").disabled(model.loading)
            }
            HStack {
                HStack(spacing: 4) {
                    ForEach(ResearchLibraryModel.Filter.allCases, id: \.self) { filter in
                        Button { model.filter = filter } label: {
                            Text(filter.rawValue).font(.system(size: 12, weight: .medium))
                                .padding(.horizontal, 14).padding(.vertical, 7)
                                .foregroundStyle(model.filter == filter ? Color.black : Theme.textSecondary)
                                .background(RoundedRectangle(cornerRadius: 7)
                                    .fill(model.filter == filter ? Color.white : Theme.chipFill))
                        }.buttonStyle(.plain)
                            .accessibilityLabel("Show \(filter.rawValue.lowercased()) research")
                            .accessibilityAddTraits(model.filter == filter ? .isSelected : [])
                    }
                }
                Spacer()
                if model.loading { ProgressView().controlSize(.small) }
                Text("\(model.visibleItems.count) jobs").font(.caption).foregroundStyle(Theme.textMuted)
            }
            if !model.errorText.isEmpty {
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.triangle")
                    Text(model.errorText).font(.callout)
                    Spacer()
                    Button("Retry", action: model.refresh).disabled(model.loading)
                }.foregroundStyle(Theme.textSecondary)
            }
            Divider().overlay(Theme.hairline)
            if model.visibleItems.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "books.vertical").font(.system(size: 30)).foregroundStyle(Theme.textMuted)
                    Text(emptyTitle).font(.headline)
                    Text(emptyDetail).font(.callout).foregroundStyle(Theme.textSecondary)
                        .multilineTextAlignment(.center).frame(maxWidth: 380)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(model.visibleItems) { item in
                            Button { onOpen(item.id) } label: { row(item) }
                                .buttonStyle(.plain)
                                .accessibilityLabel("\(item.title), \(item.stateLabel)\(item.pinned ? ", pinned" : "")")
                                .accessibilityHint("Open saved research")
                        }
                    }
                }
            }
            Text("Opening saved research does not start a new search. Review a plan or choose Resume when you're ready.")
                .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
        }
        .padding(24)
        .frame(minWidth: 620, minHeight: 480)
        .foregroundStyle(Theme.textPrimary)
        .background(Theme.surface)
        .preferredColorScheme(.dark)
        .tint(Theme.accent)
    }

    private var emptyTitle: String {
        if model.loading { return "Loading saved research…" }
        if !model.errorText.isEmpty && model.items.isEmpty { return "Library unavailable" }
        if !model.query.isEmpty { return "No matching research" }
        if model.filter == .pinned { return "No pinned research" }
        if model.filter == .unfinished { return "No unfinished research" }
        return "Your research lives here"
    }

    private var emptyDetail: String {
        if model.loading { return "Reading your saved jobs." }
        if !model.errorText.isEmpty && model.items.isEmpty { return "Try again when Wisp's local service is available." }
        if !model.query.isEmpty { return "Try a different title or question." }
        if model.filter == .pinned { return "Pin a job from its research view to keep it available here." }
        if model.filter == .unfinished { return "Saved plans and paused or running jobs appear here." }
        return "Start a research plan, then return here to revisit its report and sources."
    }

    private func row(_ item: ResearchLibraryItem) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: item.pinned ? "pin.fill" : "doc.text.magnifyingglass")
                .foregroundStyle(Theme.textSecondary).frame(width: 20).padding(.top, 3)
            VStack(alignment: .leading, spacing: 6) {
                Text(item.title).font(.system(size: 14, weight: .semibold)).lineLimit(2)
                if item.objective != item.title && !item.objective.isEmpty {
                    Text(item.objective).font(.system(size: 12)).foregroundStyle(Theme.textSecondary).lineLimit(2)
                }
                HStack(spacing: 8) {
                    Text(item.stateLabel).fontWeight(.medium)
                    Text("·")
                    Text(item.updatedAt, format: .dateTime.month(.abbreviated).day().year().hour().minute())
                }.font(.system(size: 11)).foregroundStyle(Theme.textMuted)
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Theme.textMuted).padding(.top, 5)
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.chipStroke))
        .contentShape(Rectangle())
    }
}

@MainActor
final class ResearchLibraryWindowController {
    private var window: NSWindow?
    private let model: ResearchLibraryModel
    private let onOpen: (String) -> Void
    private let onNew: () -> Void

    init(client: ResearchClient, onOpen: @escaping (String) -> Void, onNew: @escaping () -> Void) {
        model = ResearchLibraryModel(client: client)
        self.onOpen = onOpen; self.onNew = onNew
    }

    func show() {
        if window == nil {
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 620),
                styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
            win.title = "Wisp Research Library"
            win.contentView = NSHostingView(rootView: ResearchLibraryView(model: model,
                onOpen: { [weak self] id in self?.window?.orderOut(nil); self?.onOpen(id) },
                onNew: { [weak self] in self?.window?.orderOut(nil); self?.onNew() }))
            win.minSize = NSSize(width: 660, height: 520)
            win.isReleasedWhenClosed = false
            win.center(); window = win
        }
        model.refresh()
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
}
