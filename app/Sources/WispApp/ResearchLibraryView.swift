import AppKit
import SwiftUI

/// Saved research, in the Wisp window's Research section. New research starts
/// in the notch's Research tab, and opening a job shows it there.
struct ResearchLibraryView: View {
    @ObservedObject var model: ResearchLibraryModel
    var onOpen: (String) -> Void
    var onNew: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Research").font(.system(size: 22, weight: .medium))
                    Text("Your 30 most recent jobs and every pinned job.")
                        .font(.system(size: 12)).foregroundStyle(Color.secondary)
                }
                Spacer()
                Button("New research", action: onNew).buttonStyle(.borderedProminent)
                    .help("Start a research plan in the notch's Research tab")
            }
            HStack {
                Image(systemName: "magnifyingglass").foregroundStyle(Color.secondary.opacity(0.8))
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
                                .foregroundStyle(model.filter == filter ? Color.primary : Color.secondary)
                                .background(RoundedRectangle(cornerRadius: 7)
                                    .fill(model.filter == filter ? Color.accentColor.opacity(0.14) : Color.primary.opacity(0.05)))
                                .overlay(RoundedRectangle(cornerRadius: 7)
                                    .stroke(model.filter == filter ? Color.accentColor.opacity(0.45) : Color.primary.opacity(0.12), lineWidth: 1))
                        }.buttonStyle(.plain)
                            .accessibilityLabel("Show \(filter.rawValue.lowercased()) research")
                            .accessibilityAddTraits(model.filter == filter ? .isSelected : [])
                    }
                }
                Spacer()
                if model.loading { ProgressView().controlSize(.small) }
                Text("\(model.visibleItems.count) jobs").font(.caption).foregroundStyle(Color.secondary.opacity(0.8))
            }
            if !model.errorText.isEmpty {
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.triangle")
                    Text(model.errorText).font(.callout)
                    Spacer()
                    Button("Retry", action: model.refresh).disabled(model.loading)
                }.foregroundStyle(Color.secondary)
            }
            Rectangle().fill(Color.primary.opacity(0.12)).frame(height: 1)
            if model.visibleItems.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "books.vertical").font(.system(size: 30)).foregroundStyle(Color.secondary.opacity(0.8))
                    Text(emptyTitle).font(.headline)
                    Text(emptyDetail).font(.callout).foregroundStyle(Color.secondary)
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
                .font(.system(size: 11)).foregroundStyle(Color.secondary.opacity(0.8))
        }
        .padding(.horizontal, 24).padding(.vertical, 18)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .foregroundStyle(Color.primary)
        .tint(Color.accentColor)
        .onAppear(perform: model.refresh)
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
                .foregroundStyle(Color.secondary).frame(width: 20).padding(.top, 3)
            VStack(alignment: .leading, spacing: 6) {
                Text(item.title).font(.system(size: 14, weight: .semibold)).lineLimit(2)
                if item.objective != item.title && !item.objective.isEmpty {
                    Text(item.objective).font(.system(size: 12)).foregroundStyle(Color.secondary).lineLimit(2)
                }
                HStack(spacing: 8) {
                    Text(item.stateLabel).fontWeight(.medium)
                    Text("·")
                    Text(item.updatedAt, format: .dateTime.month(.abbreviated).day().year().hour().minute())
                }.font(.system(size: 11)).foregroundStyle(Color.secondary.opacity(0.8))
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.secondary.opacity(0.8)).padding(.top, 5)
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.primary.opacity(0.12)))
        .contentShape(Rectangle())
    }
}

