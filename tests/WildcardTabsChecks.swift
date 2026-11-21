// Contract for Customize Wisp (app/Sources/WispApp/WildcardTabs.swift): the
// persona to tabs mapping, the two-tab limit, the validator and the saved
// choice. Compiled with NotchTabChecks.swift, which calls wildcardTabsChecks.
// Pure logic on a throwaway defaults suite: never touches the person's settings.
import Foundation

func wildcardTabsChecks(_ check: (Bool, String) -> Void) {
    let suite = "wisp.checks.wildcard.\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer { defaults.removePersistentDomain(forName: suite) }

    // Seven top-level answers; every leaf belongs to one, none is lost.
    check(HarborPersonaGroup.allCases.count == 7, "seven top-level answers")
    check(HarborPersona.allCases.allSatisfy { $0.group.leaves.contains($0) }, "every answer sits in its group")
    check(Set(HarborPersona.allCases.map(\.rawValue)).count == HarborPersona.allCases.count, "persona ids are unique")
    check(HarborPersonaGroup.student.hasSubtypes && HarborPersonaGroup.builder.hasSubtypes
          && !HarborPersonaGroup.meetings.hasSubtypes && !HarborPersonaGroup.basics.hasSubtypes,
          "only Student and I build things ask a second question")
    check(HarborPersonaGroup.allCases.allSatisfy { !$0.title.isEmpty && !$0.subtitle.isEmpty }, "every group has words")
    check(HarborPersona.allCases.allSatisfy { !$0.title.isEmpty }, "every answer has a title")

    // Persona to tabs: at most two, only Meet, Study or Ideate, no repeats.
    let expected: [(HarborPersona, [NotchTab], IdeateKind?)] = [
        (.studentHighSchool, [.study], nil),
        (.studentCollege, [.study], nil),
        (.studentGrad, [.study, .meet], nil),
        (.builderUI, [.ideate], .ui),
        (.builderDebug, [.ideate], .debug),
        (.builderLearn, [.ideate, .study], .learn),
        (.builderGeneral, [.ideate], .general),
        (.builderElse, [.ideate], .script),
        (.meetings, [.meet], nil),
        (.manager, [.meet], nil),
        (.creator, [.ideate], .general),
        (.home, [], nil),
        (.basics, [], nil),
    ]
    check(expected.count == HarborPersona.allCases.count, "the mapping covers every persona")
    for (persona, tabs, kind) in expected {
        check(persona.suggestedTabs == tabs, "\(persona) suggests \(tabs)")
        check(persona.ideateKind == kind, "\(persona) starts Ideate as \(String(describing: kind))")
        check(persona.suggestedTabs.count <= 2, "\(persona) suggests two tabs at most")
        check(persona.suggestedTabs.allSatisfy(WildcardTabs.candidates.contains), "\(persona) suggests only wildcard tabs")
        check((persona.ideateKind != nil) == persona.suggestedTabs.contains(.ideate), "\(persona): a variant exactly when Ideate is suggested")
    }

    // The validator: wildcard tabs only, once each, two at most, in order.
    check(WildcardTabs.validated([]) == [], "none is allowed")
    check(WildcardTabs.validated([.study, .study, .meet, .ideate]) == [.study, .meet], "dedupe, then two, in order")
    check(WildcardTabs.validated([.ask, .brief, .day, .research, .ideate]) == [.ideate], "Ask, Brief, Day and Research never join")
    check(WildcardTabs.validated([.ideate, .meet, .study]) == [.ideate, .meet], "the first two win")

    // Change: untick to free a slot; a third is refused.
    check(WildcardTabs.toggled(.ideate, in: []) == [.ideate], "a free tab joins")
    check(WildcardTabs.toggled(.study, in: [.ideate]) == [.ideate, .study], "a second joins at the end")
    check(WildcardTabs.toggled(.meet, in: [.ideate, .study]) == [.ideate, .study], "a third is refused")
    check(!WildcardTabs.canAdd(.meet, to: [.ideate, .study]) && WildcardTabs.canAdd(.meet, to: [.ideate]), "canAdd follows the limit")
    check(WildcardTabs.toggled(.ideate, in: [.ideate, .study]) == [.study], "a chosen tab leaves")
    check(WildcardTabs.toggled(.ask, in: [.study]) == [.study], "a fixed tab can't be toggled")

    // The words: title, subtitle, state line and count for 0, 1 and 2 picked.
    check(WildcardTabs.rowTitle == "Extra tabs", "the row is called Extra tabs")
    check(WildcardTabs.rowSubtitle == "Add up to 2 tabs next to Ask, Brief and Day", "the subtitle says what it does and the limit")
    check(WildcardTabs.rowSubtitle.contains("\(WildcardTabs.maxCount)"), "the subtitle's number is the limit")
    check(WildcardTabs.stateLine([]) == "None added yet", "state line: none")
    check(WildcardTabs.stateLine([.ideate]) == "Ideate is on", "state line: one")
    check(WildcardTabs.stateLine([.study, .meet]) == "Study and Meet are on", "state line: two, in order")
    check(WildcardTabs.stateLine([.meet, .ideate]) == "Meet and Ideate are on", "state line follows the saved order")
    check(WildcardTabs.stateLine([.study, .meet, .ideate]) == "Study and Meet are on", "state line never names a third")
    check(WildcardTabs.countText([]) == "0 of 2" && WildcardTabs.countText([.meet]) == "1 of 2"
          && WildcardTabs.countText([.meet, .study]) == "2 of 2" && WildcardTabs.countText([.meet, .study, .ideate]) == "2 of 2",
          "count: 0 of 2, 1 of 2, 2 of 2")
    check(WildcardTabs.blockedNote(for: .ideate) == "Remove one to add Ideate"
          && WildcardTabs.blockedNote(for: .study) == "Remove one to add Study", "blocked note names the tab")
    check(WildcardTabs.chipValue(.meet, in: [.meet]) == "on" && WildcardTabs.chipValue(.study, in: [.meet]) == "off", "chips say on or off")
    check(WildcardTabs.chipHint(.meet, in: [.meet]) == "Removes Meet from the tab strip"
          && WildcardTabs.chipHint(.study, in: [.meet]) == "Adds Study to the tab strip"
          && WildcardTabs.chipHint(.ideate, in: [.meet, .study]) == "Remove one to add Ideate", "chip hints say what a press does")

    // Pressing: the third is refused with the blocked note, never silently.
    check(WildcardTabs.press(.study, in: []) == WildcardTabs.Press(tabs: [.study], blockedNote: nil), "press adds when there is room")
    check(WildcardTabs.press(.meet, in: [.study]) == WildcardTabs.Press(tabs: [.study, .meet], blockedNote: nil), "the second joins at the end")
    check(WildcardTabs.press(.ideate, in: [.study, .meet]) == WildcardTabs.Press(tabs: [.study, .meet], blockedNote: "Remove one to add Ideate"),
          "a third is refused and reports the blocked note")
    let afterRemove = WildcardTabs.press(.study, in: [.study, .meet])
    check(afterRemove == WildcardTabs.Press(tabs: [.meet], blockedNote: nil), "pressing a picked tab removes it")
    let afterAdd = WildcardTabs.press(.ideate, in: afterRemove.tabs)
    check(afterAdd == WildcardTabs.Press(tabs: [.meet, .ideate], blockedNote: nil), "after removing one, adding works and order is kept")
    check(WildcardTabs.press(.ask, in: [.study]) == WildcardTabs.Press(tabs: [.study], blockedNote: nil), "a fixed tab is ignored without a note")
    check(WildcardTabs.stateLine(afterAdd.tabs) == "Meet and Ideate are on", "the state line follows the press")

    // The saved choice: unset keeps Meet and Study; an empty list is a real choice.
    check(WildcardTabs.chosen(defaults: defaults) == [.meet, .study] && !WildcardTabs.wasChosen(defaults: defaults),
          "never chosen: Meet and Study, so nothing changes for an existing install")
    check(WildcardTabs.set([.ideate, .study, .meet], defaults: defaults) == [.ideate, .study]
          && WildcardTabs.chosen(defaults: defaults) == [.ideate, .study] && WildcardTabs.wasChosen(defaults: defaults),
          "a choice is validated, saved and read back in order")
    check(defaults.stringArray(forKey: WildcardTabs.key) == ["ideate", "study"], "saved as ids, not numbers")
    WildcardTabs.set([], defaults: defaults)
    check(WildcardTabs.chosen(defaults: defaults) == [] && WildcardTabs.wasChosen(defaults: defaults), "none chosen stays none")
    defaults.set(["study", "nonsense", "study", "ask"], forKey: WildcardTabs.key)
    check(WildcardTabs.chosen(defaults: defaults) == [.study], "junk ids and repeats are dropped")
    defaults.set("study", forKey: WildcardTabs.key)
    check(WildcardTabs.chosen(defaults: defaults) == [.meet, .study], "a value of the wrong type counts as never chosen")
    check(WildcardTabs.ideateIsOn(defaults: defaults) == false, "Ideate is off when not chosen")
    check(WildcardTabs.key == "wisp.wildcardTabs" && HarborPersonaStore.ideateKindKey == "wisp.ideate.kind",
          "the saved keys are the ones the Ideate model and the plan name")

    // The persona store applies tabs and the Ideate variant together.
    defaults.removePersistentDomain(forName: suite)
    check(HarborPersonaStore.persona(defaults: defaults) == nil && HarborPersonaStore.ideateKind(defaults: defaults) == .general,
          "no persona and the general variant until chosen")
    HarborPersonaStore.choose(.builderDebug, defaults: defaults)
    check(HarborPersonaStore.persona(defaults: defaults) == .builderDebug
          && HarborPersonaStore.ideateKind(defaults: defaults) == .debug
          && WildcardTabs.chosen(defaults: defaults) == [.ideate], "a builder gets Ideate and its variant")
    HarborPersonaStore.setIdeateKind(.script, defaults: defaults)
    check(HarborPersonaStore.ideateKind(defaults: defaults) == .script, "the variant can change later")
    HarborPersonaStore.choose(.studentGrad, defaults: defaults)
    check(WildcardTabs.chosen(defaults: defaults) == [.study, .meet]
          && HarborPersonaStore.ideateKind(defaults: defaults) == .script,
          "a persona without Ideate leaves the variant as it was")
    HarborPersonaStore.choose(.basics, defaults: defaults)
    check(WildcardTabs.chosen(defaults: defaults).isEmpty, "Just the basics: Ask, Brief and Day")
    defaults.set("bogus", forKey: HarborPersonaStore.personaKey)
    defaults.set("bogus", forKey: HarborPersonaStore.ideateKindKey)
    check(HarborPersonaStore.persona(defaults: defaults) == nil && HarborPersonaStore.ideateKind(defaults: defaults) == .general,
          "unknown saved ids fall back")

    // Words the step and the tour use.
    check(NotchTab.allCases.allSatisfy { !WildcardTabs.tourLine($0).isEmpty }, "every tab has a tour line")
    check(WildcardTabs.candidates.allSatisfy { !WildcardTabs.summary($0).isEmpty }, "every wildcard has a summary")
    check(WildcardTabs.studyDisclosure.contains("cloud") && WildcardTabs.studyDisclosure.contains("own")
          && WildcardTabs.studyDisclosure.contains("Study only"), "Study says its material is written in the cloud, with the person's key")
    check(WildcardTabs.meetNote.contains("asks for nothing"), "Meet asks for nothing in setup")

    // Live changes post a notification the strip listens for.
    var posted = 0
    let observer = NotificationCenter.default.addObserver(forName: .wispWildcardTabsChanged, object: nil, queue: nil) { _ in posted += 1 }
    WildcardTabs.set([.meet], defaults: defaults)
    HarborPersonaStore.setIdeateKind(.ui, defaults: defaults)
    NotificationCenter.default.removeObserver(observer)
    check(posted == 2, "changing the tabs or the variant tells the strip")
}
