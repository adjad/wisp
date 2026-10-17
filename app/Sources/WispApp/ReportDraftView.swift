import SwiftUI

/// Report a problem, unfolded under what it is about (a reply in the notch,
/// Today's plan) instead of a window per click. Nothing is uploaded: the report
/// is saved locally where the person chooses, and the preview shows exactly what
/// is written. No sheets or alerts, so it works in the notch's non-activating panel.
struct ReportDraftCard: View {
    enum Look { case notch, window }

    @ObservedObject var draft: ReportDraft
    var title = "Report a problem with this reply"
    var detailsLabel = "Include conversation details"
    var look: Look = .notch
    /// Runs the save panel. The notch wraps it so the panel's focus isn't
    /// mistaken for the person leaving.
    var save: () -> Void
    @State private var larger = false

    var body: some View {
        if let snapshot = draft.snapshot {
            VStack(alignment: .leading, spacing: 9) {
                HStack(alignment: .firstTextBaseline) {
                    Text(title).font(.system(size: 13, weight: .semibold)).foregroundStyle(primary)
                    Spacer(minLength: 8)
                    if let seconds = snapshot.metadata["client_duration_seconds"] as? Double {
                        Text(String(format: "%.1f s", seconds)).font(.system(size: 11)).foregroundStyle(muted)
                    }
                }
                TextField("What did you expect Wisp to do? (optional)", text: $draft.note, axis: .vertical)
                    .textFieldStyle(.plain).lineLimit(2...4)
                    .font(.system(size: 12.5)).foregroundStyle(primary)
                    .padding(.horizontal, 10).padding(.vertical, 7)
                    .background(RoundedRectangle(cornerRadius: 9).fill(field))
                    .overlay(RoundedRectangle(cornerRadius: 9).stroke(stroke, lineWidth: 1))
                if snapshot.hasDetails {
                    Toggle(detailsLabel, isOn: $draft.includeDetails)
                        .toggleStyle(.switch).controlSize(.mini).tint(look == .notch ? Theme.accent : .accentColor)
                        .font(.system(size: 12.5)).foregroundStyle(primary)
                    Text("Details can include private messages, titles and model inputs. Redaction of passwords, tokens and keys is best effort and cannot catch everything. Review the preview before sharing.")
                        .font(.system(size: 11)).foregroundStyle(muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack {
                    Text("PREVIEW · SAVED LOCALLY, NEVER UPLOADED")
                        .font(.system(size: 10.5, weight: .semibold)).tracking(0.6).foregroundStyle(muted)
                    Spacer()
                    Button(larger ? "Smaller" : "Larger") { larger.toggle() }
                        .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(secondary)
                        .help("Show more of the preview")
                }
                ScrollView {
                    Text(verbatim: draft.preview)
                        .font(.system(size: 10.5, design: .monospaced)).foregroundStyle(secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                }
                .frame(height: larger ? 340 : 118)
                .background(RoundedRectangle(cornerRadius: 9).fill(field))
                .overlay(RoundedRectangle(cornerRadius: 9).stroke(stroke, lineWidth: 1))
                HStack(spacing: 8) {
                    Text(draft.status).font(.system(size: 11)).foregroundStyle(secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    chip(draft.status.hasPrefix("Saved") ? "Done" : "Cancel") { draft.close() }
                    chip("Save report…", primary: true) { save() }
                }
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 12).fill(card))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(stroke, lineWidth: 1))
            .accessibilityElement(children: .contain)
            .accessibilityLabel(title)
        }
    }

    // MARK: Look

    private var primary: Color { look == .notch ? Theme.textPrimary : .primary }
    private var secondary: Color { look == .notch ? Theme.textSecondary : .secondary }
    private var muted: Color { look == .notch ? Theme.textMuted : .secondary }
    private var stroke: Color { look == .notch ? Theme.chipStroke : Color.secondary.opacity(0.25) }
    private var field: Color { look == .notch ? Color.white.opacity(0.05) : Color.secondary.opacity(0.08) }
    private var card: Color { look == .notch ? Color.white.opacity(0.03) : Color.secondary.opacity(0.05) }

    private func chip(_ title: String, primary isPrimary: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 12, weight: isPrimary ? .semibold : .regular))
                .foregroundStyle(isPrimary ? (look == .notch ? Color.black : Color.white) : primary)
                .padding(.horizontal, 12).padding(.vertical, 4)
                .background(Capsule().fill(isPrimary ? (look == .notch ? Color.white : Color.accentColor)
                                                     : (look == .notch ? Theme.chipFill : Color.secondary.opacity(0.12))))
                .overlay(Capsule().stroke(isPrimary ? Color.clear : stroke, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .fixedSize()
    }
}
