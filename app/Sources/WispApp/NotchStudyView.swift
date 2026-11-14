import SwiftUI
import AppKit

/// The notch's Study tab: your courses and grades, what is due, and study
/// material (guides, quizzes, lessons, flash cards) made from your real
/// coursework. School pages are only ever read, never changed; material is
/// written by the Study connector, a cloud model with its own key.
///
/// Everything stays inline: the panel is non-activating, so there are no
/// sheets, alerts or pop-up menus. An opened material takes the whole tab.
struct NotchStudyView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var study: StudyModel

    /// The course chip picked: filters the due list and the library, and is what
    /// "Make something" writes for.
    @State private var courseID: String?
    /// The one due row showing its make buttons.
    @State private var expanded: String?
    @State private var showAllDue = false
    @State private var topic = ""
    @FocusState private var topicFocused: Bool
    @State private var confirmingClear = false
    /// The library row whose delete is waiting for a second click.
    @State private var confirmingDelete: String?
    @State private var now = Date()

    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride
    private var reduceMotion: Bool { reduceMotionOverride ?? systemReduceMotion }

    /// Due rows before "Show all".
    private static let dueRows = 6

    init(model: OverlayModel, study: StudyModel) {
        self.model = model
        self.study = study
    }

    /// The pieces whose coming and going changes the tab's height. Only these
    /// drive the animation and the panel resize, never the data itself.
    private struct Showing: Equatable {
        var loaded, hasData, sample: Bool
        var opened: String?
        var expanded: String?
        var course: String?
        var busy: StudyBusy?
        var notice: Bool
        var connector: Bool
        var showAll: Bool
        var clearing: Bool
        var deleting: String?
        var assignments: Int
        var materials: Int
    }

    private var showing: Showing {
        let state = study.state
        return Showing(loaded: state != nil, hasData: state?.hasData == true, sample: state?.sample == true,
                       opened: study.opened?.id, expanded: expanded, course: courseID, busy: study.busy,
                       notice: study.notice != nil, connector: study.showingConnector,
                       showAll: showAllDue, clearing: confirmingClear, deleting: confirmingDelete,
                       assignments: state?.assignments.count ?? 0, materials: state?.materials.count ?? 0)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let notice = study.notice { noticeRow(notice).transition(.opacity) }
            if let material = study.opened {
                StudyMaterialPage(study: study, material: material).transition(.opacity)
            } else {
                home.transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.bottom, 8)
        .animation(Theme.Motion.panel(reduceMotion: reduceMotion), value: showing)
        .onChange(of: showing) { _, _ in
            now = Date()
            model.onResize()
        }
        .onChange(of: topicFocused) { _, focused in study.editing = focused }
        .onChange(of: study.state?.courses.map(\.id) ?? []) { _, ids in
            // A course that left (a clear, a fresh read) is no longer a filter.
            if let courseID, !ids.contains(courseID) { self.courseID = nil }
        }
        .onDisappear { study.editing = false }
        .task {
            now = Date()
            await study.refresh()
            model.onResize()
        }
    }

    // MARK: Home

    @ViewBuilder private var home: some View {
        if let state = study.state, state.hasData {
            content(state)
        } else if study.state == nil && study.busy == .loading {
            ProcessingRow(label: StudyText.busy(.loading))
        } else {
            intro
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(StudyText.emptyTitle).font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .accessibilityAddTraits(.isHeader)
            Text(Copy.intro)
                .font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(StudyText.emptyBody)
                .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            if !hasAccessibility { accessibilityHint }
            if let busy = study.busy {
                StudyBusyRow(label: busy == .loading ? Copy.loadingSample : StudyText.busy(busy))
            } else {
                HStack(spacing: 8) {
                    StudyChrome.pill(StudyText.readButton, icon: "text.viewfinder", primary: true) { read() }
                    StudyChrome.pill(Copy.sampleButton, icon: "sparkles") {
                        Task { await study.loadSample() }
                    }
                }
            }
            Text(StudyText.readOnlyNote)
                .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            footer(nil)
        }
    }

    private var hasAccessibility: Bool {
        #if WISP_NOTCH_QA
        return true
        #else
        return StudyPageReader.hasAccessibility
        #endif
    }

    private var accessibilityHint: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "hand.raised").font(.wisp(size: 10.5)).foregroundStyle(Theme.warn)
                .accessibilityHidden(true)
            Text(StudyText.needsAccessibility)
                .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 6)
            NotchChrome.small("Open Settings") {
                if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
                    NSWorkspace.shared.open(url)
                }
            }
        }
    }

    // MARK: With data

    private func content(_ state: StudyState) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                headerRow(state)
                if state.sample {
                    Text(StudyText.sampleNote).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let busy = study.busy, busy != .loading {
                StudyBusyRow(label: StudyText.busy(busy)).transition(.opacity)
            }
            if !state.courses.isEmpty { coursesRow(state) }
            dueSection(state)
            makeSection(state)
            librarySection(state)
            footer(state)
        }
    }

    private func headerRow(_ state: StudyState) -> some View {
        HStack(alignment: .center, spacing: 8) {
            Image(systemName: "graduationcap").font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                .accessibilityHidden(true)
            Text(StudyText.lastRead(state.lastImport, now: now))
                .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                .lineLimit(1)
            if state.sample { sampleChip }
            Spacer(minLength: 8)
            NotchChrome.chip(state.sample ? StudyText.readButton : Copy.readAgain, icon: "arrow.clockwise",
                             help: StudyText.readOnlyNote) { read() }
                .disabled(study.busy != nil)
        }
    }

    private var sampleChip: some View {
        Text(StudyText.sampleBadge.uppercased())
            .font(.wisp(size: 9, weight: .medium, design: .monospaced)).tracking(0.6)
            .foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .fixedSize()
    }

    // MARK: Courses

    private func coursesRow(_ state: StudyState) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            StudyChrome.label(StudyText.coursesHeader)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    StudyCourseChip(title: Copy.allCourses, grade: nil, chosen: courseID == nil,
                                    help: Copy.allCoursesHelp) { pickCourse(nil) }
                    ForEach(state.courses) { course in
                        StudyCourseChip(title: course.shortName, grade: StudyText.grade(course),
                                        chosen: courseID == course.id, help: course.name) {
                            pickCourse(course.id)
                        }
                    }
                }
                .padding(.vertical, 1)
            }
        }
    }

    private func pickCourse(_ id: String?) {
        courseID = courseID == id ? nil : id
        expanded = nil
        showAllDue = false
    }

    private func selectedCourse(_ state: StudyState) -> StudyCourse? { state.course(courseID) }

    // MARK: Due

    private func dueGroups(_ state: StudyState) -> [StudyDueGroup] {
        let items: [StudyAssignment]
        if let course = selectedCourse(state) {
            items = state.assignments(in: course)
        } else {
            items = state.assignments
        }
        return StudyDue.groups(items, now: now, calendar: .current)
    }

    /// The groups cut to the first rows unless "Show all" is on.
    private func shownGroups(_ groups: [StudyDueGroup]) -> [StudyDueGroup] {
        if showAllDue { return groups }
        var left = Self.dueRows
        var shown: [StudyDueGroup] = []
        for group in groups where left > 0 {
            let items = Array(group.items.prefix(left))
            left -= items.count
            shown.append(StudyDueGroup(id: group.id, title: group.title, items: items))
        }
        return shown
    }

    private func dueSection(_ state: StudyState) -> some View {
        let groups = dueGroups(state)
        let total = groups.reduce(0) { $0 + $1.items.count }
        return VStack(alignment: .leading, spacing: 8) {
            StudyChrome.label(StudyText.dueHeader)
            if groups.isEmpty {
                Text(StudyText.nothingDue).font(.wisp(.body)).foregroundStyle(Theme.textMuted)
            } else {
                ForEach(shownGroups(groups)) { group in
                    dueGroup(group, state: state)
                }
                if total > Self.dueRows {
                    Button(showAllDue ? Copy.showFewer : "Show all (\(total))") {
                        showAllDue.toggle()
                    }
                    .buttonStyle(.plain)
                    .font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .frame(minHeight: 24)
                }
            }
        }
    }

    private func dueGroup(_ group: StudyDueGroup, state: StudyState) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(group.title)
                .font(.wisp(.caption, weight: .semibold))
                .foregroundStyle(group.id == "missing" ? Theme.warn : Theme.textSecondary)
                .padding(.bottom, 2)
                .accessibilityAddTraits(.isHeader)
            ForEach(group.items) { item in
                StudyDueRow(item: item, now: now, missing: group.id == "missing",
                            expanded: expanded == item.id, busy: study.busy != nil,
                            onToggle: { toggle(item) },
                            onMake: { kind in make(kind, assignment: item, state: state) })
            }
        }
    }

    private func toggle(_ item: StudyAssignment) {
        expanded = expanded == item.id ? nil : item.id
    }

    // MARK: Make something

    private func makeSection(_ state: StudyState) -> some View {
        let course = selectedCourse(state)
        let trimmed = topic.trimmingCharacters(in: .whitespacesAndNewlines)
        let ready = !trimmed.isEmpty || course != nil
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                StudyChrome.label(Copy.makeHeader)
                if let course {
                    Text("for " + course.shortName).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                }
            }
            TextField(Copy.topicPlaceholder, text: $topic)
                .textFieldStyle(.plain)
                .font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .stroke(topicFocused ? Color.white.opacity(0.35) : Theme.chipStroke, lineWidth: 1))
                .focused($topicFocused)
                .onSubmit { if ready { makeFromTopic(.guide, state: state) } }
                .accessibilityLabel(Copy.makeHeader)
            StudyKindButtons(disabled: study.busy != nil || !ready) { kind in
                makeFromTopic(kind, state: state)
            }
        }
    }

    // MARK: Library

    private func librarySection(_ state: StudyState) -> some View {
        let materials = libraryItems(state)
        return VStack(alignment: .leading, spacing: 6) {
            StudyChrome.label(StudyText.libraryHeader)
            if materials.isEmpty {
                Text(StudyText.noMaterials).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(materials) { item in
                        StudyLibraryRow(item: item, now: now, confirming: confirmingDelete == item.id,
                                        disabled: study.busy != nil,
                                        onOpen: { Task { await study.open(item.id) } },
                                        onAskDelete: { confirmingDelete = item.id },
                                        onDelete: {
                                            confirmingDelete = nil
                                            Task { await study.delete(item.id) }
                                        },
                                        onKeep: { confirmingDelete = nil })
                    }
                }
            }
        }
    }

    /// Newest first; just the picked course's when one is picked.
    private func libraryItems(_ state: StudyState) -> [StudyMaterialSummary] {
        let all = state.materials.sorted { $0.createdAt > $1.createdAt }
        guard let course = selectedCourse(state) else { return all }
        return all.filter { $0.courseID == course.id }
    }

    // MARK: Footer

    private func footer(_ state: StudyState?) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Rectangle().fill(Theme.hairline).frame(height: 1)
            HStack(alignment: .center, spacing: 8) {
                connectorButton
                Spacer(minLength: 8)
                if state?.hasData == true && !confirmingClear {
                    Button(StudyText.clearButton) { confirmingClear = true }
                        .buttonStyle(.plain)
                        .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                        .frame(minHeight: 24)
                        .disabled(study.busy != nil)
                }
            }
            if confirmingClear { clearConfirm.transition(.opacity) }
            if study.showingConnector {
                StudyConnectorCard(study: study).transition(.opacity)
            }
        }
    }

    private var connectorButton: some View {
        let connector = study.state?.connector
        let ready = connector?.ready == true
        return Button { study.showingConnector.toggle() } label: {
            HStack(spacing: 6) {
                Image(systemName: ready ? "key" : "key.slash")
                    .font(.wisp(size: 10)).foregroundStyle(ready ? Theme.textMuted : Theme.warn)
                Text(connectorLine(connector))
                    .font(.wisp(.caption)).foregroundStyle(ready ? Theme.textMuted : Theme.textSecondary)
                    .lineLimit(1)
                Image(systemName: study.showingConnector ? "chevron.up" : "chevron.down")
                    .font(.wisp(size: 8, weight: .semibold)).foregroundStyle(Theme.textMuted)
            }
            .frame(minHeight: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(study.showingConnector ? "Hide the Study connector" : "Choose the Study model and its key")
    }

    private func connectorLine(_ connector: StudyConnectorState?) -> String {
        guard let connector, connector.ready else { return Copy.addKey }
        if connector.usesCloudFallback { return StudyText.connectorTitle + ": this Mac's cloud connection" }
        let provider = connector.current
        let model = provider?.model(connector.modelID)?.label ?? connector.modelID
        return StudyText.connectorTitle + ": " + model + (provider.map { " · " + $0.label } ?? "")
    }

    private var clearConfirm: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(StudyText.clearConfirm)
                .font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button {
                    confirmingClear = false
                    courseID = nil
                    expanded = nil
                    Task { await study.clearAll() }
                } label: {
                    Text(Copy.clearYes).font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.bad)
                        .padding(.horizontal, 12).frame(minHeight: 24)
                        .background(Capsule().fill(Theme.bad.opacity(0.12)))
                        .overlay(Capsule().stroke(Theme.bad.opacity(0.5), lineWidth: 1))
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                StudyChrome.pill(Copy.clearNo) { confirmingClear = false }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline, lineWidth: 1))
    }

    // MARK: Notice

    private func noticeRow(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "info.circle").font(.wisp(size: 10.5)).foregroundStyle(Theme.textMuted)
                .accessibilityHidden(true)
            Text(text).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
            NotchChrome.iconButton("xmark", help: "Dismiss") { study.dismissNotice() }
        }
    }

    // MARK: Actions

    private func read() {
        expanded = nil
        Task { await study.readSchoolPages() }
    }

    /// Without a ready connector, the key card opens instead of a request that can only fail.
    private func needsConnector(_ state: StudyState) -> Bool {
        if state.connector.ready { return false }
        study.showingConnector = true
        return true
    }

    private func make(_ kind: StudyKind, assignment: StudyAssignment, state: StudyState) {
        guard !needsConnector(state) else { return }
        let course = state.course(assignment.courseID)
        expanded = nil
        Task { await study.make(kind, course: course, assignment: assignment, topic: nil) }
    }

    private func makeFromTopic(_ kind: StudyKind, state: StudyState) {
        guard !needsConnector(state) else { return }
        let trimmed = topic.trimmingCharacters(in: .whitespacesAndNewlines)
        let course = selectedCourse(state)
        guard !trimmed.isEmpty || course != nil else { return }
        topicFocused = false
        Task {
            await study.make(kind, course: course, assignment: nil, topic: trimmed.isEmpty ? nil : trimmed)
            if study.opened != nil { topic = "" }
        }
    }

    // MARK: Copy

    private enum Copy {
        static let intro = "Study reads the school pages open in your browser, keeps your courses and what's due on "
            + "this Mac, and turns your real coursework into guides, quizzes, lessons and flash cards."
        static let sampleButton = "Try with sample data"
        static let loadingSample = "Loading the sample…"
        static let readAgain = "Read again"
        static let allCourses = "All"
        static let allCoursesHelp = "Show every course"
        static let showFewer = "Show fewer"
        static let makeHeader = "Make something"
        static let topicPlaceholder = "Any topic, e.g. photosynthesis"
        static let addKey = "Add a key to make study material"
        static let clearYes = "Remove"
        static let clearNo = "Keep"
    }
}

