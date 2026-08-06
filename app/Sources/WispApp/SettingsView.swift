import AppKit
import SwiftUI

@MainActor
final class SettingsLoader: ObservableObject {
    @Published var installed: [String] = []
    @Published var roles: [String: String] = [:]
    @Published var saving = false
    @Published var fullAccess = false
    @Published var idleMinutes: Double = 5
    @Published var airEnabled = false
    @Published var airBaseURL = WispConfig.airBaseURLDefault
    @Published var airRouting = true
    @Published var airSummaries = false
    @Published var airDraft = false
    @Published var airStatus = "not_checked"
    @Published var airStatusMessage = ""
    @Published var superModel = ""
    @Published var superModelFavorites: [String] = []
    @Published var profileSources: [WispClient.ProfileSourceMeta] = []
    @Published var profileMissing: [String] = []

    let roleOrder = ["fast", "coding", "reasoning", "vision", "general", "profile_map", "profile"]
    let fallbackModels = [
        "gemma-4-e4b-it-4bit",
        "gpt-oss-20b-MXFP4-Q8",
        "Qwen3-VL-8B-Instruct-4bit",
    ]
    let fallbackRoles = [
        "fast": "gemma-4-e4b-it-4bit",
        "coding": "gpt-oss-20b-MXFP4-Q8",
        "reasoning": "gpt-oss-20b-MXFP4-Q8",
        "vision": "Qwen3-VL-8B-Instruct-4bit",
        "general": "gpt-oss-20b-MXFP4-Q8",
        // Matches service/config/models.yaml's defaults — see that file for
        // why these are two separate roles instead of reusing `general`.
        "profile_map": "gemma-4-E4B-it-qat-4bit",
        "profile": "gpt-oss-20b-MXFP4-Q8",
    ]
    private let client = WispClient()

    func label(_ r: String) -> String {
        ["fast": "Fast & routing", "coding": "Coding", "reasoning": "Reasoning",
         "vision": "Vision", "general": "General & agentic",
         "profile_map": "Profile: reading", "profile": "Profile: merging"][r] ?? r
    }
    func desc(_ r: String) -> String {
        ["fast": "Quick replies, picks the expert", "coding": "Writing and fixing code",
         "reasoning": "Multi-step thinking, planning", "vision": "Screenshots and images",
         "general": "Everything else, tool use",
         "profile_map": "Reads mail/messages/notes for facts — most of the calls, "
                         + "runs best on something small",
         "profile": "Categorizes and merges facts into sections — fewer calls, "
                     + "benefits from a stronger model"][r] ?? ""
    }

    var modelChoices: [String] {
        Array(Set(installed + Array(roles.values) + fallbackModels)).sorted()
    }

    // Super Model deliberately shows EVERY installed model plus every model
    // starred as a favorite in oMLX, not just the ones currently assigned to
    // a role — the whole point is picking something that might not otherwise
    // be in rotation. `favorites` comes straight from oMLX's own settings
    // file server-side and is available even when `installed` is empty
    // (oMLX's server subprocess isn't running — a common state, since Wisp
    // only starts it on demand) — that gap was why only 2-3 models used to
    // show up here. Always includes whatever's currently selected so a prior
    // choice never disappears even if it later falls out of both lists.
    var superModelChoices: [String] {
        let base = installed.isEmpty ? modelChoices : installed
        return Array(Set(base + superModelFavorites + [superModel]).subtracting([""])).sorted()
    }

    func selectedModel(for role: String) -> String {
        roles[role] ?? fallbackRoles[role] ?? modelChoices.first ?? ""
    }

