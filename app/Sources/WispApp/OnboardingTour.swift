import SwiftUI

// A short look at Brief, Day and Ask while the model downloads (How to use).
// Sample cards only, each marked "Example": nothing here reads the person's
// data, asks the service or the model, or can be clicked into doing anything.

enum OnboardingTour {
    struct Page: Equatable {
        let tab: String
        let line: String
    }

    /// One page per notch tab, in the notch's order of a morning.
    static let pages: [Page] = [
        Page(tab: "Brief", line: "Each morning Wisp gathers what needs you, and gets a reply ready for you to check."),
        Page(tab: "Day", line: "Your meetings and to-dos in time order, so you can see the shape of your day."),
        Page(tab: "Ask", line: "Ask in your own words. Wisp looks through your mail, calendar and notes to answer."),
    ]

    /// Where Next and Back lead, staying on the pages. Pure.
    static func step(_ page: Int, by delta: Int) -> Int {
        min(max(page + delta, 0), pages.count - 1)
    }

    /// "2 of 3", for the dots' accessibility value. Pure.
    static func position(_ page: Int) -> String { "\(page + 1) of \(pages.count)" }

    // The examples, kept here so the copy can be checked.
    static let briefMeta = "NEW · MAIL"
    static let briefTitle = "Reply to Sam about Friday's dinner"
    static let briefBody = "Sam asked if 7 still works. I wrote a short yes for you to read first."
    static let briefButton = "Review reply"
    static let dayRows: [(time: String, title: String)] = [
        ("9:30", "Team stand-up"),
        ("12:00", "Lunch with Priya"),
        ("Due 5 PM", "Send the budget draft"),
        ("Anytime", "Call the dentist"),
    ]
    static let askQuestion = "What did Sam say about Friday?"
    static let askAnswer = "Sam asked whether dinner at 7 still works, and said they'll book the table once you say yes."
}

struct OnboardingTourView: View {
    @State private var page = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let current = OnboardingTour.pages[page]
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text("A quick look").font(.system(size: 13, weight: .medium))
                OnboardingPill(text: "Example")
                Spacer()
                dots
            }
            Text(current.tab).font(.system(size: 12, weight: .semibold)).foregroundStyle(OnboardingPalette.lilac)
                .accessibilityAddTraits(.isHeader)
            Text(current.line).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Group {
                switch page {
                case 0: brief
                case 1: day
                default: ask
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .transition(reduceMotion ? .opacity : .opacity.combined(with: .move(edge: .trailing)))
            .id(page)
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Example \(current.tab)")
            HStack {
                Button("Back") { turn(-1) }
                    .buttonStyle(FirstRunButtonStyle())
                    .disabled(page == 0)
                Spacer()
                Button(page == OnboardingTour.pages.count - 1 ? "Start over" : "Next") {
                    turn(page == OnboardingTour.pages.count - 1 ? -page : 1)
                }
                .buttonStyle(FirstRunButtonStyle())
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.white.opacity(0.03)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.hairline))
        .padding(.bottom, 14)
    }

    private func turn(_ delta: Int) {
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.2)) { page = OnboardingTour.step(page, by: delta) }
    }

    private var dots: some View {
        HStack(spacing: 5) {
            ForEach(OnboardingTour.pages.indices, id: \.self) { i in
                Circle().fill(i == page ? Theme.textPrimary : Theme.textMuted.opacity(0.5)).frame(width: 6, height: 6)
            }
        }
        .accessibilityElement()
        .accessibilityLabel("Page")
        .accessibilityValue(OnboardingTour.position(page))
    }

    /// Notch-dark, the way the open notch draws its cards.
    private func sample<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6, content: content)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color.black.opacity(0.55)))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.hairline))
    }

    private var brief: some View {
        sample {
            Text(OnboardingTour.briefMeta).font(.system(size: 10.5, weight: .medium, design: .monospaced))
                .foregroundStyle(Theme.textMuted)
            Text(OnboardingTour.briefTitle).font(.system(size: 14, weight: .semibold))
            Text(OnboardingTour.briefBody).font(.system(size: 12)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(OnboardingTour.briefButton).font(.system(size: 12, weight: .medium)).foregroundStyle(.black)
                .padding(.horizontal, 12).padding(.vertical, 5)
                .background(Capsule().fill(Color.white.opacity(0.9)))
                .padding(.top, 2)
        }
    }

    private var day: some View {
        sample {
            ForEach(OnboardingTour.dayRows, id: \.title) { row in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(row.time).font(.system(size: 11.5).monospacedDigit()).foregroundStyle(Theme.textMuted)
                        .frame(width: 64, alignment: .leading)
                    Text(row.title).font(.system(size: 12.5))
                }
            }
        }
    }

    private var ask: some View {
        sample {
            HStack {
                Spacer(minLength: 40)
                Text(OnboardingTour.askQuestion).font(.system(size: 12.5))
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Theme.chipFill))
            }
            Text(OnboardingTour.askAnswer).font(.system(size: 12.5)).foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
