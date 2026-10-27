import AppKit
import SwiftUI

// The onboarding window's shared pieces (cards, headings, its backdrop), its
// Google step, and the two drawings on its last step: where the notch is, and
// the notch opened on its three tabs. Drawn in SwiftUI shapes, no bitmaps, and
// the screen around the notch follows light and dark (the notch itself is
// black in both, as the hardware is).

enum OnboardingPalette {
    static let violet = Color(red: 0.43, green: 0.42, blue: 0.94)
    static let lilac = Color(red: 0.73, green: 0.74, blue: 0.96)
}

/// Dark and frosted: the window's own material, with a faint violet glow at
/// the top, the way the notch's aurora reads.
struct OnboardingBackdrop: View {
    var body: some View {
        ZStack {
            Color(white: 0.07)
            VisualEffectBlur().opacity(0.55)
            RadialGradient(colors: [OnboardingPalette.violet.opacity(0.16), .clear],
                           center: .top, startRadius: 0, endRadius: 420)
        }
        .ignoresSafeArea()
    }
}

/// The window's own behind-window blur, so the backdrop is frosted, not flat.
struct VisualEffectBlur: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .followsWindowActiveState
        return view
    }
    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {}
}

struct OnboardingHeading: View {
    let title: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 20, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
            Text(subtitle).font(.system(size: 13)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 18)
    }
}

struct OnboardingCard<Trailing: View>: View {
    let icon: String
    let title: String
    let detail: String
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon).font(.system(size: 15))
                .foregroundStyle(OnboardingPalette.lilac)
                .frame(width: 30, height: 30)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.chipFill))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .medium))
                Text(detail).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            trailing()
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.hairline))
        .padding(.bottom, 9)
    }
}

struct OnboardingPill: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 11)).foregroundStyle(Theme.textSecondary)
            .padding(.horizontal, 9).padding(.vertical, 3)
            .background(Capsule().fill(Theme.chipFill))
            .overlay(Capsule().stroke(Theme.chipStroke))
    }
}

// MARK: - Google

/// Step 3: connect Google, optional. The same sign-in as Settings ▸ Google
/// (GoogleAccountModel.connect with Wisp's built-in client), run from here: the
/// browser opens Google's consent page, this says "Waiting for Google…", and
/// then shows the account with what it shared. More than one account is fine.
struct OnboardingGoogleStep: View {
    @ObservedObject var model: GoogleAccountModel
    /// Off in NotchQA's renders, which must not reach the service.
    var refreshes = true

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            OnboardingHeading(title: "Connect Google",
                              subtitle: "Optional. Brief reads your Gmail and Google Calendar, read-only, and Ask can look "
                                  + "in Drive, Docs and Sheets. Wisp builds your to-dos on this Mac and asks before anything changes.")
            ForEach(model.status.accounts) { account in accountCard(account) }
            if model.authorizing {
                OnboardingCard(icon: "globe", title: "Waiting for Google…",
                               detail: "Finish signing in in your browser, then come back here.") {
                    ProgressView().controlSize(.small)
                }
                .accessibilityElement(children: .combine)
            } else {
                connectRow
            }
            if model.message == GoogleAccountModel.serviceDown {
                // The service didn't answer: nothing about the sign-in itself, so
                // the action reads again instead of starting a new sign-in.
                noticeRow(OnboardingGoogleCopy.serviceDown, button: "Check again") { Task { await model.refresh() } }
            } else if !model.authorizing, let problem {
                noticeRow(problem, button: "Try again") { model.connect() }
            }
        }
        .task {
            guard refreshes else { return }
            await model.refresh()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard !Task.isCancelled else { return }
                if !model.authorizing { await model.refresh() }
            }
        }
    }

    /// The last sign-in's trouble, when there is one worth showing.
    private var problem: String? { OnboardingGoogleCopy.signInProblem(model.message) }

    @ViewBuilder private var connectRow: some View {
        if model.loaded && !model.canConnect {
            OnboardingCard(icon: "person.crop.circle.badge.questionmark", title: "Google sign-in isn't built into this copy",
                           detail: "You can add your own Google sign-in in Settings ▸ Google later. Skip this step for now.") {
                EmptyView()
            }
        } else {
            let first = model.status.accounts.isEmpty
            OnboardingCard(icon: first ? "person.crop.circle.badge.plus" : "plus.circle",
                           title: first ? "Google account" : "Add another account",
                           detail: GoogleConsentCopy.notice(model.status).isEmpty
                               ? "Opens Google's sign-in page in your browser."
                               : "Opens Google's sign-in page in your browser. " + GoogleConsentCopy.notice(model.status)) {
                Button(first ? "Connect Google" : "Add account") { model.connect() }
                    .buttonStyle(FirstRunButtonStyle(primary: first))
                    .disabled(!model.loaded || model.authorizing)
            }
        }
    }

    private func noticeRow(_ text: String, button: String, action: @escaping () -> Void) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.circle").foregroundStyle(Color.orange).accessibilityHidden(true)
            Text(text).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            Button(button, action: action).buttonStyle(FirstRunButtonStyle())
                .disabled(model.authorizing)
        }
        .padding(.top, 2).padding(.bottom, 9)
    }

    private func accountCard(_ account: GoogleStatus.Account) -> some View {
        let tools = GoogleGrantNames.split(calendar: account.calendar, workspaceRequested: account.workspaceRequested,
                                           declinedScopes: account.workspaceDeclined)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Image(systemName: account.state == .connected ? "checkmark.circle.fill" : "exclamationmark.circle")
                    .font(.system(size: 15))
                    .foregroundStyle(account.state == .connected ? Color.green.opacity(0.85) : Color.orange)
                    .frame(width: 30, height: 30)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(account.id).font(.system(size: 13, weight: .medium))
                    Text(account.statusLine).font(.system(size: 12)).foregroundStyle(Theme.textMuted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                if let title = account.reconnectTitle {
                    Button(title) { model.connect(reconnect: account.id) }
                        .buttonStyle(FirstRunButtonStyle())
                        .disabled(model.authorizing)
                }
            }
            toolLine("Shared", tools.granted, icon: "checkmark", color: Color.green.opacity(0.85))
            if !tools.declined.isEmpty {
                toolLine("Not shared", tools.declined, icon: "minus", color: Theme.textMuted)
                Text("To add them, sign in again and tick their boxes on Google's screen.")
                    .font(.system(size: 11)).foregroundStyle(Theme.textMuted)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 11)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.white.opacity(0.04)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.hairline))
        .padding(.bottom, 9)
        .accessibilityElement(children: .contain)
    }

    private func toolLine(_ label: String, _ names: [String], icon: String, color: Color) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Image(systemName: icon).font(.system(size: 9, weight: .bold)).foregroundStyle(color).accessibilityHidden(true)
            Text(label + ": ").font(.system(size: 11.5, weight: .medium)).foregroundStyle(Theme.textSecondary)
                + Text(names.joined(separator: ", ")).font(.system(size: 11.5)).foregroundStyle(Theme.textSecondary)
        }
        .padding(.leading, 42)
    }
}