    // Mirrors scheduler.py's _PROFILE_SOURCE_CYCLE / _maybe_daily_profile
    // exactly: `date.toordinal() % 4` in Python. toordinal(2000-01-01) % 4 == 0
    // (verified separately), so "days between 2000-01-01 and today" mod 4
    // equals Python's toordinal mod 4 for any date — no need to reproduce
    // toordinal()'s actual epoch, just the day-count DIFFERENCE, which is the
    // same real-world quantity in both languages' Gregorian calendars.
    static let profileSourceCycle = ["messages", "email", "notes", "calendar"]
    var nextProfileSource: String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = .current
        guard let ref = cal.date(from: DateComponents(year: 2000, month: 1, day: 1)) else {
            return Self.profileSourceCycle[0]
        }
        let days = cal.dateComponents([.day], from: cal.startOfDay(for: ref),
                                      to: cal.startOfDay(for: Date())).day ?? 0
        let idx = ((days % 4) + 4) % 4
        return Self.profileSourceCycle[idx]
    }

    func refresh() {
        Task {
            let m = await client.models()
            self.installed = m.installed
            self.roles = m.roles
            self.fullAccess = await client.mode().fullAccess
            self.idleMinutes = await client.idleTimeout()
            self.apply(await client.airCompute())
            let superInfo = await client.superModelInfo()
            self.superModel = superInfo.model
            self.superModelFavorites = superInfo.favorites
            let profMeta = await client.fetchProfileMeta()
            self.profileSources = profMeta.sources
            self.profileMissing = profMeta.missing
        }
    }

    func setSuperModel(_ model: String) {
        superModel = model
        saving = true
        Task {
            await PendingConfigWrites.shared.begin()
            self.superModel = await client.setSuperModelName(model)
            self.saving = false
            await PendingConfigWrites.shared.end()
        }
    }

    private func apply(_ air: WispClient.AirCompute) {
        airEnabled = air.enabled
        airBaseURL = air.baseURL
        airRouting = air.routing
        airSummaries = air.summaries
        airDraft = air.draft
        airStatus = air.status
        airStatusMessage = air.statusMessage
    }

    func setFullAccess(_ on: Bool) {
        fullAccess = on
        Task { self.fullAccess = await client.setFullAccess(on).fullAccess }
    }

    func setIdleMinutes(_ minutes: Double) {
        idleMinutes = minutes
        Task { self.idleMinutes = await client.setIdleTimeout(minutes) }
    }

    func setAirEnabled(_ on: Bool) {
        airEnabled = on
        Task { self.apply(await client.setAirCompute(enabled: on)) }
    }

    func setAirRouting(_ on: Bool) {
        airRouting = on
        Task { self.apply(await client.setAirCompute(routing: on)) }
    }

    func saveAirBaseURL() {
        saving = true
        Task {
            self.apply(await client.setAirCompute(baseURL: airBaseURL))
            self.saving = false
        }
    }

    func checkAirCompute() {
        Task { self.apply(await client.checkAirCompute()) }
    }

    var airStatusLabel: String {
        switch airStatus {
        case "available": return "Available"
        case "offline": return "Offline"
        default: return "Not checked"
        }
    }

    func set(_ role: String, _ model: String) {
        roles[role] = model
        saving = true
        Task {
            await PendingConfigWrites.shared.begin()
            var req = URLRequest(url: WispClient.baseURL.appendingPathComponent("config"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["role": role, "model": model])
            _ = try? await WispSession.shared.data(for: req)
            self.saving = false
            await PendingConfigWrites.shared.end()
        }
    }
}

struct SettingsView: View {
    @StateObject private var loader = SettingsLoader()
    @ObservedObject private var sync = SyncProgress.shared
    // Four theme groups (down from six single-purpose ones): Models, Automation
    // (background/scheduled work — sync status + the profile build rotation),
    // Privacy & Access (what Wisp is allowed to do + which accounts it reads),
    // Advanced (Air Compute + Memory — occasional, technical knobs). Models
    // stays expanded by default since it's the one most people actually open
    // Settings for; everything else starts collapsed, same as before.
    @State private var showModels = true
    @State private var showAutomation = false
    @State private var showAccess = false
    @State private var showAdvanced = false
    // Mirrors AppQuitter.isEnabled (UserDefaults-backed) so the switch has
    // something to bind to. Off unless the user has turned it on.
    @State private var superModelQuitsApps = AppQuitter.isEnabled

