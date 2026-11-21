import Foundation

// Customize Wisp: who the person is picks the notch's wildcard tabs (the
// strip is always Ask, Brief and Day, then at most two of Meet, Study and
// Ideate) and, for someone who builds things, the Ideate variant.
//
// Pure logic and a UserDefaults-backed store, Foundation only, so
// tests/WildcardTabsChecks.swift compiles it with NotchTab.swift and
// IdeateKind.swift and no app. Nothing here reads anything from the Mac or the
// service; the persona is one local choice that drives only the tab strip and
// the Ideate defaults.

extension Notification.Name {
    /// Posted each time the wildcard tabs or the Ideate variant change, so the
    /// open strip and quick settings redraw at once.
    static let wispWildcardTabsChanged = Notification.Name("wisp.wildcardTabsChanged")
}

/// The seven top-level answers to "Who are you?".
enum HarborPersonaGroup: String, CaseIterable, Equatable {
    case student, builder, meetings, manager, creator, home, basics

    var title: String {
        switch self {
        case .student: return "I'm a student"
        case .builder: return "I build things"
        case .meetings: return "Meetings fill my day"
        case .manager: return "I manage a team"
        case .creator: return "I write or create"
        case .home: return "Home and life"
        case .basics: return "Just the basics"
        }
    }

    /// One plain line under the title.
    var subtitle: String {
        switch self {
        case .student: return "Classes, assignments and exams"
        case .builder: return "Apps, pages, scripts and fixing code"
        case .meetings: return "Calls and meetings I want notes from"
        case .manager: return "Follow-ups and action items for many people"
        case .creator: return "Posts, newsletters, sites and scripts"
        case .home: return "Family, bills and errands"
        case .basics: return "Ask, Brief and Day, nothing more"
        }
    }

    /// The answers inside this one. A group with one leaf has no second question.
    var leaves: [HarborPersona] { HarborPersona.allCases.filter { $0.group == self } }

    var hasSubtypes: Bool { leaves.count > 1 }
}

/// What the person said they are. The raw value is saved in UserDefaults, so
/// never rename one.
enum HarborPersona: String, CaseIterable, Equatable {
    case studentHighSchool = "student.highschool"
    case studentCollege = "student.college"
    case studentGrad = "student.grad"
    case builderUI = "builder.ui"
    case builderDebug = "builder.debug"
    case builderLearn = "builder.learn"
    case builderGeneral = "builder.general"
    case builderElse = "builder.else"
    case meetings
    case manager
    case creator
    case home
    case basics

    var group: HarborPersonaGroup {
        switch self {
        case .studentHighSchool, .studentCollege, .studentGrad: return .student
        case .builderUI, .builderDebug, .builderLearn, .builderGeneral, .builderElse: return .builder
        case .meetings: return .meetings
        case .manager: return .manager
        case .creator: return .creator
        case .home: return .home
        case .basics: return .basics
        }
    }

    /// The subtype's name, or the group's when it has none.
    var title: String {
        switch self {
        case .studentHighSchool: return "High school"
        case .studentCollege: return "College"
        case .studentGrad: return "Grad school or research"
        case .builderUI: return "UI developer"
        case .builderDebug: return "Debugging"
        case .builderLearn: return "CS student"
        case .builderGeneral: return "Turning ideas into projects"
        case .builderElse: return "Something else"
        default: return group.title
        }
    }

    /// The wildcard tabs suggested for this answer: at most two, from Meet,
    /// Study and Ideate, in the order they sit in the strip.
    var suggestedTabs: [NotchTab] {
        let tabs: [NotchTab]
        switch self {
        case .studentHighSchool, .studentCollege: tabs = [.study]
        case .studentGrad: tabs = [.study, .meet]
        case .builderUI, .builderDebug, .builderGeneral, .builderElse: tabs = [.ideate]
        case .builderLearn: tabs = [.ideate, .study]
        case .meetings, .manager: tabs = [.meet]
        case .creator: tabs = [.ideate]
        case .home, .basics: tabs = []
        }
        return WildcardTabs.validated(tabs)
    }