/// What the Google step says under its rows. Pure.
enum OnboardingGoogleCopy {
    static let serviceDown = "Wisp's service didn't answer just now, so this can't show your accounts yet."

    /// A sign-in's outcome worth showing as a problem, in plain words; nil for
    /// none (success, the "finish in your browser" hint, or a service outage,
    /// which is not a sign-in problem).
    static func signInProblem(_ message: String) -> String? {
        let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text != "Account connected.", text != "Finish signing in with Google in your browser.",
              text != GoogleAccountModel.serviceDown else { return nil }
        return GoogleGrantNames.gmailNotGranted(text) ? GoogleGrantNames.gmailNotGrantedMessage : text
    }
}

// MARK: - Where the notch is

/// The top of a Mac screen: the menu bar, Wisp in the middle (the camera notch,
/// or on a Mac without one the top-centre spot Wisp opens from), and Wisp's
/// menu-bar icon on the right, each marked. Light or dark like the person's Mac.
struct NotchLocationIllustration: View {
    let style: SurfaceLayout.Style
    @Environment(\.colorScheme) private var scheme

    private var dark: Bool { scheme == .dark }
    private var wallpaper: [Color] {
        dark ? [Color(red: 0.16, green: 0.15, blue: 0.30), Color(red: 0.08, green: 0.09, blue: 0.16)]
             : [Color(red: 0.78, green: 0.82, blue: 0.97), Color(red: 0.93, green: 0.90, blue: 0.98)]
    }
    private var menuBar: Color { dark ? Color.black.opacity(0.35) : Color.white.opacity(0.55) }
    private var ink: Color { dark ? Color.white.opacity(0.78) : Color.black.opacity(0.72) }
    private var faint: Color { dark ? Color.white.opacity(0.32) : Color.black.opacity(0.28) }
    private var mark: Color { dark ? OnboardingPalette.lilac : OnboardingPalette.violet }

    private let barHeight: CGFloat = 22

