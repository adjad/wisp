import SwiftUI
import AppKit

// The Study tab's sources: Google Docs and Slides added from Drive, and school
// pages kept from an import (docs/STUDY_TAB.md). These views sit inside the
// home screen's CappedScroll, so they never grow the panel past its cap; each
// refits the panel when it opens or closes. Inline only: the panel is
// non-activating, so the course "menu" is a row of chips, not a pop-up.

// MARK: - Progress line

/// Under the busy row while material is being made: what it is reading, e.g.
/// "Reading 3 sources (48k chars)". Nothing when it reads only the course and
/// assignment, as before.
struct StudyProgressLine: View {
    @ObservedObject var study: StudyModel

    var body: some View {
        if let plan = study.making, let detail = StudyText.progressDetail(plan) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: plan.wholeCourse == nil ? "books.vertical" : "square.stack.3d.up")
                    .font(.wisp(size: 10)).foregroundStyle(Theme.textMuted)
                    .accessibilityHidden(true)
                Text(detail + "…")
                    .font(.wisp(.caption)).monospacedDigit().foregroundStyle(Theme.textMuted)
                    .lineLimit(1).truncationMode(.middle)
            }
            .padding(.horizontal, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(StudyText.progress(plan))
            .transition(.opacity)
        }
    }
}

// MARK: - Make options

/// Under the topic field: "Use my sources" when there are any, and "Whole
/// course" for the picked course when it has sources or assignments, with
/// a caption when the Study model reads the whole course at once.
struct StudyMakeOptions: View {
    @ObservedObject var study: StudyModel
    let state: StudyState
    let course: StudyCourse?

    var body: some View {
        let sourceCount = state.sourceIDs(for: course?.id).count
        let whole = course.flatMap { c in
            (!state.sources(inCourse: c.id).isEmpty || !state.assignments(in: c).isEmpty) ? c : nil
        }
        if sourceCount > 0 || whole != nil {
            HStack(alignment: .center, spacing: 6) {
                if sourceCount > 0 {
                    StudyToggleChip(title: StudyText.useSources, count: sourceCount, on: study.useSources,
                                    help: "Read your \(sourceCount == 1 ? "source" : "sources") when making material") {
                        study.useSources.toggle()
                    }
                }
                if let whole {
                    StudyChrome.pill(StudyText.wholeCourse, icon: "square.stack.3d.up") { makeWhole(whole) }
                        .disabled(study.busy != nil)
                        .help(StudyText.wholeCourseHelp)
                        .accessibilityLabel("Whole course study guide for \(whole.name)")
                    if state.connector.longContext {
                        Text(StudyText.longContextNote)
                            .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                            .lineLimit(1).truncationMode(.tail)
                    }
                }
                Spacer(minLength: 0)
            }
        }
    }

    private func makeWhole(_ course: StudyCourse) {
        // Without a ready connector, the key card opens instead of a request that can only fail.
        guard state.connector.ready else {
            study.showingConnector = true
            return
        }
        Task { await study.makeWholeCourse(.guide, course: course) }
    }
}

/// A capsule that stays on or off: white when on, like a picked course chip.
struct StudyToggleChip: View {
    let title: String
    var count: Int? = nil
    let on: Bool
    let help: String
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Image(systemName: on ? "checkmark" : "plus").font(.wisp(size: 9, weight: .bold))
                Text(title).font(.wisp(.footnote, weight: on ? .semibold : .medium))
                if let count {
                    Text("\(count)").font(.wisp(.caption, design: .monospaced)).monospacedDigit()
                        .foregroundStyle(on ? Color.black.opacity(0.6) : Theme.textMuted)
                }
            }
            .foregroundStyle(on ? Color.black : Theme.textPrimary)
            .padding(.horizontal, 10)
            .frame(minHeight: 24)
            .background(Capsule().fill(on ? Color.white : (hovering ? Color.white.opacity(0.14) : Theme.chipFill)))
            .overlay(Capsule().stroke(on ? Color.clear : Theme.chipStroke, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hovering = $0 }
        .help(help)
        .accessibilityLabel(title)
        .accessibilityValue(on ? "On" : "Off")
        .accessibilityAddTraits(on ? [.isButton, .isSelected] : .isButton)
    }
}

// MARK: - Sources section

