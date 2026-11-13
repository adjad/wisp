import SwiftUI

// "Let Wisp act for me" (service/proactive/acting.py): the one yes behind everything Wisp
// does without being asked. Off until the person says yes; a yes covers only messages that
// arrive after it; off stops everything at once. What Wisp does once it is on is the
// service's own list (`does`), shown word for word, so no screen can claim more than is
// built. Wisp never sends anything by itself. Unknown state shows "Check", never a guess.

struct ActForMe: Equatable {
    struct Done: Equatable, Identifiable {
        let id: Int
        let title: String
        let who: String
        let at: Double
        let undone: Bool
        /// Not empty when Wisp began this and never learned the outcome: where to look for it.
        let check: String
    }

    let enabled: Bool
    let enabledAt: Double?
    let asked: Bool
    let perDay: Int
    let does: [String]
    let never: String
    /// What the yes reaches, in the service's words (messages after the yes; deadlines already there).
    let scope: String
    /// While on: what the person's yes named, and what Wisp can do now that it did not name.
    /// Wisp does only the first; the second waits for its own yes.
    let agreed: [String]
    let new: [String]
    let recent: [Done]

    static let title = "Let Wisp act for me"

    init?(_ json: [String: Any]) {
        guard let enabled = json["enabled"] as? Bool else { return nil }
        self.enabled = enabled
        enabledAt = json["enabled_at"] as? Double
        asked = json["asked"] as? Bool ?? false
        perDay = json["per_day"] as? Int ?? 10
        does = (json["does"] as? [String] ?? []).filter { !$0.isEmpty }
        never = json["never"] as? String ?? "Wisp never sends anything by itself."
        scope = json["scope"] as? String ?? "Adding covers only messages that arrive after you say yes."
        agreed = (json["agreed"] as? [String] ?? []).filter { !$0.isEmpty }
        new = (json["new"] as? [String] ?? []).filter { !$0.isEmpty }
        recent = (json["recent"] as? [[String: Any]] ?? []).enumerated().compactMap { index, row in
            guard let title = row["title"] as? String, !title.isEmpty else { return nil }
            return Done(id: index, title: title, who: row["who"] as? String ?? "",
                        at: row["at"] as? Double ?? 0, undone: row["undone"] as? Bool ?? false,
                        check: row["check"] as? String ?? "")
        }
    }

    /// The one line under the title.
    var line: String {
        !enabled ? "Off. Wisp does none of this without your tap."
            : (new.isEmpty ? "On. "
               : (agreed.isEmpty ? "On, but nothing is agreed yet; " : "On for what you agreed to; ")
                 + "\(new.count == 1 ? "one action waits" : "\(new.count) actions wait") "
                 + "for your yes (turn this off and on to review them). ")
              + "Wisp shows each thing it does in the Brief as “Wisp did this”, with an Undo. "
              + "At most \(perDay) a day. " + never
    }

    /// The confirmation for the notch row: compact, and explicit that a yes covers only what
    /// arrives after it. Same facts as `consent`.
    var confirmation: String {
        let list = does.joined(separator: " · ")
        return "Without asking first, Wisp: " + list
            + ". " + scope + " At most \(perDay) a day, and each one has an Undo. " + never
    }

    /// What the person agrees to: the service's list, then the limits, then what never happens.
    var consent: String {
        // While on with actions still waiting, only what was agreed is said to be happening.
        let waiting = enabled && !new.isEmpty
        let bullets = { (lines: [String]) in lines.map { "• " + $0 }.joined(separator: "\n") }
        let list = !waiting ? bullets(does)
            : (agreed.isEmpty ? "• Nothing yet." : bullets(agreed)) + "\n\nWaiting for your yes:\n" + bullets(new)
        return "Without asking first, Wisp:\n" + list
            + "\n\n" + scope + " Never from group chats or promotions. "
            + "At most \(perDay) a day. Each one shows in the Brief as "
            + "“Wisp did this” with an Undo. " + never + " You can turn this off at any time."
    }
}

@MainActor
final class ActForMeModel: ObservableObject {
    static let shared = ActForMeModel()

    @Published private(set) var state: ActForMe?
    @Published private(set) var pending = false
    @Published private(set) var problem: String?

    private static let path = "assistant/proactive/act"

    func refresh() async {
        if case .ok(let json) = await MeetingClient.send("GET", Self.path) { state = ActForMe(json) }
    }

    /// `onlyNew`: the yes on the card about new actions. It adds to a yes that is in force and
    /// is refused if the person turned everything off meanwhile.
    func set(_ on: Bool, onlyNew: Bool = false) {
        guard !pending else { return }
        pending = true
        problem = nil
        Task {
            let body: [String: Any] = on && onlyNew ? ["enabled": true, "only_new": true] : ["enabled": on]
            switch await MeetingClient.send("PUT", Self.path, body: body) {
            case .ok(let json): state = ActForMe(json)
            case .refused(let why):
                problem = why
                // The switch shows what is true, never what was asked for.
                await refresh()
            }
            pending = false
        }
    }

    /// The question was shown and put aside without a yes: it is not asked again.
    func putAside() {
        guard !pending else { return }
        pending = true
        problem = nil
        Task {
            switch await MeetingClient.send("POST", Self.path + "/asked", body: [:]) {
            case .ok(let json): state = ActForMe(json)
            case .refused(let why): problem = why
            }
            pending = false
        }
    }
}