// MARK: - Opened material

/// A material full-tab: a back bar, its title and kind, then its player or reader.
private struct StudyMaterialPage: View {
    @ObservedObject var study: StudyModel
    let material: StudyMaterial

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            topBar
            titleBlock
            materialBody.id(material.id)
        }
        .onExitCommand { study.close() }
    }

    private var topBar: some View {
        HStack(alignment: .center, spacing: 8) {
            Button { study.close() } label: {
                HStack(spacing: 4) {
                    Image(systemName: "chevron.left").font(.wisp(size: 10, weight: .semibold))
                    Text(StudyText.tabTitle).font(.wisp(.footnote, weight: .medium))
                }
                .foregroundStyle(Theme.textSecondary)
                .frame(minHeight: 24)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Back to Study (Esc)")
            .accessibilityLabel("Back to Study")
            Spacer(minLength: 8)
            HStack(spacing: 4) {
                Image(systemName: material.kind.symbol).font(.wisp(size: 9.5))
                Text(material.kind.title.uppercased())
                    .font(.wisp(size: 9.5, weight: .medium, design: .monospaced)).tracking(0.6)
            }
            .foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .fixedSize()
        }
    }

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(material.title)
                .font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
            Text(metaLine).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
        }
    }

    private var metaLine: String {
        var parts: [String] = [StudyText.count(kind: material.kind, material.count)]
        if let course = study.state?.course(material.courseID) { parts.append(course.shortName) }
        if let best = StudyText.best(material.bestScore) { parts.append(best) }
        if material.sample { parts.append(StudyText.sampleBadge) }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder private var materialBody: some View {
        switch material.body {
        case .quiz(let quiz):
            StudyQuizView(quiz: quiz, onFinish: record)
        case .flashcards(let deck):
            StudyFlashcardsView(deck: deck, onFinish: record)
        case .guide(let guide):
            StudyGuideView(guide: guide)
        case .lesson(let lesson):
            StudyLessonView(lesson: lesson, onFinish: record)
        }
    }

    private func record(_ correct: Int, _ total: Int) {
        guard total > 0 else { return }
        Task { await study.record(correct: correct, total: total) }
    }
}

