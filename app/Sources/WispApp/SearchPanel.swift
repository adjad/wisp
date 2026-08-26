import AppKit
import SwiftUI

// The ⌘⇧F content view. It does NOT own its window — AppDelegate hosts it in
// an `OverlayPanel`, the same notch-fused reveal panel the chat assistant
// uses, so Smart Search gets the identical black surface, rounded-bottom
// notch corners, and spring-open/roll-up animation for free rather than a
// second, visually distinct floating card. The two panels are mutually
// exclusive at the notch — see AppDelegate.toggleSearch/expand.
struct SearchView: View {
    @ObservedObject var model: SearchModel
    @FocusState private var focused: Bool

    private var hasNotch: Bool {
        guard let screen = NSScreen.main else { return false }
        return OverlayPanel.notchMetrics(for: screen) != nil
    }
    private var notchInset: CGFloat {
        guard let screen = NSScreen.main,
              let m = OverlayPanel.notchMetrics(for: screen) else { return 0 }
        return m.inset
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            field
            if model.needsAccessibility && !model.hasPage {
                permissionPrompt
            } else if !model.errorText.isEmpty && model.rows.isEmpty {
                notice(model.errorText, symbol: "exclamationmark.triangle")
            } else {
                content
            }
        }
        // Fused with the notch exactly like the chat panel: it hangs flush
        // from the physical top, so its center sits behind the camera
        // housing — push content below it by the same inset.
        .padding(EdgeInsets(top: hasNotch ? notchInset + 8 : 16,
                            leading: 22, bottom: 16, trailing: 22))
        .background(GeometryReader { geo in
            Color.clear.onChange(of: geo.size.height) { _, _ in model.onResize() }
        })
        .frame(width: 640)
        .background(Theme.surface)
        .clipShape(Theme.notchCorners)
        .shadow(color: .black.opacity(0.55), radius: 20, y: 10)
        .onAppear { focused = true }
    }

    // MARK: Search field

    private var field: some View {
        VStack(spacing: 6) {
            HStack(spacing: 10) {
                Image(systemName: "sparkle.magnifyingglass")
                    .foregroundStyle(Theme.textSecondary)
                TextField("Ask about this page…", text: $model.query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 16, weight: .regular))
                    .foregroundStyle(Theme.textPrimary)
                    .focused($focused)
                    .onChange(of: model.query) { _, _ in model.queryChanged() }
                if model.busy {
                    ProgressView().scaleEffect(0.5).frame(width: 14, height: 14)
                }
                if !model.rows.isEmpty {
                    Text("\(model.selection + 1)/\(model.rows.count)")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(Theme.textMuted)
                }
            }
            HStack(spacing: 6) {
                Text(model.status)
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.textMuted)
                if model.semanticOff {
                    Text("· semantic search unavailable")
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.textMuted)
                }
                if !model.indexingStatus.isEmpty {
                    Text("· \(model.indexingStatus)")
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.textMuted)
                }
                Spacer()
                if model.elapsedMs > 0 {
                    Text("\(model.elapsedMs)ms")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(Theme.textMuted)
                }
            }
        }
        .padding(.bottom, 10)
    }

    // MARK: Body

    @ViewBuilder private var content: some View {
        if model.hasPage && model.query.isEmpty {
            EmptyView()
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if model.answering { answeringCard }
                    if !model.answer.isEmpty { answerCard }
                    if model.notFound { notFoundCard }
                    resultList
                }
                .padding(.bottom, 4)
            }
            .frame(maxHeight: 360)
        }
    }

    private var answeringCard: some View {
        HStack(spacing: 8) {
            ProgressView().scaleEffect(0.45).frame(width: 12, height: 12)
            Text(model.upgradingModel.isEmpty
                 ? "Reading the passages…"
                 : "Loading a stronger model for this…")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary)
        }
        .padding(.vertical, 10).padding(.horizontal, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
    }

    private var answerCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            // An overview is inferred across a sample of the document rather
            // than lifted from one passage — say so, so it isn't read as a
            // located quotation.
            if model.answerScope == "global" {
                HStack(spacing: 5) {
                    Image(systemName: "doc.text.magnifyingglass")
                        .font(.system(size: 9))
                    Text("Overview · read across the document")
                        .font(.system(size: 9, weight: .medium))
                }
                .foregroundStyle(Theme.textMuted)
            }
            Text(model.answer)
                .font(.system(size: 13))
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)

            HStack(spacing: 6) {
                ForEach(model.citations) { c in
                    Button {
                        model.reveal(citation: c)
                    } label: {
                        Text("[\(c.n)]")
                            .font(.system(size: 10, design: .monospaced))
                            .padding(.horizontal, 7).padding(.vertical, 3)
                            .background(Capsule().fill(
                                model.expandedCitation == c.n
                                ? Color.white.opacity(0.18) : Theme.chipFill))
                            .overlay(Capsule().stroke(Theme.chipStroke))
                    }
                    .buttonStyle(.plain)
                    .help("Show this source")
                }
                Spacer()
                Button { model.copyAnswer() } label: {
                    Image(systemName: "doc.on.doc")
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.textMuted)
                }
                .buttonStyle(.plain)
                .help("Copy answer with source")
            }

            if let n = model.expandedCitation,
               let c = model.citations.first(where: { $0.n == n }) {
                citationSource(c)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke))
    }

    // Deliberately NOT "Not in this document" — that claim was usually false.
    // A span lookup failing means the matching passages didn't state an answer,
    // which is a different thing from the document not containing one, and
    // saying the stronger thing trained distrust in a feature whose whole value
    // is being believable. Offer the wider read instead of a dead end.
    /// The passage behind a citation, shown in the panel. Always available —
    /// scrolling the source app is best-effort and impossible in some viewers
    /// (Safari's PDF view among them), so this is what actually makes a
    /// citation checkable.
    private func citationSource(_ c: SearchModel.Citation) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(c.preview)
                .font(.system(size: 11))
                .foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            HStack(spacing: 8) {
                if !model.canNavigate {
                    Text("can't scroll this app — copy to find it there")
                        .font(.system(size: 9))
                        .foregroundStyle(Theme.textMuted)
                }
                Spacer()
                Button { model.copyCitation(c) } label: {
                    Text("Copy quote")
                        .font(.system(size: 10))
                        .foregroundStyle(Theme.textMuted)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline))
    }

    private var notFoundCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "questionmark.circle").foregroundStyle(Theme.textMuted)
                Text(model.canWiden
                     ? "No direct answer in the matching passages."
                     : "Couldn't answer that from this document.")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary)
                Spacer()
            }
            if model.canWiden {
                Button { model.searchWholeDocument() } label: {
                    Text("Read the whole document")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(Theme.textPrimary)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(Capsule().fill(Theme.chipFill))
                        .overlay(Capsule().stroke(Theme.chipStroke))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.vertical, 10).padding(.horizontal, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
    }

    private func notice(_ text: String, symbol: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).foregroundStyle(Theme.textMuted)
            Text(text).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
            Spacer()
        }
        .padding(.vertical, 10).padding(.horizontal, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
    }

    private var permissionPrompt: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Wisp can't read this window yet")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(Theme.textPrimary)
            Text("Grant Accessibility to search text inside other apps. Without it, "
                 + "Wisp can only read what's visible on screen.")
                .font(.system(size: 11))
                .foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Button("Open Accessibility Settings…") {
                PageReader.requestAccessibility()
                if let url = URL(string: "x-apple.systempreferences:com.apple.preference"
                                 + ".security?Privacy_Accessibility") {
                    NSWorkspace.shared.open(url)
                }
            }
            .buttonStyle(.plain)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 10).padding(.vertical, 5)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke))
        }
    }

    private var resultList: some View {
        VStack(spacing: 2) {
            ForEach(Array(model.rows.enumerated()), id: \.element.id) { i, r in
                Button {
                    model.selection = i
                    model.revealSelected()
                } label: {
                    row(r, selected: i == model.selection)
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func row(_ r: SearchModel.Result, selected: Bool) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon(for: r.kind))
                .font(.system(size: 9))
                .foregroundStyle(Theme.textMuted)
                .frame(width: 12)
                .padding(.top, 2)
            (Text(r.prefix).foregroundColor(Theme.textMuted)
             + Text(r.match).foregroundColor(Theme.textPrimary).bold()
             + Text(r.suffix).foregroundColor(Theme.textMuted))
                .font(.system(size: 11))
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            Text("\(r.line)")
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
        }
        .padding(.vertical, 6).padding(.horizontal, 9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8)
            .fill(selected ? Theme.chipFill : Color.clear))
    }

    private func icon(for kind: String) -> String {
        switch kind {
        case "literal":  return "text.magnifyingglass"
        case "lexical":  return "textformat.abc"
        default:         return "sparkle"
        }
    }
}