/// "SOURCES": each course's sources, then "Not in a course", and the
/// "Add from Google Drive" row that opens the picker in place.
struct StudySourcesSection: View {
    @ObservedObject var study: StudyModel
    let state: StudyState
    /// The course chip picked on the home screen: filters the list and is the
    /// picker's default course.
    let courseID: String?
    let onResize: () -> Void

    @State private var showAll = false
    @State private var confirming: String?
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.reduceMotionOverride) private var reduceMotionOverride
    private var reduceMotion: Bool { reduceMotionOverride ?? systemReduceMotion }

    /// Source rows before "Show all".
    private static let rows = 4

    var body: some View {
        let groups = state.sourceGroups(courseID: courseID)
        let total = groups.reduce(0) { $0 + $1.items.count }
        let shown = trimmed(groups)
        // Course names only when they tell rows apart.
        let titled = groups.count > 1 || groups.first?.id != (courseID ?? "")
        VStack(alignment: .leading, spacing: 2) {
            if total > 0 {
                header(total: total)
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(shown) { group in
                        if titled {
                            Text(group.title)
                                .font(.wisp(.caption, weight: .medium)).foregroundStyle(Theme.textMuted)
                                .lineLimit(1)
                                .padding(.horizontal, 8).padding(.top, 4)
                                .accessibilityAddTraits(.isHeader)
                        }
                        ForEach(group.items) { source in
                            StudySourceRow(source: source, confirming: confirming == source.id,
                                           disabled: study.busy != nil,
                                           onOpenPages: { Task { await study.openDeck(source) } },
                                           onAskRemove: { set { confirming = source.id } },
                                           onRemove: {
                                               set { confirming = nil }
                                               Task { await study.deleteSource(id: source.id) }
                                           },
                                           onKeep: { set { confirming = nil } })
                        }
                    }
                }
            }
            StudyDriveRow(open: study.showingDrive, disabled: study.addingFile != nil) { toggleDrive() }
            if study.showingDrive {
                StudyDrivePicker(study: study, state: state, defaultCourse: courseID)
                    .padding(.top, 4)
                    .revealInPanel("study-drive")
                    .transition(.opacity)
            }
        }
        .studyReportsHeight(onResize)
        .onChange(of: study.showingDrive) { _, _ in onResize() }
        .onChange(of: courseID) { _, _ in showAll = false }
    }

    private func header(total: Int) -> some View {
        HStack(alignment: .center, spacing: 8) {
            StudyChrome.label(StudyText.sourcesHeader)
            Spacer(minLength: 8)
            if total > Self.rows {
                Button(showAll ? "Show fewer" : "Show all (\(total))") { set { showAll.toggle() } }
                    .buttonStyle(.plain)
                    .font(.wisp(.caption, weight: .medium)).foregroundStyle(Theme.textSecondary)
                    .frame(minHeight: 24)
                    .contentShape(Rectangle())
            }
        }
        .frame(minHeight: 18)
    }

    /// The groups cut to `rows` sources in all unless "Show all" is on.
    private func trimmed(_ groups: [StudySourceGroup]) -> [StudySourceGroup] {
        guard !showAll else { return groups }
        var left = Self.rows
        var result: [StudySourceGroup] = []
        for group in groups where left > 0 {
            let items = Array(group.items.prefix(left))
            left -= items.count
            result.append(StudySourceGroup(id: group.id, title: group.title, items: items))
        }
        return result
    }

    private func toggleDrive() {
        if study.showingDrive {
            set { study.closeDrive() }
        } else {
            set { study.showingDrive = true }
            Task { await study.openDrive() }
        }
    }

    private func set(_ change: () -> Void) {
        withAnimation(Theme.Motion.panel(reduceMotion: reduceMotion), change)
    }
}

/// "Add from Google Drive", as a library-style row.
private struct StudyDriveRow: View {
    let open: Bool
    let disabled: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "plus.circle").font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                    .frame(width: 16)
                    .accessibilityHidden(true)
                Text(StudyText.driveButton).font(.wisp(.body, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Text("Docs · Slides").font(.wisp(.caption)).foregroundStyle(Theme.textMuted).lineLimit(1)
                Image(systemName: open ? "chevron.up" : "chevron.down")
                    .font(.wisp(size: 8, weight: .semibold)).foregroundStyle(Theme.textMuted)
                    .accessibilityHidden(true)
            }
            .frame(minHeight: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .padding(.horizontal, 8).padding(.vertical, 1)
        .background(RoundedRectangle(cornerRadius: 8).fill(hovering ? Color.white.opacity(0.05) : Color.clear))
        .onHover { hovering = $0 }
        .help(StudyText.driveHelp)
        .accessibilityValue(open ? "Expanded" : "Collapsed")
    }
}