    /// The Ideate variant this answer starts with; nil when Ideate isn't suggested.
    var ideateKind: IdeateKind? {
        switch self {
        case .builderUI: return .ui
        case .builderDebug: return .debug
        case .builderLearn: return .learn
        case .builderGeneral: return .general
        case .builderElse: return .script
        case .creator: return .general
        default: return nil
        }
    }
}

/// The wildcard tabs: which are chosen, and the rules for choosing.
enum WildcardTabs {
    static let key = "wisp.wildcardTabs"
    static let maxCount = 2
    /// The tabs that can be wildcards, in the order the Change row lists them.
    static let candidates: [NotchTab] = [.study, .meet, .ideate]
    /// What a Mac that never chose has had all along (Meet, then Study).
    static let defaultTabs: [NotchTab] = [.meet, .study]

    /// Ids saved in UserDefaults; never the enum's raw numbers.
    static func id(_ tab: NotchTab) -> String? {
        switch tab {
        case .meet: return "meet"
        case .study: return "study"
        case .ideate: return "ideate"
        default: return nil
        }
    }

    static func tab(id: String) -> NotchTab? { candidates.first { Self.id($0) == id } }

    /// Only wildcard tabs, each once, the first two, in the order given.
    static func validated(_ tabs: [NotchTab]) -> [NotchTab] {
        var out: [NotchTab] = []
        for tab in tabs where candidates.contains(tab) && !out.contains(tab) {
            out.append(tab)
            if out.count == maxCount { break }
        }
        return out
    }

    /// What Change does with one tab: a chosen tab leaves, a free tab joins at
    /// the end while there is room, and a third is refused (untick one first).
    static func toggled(_ tab: NotchTab, in current: [NotchTab]) -> [NotchTab] {
        let now = validated(current)
        guard candidates.contains(tab) else { return now }
        if now.contains(tab) { return now.filter { $0 != tab } }
        return canAdd(tab, to: now) ? now + [tab] : now
    }

    static func canAdd(_ tab: NotchTab, to current: [NotchTab]) -> Bool {
        let now = validated(current)
        return candidates.contains(tab) && !now.contains(tab) && now.count < maxCount
    }

    /// The saved wildcard tabs; Meet and Study when nothing was ever saved, so an
    /// existing install keeps its strip. An empty saved list is a real choice
    /// (none), not "unset".
    static func chosen(defaults: UserDefaults = .standard) -> [NotchTab] {
        guard let ids = defaults.object(forKey: key) as? [String] else { return defaultTabs }
        return validated(ids.compactMap(tab(id:)))
    }

    /// Whether the person ever chose (the customize step or quick settings).
    static func wasChosen(defaults: UserDefaults = .standard) -> Bool {
        defaults.object(forKey: key) is [String]
    }

    /// Saves the choice (validated) and tells the strip.
    @discardableResult
    static func set(_ tabs: [NotchTab], defaults: UserDefaults = .standard) -> [NotchTab] {
        let kept = validated(tabs)
        defaults.set(kept.compactMap(id), forKey: key)
        NotificationCenter.default.post(name: .wispWildcardTabsChanged, object: nil)
        return kept
    }

    /// One line per tab for the tour's picture of the open notch.
    static func tourLine(_ tab: NotchTab) -> String {
        switch tab {
        case .ask, .research: return "Ask anything. With your permission it reads your mail, calendar, reminders and notes."
        case .brief: return "What needs you today, prepared before you ask."
        case .day: return "Your day hour by hour, with to-dos placed on the timeline."
        case .meet: return "Notes from a meeting as you listen: a summary, decisions and action items."
        case .study: return "School work in one place, with study guides and quizzes made from it."
        case .ideate: return "Describe a small project and Wisp builds it in its own folder, asking before anything risky."
        }
    }

    /// What each wildcard tab is, for the Change list.
    static func summary(_ tab: NotchTab) -> String {
        switch tab {
        case .study: return "School work, guides and quizzes"
        case .meet: return "Meeting notes and action items"
        case .ideate: return "Build a small project from an idea"
        default: return ""
        }
    }

    /// Said where Study is chosen, because it is the one wildcard whose material
    /// is written off this Mac (docs/STUDY_TAB.md): a cloud model reached with
    /// the person's own key, kept in the Keychain and used for Study only.
    static let studyDisclosure = "Study writes its material with a cloud model, reached with your own OpenRouter or Gemini key. The key stays in your Keychain and is used for Study only."

