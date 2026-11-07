import SwiftUI

/// The permission page, in place of the tab's content like quick settings: a
/// title and Done that stay put, then one line for each permission, which opens to
/// say what it's for and what happens next. It scrolls past `cap`. The panel is
/// non-activating, so nothing here opens a sheet, an alert or a popup menu.
struct NotchPermissionsView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var page: NotchPermissionPageModel
    @ObservedObject var access: FirstRunPermissions
    /// The page's room under the header (SurfaceLayout.pageContentCap).
    let cap: CGFloat?
    /// The title row's laid-out height, which grows with Text size.
    @State private var titleHeight: CGFloat = 0

    typealias Item = NotchPermissionPage.Item

    private static let spacing: CGFloat = 12

    init(model: OverlayModel, cap: CGFloat? = nil) {
        self.model = model
        page = model.permissionPage
        access = model.permissionPage.access
        self.cap = cap
    }

    private var openRow: Item? {
        NotchPermissionPage.openRow(page.rowChoice, focus: page.focus,
                                    reporting: Item.allCases.first(where: hasReport),
                                    fullDiskAccess: access.state(.fullDiskAccess))
    }

    /// The row has news about the person's last click, not just the usual next step.
    private func hasReport(_ item: Item) -> Bool {
        access.waiting.contains(item) || access.resetItems.contains(item) || access.unanswered[item] == true
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Self.spacing) {
            HStack {
                Text(page.focus.map(NotchPermissionPage.title) ?? "Mac permissions")
                    .font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                NotchSettingsChrome.doneButton { model.closePermissionPage() }
                    .keyboardShortcut(.cancelAction)
                    .help("Close this page and go back to \(model.showingQuickSettings ? "Settings" : model.tab.stripTab.title)")
            }
            .background(GeometryReader { geo in
                Color.clear.onAppear { titleHeight = geo.size.height }
                    .onChange(of: geo.size.height) { titleHeight = geo.size.height }
            })
            CappedScroll(cap: cap.map(scrollCap), reveals: true) { rows }
        }
        .onChange(of: access.states) { _, _ in model.onResize() }
        .onChange(of: access.waiting) { _, _ in model.onResize() }
        .onChange(of: page.focus.map { access.state($0) }) { old, new in announce(old: old, new: new) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Mac permissions")
    }

    /// What's left for the rows under the title.
    private func scrollCap(_ cap: CGFloat) -> CGFloat {
        max(cap - titleHeight - Self.spacing, SurfaceLayout.minTabScroll)
    }

    private var rows: some View {
        NotchSettingsChrome.card {
            ForEach(Array(NotchPermissionPage.order(focus: page.focus).enumerated()), id: \.element) { index, item in
                if index > 0 { NotchSettingsChrome.hairline }
                row(item)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: A row

    private func row(_ item: Item) -> some View {
        let state = access.state(item)
        let open = openRow == item
        return VStack(alignment: .leading, spacing: 0) {
            summary(item, state, open: open)
            if open { detail(item) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(NotchPermissionPage.title(item))
    }

    /// One line where it all fits, then two, then three, so a narrow panel or large
    /// text never breaks a word or pushes a button out of reach. The button behind it is the whole row,
    /// so what sits on top lets clicks through, except the row's own action.
    private func summary(_ item: Item, _ state: FirstRunPermissions.State, open: Bool) -> some View {
        let action = self.action(item)
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                name(item).allowsHitTesting(false)
                Spacer(minLength: 8)
                status(item, state).allowsHitTesting(false)
                rowAction(item, action)
                chevron(open: open)
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 10) {
                    name(item).allowsHitTesting(false)
                    Spacer(minLength: 8)
                    chevron(open: open)
                }
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 10) {
                        status(item, state).allowsHitTesting(false)
                        Spacer(minLength: 8)
                        rowAction(item, action)
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        status(item, state).allowsHitTesting(false)
                        rowAction(item, action)
                    }
                }
                .padding(.leading, 28)
            }
        }
        .accessibilityElement(children: .contain)
        .frame(minHeight: 26)
        .padding(.horizontal, 14).padding(.vertical, 5)
        .background {
            Button {
                page.toggleRow(item, open: openRow)
            } label: {
                Color.clear.contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(NotchPermissionPage.purpose(item))
            .accessibilityLabel(NotchPermissionPage.title(item))
            .accessibilityValue("\(statusText(item, state)), \(open ? "open" : "closed")")
            .accessibilityHint(open ? "Hides what this is for" : "Shows what this is for and what happens next")
        }
    }

    private func name(_ item: Item) -> some View {
        HStack(spacing: 10) {
            Image(systemName: NotchPermissionPage.symbol(item)).font(.wisp(.body))
                .foregroundStyle(Theme.textSecondary).frame(width: 18)
            Text(NotchPermissionPage.title(item)).font(.wisp(.reading, weight: .medium))
                .foregroundStyle(Theme.textPrimary)
        }
        .fixedSize()
        // The row's button says this, with the state.
        .accessibilityHidden(true)
    }

    private func chevron(open: Bool) -> some View {
        Image(systemName: open ? "chevron.down" : "chevron.right")
            .font(.wisp(.footnote, weight: .semibold)).foregroundStyle(Theme.textMuted)
            .frame(width: 12)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }

    /// What it's for, and what happens next in the state it's in.
    private func detail(_ item: Item) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(NotchPermissionPage.purpose(item))
            let next = self.next(item)
            if !next.isEmpty { Text(next) }
        }
        .font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.leading, 42).padding(.trailing, 14).padding(.bottom, 10)
        .revealInPanel("permission-detail-\(item.rawValue)")
    }

    /// The button stays on the row, closed or open. Return presses the one the page
    /// opened for, and hovering it says what's about to happen.
    @ViewBuilder private func rowAction(_ item: Item, _ action: NotchPermissionPage.Action?) -> some View {
        if let action {
            let button = NotchSettingsChrome.pill(NotchPermissionPage.actionTitle(action, asking: access.asking.contains(item)),
                                                  primary: true) { page.act(item) }
                .disabled(access.asking.contains(item))
                .help(next(item))
                .accessibilityLabel(actionLabel(action, item))
            if item == page.focus {
                button.keyboardShortcut(.defaultAction)
            } else {
                button
            }
        }
    }

    // MARK: Pieces

    private func contactsWait(_ item: Item) -> Bool {
        item == .contacts && FirstRunPermissions.contactsWaits(access.states, messagesReadable: access.messagesReadable)
    }

    private func action(_ item: Item) -> NotchPermissionPage.Action? {
        NotchPermissionPage.action(item, access.state(item), waiting: access.waiting.contains(item),
                                   contactsWait: contactsWait(item))
    }

    private func next(_ item: Item) -> String {
        NotchPermissionPage.next(item, access.state(item), waiting: access.waiting.contains(item),
                                 reset: access.resetItems.contains(item), unanswered: access.unanswered[item] == true,
                                 contactsWait: contactsWait(item))
    }

    private func statusText(_ item: Item, _ state: FirstRunPermissions.State) -> String {
        if contactsWait(item) { return "After Full Disk Access" }
        return state == .checking ? "Checking…" : FirstRunPermissions.label(item, state)
    }

    /// The state in words, with a mark, so color never carries it alone. The row's
    /// button says it to VoiceOver.
    private func status(_ item: Item, _ state: FirstRunPermissions.State) -> some View {
        HStack(spacing: 4) {
            if state == .allowed { Image(systemName: "checkmark") }
            if state == .notAllowed { Image(systemName: "exclamationmark.circle") }
            Text(statusText(item, state))
        }
        .fixedSize()
        .font(.wisp(.footnote, weight: state == .allowed ? .medium : .regular))
        .foregroundStyle(state == .allowed ? Theme.good : state == .notAllowed ? Theme.warn : Theme.textMuted)
        .accessibilityHidden(true)
    }

    private func actionLabel(_ action: NotchPermissionPage.Action, _ item: Item) -> String {
        let title = NotchPermissionPage.title(item)
        switch action {
        case .allow: return "Allow \(title)"
        case .askAgain: return "Ask again for \(title)"
        case .openSettings: return "Open System Settings for \(title)"
        }
    }

    /// VoiceOver hears the answer without hunting for it.
    private func announce(old: FirstRunPermissions.State?, new: FirstRunPermissions.State?) {
        guard let focus = page.focus, let new, old != nil, old != .checking, old != new, new != .checking else { return }
        let text = "\(NotchPermissionPage.title(focus)): \(FirstRunPermissions.label(focus, new))"
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }
}
