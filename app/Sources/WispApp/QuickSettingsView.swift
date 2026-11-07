import SwiftUI

/// The notch gear's short list: the settings people flip from week to week, one
/// section at a time under a switcher that stays put, scrolling past `cap`.
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
    /// The page's room under the header (SurfaceLayout.pageContentCap).
    var cap: CGFloat?
    /// The section showing, as last left.
    @State var section = QuickSettingsSection.saved()
    /// The brief time whose inline hour list is open.
    @State var picking: BriefSlot?
    /// The working hours and chosen Brief times; nil until the service says.
    @State var hours: UserHours?
    /// The voice list is open.
    @State var choosingVoice = false
    @State private var briefTimesError: String?
    @State private var briefTimesSaving = false
    @State private var voices: [ReadAloudVoice.Candidate] = []
    @StateObject private var voicePreview = ReadAloudPreview()
    @AppStorage(ReadAloudVoice.defaultsKey) private var voiceChoice = ""
    @AppStorage(ReadAloudVoice.rateKey) private var voiceRate = 1.0
    /// The title and switcher's laid-out height, which grows with Text size.
    @State private var fixedHeight: CGFloat = 0
    /// Decides whether the Google row shows at all (new users only).
    @StateObject var google = NotchGoogleModel()
    @AppStorage(VoiceReplyMode.defaultsKey) private var voiceRepliesRaw = VoiceReplyMode.afterVoice.rawValue
    /// The panel's Text size scale (Theme.textScale reads the same key).
    @AppStorage(Theme.textSizeKey) private var textSize: Double = 1.0

    enum BriefSlot { case morning, evening }

    private static let spacing: CGFloat = 12

    var body: some View {
        VStack(alignment: .leading, spacing: Self.spacing) {
            VStack(alignment: .leading, spacing: Self.spacing) {
                HStack {
                    Text("Settings").font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                        .accessibilityAddTraits(.isHeader)
                    Spacer()
                    NotchSettingsChrome.doneButton { model.showingQuickSettings = false }
                        .keyboardShortcut(.cancelAction)
                        .help("Close settings and go back to \(model.tab.stripTab.title)")
                }
                if let notice = model.transientNotice {
                    Text(notice).font(.wisp(.footnote)).foregroundStyle(Theme.bad)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                sectionSwitcher
            }
            .background(GeometryReader { geo in
                Color.clear.onAppear { fixedHeight = geo.size.height }
                    .onChange(of: geo.size.height) { fixedHeight = geo.size.height }
            })
            CappedScroll(cap: cap.map(scrollCap), reveals: true) { scrollingPart }
        }
        .onChange(of: section) { _, chosen in
            picking = nil
            choosingVoice = false
            voicePreview.stop()
            chosen.save()
        }
        .onAppear { voices = ReadAloudVoice.installed() }
        .onDisappear { voicePreview.stop() }
        .task {
            guard refreshes else { return }
            await settings.refreshQuick()
            await model.refreshModelMemory()
            hours = await WispClient().userHours()
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

    // MARK: Sections

    /// What's left under the title and switcher.
    private func scrollCap(_ cap: CGFloat) -> CGFloat {
        max(cap - fixedHeight - Self.spacing, SurfaceLayout.minTabScroll)
    }

    private var scrollingPart: some View {
        VStack(alignment: .leading, spacing: Self.spacing) {
            sectionCard
            HStack {
                Text("Models, Google, privacy, activity").font(.wisp(.callout)).foregroundStyle(Theme.textSecondary)
                Spacer()
                pillButton("All settings  ⌘,", primary: true) { WispRouter.shared.open(.settings) }
            }
            .padding(.horizontal, 4)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// One row where all five fit, two where they don't (a narrow panel, large text).
    private var sectionSwitcher: some View {
        ViewThatFits(in: .horizontal) {
            sectionButtons(QuickSettingsSection.allCases)
            VStack(alignment: .leading, spacing: 4) {
                sectionButtons(Array(QuickSettingsSection.allCases.prefix(3)))
                sectionButtons(Array(QuickSettingsSection.allCases.dropFirst(3)))
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Settings sections")
    }

    private func sectionButtons(_ sections: [QuickSettingsSection]) -> some View {
        HStack(spacing: 0) {
            ForEach(sections) { option in
                let chosen = option == section
                Button { section = option } label: {
                    Text(option.title)
                        .font(.wisp(.body, weight: chosen ? .semibold : .regular))
                        .foregroundStyle(chosen ? Color.black : Theme.textSecondary)
                        .padding(.horizontal, 12).padding(.vertical, 5)
                        .background(Capsule().fill(chosen ? Color.white : Color.clear))
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .fixedSize()
                .help(option.contents)
                .accessibilityHint(option.contents)
                .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
            }
        }
        .padding(1)
        .overlay(Capsule().strokeBorder(Theme.chipStroke, lineWidth: 1))
    }

    /// Every section is laid out and only the chosen one shows, so the card, and the
    /// panel with it, keeps one height as the person moves between them. A hidden
    /// section takes no clicks, keyboard focus or VoiceOver.
    private var sectionCard: some View {
        NotchSettingsChrome.card {
            ZStack(alignment: .top) {
                ForEach(QuickSettingsSection.allCases) { option in
                    if option == section {
                        rows(option)
                    } else {
                        rows(option).hidden()
                    }
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(section.title)
    }

    private func rows(_ section: QuickSettingsSection) -> some View {
        VStack(spacing: 0) {
            switch section {
            case .general:
                fullAccessRow
                hairline
                learningRow
                hairline
                macPermissionsRow
                hairline
                setupRow
                if google.showsRow {
                    // The user's deliberate exception for the very first connect
                    // only: a new user with no Google account (and an OAuth client
                    // to use) gets one Connect button. Once any account is
                    // connected there is no Google row here at all; accounts and
                    // Disconnect & erase live in Settings ▸ Google
                    // (NotchGoogleVisibility).
                    hairline
                    NotchConnectGoogle(model: google)
                }
            case .brief:
                dailySummaryRow
                hairline
                briefUpdatesRow
            case .voice:
                voiceRepliesRow
                hairline
                voiceChoiceRow
                hairline
                speakingRateRow
                hairline
                textSizeRow
            case .power:
                saveWhenAwayRow
                hairline
                saveMemoryRow
            case .advanced:
                replyDetailsRow
                hairline
                memoryRow
            }
        }
    }

    // MARK: Rows

    private var macPermissionsRow: some View {
        row("Mac permissions", help: "What Wisp can reach on this Mac, and the one thing to do for each that's off.") {
            Text("Calendar, Mail, Full Disk Access and more")
        } control: {
            pillButton("Review") { model.openPermissionPage(nil) }
        }
    }

    private var setupRow: some View {
        row("Setup", help: QuickSettingsText.setupHelp) {
            Text("Permissions, Google, About you and How to use")
        } control: {
            pillButton("Open") { NotificationCenter.default.post(name: .wispOpenPermissions, object: nil) }
        }
    }

    private var replyDetailsRow: some View {
        row("Reply details", help: "Open each reply's model, route, timing and tool calls by default (Debug Mode).") {
            Text("Model, timing and tools under each reply")
        } control: {
            Toggle("Reply details", isOn: $model.debugMode).toggleStyle(NotchSwitch())
        }
    }

    /// When Wisp says its answer out loud (VoiceReplyMode). Three chips, not a
    /// menu: the panel is non-activating, so no popup.
    private var voiceRepliesRow: some View {
        row("Read answers aloud",
            help: "Wisp speaks its answer with one of your Mac's voices; choose it in All settings › General › Voice. Ask by voice with the mic or ⌥⇧Space.") {
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

    private var saveWhenAwayRow: some View {
        row("Save power", help: QuickSettingsText.saveWhenAwayHelp) {
            Text(QuickSettingsText.saveWhenAway(on: settings.saveWhenAway, pending: settings.saveWhenAwayPending))
        } control: {
            if settings.saveWhenAway != nil {
                Toggle("Save power", isOn: Binding(get: { settings.saveWhenAway == true },
                                                       set: { settings.setSaveWhenAway($0) }))
                    .toggleStyle(NotchSwitch())
                    .disabled(settings.saveWhenAwayPending)
            } else {
                pillButton("Check") { Task { await settings.refreshSaveWhenAway() } }
                    .disabled(settings.saveWhenAwayPending)
            }
        }
    }

    private var saveMemoryRow: some View {
        row("Save memory", help: QuickSettingsText.saveMemoryHelp(detail: settings.saveMemoryDetail)) {
            Text(QuickSettingsText.saveMemory(on: settings.saveMemory, pending: settings.saveMemoryPending,
                                              detail: settings.saveMemoryDetail))
        } control: {
            if settings.saveMemory != nil {
                Toggle("Save memory", isOn: Binding(get: { settings.saveMemory == true },
                                                        set: { settings.setSaveMemory($0) }))
                    .toggleStyle(NotchSwitch())
                    .disabled(settings.saveMemoryPending)
            } else {
                pillButton("Check") { Task { await settings.refreshSaveMemory() } }
                    .disabled(settings.saveMemoryPending)
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

    /// How often the Brief updates. It follows the working hours, which are set in
    /// the Wisp window, so nothing here changes it.
    @ViewBuilder private var briefUpdatesRow: some View {
        let chosen = hours?.briefTimes
        row("Brief updates", help: QuickSettingsText.briefUpdatesHelp) {
            if let chosen {
                Text(QuickSettingsText.briefUpdates(chosen: UserHours.clockList(chosen)))
            } else {
                Text(QuickSettingsText.briefUpdates(hours: hours.map {
                    "\(UserHours.clock($0.working.start)) to \(UserHours.clock($0.working.end))"
                }))
            }
        } control: {
            if let hours {
                HStack(spacing: 6) {
                    choiceChip("Automatic", chosen: chosen == nil) { saveBriefTimes([]) }
                    choiceChip("Choose times", chosen: chosen != nil) {
                        if chosen == nil { saveBriefTimes(hours.suggestedBriefTimes) }
                    }
                }
                .disabled(briefTimesSaving)
            }
        }
        if let error = briefTimesError {
            Text(error).font(.wisp(.footnote)).foregroundStyle(Theme.bad)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14).padding(.bottom, 8)
        }
        if let hours {
            briefHourGrid(hours)
            if let chosen, chosen.count >= hours.maxBriefTimes {
                Text("Up to \(hours.maxBriefTimes) a day. Turn one off to choose another.")
                    .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                    .padding(.horizontal, 14).padding(.bottom, 11)
            }
        }
    }

    /// A morning line and an afternoon line, where they fit; four across where they
    /// don't. On Automatic it shows about when the day's updates fall, and pressing an
    /// hour starts choosing from there.
    private func briefHourGrid(_ hours: UserHours) -> some View {
        let start = hours.briefTimes ?? hours.suggestedBriefTimes
        return ViewThatFits(in: .horizontal) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 4) {
                    Text("AM").frame(width: 26, alignment: .leading)
                    ForEach(0..<12, id: \.self) { briefHourButton($0, hours: hours, start: start).frame(minWidth: 34) }
                }
                HStack(spacing: 4) {
                    Text("PM").frame(width: 26, alignment: .leading)
                    ForEach(12..<24, id: \.self) { briefHourButton($0, hours: hours, start: start).frame(minWidth: 34) }
                }
            }
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 4), spacing: 4) {
                ForEach(0..<24, id: \.self) { hour in
                    briefHourButton(hour, hours: hours, start: start, withPeriod: true)
                }
            }
        }
        .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
        .padding(.horizontal, 14).padding(.bottom, start.count >= hours.maxBriefTimes ? 4 : 11)
        .disabled(briefTimesSaving)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Brief update times")
    }

    private func briefHourButton(_ hour: Int, hours: UserHours, start: [Int], withPeriod: Bool = false) -> some View {
        let minute = hour * 60
        let on = start.contains(minute)
        let picked = on && hours.briefTimes != nil
        var from = hours
        from.briefTimes = start
        let next = from.briefTimes(toggling: minute)
        return Button {
            if let next { saveBriefTimes(next) }
        } label: {
            Text(withPeriod ? QuickSettingsText.hourLabel(hour) : QuickSettingsText.hourLabel(hour).split(separator: " ").first.map(String.init) ?? "")
                .monospacedDigit()
                .font(.wisp(.body, weight: picked ? .semibold : .regular))
                .foregroundStyle(picked ? Color.black : Theme.textPrimary)
                .frame(maxWidth: .infinity).padding(.vertical, 3)
                .background(RoundedRectangle(cornerRadius: 7).fill(
                    picked ? Theme.textPrimary : on ? Color.white.opacity(0.24) : Theme.chipFill))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(next == nil)
        .accessibilityLabel(QuickSettingsText.hourLabel(hour))
        .accessibilityAddTraits(picked ? [.isButton, .isSelected] : .isButton)
    }

    private func saveBriefTimes(_ minutes: [Int]) {
        guard refreshes else { return }
        briefTimesSaving = true
        briefTimesError = nil
        Task {
            let reply = await WispClient().saveBriefTimes(minutes)
            briefTimesSaving = false
            if let saved = reply.hours { hours = saved } else { briefTimesError = reply.error }
        }
    }

    // MARK: Voice

    /// What the picker offers here: everything but the novelty voices (Zarvox, Bells…),
    /// which All settings still lists.
    private var listedVoices: [ReadAloudVoice.Candidate] {
        ReadAloudVoice.listed(voices, preferredLanguage: Locale.preferredLanguages.first ?? "en").filter { !$0.isNovelty }
    }

    private var voiceLine: String {
        if voiceChoice == ReadAloudVoice.systemChoice { return "Your Mac's system voice" }
        if let saved = listedVoices.first(where: { $0.id == voiceChoice })
            ?? voices.first(where: { $0.id == voiceChoice }) {
            return ReadAloudVoice.shortLabel(saved, among: listedVoices)
        }
        return ReadAloudVoice.automatic(voices).map { "Automatic · \($0.name)" } ?? "Automatic"
    }

    @ViewBuilder private var voiceChoiceRow: some View {
        row("Voice", help: "The voice Wisp reads answers with. It stays on this Mac. The same choice is in All settings › General › Voice.") {
            Text(voiceLine)
        } control: {
            HStack(spacing: 6) {
                pillButton(voicePreview.speaking ? "Stop" : "Hear it") {
                    if voicePreview.speaking { voicePreview.stop() } else { voicePreview.play(choice: voiceChoice, rate: voiceRate) }
                }
                .accessibilityLabel(voicePreview.speaking ? "Stop the sample" : "Hear this voice")
                pillButton(choosingVoice ? "Close" : "Change") { choosingVoice.toggle() }
                    .accessibilityLabel(choosingVoice ? "Close the voice list" : "Change the voice")
            }
        }
        if choosingVoice { voiceList.revealInPanel("voice-list") }
    }

    private var voiceList: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView(.vertical) {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 4)], spacing: 4) {
                    voiceChip(ReadAloudVoice.automatic(voices).map { "Automatic · \($0.name)" } ?? "Automatic", id: "")
                    voiceChip("System voice", id: ReadAloudVoice.systemChoice)
                    ForEach(listedVoices, id: \.id) { voice in
                        voiceChip(ReadAloudVoice.shortLabel(voice, among: listedVoices), id: voice.id)
                    }
                }
            }
            .frame(height: 104 * Theme.textScale)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    voiceDownloadNote
                    Spacer(minLength: 8)
                    voiceDownloadButton
                }
                VStack(alignment: .leading, spacing: 6) {
                    voiceDownloadNote
                    voiceDownloadButton
                }
            }
        }
        .padding(.horizontal, 14).padding(.bottom, 11)
    }

    private var voiceDownloadNote: some View {
        Text(ReadAloudVoice.onlyBasicVoices(listedVoices)
             ? "These are all basic voices. Natural ones are a free download in System Settings."
             : "More natural voices are a free download in System Settings.")
            .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
            .fixedSize(horizontal: false, vertical: true)
            .frame(minWidth: 150, idealWidth: 150, alignment: .leading)
            .help("System Settings › Accessibility › Spoken Content › System Voice › Manage Voices.")
    }

    private var voiceDownloadButton: some View {
        pillButton("Get voices") {
            if let url = URL(string: ReadAloudVoice.downloadPane) { Permissions.openFix(url) }
        }
        .accessibilityLabel("Get more voices in System Settings")
    }

    private func voiceChip(_ title: String, id: String) -> some View {
        let chosen = voiceChoice == id
            || (id.isEmpty && voiceChoice != ReadAloudVoice.systemChoice && !voices.contains { $0.id == voiceChoice })
        return Button {
            voiceChoice = id
            voicePreview.stop()
        } label: {
            Text(title).lineLimit(1).font(.wisp(.body, weight: chosen ? .semibold : .regular))
                .foregroundStyle(chosen ? Color.black : Theme.textPrimary)
                .frame(maxWidth: .infinity).padding(.vertical, 4).padding(.horizontal, 6)
                .background(RoundedRectangle(cornerRadius: 7).fill(chosen ? Theme.textPrimary : Theme.chipFill))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
    }

    /// How fast it reads. Chips, like the rows around it: the panel is non-activating,
    /// and a slider's drag is untested there. All settings has the slider.
    private var speakingRateRow: some View {
        let multiple = ReadAloudVoice.rateMultiple(voiceRate)
        return row("Speaking rate", help: "How fast Wisp reads answers aloud. Hear it with the Voice row's button.") {
            Text(ReadAloudVoice.rateText(multiple))
        } control: {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 4) { rateChips(ReadAloudVoice.rateChoices, multiple: multiple) }
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 4) { rateChips(Array(ReadAloudVoice.rateChoices.prefix(3)), multiple: multiple) }
                    HStack(spacing: 4) { rateChips(Array(ReadAloudVoice.rateChoices.dropFirst(3)), multiple: multiple) }
                }
            }
        }
    }

    private func rateChips(_ choices: [Double], multiple: Double) -> some View {
        ForEach(choices, id: \.self) { choice in
            let on = abs(choice - multiple) < 0.001
            pillButton(choice == 1 ? "Normal" : "\(Int((choice * 100).rounded()))%", primary: on) { voiceRate = choice }
                .accessibilityLabel(ReadAloudVoice.rateText(choice))
                .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
        }
    }

    /// A pill that shows which of two is on.
    private func choiceChip(_ title: String, chosen: Bool, action: @escaping () -> Void) -> some View {
        pillButton(title, primary: chosen, action: action)
            .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
    }

    @ViewBuilder private var dailySummaryRow: some View {
        row("Daily summary", help: QuickSettingsText.dailySummaryHelp) {
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
        .accessibilityLabel("\(slot == .morning ? "Morning" : "Evening") summary, \(QuickSettingsText.hourLabel(hour))")
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
        .accessibilityLabel(slot == .morning ? "Morning summary times" : "Evening summary times")
    }

    // MARK: Pieces

    private var hairline: some View { NotchSettingsChrome.hairline }

    private func row<Detail: View, Control: View>(_ title: String, help: String,
                                                  @ViewBuilder detail: () -> Detail,
                                                  @ViewBuilder control: () -> Control) -> some View {
        let label = VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.wisp(.reading, weight: .medium)).foregroundStyle(Theme.textPrimary)
            detail().font(.wisp(.body)).foregroundStyle(Theme.textSecondary).lineLimit(2).truncationMode(.middle)
        }
        .help(help)
        // Beside its control while the words keep room to read; under them on the
        // narrowest panel or at large text, where a row would otherwise break words
        // and push its control off the edge, out of the scroll's reach.
        return ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 12) {
                label.frame(minWidth: 150, idealWidth: 150, alignment: .leading)
                Spacer(minLength: 12)
                control()
            }
            VStack(alignment: .leading, spacing: 8) {
                label
                control()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14).padding(.vertical, 11)
        .accessibilityElement(children: .contain)
    }

    private func pillButton(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
        NotchSettingsChrome.pill(title, primary: primary, action: action)
    }
}

/// The card, hairline and pill quick settings and the permission page share.
enum NotchSettingsChrome {
    /// A card: rows separated by hairlines.
    static func card<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        VStack(spacing: 0) { content() }
            .background(RoundedRectangle(cornerRadius: 14).fill(Theme.raised))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.hairline, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    static var hairline: some View { Rectangle().fill(Theme.hairline).frame(height: 1) }

    /// How the page closes, so it can be found at a glance. Esc does the same.
    static func doneButton(action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text("Done").font(.wisp(.callout, weight: .semibold)).foregroundStyle(Color.black)
                .padding(.horizontal, 14).padding(.vertical, 5)
                .background(Capsule().fill(Color.white))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
    }

    static func pill(_ title: String, primary: Bool = false, action: @escaping () -> Void) -> some View {
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