    var body: some View {
        GeometryReader { _ in
            ZStack(alignment: .top) {
                LinearGradient(colors: wallpaper, startPoint: .top, endPoint: .bottom)
                menuBarRow
                wispSpot.frame(maxWidth: .infinity)
                // The marker under Wisp's spot.
                VStack(spacing: 3) {
                    Image(systemName: "arrow.up").font(.system(size: 11, weight: .bold))
                    Text(FirstRunSetupView.openHint(style)).font(.system(size: 12, weight: .semibold))
                }
                .foregroundStyle(mark)
                .padding(.top, barHeight + (style == .floating ? 34 : 18))
                .frame(maxWidth: .infinity)
            }
        }
        .frame(height: 118)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(dark ? Theme.hairline : Color.black.opacity(0.12)))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        switch style {
        case .notch: return "The top of your screen: Wisp lives in the camera notch at the top centre, and has an icon in the menu bar."
        case .menuBar: return "The top of your screen: Wisp opens from the top centre when you push the pointer there, and has an icon in the menu bar."
        case .floating: return "The top of your screen: Wisp is the small bar at the top centre, and has an icon in the menu bar."
        }
    }

    private var menuBarRow: some View {
        HStack(spacing: 12) {
            Image(systemName: "apple.logo").font(.system(size: 11, weight: .semibold)).foregroundStyle(ink)
            Text("Finder").font(.system(size: 11, weight: .bold)).foregroundStyle(ink)
            ForEach(["File", "Edit", "View"], id: \.self) { Text($0).font(.system(size: 11)).foregroundStyle(ink) }
            Spacer()
            // Wisp's own icon: the orb, ringed.
            Circle().fill(RadialGradient(colors: [ink, ink.opacity(0.4)], center: .center, startRadius: 0, endRadius: 5))
                .frame(width: 9, height: 9)
                .padding(4)
                .overlay(Circle().stroke(mark, lineWidth: 1.5))
                // The marker under it, hanging below the menu bar.
                .overlay(alignment: .top) {
                    VStack(spacing: 2) {
                        Image(systemName: "arrow.up").font(.system(size: 9, weight: .bold))
                        Text("Menu bar icon").font(.system(size: 10.5, weight: .medium))
                    }
                    .foregroundStyle(ink)
                    .fixedSize()
                    .offset(y: 24)
                }
                .padding(.trailing, 6)
            Image(systemName: "wifi").font(.system(size: 10.5)).foregroundStyle(ink)
            Image(systemName: "battery.75percent").font(.system(size: 11)).foregroundStyle(ink)
            Text("9:41").font(.system(size: 11, weight: .medium)).foregroundStyle(ink)
        }
        .padding(.horizontal, 12)
        .frame(height: barHeight)
        .frame(maxWidth: .infinity)
        .background(menuBar)
    }

    @ViewBuilder private var wispSpot: some View {
        switch style {
        case .notch:
            UnevenRoundedRectangle(bottomLeadingRadius: 9, bottomTrailingRadius: 9)
                .fill(Color.black)
                .frame(width: 112, height: barHeight + 4)
                .overlay(alignment: .bottom) {
                    // The camera.
                    Circle().fill(Color(white: 0.12)).frame(width: 5, height: 5).padding(.bottom, 9)
                }
                .padding(3)
                .overlay(UnevenRoundedRectangle(bottomLeadingRadius: 11, bottomTrailingRadius: 11)
                    .stroke(mark, lineWidth: 2))
        case .menuBar:
            // No notch: the top-centre spot Wisp opens from, drawn as the shape it opens into.
            UnevenRoundedRectangle(bottomLeadingRadius: 9, bottomTrailingRadius: 9)
                .fill(mark.opacity(0.14))
                .frame(width: 112, height: barHeight + 4)
                .overlay(UnevenRoundedRectangle(bottomLeadingRadius: 9, bottomTrailingRadius: 9)
                    .stroke(mark, style: StrokeStyle(lineWidth: 1.5, dash: [4, 3])))
        case .floating:
            Capsule().fill(Color.black)
                .frame(width: 96, height: 18)
                .padding(3)
                .overlay(Capsule().stroke(mark, lineWidth: 2))
                .padding(.top, barHeight + 4)
        }
    }
}

// MARK: - The notch, open

/// The notch opened: its black shape fused to the top edge, the orb, and the
/// tabs in their real order (Ask · Brief · Day), each with its one line.
struct OpenNotchIllustration: View {
    let tabs: [(tab: String, line: String)]
    @State private var breathe = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Circle().fill(Theme.orb).frame(width: 16, height: 16)
                    .scaleEffect(breathe ? 1.08 : 0.95)
                    .animation(reduceMotion ? nil : .easeInOut(duration: 4).repeatForever(autoreverses: true), value: breathe)
                ForEach(Array(tabs.enumerated()), id: \.offset) { index, tab in
                    Text(tab.tab).font(.system(size: 11.5, weight: index == 0 ? .semibold : .regular))
                        .foregroundStyle(index == 0 ? Theme.textPrimary : Theme.textSecondary)
                        .padding(.horizontal, 10).padding(.vertical, 3)
                        .background(Capsule().fill(index == 0 ? Theme.chipFill : .clear))
                        .overlay(Capsule().stroke(index == 0 ? Theme.chipStroke : .clear))
                }
                Spacer()
            }
            ForEach(Array(tabs.enumerated()), id: \.offset) { _, tab in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(tab.tab).font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                        .frame(width: 44, alignment: .leading)
                    Text(tab.line).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .padding(.horizontal, 18).padding(.top, 14).padding(.bottom, 16)
        .background(UnevenRoundedRectangle(bottomLeadingRadius: 22, bottomTrailingRadius: 22).fill(Color.black))
        .overlay(UnevenRoundedRectangle(bottomLeadingRadius: 22, bottomTrailingRadius: 22)
            .stroke(Color.white.opacity(0.10)))
        .onAppear { if !reduceMotion { breathe = true } }
    }
}
