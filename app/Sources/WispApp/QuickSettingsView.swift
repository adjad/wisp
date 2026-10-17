import SwiftUI

/// The notch gear's short list: the settings people flip from week to week.
/// Everything that needs a form, a key, OAuth or widens what Wisp reads (models,
/// Google, cloud keys, privacy sources such as browser history and contacts,
/// the source controls under Memory) stays in the full Settings window.
///
/// It binds to the app's one SettingsLoader, the same object the Settings window
/// uses, so "waiting for confirmation" and "not confirmed" read the same in both
/// places and a change in flight survives the notch collapsing.
struct QuickSettingsView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var settings: SettingsLoader

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Quick settings").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                Spacer()
                Button("Done") { model.showingQuickSettings = false }
                    .buttonStyle(.plain).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .keyboardShortcut(.cancelAction)
            }
            .padding(.bottom, 6)
            if let notice = model.transientNotice {
                Text(notice).font(.system(size: 11)).foregroundStyle(Theme.bad)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.bottom, 4)
            }
            fullAccessRow
            divider
            learningRow
            divider
            briefRow
            divider
            row("Show Details under replies", detail: "Open each reply's model, route, timing and tool calls by default (Debug Mode).") {
                Toggle("", isOn: $model.debugMode).labelsHidden().toggleStyle(.switch).tint(Theme.accent)
            }
            divider
            row("Free up memory", detail: "Unload models now. Wisp reloads them on the next ask.") {
                chip(model.freeingMemory ? "Unloading…" : "Unload") { model.freeUpMemory() }
                    .disabled(model.freeingMemory || !model.conversationSettled || model.briefComposing)
                    .help(model.conversationSettled ? "Unload models now" : "Available when Wisp finishes answering")
            }
            divider
            HStack {
                Text("Models, Google, privacy sources and activity")
                    .font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                Spacer()
                chip("All settings  ⌘,") { WispRouter.shared.open(.settings) }
            }
            .padding(.top, 10)
        }
        .task { await settings.refreshQuick() }
    }

    // MARK: Rows

    @ViewBuilder private var fullAccessRow: some View {
        row("Full access", detail: settings.fullAccessExplanation) {
            if let on = settings.fullAccess {
                Toggle("", isOn: Binding(get: { on }, set: { settings.setFullAccess($0) }))
                    .labelsHidden().toggleStyle(.switch).tint(Theme.accent)
                    .disabled(settings.fullAccessPending)
            } else {
                HStack(spacing: 6) {
                    chip("Check") { Task { await settings.refreshFullAccess() } }
                    chip("Turn off") { settings.setFullAccess(false) }
                }
                .disabled(settings.fullAccessPending)
            }
        }
    }

    @ViewBuilder private var learningRow: some View {
        row("Learn my routines", detail: settings.behaviorExplanation) {
            if settings.behaviorEnabled != nil {
                Toggle("", isOn: Binding(get: { settings.behaviorEnabled == true },
                                         set: { settings.setBehaviorEnabled($0) }))
                    .labelsHidden().toggleStyle(.switch).tint(Theme.accent)
                    .disabled(settings.behaviorChanging)
            } else {
                HStack(spacing: 6) {
                    chip("Check") { Task { await settings.refreshBehavior() } }
                    chip("Turn off") { settings.setBehaviorEnabled(false) }
                }
                .disabled(settings.behaviorChanging)
            }
        }
        if !settings.behaviorChanging, settings.behaviorEnabled != nil, let status = settings.behaviorStatus {
            Text(status).font(.system(size: 11)).foregroundStyle(Theme.textMuted).padding(.bottom, 6)
        }
    }

    @ViewBuilder private var briefRow: some View {
        let detail = settings.briefTimesPending ? "Saving brief times. Waiting for confirmation…"
            : settings.briefTimes == nil ? "Brief times haven't been confirmed. Check again."
            : "Wisp composes your Daily Summary each morning and evening."
        row("Daily brief", detail: detail) {
            if let times = settings.briefTimes {
                HStack(spacing: 6) {
                    hourMenu(times.morning, hours: 0..<12) { settings.setBriefTimes(morning: $0) }
                    hourMenu(times.evening, hours: 12..<24) { settings.setBriefTimes(evening: $0) }
                }
                .disabled(settings.briefTimesPending)
            } else {
                chip("Check") { Task { await settings.refreshBriefTimes() } }
                    .disabled(settings.briefTimesPending)
            }
        }
    }

    // MARK: Pieces

    private var divider: some View { Divider().overlay(Theme.hairline) }

    private func row<Control: View>(_ title: String, detail: String,
                                    @ViewBuilder control: () -> Control) -> some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .medium)).foregroundStyle(Theme.textPrimary)
                Text(detail).font(.system(size: 11)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            control()
        }
        .padding(.vertical, 9)
        .accessibilityElement(children: .contain)
    }

    private func hourMenu(_ hour: Int, hours: Range<Int>, set: @escaping (Int) -> Void) -> some View {
        Menu {
            ForEach(Array(hours), id: \.self) { value in
                Button(Self.hourLabel(value)) { if value != hour { set(value) } }
            }
        } label: {
            Text(Self.hourLabel(hour)).font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                .padding(.horizontal, 9).padding(.vertical, 4)
                .background(Capsule().fill(Theme.chipFill))
                .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
        }
        .menuStyle(.button).buttonStyle(.plain).menuIndicator(.hidden).fixedSize()
    }

    static func hourLabel(_ hour: Int) -> String {
        let display = hour % 12 == 0 ? 12 : hour % 12
        return "\(display) \(hour < 12 ? "AM" : "PM")"
    }

    private func chip(_ title: String, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(.plain)
            .font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .fixedSize()
    }
}