    /// Meet asks for nothing here: the microphone belongs to the moment a meeting starts.
    static let meetNote = "Meet asks for nothing now. It listens only when you start it, and the microphone has to be allowed first."

    static let ideateNote = "Ideate builds in its own folder and asks before anything risky."

    // MARK: The words and the choice, shared by quick settings and the You step

    /// The row's title, in quick settings and in the You step.
    static let rowTitle = "Extra tabs"

    /// What the row does and its limit.
    static let rowSubtitle = "Add up to 2 tabs next to Ask, Brief and Day"

    /// Which are on: "Study and Meet are on", "Ideate is on", "None added yet".
    static func stateLine(_ current: [NotchTab]) -> String {
        let names = validated(current).map(\.title)
        switch names.count {
        case 0: return "None added yet"
        case 1: return "\(names[0]) is on"
        default: return "\(names[0]) and \(names[1]) are on"
        }
    }

    /// "0 of 2", "1 of 2", "2 of 2".
    static func countText(_ current: [NotchTab]) -> String {
        "\(validated(current).count) of \(maxCount)"
    }

    /// Shown (and as a tooltip) when a third tab is pressed.
    static func blockedNote(for tab: NotchTab) -> String { "Remove one to add \(tab.title)" }

    /// What a chip says to VoiceOver: "on" or "off".
    static func chipValue(_ tab: NotchTab, in current: [NotchTab]) -> String {
        validated(current).contains(tab) ? "on" : "off"
    }

    /// What pressing a chip does, for VoiceOver's hint.
    static func chipHint(_ tab: NotchTab, in current: [NotchTab]) -> String {
        let now = validated(current)
        if now.contains(tab) { return "Removes \(tab.title) from the tab strip" }
        return canAdd(tab, to: now) ? "Adds \(tab.title) to the tab strip" : blockedNote(for: tab)
    }

    /// The result of pressing one chip: the new list, and the note to show when
    /// the press was refused (a third tab). Never a silent no-op.
    struct Press: Equatable {
        let tabs: [NotchTab]
        let blockedNote: String?
    }

    static func press(_ tab: NotchTab, in current: [NotchTab]) -> Press {
        let now = validated(current)
        guard candidates.contains(tab) else { return Press(tabs: now, blockedNote: nil) }
        if !now.contains(tab) && !canAdd(tab, to: now) { return Press(tabs: now, blockedNote: blockedNote(for: tab)) }
        return Press(tabs: toggled(tab, in: now), blockedNote: nil)
    }

    static func ideateIsOn(defaults: UserDefaults = .standard) -> Bool {
        chosen(defaults: defaults).contains(.ideate)
    }
}

/// The saved persona and Ideate variant. `wisp.ideate.kind` is the key the
/// Ideate model reads, so the variant set here is the one a new project uses.
enum HarborPersonaStore {
    static let personaKey = "wisp.persona"
    static let ideateKindKey = "wisp.ideate.kind"

    static func persona(defaults: UserDefaults = .standard) -> HarborPersona? {
        defaults.string(forKey: personaKey).flatMap(HarborPersona.init(rawValue:))
    }

    static func ideateKind(defaults: UserDefaults = .standard) -> IdeateKind {
        defaults.string(forKey: ideateKindKey).flatMap(IdeateKind.init(rawValue:)) ?? IdeateKind.defaultKind
    }

    static func setIdeateKind(_ kind: IdeateKind, defaults: UserDefaults = .standard) {
        defaults.set(kind.rawValue, forKey: ideateKindKey)
        NotificationCenter.default.post(name: .wispWildcardTabsChanged, object: nil)
    }

    /// Saves the persona and applies what it suggests: the wildcard tabs, and
    /// the Ideate variant when it has one.
    static func choose(_ persona: HarborPersona, defaults: UserDefaults = .standard) {
        defaults.set(persona.rawValue, forKey: personaKey)
        if let kind = persona.ideateKind { defaults.set(kind.rawValue, forKey: ideateKindKey) }
        WildcardTabs.set(persona.suggestedTabs, defaults: defaults)
    }
}