/// One source on one line: kind glyph, title, size; remove on hover with a
/// second click to confirm. A click on an https source opens it in the browser.
private struct StudySourceRow: View {
    let source: StudySource
    let confirming: Bool
    let disabled: Bool
    let onOpenPages: () -> Void
    let onAskRemove: () -> Void
    let onRemove: () -> Void
    let onKeep: () -> Void
    @State private var hovering = false

    private var link: URL? {
        guard let url = source.url.flatMap({ URL(string: $0) }), url.scheme?.lowercased() == "https" else { return nil }
        return url
    }

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Button { if let link { NSWorkspace.shared.open(link) } } label: { label }
                .buttonStyle(.plain)
                .disabled(link == nil)
                .help(link == nil ? source.title : "Open in your browser")
                .accessibilityLabel("\(kindName): \(source.title), \(StudyText.chars(source.chars))")
            if source.hasPages && !confirming { pagesButton }
            trailing
        }
        .padding(.horizontal, 8).padding(.vertical, 1)
        .background(RoundedRectangle(cornerRadius: 8).fill(hovering ? Color.white.opacity(0.05) : Color.clear))
        .onHover { hovering = $0 }
    }

    private var kindName: String {
        if source.kind == "page" { return "School page" }
        return source.isSlides ? "Google Slides" : "Google Doc"
    }

    private var label: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: source.symbol).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                .frame(width: 16)
                .accessibilityHidden(true)
            Text(source.title).font(.wisp(.body, weight: .medium)).foregroundStyle(Theme.textPrimary)
                .lineLimit(1)
                .layoutPriority(1)
            Spacer(minLength: 8)
            Text(StudyText.chars(source.chars)).font(.wisp(.caption)).monospacedDigit()
                .foregroundStyle(Theme.textMuted).lineLimit(1).fixedSize()
        }
        .frame(minHeight: 24)
        .contentShape(Rectangle())
    }

    /// "12 pages ›": opens the deck's pages.
    private var pagesButton: some View {
        Button(action: onOpenPages) {
            HStack(spacing: 3) {
                Text(StudyText.pagesCount(source.pages)).font(.wisp(.caption, weight: .medium)).monospacedDigit()
                Image(systemName: "chevron.right").font(.wisp(size: 8, weight: .semibold))
            }
            .foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 7)
            .frame(minHeight: 20)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(StudyRowButtonStyle())
        .fixedSize()
        .disabled(disabled)
        .help("See this deck's pages and make something from them")
        .accessibilityLabel("\(StudyText.pagesCount(source.pages)) in \(source.title)")
    }

    @ViewBuilder private var trailing: some View {
        if confirming {
            HStack(spacing: 4) {
                NotchChrome.small("Remove") { onRemove() }
                    .accessibilityLabel("Remove \(source.title) from Study")
                NotchChrome.small("Keep") { onKeep() }
            }
        } else {
            Button(action: onAskRemove) {
                Image(systemName: "xmark").font(.wisp(size: 9, weight: .bold)).foregroundStyle(Theme.textMuted)
                    .frame(width: 24, height: 24).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .opacity(hovering ? 1 : 0)
            .disabled(disabled)
            .help("Remove from Study. Nothing changes in Google Drive.")
            .accessibilityLabel("Remove \(source.title)")
        }
    }
}

// MARK: - Drive picker

/// The compact picker: a search field, the course to file it under, then the
/// newest matching Docs and Slides. A click adds one. Loading, empty,
/// not-connected and error states replace the list; a failed add says so
/// above it.
struct StudyDrivePicker: View {
    @ObservedObject var study: StudyModel
    let state: StudyState
    let defaultCourse: String?

    @State private var query = ""
    @State private var course: String?
    @State private var added: Set<String> = []
    @FocusState private var searching: Bool

    /// Rows shown at once; search narrows the rest.
    private static let rows = 6

