import SwiftUI
import AppKit
#if WISP_NOTCH_QA
import Combine
#endif

/// The notch's Study tab: your courses and grades, what is due, and study
/// material (guides, quizzes, lessons, flash cards) made from your real
/// coursework. School pages are only ever read, never changed; material is
/// written by the Study connector, a cloud model with its own key.
///
/// Everything stays inline: the panel is non-activating, so there are no
/// sheets, alerts or pop-up menus. An opened material takes the whole tab.
///
/// The tab scrolls inside `cap` (SurfaceLayout.studyContentCap, half the
/// screen): the home screen as a whole, an opened material under its fixed
/// back row and title.
struct NotchStudyView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var study: StudyModel
    let cap: CGFloat?

    /// The course chip picked: filters the due list and the library, and is what
    /// "Make something" writes for.
    @State private var courseID: String?
    /// The one due row showing its make buttons.
    @State private var expanded: String?
    @State private var showAllDue = false
    @State private var showAllMade = false
    /// The inactive courses (nothing due, no grade) are showing in the chips row.
    @State private var showAllCourses = false
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
    private static let dueRows = 4
    /// Library rows before "Show all".
    private static let madeRows = 3

    /// Where the home screen's scroll was left while the panel is open. Opening
    /// or closing a material forgets it, so each starts at its top.
    private static let homeScroll = PanelScrollMemory.key(.study)

    /// An opened material is one page deeper than the tab, so it slides as the
    /// notch's own push does (NotchPage.motion). The home list always sits under
    /// it and the material always over it, so each keeps one offset both ways:
    /// the list `removal` (leaving on a push, returning on the way back), the
    /// material `insertion` (arriving, and leaving the same way it came).
    private static let push = NotchPage.motion(from: .tab(.study), to: .settings)

    #if WISP_NOTCH_QA
    /// The motion checks open and close a due row as a click does (`toggle`).
    static let qaToggle = PassthroughSubject<String, Never>()
    #endif

    init(model: OverlayModel, study: StudyModel, cap: CGFloat? = nil) {
        self.model = model
        self.study = study
        self.cap = cap
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
        var showAllMade: Bool
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
                       showAll: showAllDue, showAllMade: showAllMade,
                       clearing: confirmingClear, deleting: confirmingDelete,
                       assignments: state?.assignments.count ?? 0, materials: state?.materials.count ?? 0)
    }

    var body: some View {
        // An opened material is pushed over the home list the way the notch pushes a
        // page: overlapping (a ZStack, so the two never stack while one leaves), the
        // material in from the trailing edge, the list drifting a step to the leading
        // edge; going back is the mirror.
        ZStack(alignment: .topLeading) {
            if let material = study.opened {
                StudyMaterialPage(study: study, material: material, cap: cap, onResize: model.onResize) {
                    notice
                }
                .transition(.studyPage(Self.push.insertion, reduceMotion: reduceMotion))
            } else {
                CappedScroll(cap: cap, reveals: true, remembers: Self.homeScroll, showsMore: true) {
                    VStack(alignment: .leading, spacing: 8) {
                        notice
                        home
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .transition(.studyPage(Self.push.removal, reduceMotion: reduceMotion))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .animation(Theme.Motion.panel(reduceMotion: reduceMotion), value: showing)
        .onChange(of: showing) { _, _ in
            now = Date()
            // As the Brief's tiles do: fit the window in this run-loop turn, before
            // the first frame (a grow lands under content that is already there,
            // which matters most with Reduce Motion, where the content swaps at
            // once), and again once SwiftUI has laid the change out.
            model.onResizeNow()
            model.onResize()
        }
        .onChange(of: study.opened?.id) { _, _ in PanelScrollMemory.forget(Self.homeScroll) }
        #if WISP_NOTCH_QA
        .onReceive(Self.qaToggle) { id in expanded = expanded == id ? nil : id }
        #endif
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

    @ViewBuilder private var notice: some View {
        if let notice = study.notice { noticeRow(notice).transition(.opacity) }
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
        VStack(alignment: .leading, spacing: 10) {
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
                    .transition(.studyReveal(reduceMotion: reduceMotion))
            } else {
                HStack(spacing: 8) {
                    StudyChrome.pill(StudyText.readButton, icon: "text.viewfinder", primary: true) { read() }
                    StudyChrome.pill(StudyText.sampleButton, icon: "sparkles") {
                        Task { await study.loadSample() }
                    }
                }
            }
            Text(StudyText.readOnlyNote)
                .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
            StudySourcesSection(study: study, state: study.state ?? .empty, courseID: nil, onResize: model.onResize)
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
                // Through Permissions, like every privacy page Wisp opens: it notes the
                // pane (so a "Quit & Reopen" brings Wisp back) and steps the panel aside.
                Permissions.openSettingsPane("com.apple.preference.security?Privacy_Accessibility")
            }
        }
    }

    // MARK: With data

    private func content(_ state: StudyState) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            VStack(alignment: .leading, spacing: 4) {
                headerRow(state)
                if state.sample {
                    Text(StudyText.sampleNote).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let busy = study.busy, busy != .loading {
                StudyBusyRow(label: StudyText.busy(busy)).transition(.studyReveal(reduceMotion: reduceMotion))
            }
            StudyProgressLine(study: study)
            if !state.courses.isEmpty { coursesRow(state) }
            dueSection(state)
            makeSection(state)
            StudyMakeOptions(study: study, state: state, course: selectedCourse(state))
            librarySection(state)
            StudySourcesSection(study: study, state: state, courseID: courseID, onResize: model.onResize)
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
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                StudyCourseChip(title: Copy.allCourses, grade: nil, chosen: courseID == nil,
                                help: Copy.allCoursesHelp) { pickCourse(nil) }
                ForEach(shownCourses(state)) { course in
                    StudyCourseChip(title: course.shortName, grade: StudyText.grade(course),
                                    chosen: courseID == course.id, help: course.name) {
                        pickCourse(course.id)
                    }
                }
                let folded = foldedCount(state)
                if folded > 0 {
                    StudyCourseChip(title: "+\(folded) more", grade: nil, chosen: false,
                                    help: Copy.moreCoursesHelp) { showAllCourses = true }
                }
            }
            .padding(.vertical, 1)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(StudyText.coursesHeader)
    }

    /// Active courses (and the picked one), then the rest once "+N more" is pressed.
    private func shownCourses(_ state: StudyState) -> [StudyCourse] {
        if showAllCourses { return state.courses }
        return state.courses.filter { $0.active || $0.id == courseID }
    }

    private func foldedCount(_ state: StudyState) -> Int {
        state.courses.count - shownCourses(state).count
    }

    private func pickCourse(_ id: String?) {
        courseID = courseID == id ? nil : id
        expanded = nil
        showAllDue = false
        showAllMade = false
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

    private func dueSection(_ state: StudyState) -> some View {
        // One flat list in the groups' order (Missing, Today, Tomorrow, ...): each
        // row's due text says the day, and missing work says so in the warn color.
        let rows = dueGroups(state).flatMap { group in group.items.map { (item: $0, missing: group.id == "missing") } }
        let shown = showAllDue ? rows : Array(rows.prefix(Self.dueRows))
        return VStack(alignment: .leading, spacing: 2) {
            sectionHeader(StudyText.dueHeader, showAll: $showAllDue, total: rows.count, over: Self.dueRows)
            if rows.isEmpty {
                Text(StudyText.nothingDue).font(.wisp(.body)).foregroundStyle(Theme.textMuted)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(shown, id: \.item.id) { row in
                        StudyDueRow(item: row.item, courseLabel: courseLabel(row.item, state: state), now: now,
                                    missing: row.missing, reduceMotion: reduceMotion,
                                    expanded: expanded == row.item.id, busy: study.busy != nil,
                                    onToggle: { toggle(row.item) },
                                    onMake: { kind in make(kind, assignment: row.item, state: state) })
                    }
                }
            }
        }
    }

    /// A section's caps label with its "Show all (N)" / "Show fewer" toggle on the
    /// right when the list is longer than `over` rows.
    private func sectionHeader(_ title: String, showAll: Binding<Bool>, total: Int, over: Int) -> some View {
        HStack(alignment: .center, spacing: 8) {
            StudyChrome.label(title)
            Spacer(minLength: 8)
            if total > over {
                Button(showAll.wrappedValue ? Copy.showFewer : "Show all (\(total))") { showAll.wrappedValue.toggle() }
                    .buttonStyle(.plain)
                    .font(.wisp(.caption, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .frame(minHeight: 24)
                    .contentShape(Rectangle())
            }
        }
        .frame(minHeight: 18)
    }

    /// Under "All", the course's short name (as on its chip); under one course,
    /// the name the assignment came with.
    private func courseLabel(_ item: StudyAssignment, state: StudyState) -> String {
        guard courseID == nil else { return item.courseName }
        if let course = state.course(item.courseID) { return course.shortName }
        return item.courseName.isEmpty ? item.courseName : StudyCourse.short(item.courseName)
    }

    private func toggle(_ item: StudyAssignment) {
        expanded = expanded == item.id ? nil : item.id
    }

    // MARK: Make something

    private func makeSection(_ state: StudyState) -> some View {
        let course = selectedCourse(state)
        let trimmed = topic.trimmingCharacters(in: .whitespacesAndNewlines)
        let ready = !trimmed.isEmpty || course != nil
        let buttons = StudyKindButtons(disabled: study.busy != nil || !ready) { kind in
            makeFromTopic(kind, state: state)
        }
        // The field and the four kinds on one line; two lines only if they cannot fit.
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) {
                topicField(course: course, ready: ready, state: state).frame(minWidth: 180)
                buttons
            }
            VStack(alignment: .leading, spacing: 6) {
                topicField(course: course, ready: ready, state: state)
                buttons
            }
        }
    }

    private func topicField(course: StudyCourse?, ready: Bool, state: StudyState) -> some View {
        TextField(course.map { "Make something for \($0.shortName)…" } ?? Copy.topicPlaceholder, text: $topic)
            .textFieldStyle(.plain)
            .font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .stroke(topicFocused ? Color.white.opacity(0.35) : Theme.chipStroke, lineWidth: 1))
            .focused($topicFocused)
            .onSubmit { if ready { makeFromTopic(.guide, state: state) } }
            .accessibilityLabel(Copy.makeHeader)
            .help(Copy.topicHelp)
    }

    // MARK: Library

    private func librarySection(_ state: StudyState) -> some View {
        let materials = libraryItems(state)
        let shown = showAllMade ? materials : Array(materials.prefix(Self.madeRows))
        return VStack(alignment: .leading, spacing: 2) {
            sectionHeader(StudyText.libraryHeader, showAll: $showAllMade, total: materials.count, over: Self.madeRows)
            if materials.isEmpty {
                Text(StudyText.noMaterials).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(shown) { item in
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
        VStack(alignment: .leading, spacing: 6) {
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
                // Opened at the foot of a scrolling tab: scrolled into sight.
                StudyConnectorCard(study: study, onResize: model.onResize)
                    .revealInPanel("study-connector")
                    .transition(.opacity)
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
                // One chevron that turns with the card, on the tab's spring.
                Image(systemName: "chevron.down")
                    .font(.wisp(size: 8, weight: .semibold)).foregroundStyle(Theme.textMuted)
                    .rotationEffect(.degrees(study.showingConnector ? 180 : 0))
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
        static let loadingSample = "Loading the sample…"
        static let readAgain = "Read again"
        static let allCourses = "All"
        static let allCoursesHelp = "Show every course"
        static let moreCoursesHelp = "Show courses with nothing due and no grade"
        static let showFewer = "Show fewer"
        static let makeHeader = "Make something"
        static let topicPlaceholder = "Make something on any topic…"
        static let topicHelp = "Type a topic, or pick a course above, then choose what to make"
        static let addKey = "Add a key to make study material"
        static let clearYes = "Remove"
        static let clearNo = "Keep"
    }
}

// MARK: - Height

extension View {
    /// Calls `onChange` whenever this view's height changes, so a Study piece
    /// that grows inside the notch refits the panel.
    func studyReportsHeight(_ onChange: @escaping () -> Void) -> some View {
        background(GeometryReader { geo in
            Color.clear.onChange(of: geo.size.height) { _, _ in onChange() }
        })
    }
}

// MARK: - Motion

private extension AnyTransition {
    /// A piece opening in place (a due row's make buttons, the writing row):
    /// it fades in while settling one slide step down into its place, and
    /// leaves the same way. With Reduce Motion it only fades, and the tab's
    /// animation is nil then, so in practice it simply appears.
    @MainActor static func studyReveal(reduceMotion: Bool) -> AnyTransition {
        reduceMotion ? .opacity : .opacity.combined(with: .offset(y: -Theme.Motion.slideStep))
    }

    /// The home list or an opened material arriving and leaving, `steps` slide
    /// steps along (NotchPageMotion: positive toward the trailing edge), and faded,
    /// like the notch's own pages. With Reduce Motion: no slide.
    @MainActor static func studyPage(_ steps: Double, reduceMotion: Bool) -> AnyTransition {
        reduceMotion ? .opacity : .opacity.combined(with: .offset(x: CGFloat(steps) * Theme.Motion.slideStep))
    }
}

// MARK: - Opened material

/// A material full-tab: a back bar, its title and kind, then its player or reader.
private struct StudyMaterialPage<Notice: View>: View {
    @ObservedObject var study: StudyModel
    let material: StudyMaterial
    /// The tab's room under the header; the back row and title stay above the scroll.
    let cap: CGFloat?
    /// Refits the panel when the page grows or shrinks (an explanation, the
    /// review list, the next lesson step, the finished deck).
    let onResize: () -> Void
    /// The tab's notice, if any, at the top of the scrolled part.
    @ViewBuilder let notice: () -> Notice
    @State private var pinnedHeight: CGFloat = 0
    private static var spacing: CGFloat { 10 }

    var body: some View {
        // The back row and title are measured a pass after the page first lays out,
        // so on that first pass the scroll is given the whole cap. Held to the
        // page's final bound from the start, the page reports its settled height at
        // once, and the window fitted in the same turn (NotchStudyView) is right.
        StudyHeightLimit(limit: cap.map { max($0, pinnedHeight + Self.spacing + SurfaceLayout.minTabScroll) }) {
            VStack(alignment: .leading, spacing: Self.spacing) {
                VStack(alignment: .leading, spacing: 8) {
                    topBar
                    titleBlock
                }
                .background(GeometryReader { geo in
                    Color.clear.onAppear { pinnedHeight = geo.size.height }
                        .onChange(of: geo.size.height) { pinnedHeight = geo.size.height }
                })
                CappedScroll(cap: cap.map(scrollCap), showsMore: true) {
                    VStack(alignment: .leading, spacing: 8) {
                        notice()
                        materialBody.id(material.id)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .id(material.id)
            }
        }
        .studyReportsHeight(onResize)
        .onExitCommand { study.close() }
    }

    /// What's left for the material under its fixed back row and title.
    private func scrollCap(_ cap: CGFloat) -> CGFloat {
        max(cap - pinnedHeight - Self.spacing, SurfaceLayout.minTabScroll)
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

/// Lays out one view at its own size, reporting no more than `limit` tall (it
/// is drawn in full, from the top). Nil: no limit.
private struct StudyHeightLimit: Layout {
    let limit: CGFloat?

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let page = subviews.first else { return .zero }
        let size = page.sizeThatFits(proposal)
        return CGSize(width: size.width, height: limit.map { min(size.height, $0) } ?? size.height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        guard let page = subviews.first else { return }
        page.place(at: bounds.origin, anchor: .topLeading,
                   proposal: ProposedViewSize(width: bounds.width, height: proposal.height))
    }
}

// MARK: - Rows and controls

/// A whole row as its button, with no pressed look: the label stays exactly as
/// drawn (no dimming), so the row doesn't blink when clicked.
private struct StudyRowButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { configuration.label }
}

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
            .padding(.horizontal, 9)
            .frame(minHeight: 22)
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

/// One assignment on one line: kind glyph, bold title, course, then the due
/// text in a right-hand column and the points. A click opens its make buttons in place.
private struct StudyDueRow: View {
    let item: StudyAssignment
    /// The course as this row names it.
    let courseLabel: String
    let now: Date
    let missing: Bool
    /// The tab's Reduce Motion (the system's, or the motion checks' override).
    let reduceMotion: Bool
    let expanded: Bool
    let busy: Bool
    let onToggle: () -> Void
    let onMake: (StudyKind) -> Void
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // Not `.plain`: on the Mac it dims the label while the mouse is down, so
            // the row's own text blinked on every click. The row's fill, outline and
            // chevron are its feedback; its icon, title and due text never fade.
            Button(action: onToggle) { summary }
                .buttonStyle(StudyRowButtonStyle())
                .accessibilityLabel(spoken)
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                .accessibilityHint("Shows what Study can make for it")
            // The make buttons fade in and settle down from under the summary while
            // the row's fill and outline ease in and the rows below glide, all on the
            // tab's one spring (NotchStudyView's animation, keyed by `expanded`).
            if expanded { actions.transition(.studyReveal(reduceMotion: reduceMotion)) }
        }
        .padding(.horizontal, 8).padding(.vertical, 2)
        .background(RoundedRectangle(cornerRadius: 8).fill(rowFill))
        // Buttons leaving as the row closes stay inside it, not drawn over the next row.
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(expanded ? Theme.hairline : Color.clear, lineWidth: 1))
        .onHover { hovering = $0 }
    }

    private var rowFill: Color {
        if expanded { return Theme.raised }
        return hovering ? Color.white.opacity(0.05) : Color.clear
    }

    private var summary: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: item.symbol).font(.wisp(size: 11))
                .foregroundStyle(missing ? Theme.warn : Theme.textSecondary)
                .frame(width: 16)
                .accessibilityHidden(true)
            Text(item.title).font(.wisp(.callout, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                .lineLimit(1)
                .layoutPriority(1)
            Text(courseLabel).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                .lineLimit(1)
            Spacer(minLength: 8)
            if item.status == "late" {
                Text("Late").font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.warn)
                    .fixedSize()
            }
            Text(StudyText.dueShort(item, now: now, calendar: .current))
                .font(.wisp(.footnote)).monospacedDigit().foregroundStyle(dueColor)
                .lineLimit(1).fixedSize()
            Text(StudyText.points(item) ?? "")
                .font(.wisp(.caption)).monospacedDigit().foregroundStyle(Theme.textMuted)
                .lineLimit(1)
                .frame(width: 48, alignment: .trailing)
                .accessibilityHidden(true)
            Image(systemName: "chevron.down")
                .font(.wisp(size: 8, weight: .semibold)).foregroundStyle(Theme.textMuted)
                .rotationEffect(.degrees(expanded ? 180 : 0))
                .opacity(hovering || expanded ? 1 : 0)
                .accessibilityHidden(true)
        }
        .frame(minHeight: 24)
        .contentShape(Rectangle())
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
        .padding(.leading, 24)
    }

    private var spoken: String {
        var parts = [item.title, item.courseName, StudyText.due(item, now: now, calendar: .current)]
        if let points = StudyText.points(item) { parts.append(points) }
        return parts.joined(separator: ", ")
    }
}

/// One made material on one line: kind glyph, title, then count, age and best
/// score on the right; delete on hover with a second click to confirm.
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
        .padding(.horizontal, 8).padding(.vertical, 1)
        .background(RoundedRectangle(cornerRadius: 8).fill(hovering ? Color.white.opacity(0.05) : Color.clear))
        .onHover { hovering = $0 }
    }

    private var label: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: item.kind.symbol).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                .frame(width: 16)
                .accessibilityHidden(true)
            Text(item.title).font(.wisp(.body, weight: .medium)).foregroundStyle(Theme.textPrimary)
                .lineLimit(1)
                .layoutPriority(1)
            Spacer(minLength: 8)
            Text(meta).font(.wisp(.caption)).foregroundStyle(Theme.textMuted).lineLimit(1)
            if let best = StudyText.best(item.bestScore) {
                Text(best).font(.wisp(.caption, weight: .medium)).monospacedDigit()
                    .foregroundStyle(Theme.textSecondary)
                    .fixedSize()
            }
        }
        .frame(minHeight: 24)
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