// MARK: - Rows and controls

/// A course chip: short name and grade; white when picked.
private struct StudyCourseChip: View {
    let title: String
    let grade: String?
    let chosen: Bool
    let help: String
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Text(title).font(.wisp(.footnote, weight: chosen ? .semibold : .medium))
                if let grade {
                    Text(grade).font(.wisp(.caption, design: .monospaced)).monospacedDigit()
                        .foregroundStyle(chosen ? Color.black.opacity(0.6) : Theme.textMuted)
                }
            }
            .foregroundStyle(chosen ? Color.black : Theme.textPrimary)
            .padding(.horizontal, 10)
            .frame(minHeight: 26)
            .background(Capsule().fill(fill))
            .overlay(Capsule().stroke(chosen ? Color.clear : Theme.chipStroke, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hovering = $0 }
        .help(help)
        .accessibilityLabel(grade.map { "\(help), \($0)" } ?? help)
        .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
    }

    private var fill: Color {
        if chosen { return Color.white }
        return hovering ? Color.white.opacity(0.14) : Theme.chipFill
    }
}

/// The four kinds as chips: Guide · Quiz · Lesson · Cards.
private struct StudyKindButtons: View {
    let disabled: Bool
    let action: (StudyKind) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(StudyKind.allCases) { kind in
                StudyChrome.pill(kind.label, icon: kind.symbol) { action(kind) }
                    .disabled(disabled)
                    .help("Make a \(kind.title.lowercased())")
            }
        }
    }
}

