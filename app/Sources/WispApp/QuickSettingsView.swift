import SwiftUI

/// The notch gear's short list: the settings people flip from week to week, in
/// three groups (Permissions, Daily brief, Advanced) with one line each.
/// Everything that needs a form, a key, OAuth or widens what Wisp reads (models,
/// Google, cloud keys, privacy sources such as browser history and contacts,
/// the source controls under Memory › Routines) stays in Settings, in the Wisp window.
/// One deliberate exception: a new user with no Google account connected gets a
/// single "Connect Google" row for the first connect (NotchGoogleVisibility).
///
/// It binds to the app's one SettingsLoader, the same object the Wisp window's
/// Settings uses, so "waiting for confirmation" and "not confirmed" read the same in both
/// places and a change in flight survives the notch collapsing. The panel is
/// non-activating, so nothing here opens a sheet, an alert or a popup menu.
struct QuickSettingsView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var settings: SettingsLoader
    /// Off only in the render fixture, which never contacts the service.
    var refreshes = true
    /// The brief time whose inline hour list is open.
    @State var picking: BriefSlot?
    /// Decides whether the Google row shows at all (new users only).
    @StateObject private var google = NotchGoogleModel()
    @AppStorage(VoiceReplyMode.defaultsKey) private var voiceRepliesRaw = VoiceReplyMode.afterVoice.rawValue
    /// The panel's Text size scale (Theme.textScale reads the same key).
    @AppStorage(Theme.textSizeKey) private var textSize: Double = 1.0

    enum BriefSlot { case morning, evening }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Settings").font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                Spacer()
                Button("Done") { model.showingQuickSettings = false }
                    .buttonStyle(.plain).font(.wisp(.callout)).foregroundStyle(Theme.textSecondary)
                    .keyboardShortcut(.cancelAction)
                    .help("Close settings and go back to \(model.tab.stripTab.title)")
            }
            if let notice = model.transientNotice {
                Text(notice).font(.wisp(.footnote)).foregroundStyle(Theme.bad)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            group("Permissions") {
                fullAccessRow
                hairline
                learningRow
            }
            group("Daily brief") { briefRow }
            if google.showsRow {
                // The user's deliberate exception for the very first connect
                // only: a new user with no Google account (and an OAuth client
                // to use) gets one Connect button. Once any account is
                // connected there is no Google row here at all; accounts and
                // Disconnect & erase live in Settings ▸ Google
                // (NotchGoogleVisibility).
                group("Google") { NotchConnectGoogle(model: google) }
            }
            group("Voice & accessibility") {
                voiceRepliesRow
                hairline
                textSizeRow
            }
            group("Power") { lowPowerRow }
            group("Advanced") {
                row("Reply details", help: "Open each reply's model, route, timing and tool calls by default (Debug Mode).") {
                    Text("Model, timing and tools under each reply")
                } control: {
                    Toggle("Reply details", isOn: $model.debugMode).toggleStyle(NotchSwitch())
                }
                hairline
                memoryRow
            }
            HStack {
                Text("Models, Google, privacy, activity").font(.wisp(.callout)).foregroundStyle(Theme.textSecondary)
                Spacer()
                pillButton("All settings  ⌘,", primary: true) { WispRouter.shared.open(.settings) }
            }
            .padding(.horizontal, 4)
        }
        .task {
            guard refreshes else { return }
            await settings.refreshQuick()
            await model.refreshModelMemory()
        }
        // A reply or a brief may have loaded a model while this is open.
        .onChange(of: model.conversationSettled) { _, settled in
            if settled, refreshes { Task { await model.refreshModelMemory() } }
        }
        .onChange(of: model.briefComposing) { _, composing in
            if !composing, refreshes { Task { await model.refreshModelMemory() } }
        }
        .onChange(of: settings.briefTimesPending) { _, pending in if pending { picking = nil } }
        .task {
            guard refreshes else { return }
            await google.keepCurrent()
        }
    }

    // MARK: Rows

    /// When Wisp says its answer out loud (VoiceReplyMode). Three chips, not a
    /// menu: the panel is non-activating, so no popup.
    private var voiceRepliesRow: some View {
        row("Read answers aloud",
            help: "Wisp speaks its answer with your Mac's voice (System Settings › Accessibility › Spoken Content). Ask by voice with the mic or ⌥⇧Space.") {
            Text(voiceReplies == .afterVoice ? "When you ask by voice (⌥⇧Space)" : voiceReplies == .always ? "Every answer" : "Never")
        } control: {
            HStack(spacing: 6) {
                ForEach([VoiceReplyMode.afterVoice, .always, .off], id: \.self) { mode in
                    pillButton(mode == .afterVoice ? "Voice" : mode.title, primary: voiceReplies == mode) {
                        voiceRepliesRaw = mode.rawValue
                        if mode == .off { model.speaker.stop() }
                    }
                    .accessibilityLabel(mode.title)
                    .accessibilityAddTraits(voiceReplies == mode ? [.isButton, .isSelected] : .isButton)
                }
            }
        }
    }

    /// Text size for the whole panel: three chips, same pattern as the voice
    /// row. Changing it rebuilds the panel (OverlayView watches the key).
    private var textSizeRow: some View {
        row("Text size", help: "Makes the text in Wisp's panel larger. Settings and the Wisp window are unchanged.") {
            Text(Theme.textSizeChoices.first { abs($0.scale - textSize) < 0.01 }?.label ?? "Default")
        } control: {
            HStack(spacing: 6) {
                ForEach(Theme.textSizeChoices, id: \.label) { choice in
                    let chosen = abs(choice.scale - textSize) < 0.01
                    pillButton(choice.label, primary: chosen) { textSize = choice.scale }
                        .accessibilityLabel(choice.label)
                        .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
                }
            }
        }
    }

    private var voiceReplies: VoiceReplyMode { VoiceReplyMode(rawValue: voiceRepliesRaw) ?? .afterVoice }

    private var lowPowerRow: some View {
        row("Low Power Mode", help: QuickSettingsText.lowPowerHelp) {
            Text(QuickSettingsText.lowPower(on: settings.lowPower, pending: settings.lowPowerPending))
        } control: {
            if settings.lowPower != nil {
                Toggle("Low Power Mode", isOn: Binding(get: { settings.lowPower == true },
                                                       set: { settings.setLowPower($0) }))
                    .toggleStyle(NotchSwitch())
                    .disabled(settings.lowPowerPending)
            } else {
                pillButton("Check") { Task { await settings.refreshLowPower() } }
                    .disabled(settings.lowPowerPending)
            }
        }
    }

    private var fullAccessRow: some View {
        let line = QuickSettingsText.fullAccess(on: settings.fullAccess, pending: settings.fullAccessPending)
        return row("Full access", help: settings.fullAccessExplanation) {
            if let status = line.status {
                Text(status).foregroundColor(settings.fullAccess == true ? Theme.warn : Theme.textSecondary)
                    + Text(" · " + line.rest)
            } else {
                Text(line.rest)
            }
        } control: {
            if let on = settings.fullAccess {
                Toggle("Full access", isOn: Binding(get: { on }, set: { settings.setFullAccess($0) }))
                    .toggleStyle(NotchSwitch(amber: true))
                    .disabled(settings.fullAccessPending)
            } else {
                HStack(spacing: 6) {
                    pillButton("Check") { Task { await settings.refreshFullAccess() } }
                    pillButton("Turn off") { settings.setFullAccess(false) }
                }
                .disabled(settings.fullAccessPending)
            }
        }
    }

    private var learningRow: some View {
        let line = QuickSettingsText.learning(enabled: settings.behaviorEnabled, changing: settings.behaviorChanging,
                                              status: settings.behaviorStatus, remote: settings.behaviorRemote,
                                              phraser: settings.behaviorPhraser)
        // A failed change that left a known state says so in place of the usual line.
        let failure = !settings.behaviorChanging && settings.behaviorEnabled != nil ? settings.behaviorStatus : nil
        return row("Learn my routines", help: settings.behaviorExplanation) {
            if let failure {
                Text(failure)
            } else if let local = line.local {
                Text(line.text + " · ") + Text(local).foregroundColor(Theme.textPrimary)
            } else {
                Text(line.text)
            }
        } control: {
            if settings.behaviorEnabled != nil {
                Toggle("Learn my routines", isOn: Binding(get: { settings.behaviorEnabled == true },
                                                          set: { settings.setBehaviorEnabled($0) }))
                    .toggleStyle(NotchSwitch())
                    .disabled(settings.behaviorChanging)
            } else {
                HStack(spacing: 6) {
                    pillButton("Check") { Task { await settings.refreshBehavior() } }
                    pillButton("Turn off") { settings.setBehaviorEnabled(false) }
                }
                .disabled(settings.behaviorChanging)
            }
        }
    }

    @ViewBuilder private var briefRow: some View {
        row("Composed twice a day",
            help: "Wisp composes your Daily Summary each morning and evening.") {
            Text(QuickSettingsText.brief(pending: settings.briefTimesPending, confirmed: settings.briefTimes != nil))
        } control: {
            if let times = settings.briefTimes {
                HStack(spacing: 4) {
                    timePill(times.morning, slot: .morning)
                    timePill(times.evening, slot: .evening)
                }
                .disabled(settings.briefTimesPending)
            } else {
                pillButton("Check") { Task { await settings.refreshBriefTimes() } }
                    .disabled(settings.briefTimesPending)
            }
        }
        if let slot = picking, let times = settings.briefTimes {
            hourList(slot, current: slot == .morning ? times.morning : times.evening)
        }
    }

    private var memoryRow: some View {
        let line = QuickSettingsText.memory(model.modelMemory, physicalBytes: ProcessInfo.processInfo.physicalMemory)
        return row("Model memory", help: model.modelMemory?.models.map(\.id).joined(separator: ", ")
                   ?? "Unload models now. Wisp reloads them on the next ask.") {
            Text(line.text)
        } control: {
            HStack(spacing: 10) {
                if let fraction = line.fraction {
                    Capsule().fill(Theme.controlFill).frame(width: 110, height: 5)
                        .overlay(alignment: .leading) {
                            Capsule().fill(Theme.textPrimary).frame(width: max(5, 110 * fraction), height: 5)
                        }
                        .accessibilityHidden(true)
                }
                pillButton(model.freeingMemory ? "Unloading…" : "Unload") { model.freeUpMemory() }
                    .disabled(model.freeingMemory || !model.conversationSettled || model.briefComposing
                              || model.modelMemory?.models.isEmpty == true)
                    .help(model.conversationSettled ? "Unload models now" : "Available when Wisp finishes answering")
            }
        }
    }

    // MARK: Brief times

    // Each pill names its run. Two pills led by "AM" and "PM" side by side read
    // as the old AM/PM switch, when both briefs actually run.
    private func timePill(_ hour: Int, slot: BriefSlot) -> some View {
        let label = QuickSettingsText.pill(hour)
        let open = picking == slot
        return Button { picking = open ? nil : slot } label: {
            HStack(spacing: 5) {
                Text(slot == .morning ? "Morning" : "Evening").foregroundStyle(Theme.textSecondary)
                Text("\(label.time) \(label.period)").foregroundStyle(Theme.textPrimary).monospacedDigit()
            }
            .font(.wisp(.body))
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 8).fill(open ? Theme.controlFill : Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(open ? Theme.textSecondary : Theme.chipStroke, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(slot == .morning ? "Morning" : "Evening") brief, \(QuickSettingsText.hourLabel(hour))")
        .accessibilityHint(open ? "Closes the time list" : "Shows the times to choose from")
    }

    private func hourList(_ slot: BriefSlot, current: Int) -> some View {
        let hours = slot == .morning ? Array(0..<12) : Array(12..<24)
        return LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 6), spacing: 4) {
            ForEach(hours, id: \.self) { hour in
                let chosen = hour == current
                Button {
                    picking = nil
                    if chosen { return }
                    if slot == .morning { settings.setBriefTimes(morning: hour) } else { settings.setBriefTimes(evening: hour) }
                } label: {
                    Text(QuickSettingsText.pill(hour).time).monospacedDigit()
                        .font(.wisp(.body, weight: chosen ? .semibold : .regular))
                        .foregroundStyle(chosen ? Color.black : Theme.textPrimary)
                        .frame(maxWidth: .infinity).padding(.vertical, 4)
                        .background(RoundedRectangle(cornerRadius: 7).fill(chosen ? Theme.textPrimary : Theme.chipFill))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(QuickSettingsText.hourLabel(hour))
                .accessibilityAddTraits(chosen ? .isSelected : [])
            }
        }
        .padding(.horizontal, 14).padding(.top, 2).padding(.bottom, 11)
        .disabled(settings.briefTimesPending)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(slot == .morning ? "Morning brief times" : "Evening brief times")
    }

    // MARK: Pieces

    /// A labelled card: rows separated by hairlines.
    private func group<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased()).font(.wisp(.footnote, weight: .semibold)).kerning(1)
                .foregroundStyle(Theme.textMuted).padding(.leading, 4)
                .accessibilityAddTraits(.isHeader)
            VStack(spacing: 0) { content() }
                .background(RoundedRectangle(cornerRadius: 14).fill(Theme.raised))
                .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.hairline, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 14))
        }
    }

    private var hairline: some View { Rectangle().fill(Theme.hairline).frame(height: 1) }

    private func row<Detail: View, Control: View>(_ title: String, help: String,
                                                  @ViewBuilder detail: () -> Detail,
                                                  @ViewBuilder control: () -> Control) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.wisp(.reading, weight: .medium)).foregroundStyle(Theme.textPrimary)
                detail().font(.wisp(.body)).foregroundStyle(Theme.textSecondary).lineLimit(2).truncationMode(.middle)
            }
            .help(help)
            Spacer(minLength: 12)
            control()
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .accessibilityElement(children: .contain)
    }

    private func pillButton(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.wisp(.body)).foregroundStyle(primary ? Theme.textPrimary : Theme.textSecondary)
                .padding(.horizontal, 12).padding(.vertical, 5)
                .background(Capsule().fill(Theme.chipFill))
                .overlay(Capsule().strokeBorder(Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
    }
}

/// Quick settings' switch: a bright track with a dark knob when on, grey when off.
/// The amber variant is for Full access.
struct NotchSwitch: ToggleStyle {
    var amber = false
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeBody(configuration: Configuration) -> some View {
        let on = configuration.isOn
        return Button { configuration.isOn.toggle() } label: {
            Capsule()
                .fill(on ? (amber ? Theme.warn : Color(white: 0.95)) : Theme.controlFill)
                .frame(width: 38, height: 22)
                .overlay(alignment: on ? .trailing : .leading) {
                    Circle().fill(on ? Color.black : Theme.textMuted)
                        .frame(width: 18, height: 18).padding(2)
                }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: on)
                .opacity(isEnabled ? 1 : 0.45)
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityRepresentation { Toggle(isOn: configuration.$isOn) { configuration.label }.toggleStyle(.switch) }
    }
}