    var body: some View {
        StudyChrome.card(padding: 10) {
            topRow
            if !state.courses.isEmpty { courseRow }
            if let notice = study.driveNotice {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "exclamationmark.circle").font(.wisp(size: 10)).foregroundStyle(Theme.warn)
                        .accessibilityHidden(true)
                    Text(notice).font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .transition(.opacity)
            }
            content
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "lock").font(.wisp(size: 9)).foregroundStyle(Theme.textMuted)
                    .accessibilityHidden(true)
                Text(StudyText.drivePrivacy)
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onAppear {
            course = defaultCourse
            query = study.driveQuery
        }
        .onChange(of: searching) { _, focused in study.editing = focused }
        .onDisappear {
            if searching { study.editing = false }
        }
        .task(id: query) {
            // A short pause so each keystroke isn't a request.
            guard query != study.driveQuery, study.drivePhase == .ready || study.drivePhase == .loading else { return }
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled else { return }
            await study.driveFiles(query: query)
        }
        .onExitCommand { study.closeDrive() }
    }

    private var topRow: some View {
        HStack(alignment: .center, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").font(.wisp(size: 10)).foregroundStyle(Theme.textMuted)
                    .accessibilityHidden(true)
                TextField(StudyText.driveSearch, text: $query)
                    .textFieldStyle(.plain)
                    .font(.wisp(size: 13)).foregroundStyle(Theme.textPrimary)
                    .focused($searching)
                    .disabled(!canSearch)
                    .onSubmit { Task { await study.driveFiles(query: query) } }
                    .accessibilityLabel(StudyText.driveSearch)
            }
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .stroke(searching ? Color.white.opacity(0.35) : Theme.chipStroke, lineWidth: 1))
            NotchChrome.iconButton("xmark", help: "Close Google Drive") { study.closeDrive() }
        }
    }

    private var canSearch: Bool {
        switch study.drivePhase {
        case .ready, .loading: return true
        default: return false
        }
    }

    private var courseRow: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(StudyText.driveCourse.uppercased())
                .font(.wisp(size: 9.5, design: .monospaced)).tracking(0.6).foregroundStyle(Theme.textMuted)
                .fixedSize()
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    StudyCourseOption(title: StudyText.driveNoCourse, chosen: course == nil,
                                      help: "Add it without a course") { course = nil }
                    ForEach(state.courses) { item in
                        StudyCourseOption(title: item.shortName, chosen: course == item.id, help: item.name) {
                            course = item.id
                        }
                    }
                }
                .padding(.vertical, 1)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Course for the file you add")
    }

    @ViewBuilder private var content: some View {
        switch study.drivePhase {
        case .idle, .loading:
            if study.driveList.isEmpty {
                waiting
            } else {
                list
            }
        case .ready:
            if study.driveList.isEmpty {
                Text(study.driveQuery.isEmpty ? StudyText.driveEmpty : StudyText.driveEmptySearch)
                    .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .frame(minHeight: 24)
            } else {
                list
            }
        case .notConnected(let message):
            notConnected(message)
        case .failed(let message):
            failed(message)
        }
    }

    private var waiting: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(StudyText.driveLoading).font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
        }
        .frame(minHeight: 24)
        .accessibilityElement(children: .combine)
    }

    private var list: some View {
        let files = Array(study.driveList.prefix(Self.rows))
        let have = Set(state.sources.compactMap(\.url))
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(files) { file in
                StudyDriveFileRow(file: file,
                                  added: added.contains(file.id) || file.url.map { have.contains($0) } == true,
                                  adding: study.addingFile == file.id,
                                  disabled: study.addingFile != nil) {
                    Task {
                        if await study.addDriveFile(id: file.id, courseId: course) { added.insert(file.id) }
                    }
                }
            }
            if study.driveList.count > Self.rows {
                Text(StudyText.driveMore).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                    .padding(.horizontal, 8).padding(.top, 2)
            }
        }
        .opacity(study.drivePhase == .loading ? 0.5 : 1)
        .padding(.horizontal, -8)
    }

    private func notConnected(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "person.crop.circle.badge.exclamationmark").font(.wisp(size: 11))
                .foregroundStyle(Theme.warn)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(message).font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(StudyText.driveConnectHint).font(.wisp(.caption)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            NotchChrome.small(StudyText.tryAgainButton) { Task { await study.openDrive() } }
        }
        .accessibilityElement(children: .combine)
    }

    private func failed(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "exclamationmark.circle").font(.wisp(size: 10.5)).foregroundStyle(Theme.warn)
                .accessibilityHidden(true)
            Text(message).font(.wisp(.footnote)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 6)
            NotchChrome.small(StudyText.tryAgainButton) { Task { await study.openDrive() } }
        }
    }
}