/// One assignment: kind glyph, bold title, course, due label and points.
/// A click opens its make buttons in place.
private struct StudyDueRow: View {
    let item: StudyAssignment
    let now: Date
    let missing: Bool
    let expanded: Bool
    let busy: Bool
    let onToggle: () -> Void
    let onMake: (StudyKind) -> Void
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button(action: onToggle) { summary }
                .buttonStyle(.plain)
                .accessibilityLabel(spoken)
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                .accessibilityHint("Shows what Study can make for it")
            if expanded { actions.transition(.opacity) }
        }
        .padding(.horizontal, 8).padding(.vertical, 6)
        .background(RoundedRectangle(cornerRadius: 8).fill(rowFill))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(expanded ? Theme.hairline : Color.clear, lineWidth: 1))
        .onHover { hovering = $0 }
    }

    private var rowFill: Color {
        if expanded { return Theme.raised }
        return hovering ? Color.white.opacity(0.05) : Color.clear
    }

    private var summary: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: item.symbol).font(.wisp(size: 11))
                .foregroundStyle(missing ? Theme.warn : Theme.textSecondary)
                .frame(width: 16).padding(.top, 2)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title).font(.wisp(.callout, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.leading)
                detailLine
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if let points = StudyText.points(item) {
                Text(points).font(.wisp(.caption)).monospacedDigit().foregroundStyle(Theme.textMuted)
                    .padding(.top, 2)
            }
            Image(systemName: expanded ? "chevron.up" : "chevron.down")
                .font(.wisp(size: 8, weight: .semibold)).foregroundStyle(Theme.textMuted)
                .padding(.top, 4)
                .opacity(hovering || expanded ? 1 : 0)
                .accessibilityHidden(true)
        }
        .contentShape(Rectangle())
    }

    private var detailLine: some View {
        let due = StudyText.due(item, now: now, calendar: .current)
        return HStack(spacing: 4) {
            Text(item.courseName).foregroundStyle(Theme.textMuted).lineLimit(1)
            Text("·").foregroundStyle(Theme.textMuted)
            Text(due).foregroundStyle(dueColor).lineLimit(1)
            if item.status == "late" {
                Text("· Late").foregroundStyle(Theme.warn)
            }
        }
        .font(.wisp(.footnote))
    }

    private var dueColor: Color {
        if missing { return Theme.warn }
        if let due = item.dueTS, due - now.timeIntervalSince1970 < 86_400, !item.isDone { return Theme.textPrimary }
        return Theme.textMuted
    }

    private var actions: some View {
        HStack(spacing: 6) {
            StudyKindButtons(disabled: busy, action: onMake)
            Spacer(minLength: 6)
            if let link = item.url.flatMap({ URL(string: $0) }), link.scheme?.lowercased() == "https" {
                NotchChrome.small("Open in browser ↗") { NSWorkspace.shared.open(link) }
                    .help("Opens this assignment in your browser. Wisp never changes anything there.")
            }
        }
        .padding(.leading, 26)
    }

    private var spoken: String {
        var parts = [item.title, item.courseName, StudyText.due(item, now: now, calendar: .current)]
        if let points = StudyText.points(item) { parts.append(points) }
        return parts.joined(separator: ", ")
    }
}