    var body: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 16) {
                DisclosureGroup(isExpanded: $showModels) {
                    VStack(alignment: .leading, spacing: 14) {
                        Text("Pick the expert for each kind of task")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        ForEach(loader.roleOrder, id: \.self) { role in
                            HStack(alignment: .center) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(loader.label(role))
                                        .font(.system(size: 14, weight: .medium))
                                    Text(loader.desc(role))
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                Picker("", selection: Binding(
                                    get: { loader.selectedModel(for: role) },
                                    set: { loader.set(role, $0) })) {
                                    ForEach(loader.modelChoices, id: \.self) {
                                        Text(OverlayModel.abbrev($0)).tag($0)
                                    }
                                }
                                .pickerStyle(.menu)
                                .labelsHidden()
                                .frame(width: 190)
                            }
                            if role != loader.roleOrder.last { Divider() }
                        }
                        Divider()
                        HStack(alignment: .center) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Super Model")
                                    .font(.system(size: 14, weight: .medium))
                                Text("Turned on from the Wisp panel or right-click menu, for the "
                                     + "toughest requests — quits other apps and raises the VRAM "
                                     + "limit for this one model. Never picked automatically.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Picker("", selection: Binding(
                                get: { loader.superModel.isEmpty ? loader.superModelChoices.first ?? "" : loader.superModel },
                                set: { loader.setSuperModel($0) })) {
                                ForEach(loader.superModelChoices, id: \.self) {
                                    Text(OverlayModel.abbrev($0)).tag($0)
                                }
                            }
                            .pickerStyle(.menu)
                            .labelsHidden()
                            .frame(width: 190)
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Models").font(.title2.weight(.medium))
                }
                .tint(.secondary)
                Divider()

                // Background/scheduled work: the two long-running syncs (mail
                // history, profile build) plus which profile source is up
                // next tonight — grouped together since they're all "things
                // Wisp does on its own over time", not something to configure
                // per se. Backed by SyncProgress.shared for the live bars:
                // mail history updates directly from MailReader's batch loop
                // (same process); profile progress relays the backend's SSE
                // events (see OverlayModel's "profile_progress" case).
                DisclosureGroup(isExpanded: $showAutomation) {
                    VStack(alignment: .leading, spacing: 14) {
                        syncStatusRow(
                            title: "Mail history (up to a year)",
                            fraction: sync.mailHistoryFraction,
                            activeLabel: "Scanning…",
                            lastDate: sync.mailHistoryLastSynced)
                        Divider()
                        syncStatusRow(
                            title: "Profile build",
                            fraction: sync.profileBuildFraction,
                            activeLabel: sync.profileBuildLabel.isEmpty ? "Building…" : sync.profileBuildLabel,
                            lastDate: sync.profileLastBuilt)
                        Divider()

                        // One source scanned per night (~4am) instead of all
                        // four in one long pass — see scheduler.py's
                        // _maybe_daily_profile for why (sustained gpt-oss load
                        // is real fan/thermal cost, not just wall-clock).
                        // "Next up" is computed client-side from today's date
                        // using the SAME rotation as the backend (see
                        // SettingsLoader.nextProfileSource) — it's not fetched,
                        // so it stays correct even before the first refresh.
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Profile sources").font(.system(size: 14, weight: .medium))
                            Text("Rotates one source a night rather than rebuilding "
                                 + "everything at once.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            ForEach(loader.profileSources, id: \.name) { s in
                                HStack {
                                    Text(s.name.capitalized)
                                        .font(.system(size: 13))
                                    Spacer()
                                    if let last = s.lastScanned {
                                        Text(last, style: .relative)
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    } else if loader.profileMissing.contains(s.name) {
                                        Text("unavailable")
                                            .font(.caption)
                                            .foregroundStyle(.orange)
                                    } else {
                                        Text("not scanned yet")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                            HStack(spacing: 4) {
                                Text("Next up tonight:")
                                Text(loader.nextProfileSource.capitalized)
                                    .fontWeight(.medium)
                            }
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(.top, 2)
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Automation").font(.title3.weight(.medium))
                }
                .tint(.secondary)
                Divider()

                // What Wisp is allowed to do (Access) and which accounts it
                // reads from (Linked Accounts) — grouped as "Privacy & Access"
                // since both answer "what can Wisp touch", just at different
                // scopes (an action vs. a data source).
                DisclosureGroup(isExpanded: $showAccess) {
                    VStack(alignment: .leading, spacing: 14) {
                        Toggle(isOn: Binding(
                            get: { loader.fullAccess },
                            set: { loader.setFullAccess($0) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Full access")
                                    .font(.system(size: 14, weight: .medium))
                                Text(loader.fullAccess
                                     ? "Actions run automatically with no confirmation."
                                     : "Risky actions pause for your confirmation.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(.orange)

                        Divider()

                        // Wisp reads whatever Mail/Calendar accounts are
                        // already configured in Mail.app/Calendar.app — every
                        // linked account shows up automatically once granted,
                        // and Wisp already tells them apart (see the
                        // `account` param on summarize_emails/view_emails/
                        // get_upcoming/get_past_events). Adding an account
                        // itself is a macOS step, deliberately NOT something
                        // Wisp does for you: only the OS should ever hold
                        // your email password/OAuth token, never a
                        // third-party app. This just opens the right System
                        // Settings pane.
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Linked Accounts")
                                .font(.system(size: 14, weight: .medium))
                            Text("Wisp automatically reads every account already set up in "
                                 + "Mail.app and Calendar.app, and can tell them apart when "
                                 + "you ask about a specific one (e.g. \"what's in my work "
                                 + "email\"). To add another account, use macOS's own "
                                 + "Internet Accounts settings — Wisp never handles your "
                                 + "email password or sign-in directly.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Button("Open Internet Accounts Settings…") {
                                if let url = URL(string:
                                    "x-apple.systempreferences:com.apple.preferences.internetaccounts") {
                                    NSWorkspace.shared.open(url)
                                }
                            }
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Privacy & Access").font(.title3.weight(.medium))
                }
                .tint(.secondary)
                Divider()

                // Occasional, technical knobs — distributed compute and
                // memory management — that most people set once and forget,
                // as opposed to Models (tuned often) or Automation (watched
                // periodically).
                DisclosureGroup(isExpanded: $showAdvanced) {
                    VStack(alignment: .leading, spacing: 14) {
                        Toggle(isOn: $superModelQuitsApps) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Super Model quits your other apps")
                                    .font(.system(size: 14, weight: .medium))
                                Text(superModelQuitsApps
                                     ? "Engaging Super Model will close other open apps to free memory."
                                     : "Super Model leaves your other apps alone.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(.blue)
                        .onChange(of: superModelQuitsApps) { _, newValue in
                            AppQuitter.isEnabled = newValue
                        }

                        Divider()

                        Toggle(isOn: Binding(
                            get: { loader.airEnabled },
                            set: { loader.setAirEnabled($0) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Use MacBook Air compute")
                                    .font(.system(size: 14, weight: .medium))
                                Text(loader.airEnabled
                                     ? "Eligible side work can run on the Air."
                                     : "All routing and side work stays local.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(.blue)

                        HStack(spacing: 10) {
                            TextField(WispConfig.airBaseURLPlaceholder, text: $loader.airBaseURL)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 12))
                                .onSubmit { loader.saveAirBaseURL() }
                            Button("Save") { loader.saveAirBaseURL() }
                        }

                        Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 10) {
                            GridRow {
                                Text("Routing").font(.system(size: 13, weight: .medium))
                                Toggle("", isOn: Binding(
                                    get: { loader.airRouting },
                                    set: { loader.setAirRouting($0) }))
                                    .labelsHidden()
                            }
                            GridRow {
                                Text("Summaries (experimental)")
                                    .font(.system(size: 13, weight: .medium))
                                Toggle("", isOn: $loader.airSummaries)
                                    .labelsHidden()
                                    .disabled(true)
                            }
                            GridRow {
                                Text("Draft model (experimental)")
                                    .font(.system(size: 13, weight: .medium))
                                Toggle("", isOn: $loader.airDraft)
                                    .labelsHidden()
                                    .disabled(true)
                            }
                        }
                        .toggleStyle(.switch)

                        HStack {
                            Circle()
                                .fill(loader.airStatus == "available" ? .green :
                                      loader.airStatus == "offline" ? .orange : .gray)
                                .frame(width: 6, height: 6)
                            Text("Air status: \(loader.airStatusLabel)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Spacer()
                            Button("Check") { loader.checkAirCompute() }
                        }

                        Divider()

                        HStack(alignment: .center) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Auto-unload idle models")
                                    .font(.system(size: 14, weight: .medium))
                                Text(loader.idleMinutes <= 0
                                     ? "Never — models stay resident until you unload them."
                                     : "Unload a model after \(Int(loader.idleMinutes)) min of no use.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Stepper(value: Binding(
                                get: { loader.idleMinutes },
                                set: { loader.setIdleMinutes($0) }), in: 0...120, step: 5) {
                                Text(loader.idleMinutes <= 0 ? "Off" : "\(Int(loader.idleMinutes))m")
                                    .font(.system(size: 13, weight: .medium))
                                    .frame(width: 40, alignment: .trailing)
                            }
                            .frame(width: 130)
                        }
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Advanced").font(.title3.weight(.medium))
                }
                .tint(.secondary)

                HStack {
                    Text("Changes apply instantly")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if loader.saving { ProgressView().controlSize(.small) }
                    Button("Refresh models") { loader.refresh() }
                }
            }
            .padding(24)
        }
        .scrollIndicators(.visible)
        .frame(width: 560, height: 620)
        .task { loader.refresh() }
    }

    // One sync's live status: a percentage + linear bar while `fraction` is
    // non-nil (actively running), otherwise a quiet "last synced <relative
    // time>" line, or "not run yet" if it's never completed this launch.
    @ViewBuilder
    private func syncStatusRow(title: String, fraction: Double?, activeLabel: String,
                               lastDate: Date?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.system(size: 14, weight: .medium))
                Spacer()
                if let fraction {
                    Text("\(Int(fraction * 100))%")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if let fraction {
                ProgressView(value: fraction)
                    .progressViewStyle(.linear)
                Text(activeLabel)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else if let lastDate {
                HStack(spacing: 4) {
                    Text("Last synced")
                    Text(lastDate, style: .relative)
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            } else {
                Text("Not run yet this launch")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
