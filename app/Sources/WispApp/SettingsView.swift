import AppKit
import SwiftUI

@MainActor
final class SettingsLoader: ObservableObject {
    @Published var installed: [String] = []
    @Published var roles: [String: String] = [:]
    @Published var saving = false
    @Published var fullAccess = false
    @Published var idleMinutes: Double = 5
    @Published var humanizerEnabled = false

    // "research" is deliberately last and optional: service/research/
    // orchestrator.py's _research_model() falls back to the `coding` role
    // whenever no explicit "research" override is set, so leaving this on
    // its default isn't a broken state — it's the normal one.
    let roleOrder = ["fast", "coding", "reasoning", "general", "research"]
    // Shown only when the backend is unreachable and can't report the real
    // roster. Mirrors service/config/models.yaml's defaults — if you change
    // them there, change them here, or an offline Settings pane shows models
    // the user never chose.
    let fallbackModels = [
        "Agents-A1-4B-oQe6",
        "gemma-4-E4B-it-qat-4bit",
    ]
    let fallbackRoles = [
        "fast": "Agents-A1-4B-oQe6",
        "coding": "Agents-A1-4B-oQe6",
        "reasoning": "Agents-A1-4B-oQe6",
        "general": "Agents-A1-4B-oQe6",
    ]
    private let client = WispClient()

    func label(_ r: String) -> String {
        ["fast": "Fast & routing", "coding": "Coding", "reasoning": "Reasoning",
         "general": "General & agentic", "research": "Research"][r] ?? r
    }
    func desc(_ r: String) -> String {
        ["fast": "Quick replies, picks the expert", "coding": "Writing and fixing code",
         "reasoning": "Multi-step thinking, planning", "general": "Everything else, tool use",
         "research": "Wisp Research's plan/query/extraction/synthesis calls"][r] ?? ""
    }

    var modelChoices: [String] {
        Array(Set(installed + Array(roles.values) + fallbackModels)).sorted()
    }

    func selectedModel(for role: String) -> String {
        if role == "research", roles["research"] == nil {
            // No explicit override yet — show what it actually resolves to
            // at runtime (the coding model) rather than an unrelated model
            // that would look "selected" without being true until touched.
            return selectedModel(for: "coding")
        }
        return roles[role] ?? fallbackRoles[role] ?? modelChoices.first ?? ""
    }

    func refresh() {
        Task {
            let m = await client.models()
            self.installed = m.installed
            self.roles = m.roles
            self.fullAccess = await client.mode().fullAccess
            self.idleMinutes = await client.idleTimeout()
            self.humanizerEnabled = await client.humanizerEnabled()
        }
    }

    func setHumanizerEnabled(_ on: Bool) {
        humanizerEnabled = on
        Task { self.humanizerEnabled = await client.setHumanizerEnabled(on) }
    }

    func setFullAccess(_ on: Bool) {
        fullAccess = on
        Task { self.fullAccess = await client.setFullAccess(on).fullAccess }
    }

    func setIdleMinutes(_ minutes: Double) {
        idleMinutes = minutes
        Task { self.idleMinutes = await client.setIdleTimeout(minutes) }
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
            _ = try? await URLSession.shared.data(for: req)
            self.saving = false
            await PendingConfigWrites.shared.end()
        }
    }
}

struct SettingsView: View {
    @StateObject private var loader = SettingsLoader()
    @ObservedObject private var sync = SyncProgress.shared
    // Four theme groups (down from six single-purpose ones): Models, Automation
    // (background/scheduled sync status), Privacy & Access (what Wisp is
    // allowed to do + which accounts it reads),
    // Advanced (Air Compute + Memory — occasional, technical knobs). Models
    // stays expanded by default since it's the one most people actually open
    // Settings for; everything else starts collapsed, same as before.
    @State private var showModels = true
    @State private var showAutomation = false
    @State private var showAccess = false
    @State private var showAdvanced = false
    // Off by default — see BrowserHistoryReader's doc comment on why this
    // needs its own explicit opt-in rather than following Mail/Notes/Messages
    // (which sync as soon as their own TCC permission is granted).
    @AppStorage(BrowserHistoryReader.enabledKey) private var browserHistoryEnabled = false

    var body: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Personal memory").font(.title2.weight(.medium))
                        Text("Review facts, evidence, and possible connections.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Open Memory") { MemoryWindow.shared.show() }
                }
                Divider()
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
                    }
                    .padding(.top, 8)
                } label: {
                    Text("Models").font(.title2.weight(.medium))
                }
                .tint(.secondary)
                Divider()

                // Background/scheduled work — currently just the mail history
                // scan. Backed by SyncProgress.shared for the live bar, which
                // MailReader's batch loop updates directly (same process).
                DisclosureGroup(isExpanded: $showAutomation) {
                    VStack(alignment: .leading, spacing: 14) {
                        syncStatusRow(
                            title: "Mail history (up to a year)",
                            fraction: sync.mailHistoryFraction,
                            activeLabel: "Scanning…",
                            lastDate: sync.mailHistoryLastSynced)
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

                        Toggle(isOn: $browserHistoryEnabled) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Browser history")
                                    .font(.system(size: 14, weight: .medium))
                                Text(browserHistoryEnabled
                                     ? "Reading recent Safari + Chrome history so Wisp can answer "
                                       + "questions about sites you've visited."
                                     : "Off. Wisp does not read Safari or Chrome history.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Text("Only the site, page, and title are kept — never the full "
                                     + "URL's query string, which can carry search text or "
                                     + "sign-in links.")
                                    .font(.caption2)
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

                // Occasional, technical knobs — memory management — that
                // most people set once and forget, as opposed to Models
                // (tuned often) or Automation (watched periodically).
                DisclosureGroup(isExpanded: $showAdvanced) {
                    VStack(alignment: .leading, spacing: 14) {
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

                        Divider()

                        Toggle(isOn: Binding(
                            get: { loader.humanizerEnabled },
                            set: { loader.setHumanizerEnabled($0) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Human-sounding replies")
                                    .font(.system(size: 14, weight: .medium))
                                Text(loader.humanizerEnabled
                                     ? "Plain conversational replies avoid stock AI phrasing "
                                       + "(forced lists, chatbot sign-offs, filler). Code and "
                                       + "tool-using replies are never affected."
                                     : "Off. Replies use the model's default phrasing.")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .toggleStyle(.switch)
                        .tint(.orange)
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