/// A small course chip in the picker: white when chosen.
private struct StudyCourseOption: View {
    let title: String
    let chosen: Bool
    let help: String
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Text(title).font(.wisp(.caption, weight: chosen ? .semibold : .medium))
                .foregroundStyle(chosen ? Color.black : Theme.textPrimary)
                .padding(.horizontal, 8)
                .frame(minHeight: 20)
                .background(Capsule().fill(chosen ? Color.white : (hovering ? Color.white.opacity(0.14) : Theme.chipFill)))
                .overlay(Capsule().stroke(chosen ? Color.clear : Theme.chipStroke, lineWidth: 1))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hovering = $0 }
        .help(help)
        .accessibilityAddTraits(chosen ? [.isButton, .isSelected] : .isButton)
    }
}

/// One Doc or Slides deck: kind glyph, name, last edit, then add, a spinner
/// while it exports, or a check once it is a source.
private struct StudyDriveFileRow: View {
    let file: StudyDriveFile
    let added: Bool
    let adding: Bool
    let disabled: Bool
    let onAdd: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: onAdd) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: file.symbol).font(.wisp(size: 11)).foregroundStyle(Theme.textSecondary)
                    .frame(width: 16)
                    .accessibilityHidden(true)
                Text(file.name).font(.wisp(.body, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.middle)
                    .layoutPriority(1)
                Spacer(minLength: 8)
                Text(StudyText.edited(file.modified, now: Date(), calendar: .current))
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted).lineLimit(1).fixedSize()
                trailing.frame(width: 44, alignment: .trailing)
            }
            .frame(minHeight: 24)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled || added)
        .padding(.horizontal, 8).padding(.vertical, 1)
        .background(RoundedRectangle(cornerRadius: 8).fill(hovering && !added ? Color.white.opacity(0.05) : Color.clear))
        .onHover { hovering = $0 }
        .help(added ? "Already one of your sources" : "Add to your Study sources")
        .accessibilityLabel("\(file.kindLabel): \(file.name)")
        .accessibilityValue(added ? StudyText.driveAdded : (adding ? "Adding" : ""))
        .accessibilityHint(added ? "" : "Adds it to your Study sources")
    }

    @ViewBuilder private var trailing: some View {
        if adding {
            ProgressView().controlSize(.mini)
        } else if added {
            HStack(spacing: 3) {
                Image(systemName: "checkmark").font(.wisp(size: 9, weight: .bold))
                Text(StudyText.driveAdded).font(.wisp(.caption, weight: .medium))
            }
            .foregroundStyle(Theme.textSecondary)
            .fixedSize()
        } else {
            Image(systemName: "plus").font(.wisp(size: 10, weight: .semibold))
                .foregroundStyle(hovering ? Theme.textPrimary : Theme.textMuted)
        }
    }
}

// MARK: - Automatic quiz preparing

/// "Get ready for quizzes automatically": one line and a muted note under it.
/// Shown only when the service has the setting.
struct StudyAutoPrepareRow: View {
    let on: Bool
    let disabled: Bool
    let onChange: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .center, spacing: 8) {
                Text(StudyText.autoPrepareTitle)
                    .font(.wisp(.footnote, weight: .medium)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 8)
                Toggle("", isOn: Binding(get: { on }, set: { onChange($0) }))
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                    .disabled(disabled)
                    .accessibilityLabel(StudyText.autoPrepareTitle)
            }
            .frame(minHeight: 24)
            Text(StudyText.autoPrepareNote)
                .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

// MARK: - Deck pages

/// A deck's pages full-tab, pushed like a material: the back row, the deck's
/// title and the make buttons stay put; the pages scroll. A click on a page
/// picks it; the buttons make from the picked pages, or from every page
/// (up to 40) when none is picked.
struct StudyDeckPagesPage<Notice: View>: View {
    @ObservedObject var study: StudyModel
    let deck: StudyOpenDeck
    let cap: CGFloat?
    let onResize: () -> Void
    @ViewBuilder let notice: () -> Notice
    @State private var selected: Set<StudySlideRef> = []