/// Settings › Privacy: the switch, and the short list of what Wisp did lately.
struct ActForMeSettingsRow: View {
    @ObservedObject private var model = ActForMeModel.shared
    @State private var confirmOn = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let state = model.state {
                Toggle(isOn: Binding(get: { state.enabled },
                                     set: { on in if on { confirmOn = true } else { model.set(false) } })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ActForMe.title).font(.system(size: 14, weight: .medium))
                        Text(state.line).font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .toggleStyle(.switch)
                .disabled(model.pending)
                .accessibilityLabel(ActForMe.title)
                .accessibilityHint(state.line)
                if let problem = model.problem {
                    Text(problem).font(.caption).foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !state.recent.isEmpty {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Lately").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                        ForEach(state.recent.prefix(6)) { done in
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Image(systemName: !done.check.isEmpty ? "questionmark" : done.undone ? "arrow.uturn.backward" : "checkmark")
                                    .font(.system(size: 9, weight: .bold)).foregroundStyle(.secondary)
                                    .accessibilityHidden(true)
                                Text(!done.check.isEmpty ? "May have added “\(done.title)”: check \(done.check)"
                                     : done.title + (done.undone ? " (undone)" : ""))
                                    .font(.caption).strikethrough(done.undone)
                                    .foregroundStyle(done.undone ? .secondary : .primary).lineLimit(1)
                                Spacer(minLength: 4)
                                Text(Date(timeIntervalSince1970: done.at), style: .relative)
                                    .font(.caption2).foregroundStyle(.secondary).monospacedDigit()
                            }
                            .accessibilityElement(children: .combine)
                            .help(done.check.isEmpty ? "" : "Wisp started this and was interrupted before it could confirm. If it is there and you don't want it, remove it in \(done.check).")
                        }
                    }
                    .padding(.top, 2)
                }
            } else {
                Text(ActForMe.title).font(.system(size: 14, weight: .medium))
                Button("Check") { Task { await model.refresh() } }
                    .controlSize(.small)
                    .accessibilityLabel("Check whether Wisp acts for you")
            }
        }
        .task { await model.refresh() }
        .confirmationDialog("Let Wisp act for you?", isPresented: $confirmOn, titleVisibility: .visible) {
            Button("Turn on") { model.set(true) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(model.state?.consent ?? "")
        }
    }
}

/// The question, asked once: in first-run setup, and for an install that was set up before
/// this existed, once as a card at the top of the Brief. It shows only while the service
/// says it has not been asked. Yes turns the gate on; Not now is remembered and it is not
/// asked again (the switch stays in Settings). Words come from the service's own list.
struct ActForMeAskCard: View {
    enum Place { case setup, brief }
    let place: Place
    @ObservedObject private var model = ActForMeModel.shared
    /// What the person just chose here, so the card can say so before it goes.
    @State private var answered: Bool?
    /// The card was asking only about actions added since an earlier yes.
    @State private var askedAboutNew = false

    static let question = "Let Wisp act for you?"
    static let yes = "Yes, let Wisp act"
    static let notNow = "Not now"

    var body: some View {
        Group {
            if let state = model.state, !state.asked || answered != nil, !state.does.isEmpty {
                card(state)
            }
        }
        .task { await model.refresh() }
    }

    private func card(_ state: ActForMe) -> some View {
        // An earlier yes named fewer actions than Wisp has now: ask about the new ones only.
        // Wisp does none of them until this is answered with a yes.
        let aboutNew = askedAboutNew || (state.enabled && !state.new.isEmpty)
        return VStack(alignment: .leading, spacing: 7) {
            Text(aboutNew ? "Wisp can do more by itself now" : Self.question)
                .font(.wisp(size: place == .setup ? 13 : 14.5, weight: .semibold))
                .foregroundStyle(Theme.textPrimary)
            if let answered, model.pending || model.problem == nil {
                Text(model.pending ? "Saving…"
                     : answered && state.enabled && state.new.isEmpty
                        ? "On. Wisp shows each thing it does as “Wisp did this”, with an Undo."
                     : aboutNew ? "Wisp won't do these." + (state.agreed.isEmpty ? "" : " It keeps doing what you agreed to before.")
                     : "Off. You can turn it on in Settings whenever you like.")
                    .font(.wisp(size: 12)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(aboutNew ? "Wisp needs your yes for these. Without asking first, it would" + (state.agreed.isEmpty ? ":" : " also:")
                              : "Without asking first, Wisp would:")
                    .font(.wisp(size: 12)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                ForEach(aboutNew && !state.new.isEmpty ? state.new : state.does, id: \.self) { line in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text("•").foregroundStyle(Theme.textMuted).accessibilityHidden(true)
                        Text(line).fixedSize(horizontal: false, vertical: true)
                    }
                    .font(.wisp(size: 12)).foregroundStyle(Theme.textPrimary)
                }
                Text(state.scope + " At most \(state.perDay) a day, and each one has an Undo. "
                     + state.never)
                    .font(.wisp(size: 11.5)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10) {
                    Button { askedAboutNew = aboutNew; answered = true; model.set(true, onlyNew: aboutNew && state.enabled) } label: {
                        Text(aboutNew ? "Yes, these too" : Self.yes).font(.wisp(size: 12.5, weight: .semibold)).foregroundStyle(Color.black)
                            .padding(.horizontal, 13).padding(.vertical, 5)
                            .background(Capsule().fill(Color.white.opacity(0.92))).contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Button { askedAboutNew = aboutNew; answered = false; model.putAside() } label: {
                        Text(Self.notNow).font(.wisp(size: 12, weight: .medium)).foregroundStyle(Theme.textSecondary)
                            .padding(.horizontal, 8).padding(.vertical, 5).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help("Wisp does none of this without your tap, and it won't ask again. The switch stays in Settings.")
                }
                .disabled(model.pending)
                .padding(.top, 2)
            }
            if let problem = model.problem {
                Text(problem).font(.wisp(size: 11)).foregroundStyle(Theme.bad).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(EdgeInsets(top: 11, leading: 13, bottom: 11, trailing: 11))
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.chipFill))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel(Self.question)
    }
}