/// One made material: kind glyph, title, count and best score; delete on hover
/// with a second click to confirm.
private struct StudyLibraryRow: View {
    let item: StudyMaterialSummary
    let now: Date
    let confirming: Bool
    let disabled: Bool
    let onOpen: () -> Void
    let onAskDelete: () -> Void
    let onDelete: () -> Void
    let onKeep: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Button(action: onOpen) { label }
                .buttonStyle(.plain)
                .disabled(disabled)
                .help("Open")
                .accessibilityLabel("\(item.kind.title): \(item.title), \(meta)")
            trailing
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: 8).fill(hovering ? Color.white.opacity(0.05) : Color.clear))
        .onHover { hovering = $0 }
    }

    private var label: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: item.kind.symbol).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                .frame(width: 16)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(item.title).font(.wisp(.body, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                Text(meta).font(.wisp(.caption)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            Spacer(minLength: 6)
            if let best = StudyText.best(item.bestScore) {
                Text(best).font(.wisp(.caption, weight: .medium)).monospacedDigit()
                    .foregroundStyle(Theme.textSecondary)
            }
        }
        .frame(minHeight: 28)
        .contentShape(Rectangle())
    }

    @ViewBuilder private var trailing: some View {
        if confirming {
            HStack(spacing: 4) {
                NotchChrome.small("Delete") { onDelete() }
                    .accessibilityLabel("Delete \(item.title)")
                NotchChrome.small("Keep") { onKeep() }
            }
        } else {
            Button(action: onAskDelete) {
                Image(systemName: "xmark").font(.wisp(size: 9, weight: .bold)).foregroundStyle(Theme.textMuted)
                    .frame(width: 24, height: 24).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .opacity(hovering ? 1 : 0)
            .disabled(disabled)
            .help("Delete")
            .accessibilityLabel("Delete \(item.title)")
        }
    }

    private var meta: String {
        [StudyText.count(item), StudyText.made(item.createdAt, now: now)]
            .filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

/// While Study reads or writes: a breathing glow beside the words and a soft
/// shimmer under them. Still, with Reduce Motion.
struct StudyBusyRow: View {
    let label: String
    @State private var breathing = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Circle()
                    .fill(Theme.orb)
                    .frame(width: 18, height: 18)
                    .scaleEffect(reduceMotion ? 1 : (breathing ? 1.15 : 0.8))
                    .opacity(reduceMotion ? 0.9 : (breathing ? 1 : 0.55))
                    .accessibilityHidden(true)
                Text(label).font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
            }
            StudyShimmer(active: !reduceMotion)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline, lineWidth: 1))
        .accessibilityElement(children: .combine)
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 1.4).repeatForever(autoreverses: true)) { breathing = true }
        }
    }
}

/// A thin line with a soft light passing along it.
private struct StudyShimmer: View {
    let active: Bool
    @State private var phase: CGFloat = -0.3

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.06))
                if active {
                    Capsule()
                        .fill(LinearGradient(colors: [Color.white.opacity(0), Color.white.opacity(0.55),
                                                      Color.white.opacity(0)],
                                             startPoint: .leading, endPoint: .trailing))
                        .frame(width: geo.size.width * 0.3)
                        .offset(x: geo.size.width * phase)
                }
            }
            .clipShape(Capsule())
        }
        .frame(height: 2)
        .accessibilityHidden(true)
        .onAppear {
            guard active else { return }
            withAnimation(.easeInOut(duration: 1.6).repeatForever(autoreverses: false)) { phase = 1.0 }
        }
    }
}
