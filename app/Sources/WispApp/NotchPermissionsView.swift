import SwiftUI

/// The permission page, in place of the tab's content like quick settings: a
/// title and Done on top, then labelled cards. The panel is non-activating, so
/// nothing here opens a sheet, an alert or a popup menu.
struct NotchPermissionsView: View {
    @ObservedObject var model: OverlayModel
    @ObservedObject var page: NotchPermissionPageModel
    @ObservedObject var access: FirstRunPermissions

    typealias Item = NotchPermissionPage.Item

    init(model: OverlayModel) {
        self.model = model
        page = model.permissionPage
        access = model.permissionPage.access
    }

    private var others: [Item] { NotchPermissionPage.order(focus: page.focus).filter { $0 != page.focus } }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(page.focus.map(NotchPermissionPage.title) ?? "Mac permissions")
                    .font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Done") { model.closePermissionPage() }
                    .buttonStyle(.plain).font(.wisp(.callout)).foregroundStyle(Theme.textSecondary)
                    .keyboardShortcut(.cancelAction)
                    .help("Close this page and go back to \(model.showingQuickSettings ? "Settings" : model.tab.stripTab.title)")
            }
            if let focus = page.focus { focusCard(focus) }
            NotchSettingsChrome.group(page.focus == nil ? "Permissions" : "Other permissions") {
                ForEach(Array(others.enumerated()), id: \.element) { index, item in
                    if index > 0 { NotchSettingsChrome.hairline }
                    row(item)
                }
            }
        }
        .onChange(of: access.states) { _, _ in model.onResize() }
        .onChange(of: access.waiting) { _, _ in model.onResize() }
        .onChange(of: page.focus.map { access.state($0) }) { old, new in announce(old: old, new: new) }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Mac permissions")
    }

    // MARK: The permission it opened on

    private func focusCard(_ item: Item) -> some View {
        let state = access.state(item)
        let action = self.action(item)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: NotchPermissionPage.symbol(item)).font(.wisp(.body))
                    .foregroundStyle(Theme.textSecondary).frame(width: 18).accessibilityHidden(true)
                Text(NotchPermissionPage.purpose(item)).font(.wisp(.reading)).foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            let next = self.next(item)
            if !next.isEmpty {
                Text(next).font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 26)
            }
            // Beside the state where both fit, under it where they don't, so a narrow
            // panel never breaks a word to make room.
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    status(item, state)
                    Spacer(minLength: 8)
                    focusAction(item, action)
                }
                VStack(alignment: .leading, spacing: 8) {
                    status(item, state)
                    focusAction(item, action)
                }
            }
            .padding(.leading, 26)
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(RoundedRectangle(cornerRadius: 14).fill(Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.hairline, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel(NotchPermissionPage.title(item))
        .accessibilityValue(statusText(item, state))
    }

    // MARK: The rest

    private func row(_ item: Item) -> some View {
        let state = access.state(item)
        let action = self.action(item)
        return VStack(alignment: .leading, spacing: 6) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .center, spacing: 10) {
                    rowLabel(item, state)
                    Spacer(minLength: 8)
                    rowAction(item, action)
                }
                VStack(alignment: .leading, spacing: 8) {
                    rowLabel(item, state)
                    rowAction(item, action).padding(.leading, 28)
                }
            }
            // Every one says what it is for: a list of switch names can't be chosen from.
            Text(NotchPermissionPage.purpose(item)).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 28)
            let next = self.next(item)
            if action != nil || item == .contacts, !next.isEmpty, needsSaying(item, state) {
                Text(next).font(.wisp(.footnote)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 28)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 14).padding(.vertical, 10)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(NotchPermissionPage.title(item))
        .accessibilityValue(statusText(item, state))
    }

    @ViewBuilder private func focusAction(_ item: Item, _ action: NotchPermissionPage.Action?) -> some View {
        if let action {
            primaryButton(NotchPermissionPage.actionTitle(action, asking: access.asking.contains(item))) { page.act(item) }
                .keyboardShortcut(.defaultAction)
                .disabled(access.asking.contains(item))
                .accessibilityLabel(actionLabel(action, item))
        }
    }

    private func rowLabel(_ item: Item, _ state: FirstRunPermissions.State) -> some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: NotchPermissionPage.symbol(item)).font(.wisp(.body))
                .foregroundStyle(Theme.textMuted).frame(width: 18).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(NotchPermissionPage.title(item)).font(.wisp(.reading, weight: .medium))
                    .foregroundStyle(Theme.textPrimary)
                status(item, state)
            }
            .fixedSize()
            .help(NotchPermissionPage.purpose(item))
        }
    }

    @ViewBuilder private func rowAction(_ item: Item, _ action: NotchPermissionPage.Action?) -> some View {
        if let action {
            NotchSettingsChrome.pill(NotchPermissionPage.actionTitle(action, asking: access.asking.contains(item)),
                                     primary: true) { page.act(item) }
                .disabled(access.asking.contains(item))
                .accessibilityLabel(actionLabel(action, item))
        }
    }

    /// In the list, the next step shows only where it changes what the person
    /// does; the usual "macOS asks" would repeat under every row.
    private func needsSaying(_ item: Item, _ state: FirstRunPermissions.State) -> Bool {
        access.waiting.contains(item) || access.resetItems.contains(item) || access.unanswered[item] == true
            || contactsWait(item) || state == .notAllowed || item == .fullDiskAccess
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

    /// The state in words, with a mark, so color never carries it alone.
    private func status(_ item: Item, _ state: FirstRunPermissions.State) -> some View {
        HStack(spacing: 4) {
            if state == .allowed { Image(systemName: "checkmark").accessibilityHidden(true) }
            if state == .notAllowed { Image(systemName: "exclamationmark.circle").accessibilityHidden(true) }
            Text(statusText(item, state))
        }
        .fixedSize()
        .font(.wisp(.footnote, weight: state == .allowed ? .medium : .regular))
        .foregroundStyle(state == .allowed ? Theme.good : state == .notAllowed ? Theme.warn : Theme.textMuted)
    }

    private func actionLabel(_ action: NotchPermissionPage.Action, _ item: Item) -> String {
        let title = NotchPermissionPage.title(item)
        switch action {
        case .allow: return "Allow \(title)"
        case .askAgain: return "Ask again for \(title)"
        case .openSettings: return "Open System Settings for \(title)"
        }
    }

    /// The Report a problem card's primary button.
    private func primaryButton(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.wisp(.body, weight: .semibold)).foregroundStyle(Color.black)
                .padding(.horizontal, 12).padding(.vertical, 5)
                .background(Capsule().fill(Color.white))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fixedSize()
    }

    /// VoiceOver hears the answer without hunting for it.
    private func announce(old: FirstRunPermissions.State?, new: FirstRunPermissions.State?) {
        guard let focus = page.focus, let new, old != nil, old != .checking, old != new, new != .checking else { return }
        let text = "\(NotchPermissionPage.title(focus)): \(FirstRunPermissions.label(focus, new))"
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue])
    }
}
