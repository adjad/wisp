import SwiftUI

// Setup's "You" step: who the person is, so the notch shows the tabs that suit
// them. One local choice that drives only the tab strip and the Ideate variant
// (WildcardTabs.swift). Skipping keeps what the strip already is (Meet and
// Study). It asks for no permission, and Meet's microphone is left for the
// moment a meeting starts.

struct OnboardingCustomizeStep: View {
    @State private var group: HarborPersonaGroup?
    @State private var persona: HarborPersona?
    @State private var tabs: [NotchTab]
    @State private var kind: IdeateKind
    @State private var changing = false

    init() {
        let saved = HarborPersonaStore.persona()
        _persona = State(initialValue: saved)
        _group = State(initialValue: saved?.group)
        _tabs = State(initialValue: WildcardTabs.chosen())
        _kind = State(initialValue: HarborPersonaStore.ideateKind())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            OnboardingHeading(title: "Who are you?",
                              subtitle: "Wisp adds one or two tabs to the notch to suit you. It's one choice kept on this Mac, "
                                  + "and you can change it any time in Settings. Skip it and nothing changes.")
            ForEach(HarborPersonaGroup.allCases, id: \.self) { item in
                groupCard(item)
                if group == item, item.hasSubtypes { subtypes(item) }
            }
            if persona != nil || WildcardTabs.wasChosen() { tabsCard.padding(.top, 8) }
        }
    }

    private func icon(_ group: HarborPersonaGroup) -> String {
        switch group {
        case .student: return "graduationcap"
        case .builder: return "hammer"
        case .meetings: return "person.2"
        case .manager: return "checklist"
        case .creator: return "pencil.and.outline"
        case .home: return "house"
        case .basics: return "square.grid.2x2"
        }
    }

    private func groupCard(_ item: HarborPersonaGroup) -> some View {
        let chosen = group == item
        return Button {
            group = item
            if item.hasSubtypes {
                // The second question decides; a tab is suggested once it is answered.
                if persona?.group != item { persona = nil }
            } else if let leaf = item.leaves.first {
                apply(leaf)
            }
        } label: {
            OnboardingCard(icon: icon(item), title: item.title, detail: item.subtitle) {
                Image(systemName: chosen ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(chosen ? OnboardingPalette.lilac : Theme.textMuted)
                    .accessibilityHidden(true)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
    }

    private func subtypes(_ item: HarborPersonaGroup) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(item.leaves, id: \.self) { leaf in
                let chosen = persona == leaf
                Button { apply(leaf) } label: {
                    HStack(spacing: 8) {
                        Image(systemName: chosen ? "largecircle.fill.circle" : "circle")
                            .font(.system(size: 12))
                            .foregroundStyle(chosen ? OnboardingPalette.lilac : Theme.textMuted)
                            .accessibilityHidden(true)
                        Text(leaf.title).font(.system(size: 12.5))
                        Spacer(minLength: 0)
                    }
                    .padding(.horizontal, 12).padding(.vertical, 7)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.white.opacity(chosen ? 0.07 : 0.03)))
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(chosen ? Theme.chipStroke : Theme.hairline))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
            }
        }
        .padding(.leading, 44).padding(.bottom, 9)
    }

    // MARK: Tabs

    private var tabsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Your tabs").font(.system(size: 13, weight: .medium))
                Spacer()
                Button(changing ? "Done" : "Change") { changing.toggle() }
                    .buttonStyle(FirstRunButtonStyle())
            }
            OnboardingTabStripPreview(tabs: NotchTab.strip(wildcards: tabs))
            if tabs.isEmpty {
                Text("Just Ask, Brief and Day. Meet, Study and Ideate are one tap away under Change.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if changing { changeList }
            if tabs.contains(.study) { note("cloud", WildcardTabs.studyDisclosure) }
            if tabs.contains(.meet) { note("mic.slash", WildcardTabs.meetNote) }
            if tabs.contains(.ideate) { note("folder", WildcardTabs.ideateNote) }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.hairline))
    }

    private var changeList: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(WildcardTabs.candidates, id: \.self) { tab in
                let on = tabs.contains(tab)
                let blocked = !on && !WildcardTabs.canAdd(tab, to: tabs)
                Button { setTabs(WildcardTabs.toggled(tab, in: tabs)) } label: {
                    HStack(spacing: 8) {
                        Image(systemName: on ? "checkmark.square.fill" : "square")
                            .font(.system(size: 13))
                            .foregroundStyle(on ? OnboardingPalette.lilac : Theme.textMuted)
                            .accessibilityHidden(true)
                        Text(tab.title).font(.system(size: 12.5, weight: .medium))
                        Text(WildcardTabs.summary(tab)).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        Spacer(minLength: 0)
                    }
                    .padding(.vertical, 3)
                    .opacity(blocked ? 0.45 : 1)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(blocked)
                .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
            }
            if tabs.count >= WildcardTabs.maxCount {
                Text(WildcardTabs.limitNote).font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
            }
            if tabs.contains(.ideate) { ideateVariants }
        }
        .padding(.top, 2)
    }

    private var ideateVariants: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Ideate is set up for").font(.system(size: 12, weight: .medium)).padding(.top, 6)
            HStack(spacing: 6) {
                ForEach(IdeateKind.allCases, id: \.self) { option in
                    Button(option.title) {
                        kind = option
                        HarborPersonaStore.setIdeateKind(option)
                    }
                    .buttonStyle(FirstRunButtonStyle(primary: kind == option))
                    .accessibilityAddTraits(kind == option ? [.isButton, .isSelected] : .isButton)
                }
            }
        }
    }

    private func note(_ symbol: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: symbol).font(.system(size: 11)).foregroundStyle(Theme.textMuted).accessibilityHidden(true)
            Text(text).font(.system(size: 11.5)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func apply(_ leaf: HarborPersona) {
        persona = leaf
        HarborPersonaStore.choose(leaf)
        tabs = WildcardTabs.chosen()
        if let suggested = leaf.ideateKind { kind = suggested }
    }

    private func setTabs(_ next: [NotchTab]) {
        tabs = WildcardTabs.set(next)
    }
}

/// The notch's tab strip, drawn small: Ask · Brief · Day and the chosen tabs.
/// Ask is shown selected, as the notch opens on it.
struct OnboardingTabStripPreview: View {
    let tabs: [NotchTab]

    var body: some View {
        HStack(spacing: 2) {
            ForEach(Array(tabs.enumerated()), id: \.offset) { index, tab in
                Text(tab.title).font(.system(size: 11.5, weight: index == 0 ? .semibold : .regular))
                    .foregroundStyle(index == 0 ? Color.black : Theme.textSecondary)
                    .padding(.horizontal, 10).padding(.vertical, 3)
                    .background(Capsule().fill(index == 0 ? Color.white : .clear))
            }
        }
        .padding(2)
        .background(Capsule().fill(Color.black))
        .overlay(Capsule().stroke(Theme.chipStroke))
        .fixedSize()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Tabs: " + tabs.map(\.title).joined(separator: ", "))
    }
}