    private static var kinds: [StudyKind] { [.flashcards, .quiz, .guide] }

    var body: some View {
        StudyPinnedPage(cap: cap, onResize: onResize, onBack: { study.closeDeck() }, scrollID: deck.source.id) {
            StudyPageBadge(symbol: "play.rectangle", title: "Slides")
        } title: {
            titleBlock
        } content: {
            VStack(alignment: .leading, spacing: 6) {
                notice()
                pageList
            }
        }
    }

    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 6) {
            VStack(alignment: .leading, spacing: 3) {
                Text(deck.source.title)
                    .font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                Text(metaLine).font(.wisp(.caption)).foregroundStyle(Theme.textMuted).lineLimit(1)
            }
            if let busy = study.busy, busy != .loading {
                StudyBusyRow(label: StudyText.busy(busy))
            } else if !deck.pages.isEmpty {
                HStack(spacing: 6) {
                    ForEach(Self.kinds) { kind in
                        StudyChrome.pill(kind.label, icon: kind.symbol, primary: kind == .quiz) { make(kind) }
                            .disabled(study.busy != nil)
                    }
                    Spacer(minLength: 4)
                    Text(StudyText.deckSelection(selected.count, total: deck.pages.count))
                        .font(.wisp(.caption)).foregroundStyle(Theme.textMuted).lineLimit(1)
                }
            }
        }
    }

    private var metaLine: String {
        var parts = [StudyText.pagesCount(deck.pages.isEmpty ? deck.source.pages : deck.pages.count)]
        if let course = study.state?.course(deck.source.courseID) { parts.append(course.shortName) }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder private var pageList: some View {
        if deck.loading {
            ProcessingRow(label: StudyText.deckPagesLoading)
        } else if deck.pages.isEmpty {
            Text(StudyText.deckPagesEmpty).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
        } else {
            VStack(alignment: .leading, spacing: 2) {
                ForEach(deck.pages) { page in
                    let ref = StudySlideRef(sourceID: deck.source.id, page: page.page)
                    StudyDeckPageRow(page: page, picked: selected.contains(ref)) {
                        if selected.contains(ref) { selected.remove(ref) } else { selected.insert(ref) }
                    }
                }
            }
        }
    }

    private func make(_ kind: StudyKind) {
        guard study.connectorReady else {
            study.notice = StudyText.problem(code: "missing")
            return
        }
        let pages = StudySlides.pagesToSend(selected: selected, all: deck.refs)
        Task { await study.makeFromPages(kind, pages: pages, title: deck.source.title) }
    }
}

/// One deck page: a check, "p. 7 · Title", its points as short bullets and
/// the figure in muted text. The whole block picks it, with no pressed look.
private struct StudyDeckPageRow: View {
    let page: StudyDeckPage
    let picked: Bool
    let onToggle: () -> Void
    @State private var hovering = false

    /// Points shown per page, so a long page stays short.
    private static let maxPoints = 4

    var body: some View {
        Button(action: onToggle) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: picked ? "checkmark.circle.fill" : "circle")
                    .font(.wisp(size: 11)).foregroundStyle(picked ? Theme.textPrimary : Theme.textMuted)
                    .frame(width: 14)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(StudyText.deckPageHeader(page))
                        .font(.wisp(.footnote, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                        .lineLimit(1).truncationMode(.tail)
                    ForEach(Array(page.points.prefix(Self.maxPoints).enumerated()), id: \.offset) { pair in
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Text("•").font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                            Text(pair.element).font(.wisp(.caption)).foregroundStyle(Theme.textSecondary)
                                .lineLimit(1).truncationMode(.tail)
                        }
                    }
                    if let figure = page.figure {
                        Text(figure).font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
                            .lineLimit(1).truncationMode(.tail)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8).padding(.vertical, 4)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(rowFill))
            .contentShape(Rectangle())
        }
        .buttonStyle(StudyRowButtonStyle())
        .onHover { hovering = $0 }
        .accessibilityLabel(StudyText.deckPageHeader(page))
        .accessibilityValue(picked ? "Picked" : "Not picked")
        .accessibilityAddTraits(picked ? [.isButton, .isSelected] : .isButton)
    }

    private var rowFill: Color {
        if picked { return Color.white.opacity(0.08) }
        return hovering ? Color.white.opacity(0.05) : Color.clear
    }
}
