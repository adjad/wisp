import SwiftUI
import AppKit

// MARK: - Shared pieces

/// Small looks shared by the Study tab, its players and its connector card.
/// They follow the notch's own chrome (NotchChrome, NotchSettingsChrome): black,
/// white ink at graded opacities, 8 pt cards, hairlines, tiny monospaced caps.
enum StudyChrome {
    /// A section's tiny caps label ("DUE", "YOUR LIBRARY").
    static func label(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.wisp(size: 10, design: .monospaced)).tracking(0.6)
            .foregroundStyle(Theme.textMuted)
            .accessibilityAddTraits(.isHeader)
    }

    /// A rounded card raised just off the black.
    static func card<Content: View>(padding: CGFloat = 12, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) { content() }
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(Theme.raised))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline, lineWidth: 1))
    }

    /// The tab's pill button: white fill for the one main action, a quiet chip otherwise.
    static func pill(_ title: String, icon: String? = nil, primary: Bool = false,
                     action: @escaping () -> Void) -> some View {
        StudyPill(title: title, icon: icon, primary: primary, action: action)
    }
}

/// A capsule button with a hover lift, at least 24 pt tall.
struct StudyPill: View {
    let title: String
    var icon: String?
    var primary = false
    let action: () -> Void
    @State private var hovering = false
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                if let icon { Image(systemName: icon).font(.wisp(size: 10.5, weight: .semibold)) }
                Text(title).font(.wisp(.footnote, weight: .medium))
            }
            .foregroundStyle(primary ? Color.black : Theme.textPrimary)
            .padding(.horizontal, 12)
            .frame(minHeight: 24)
            .background(Capsule().fill(fill))
            .overlay(Capsule().stroke(primary ? Color.clear : Theme.chipStroke, lineWidth: 1))
            .contentShape(Capsule())
            .opacity(isEnabled ? 1 : 0.4)
        }
        .buttonStyle(.plain)
        .fixedSize()
        .onHover { hovering = $0 }
    }

    private var fill: Color {
        if primary { return hovering ? Color(white: 0.88) : Color.white }
        return hovering ? Color.white.opacity(0.14) : Theme.chipFill
    }
}

/// Text with `backtick` spans in a monospaced face. Nothing fancier.
enum StudyInline {
    private typealias FontKey = AttributeScopes.SwiftUIAttributes.FontAttribute
    private typealias BackgroundKey = AttributeScopes.SwiftUIAttributes.BackgroundColorAttribute

    static func attributed(_ source: String, size: CGFloat, weight: Font.Weight = .regular) -> AttributedString {
        let parts = source.components(separatedBy: "`")
        // No backticks, or an unmatched one: the text as it is.
        guard parts.count > 1, parts.count % 2 == 1 else {
            var plain = AttributedString(source)
            plain[FontKey.self] = Font.wisp(size: size, weight: weight)
            return plain
        }
        var result = AttributedString()
        for (index, part) in parts.enumerated() where !part.isEmpty {
            var piece = AttributedString(part)
            if index % 2 == 1 {
                piece[FontKey.self] = Font.wisp(size: size * 0.94, weight: weight, design: .monospaced)
                piece[BackgroundKey.self] = Color.white.opacity(0.08)
            } else {
                piece[FontKey.self] = Font.wisp(size: size, weight: weight)
            }
            result.append(piece)
        }
        return result
    }

    static func text(_ source: String, size: CGFloat, weight: Font.Weight = .regular) -> Text {
        Text(attributed(source, size: size, weight: weight))
    }

    /// Whether an example reads as code: backticks, or an indented line after a newline.
    static func looksLikeCode(_ text: String) -> Bool {
        if text.contains("`") { return true }
        return text.contains("\n    ") || text.contains("\n\t") || text.contains("\n  ")
    }
}

/// How a choice row looks once the question is answered.
enum StudyChoiceLook: Equatable {
    case open, right, wrong, dimmed
}

/// One answer choice: a letter badge and the text, full width.
struct StudyChoiceRow: View {
    let index: Int
    let text: String
    let look: StudyChoiceLook
    let action: () -> Void
    @State private var hovering = false

    private static let letters = ["A", "B", "C", "D", "E", "F"]

    private var letter: String { index < Self.letters.count ? Self.letters[index] : String(index + 1) }

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: 10) {
                badge
                StudyInline.text(text, size: 13)
                    .padding(.top, 2)
                    .foregroundStyle(look == .dimmed ? Theme.textMuted : Theme.textPrimary)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                trailingMark.padding(.top, 4)
            }
            .padding(.horizontal, 10).padding(.vertical, 8)
            .frame(maxWidth: .infinity, minHeight: 32, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 8).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(stroke, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(look != .open)
        .onHover { hovering = $0 && look == .open }
        .accessibilityLabel("\(letter). \(text)")
        .accessibilityValue(accessibilityValue)
    }

    private var badge: some View {
        Text(letter)
            .font(.wisp(size: 10, weight: .semibold, design: .monospaced))
            .foregroundStyle(look == .right ? Color.black : Theme.textSecondary)
            .frame(width: 20, height: 20)
            .background(Circle().fill(look == .right ? Color.white : Theme.chipFill))
            .overlay(Circle().stroke(badgeStroke, lineWidth: 1))
            .accessibilityHidden(true)
    }

    @ViewBuilder private var trailingMark: some View {
        switch look {
        case .right:
            Image(systemName: "checkmark").font(.wisp(size: 11, weight: .bold)).foregroundStyle(Theme.good)
                .accessibilityHidden(true)
        case .wrong:
            Image(systemName: "xmark").font(.wisp(size: 11, weight: .bold)).foregroundStyle(Theme.warn)
                .accessibilityHidden(true)
        default:
            EmptyView()
        }
    }

    private var fill: Color {
        switch look {
        case .right: return Color.white.opacity(0.12)
        case .wrong: return Theme.warn.opacity(0.12)
        case .dimmed: return Color.clear
        case .open: return hovering ? Color.white.opacity(0.09) : Theme.chipFill
        }
    }

    private var stroke: Color {
        switch look {
        case .right: return Color.white.opacity(0.55)
        case .wrong: return Theme.warn.opacity(0.6)
        case .dimmed: return Theme.hairline
        case .open: return hovering ? Theme.chipStroke.opacity(1.6) : Theme.chipStroke
        }
    }

    private var badgeStroke: Color {
        switch look {
        case .right: return Color.clear
        case .wrong: return Theme.warn.opacity(0.7)
        default: return Theme.chipStroke
        }
    }

    private var accessibilityValue: String {
        switch look {
        case .right: return "Right answer"
        case .wrong: return "Your answer, not right"
        default: return ""
        }
    }
}

/// A thin progress bar.
struct StudyProgressBar: View {
    let fraction: Double
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.08))
                Capsule().fill(Color.white.opacity(0.7))
                    .frame(width: max(4, geo.size.width * CGFloat(min(max(fraction, 0), 1))))
            }
        }
        .frame(height: 3)
        .animation(reduceMotion ? nil : Theme.Motion.panel, value: fraction)
        .accessibilityHidden(true)
    }
}

/// A drawn score ring: the share right, as a white arc that fills on appear.
struct StudyScoreRing: View {
    let correct: Int
    let total: Int
    @State private var shown: Double = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var fraction: Double { total > 0 ? Double(correct) / Double(total) : 0 }

    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.08), lineWidth: 6)
            Circle()
                .trim(from: 0, to: CGFloat(shown))
                .stroke(Color.white.opacity(0.9), style: StrokeStyle(lineWidth: 6, lineCap: .round))
                .rotationEffect(.degrees(-90))
            VStack(spacing: 0) {
                Text("\(correct)").font(.wisp(size: 22, weight: .semibold)).monospacedDigit()
                    .foregroundStyle(Theme.textPrimary)
                Text("of \(total)").font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
            }
        }
        .frame(width: 78, height: 78)
        .onAppear {
            if reduceMotion {
                shown = fraction
            } else {
                withAnimation(.easeOut(duration: 0.9).delay(0.1)) { shown = fraction }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(correct) of \(total) right")
    }
}

/// The kind words under a score, by band.
enum StudyScoreWords {
    static func line(correct: Int, total: Int) -> String {
        let share = total > 0 ? Double(correct) / Double(total) : 0
        if share >= 0.85 { return "Lovely work." }
        if share >= 0.6 { return "Good progress. A second pass will help." }
        return "A good start. Try the lesson, then come back."
    }
}

// MARK: - Slide line

/// "From Lecture 7 · p. 7": one muted line naming the slide a question came from.
struct StudySlideLine: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Image(systemName: "play.rectangle").font(.wisp(size: 9)).accessibilityHidden(true)
            Text(text).font(.wisp(.caption)).lineLimit(1).truncationMode(.middle)
        }
        .foregroundStyle(Theme.textMuted)
    }
}

// MARK: - Quiz

/// The practice-quiz player: one question at a time, choices as rows, the
/// reason after each answer, then a score ring and a review of every answer.
struct StudyQuizView: View {
    let quiz: StudyQuiz
    /// Right, total, and the 0-based indices of the questions answered wrong.
    let onFinish: (Int, Int, [Int]) -> Void
    /// "From Lecture 7 · p. 7" for a question made from a slide.
    let slideLabel: (StudySlideRef) -> String
    @State private var run: QuizRun
    @State private var reported = false
    @State private var reviewing = false
    @FocusState private var focused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(quiz: StudyQuiz, slideLabel: @escaping (StudySlideRef) -> String = { StudyText.slideLine($0, deck: nil) },
         onFinish: @escaping (Int, Int, [Int]) -> Void) {
        self.quiz = quiz
        self.onFinish = onFinish
        self.slideLabel = slideLabel
        _run = State(initialValue: QuizRun(quiz: quiz))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if run.finished {
                finishView.transition(.opacity)
            } else if let question = run.current {
                questionView(question).transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .animation(reduceMotion ? nil : Theme.Motion.panel, value: run)
        .animation(reduceMotion ? nil : Theme.Motion.panel, value: reviewing)
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onKeyPress(phases: .down) { press in handleKey(press) }
        .onAppear { focused = true }
        .onChange(of: run.finished) { _, done in
            guard done, !reported else { return }
            reported = true
            onFinish(run.correct, run.total, run.missed)
        }
    }

    // MARK: Question

    private func questionView(_ question: StudyQuestion) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            progressHeader
            StudyInline.text(question.prompt, size: 15, weight: .semibold)
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            choiceList(question)
            if run.chosen != nil {
                whyBlock(question).transition(.opacity.combined(with: .move(edge: .top)))
                HStack {
                    Spacer()
                    StudyChrome.pill(run.isLast ? StudyText.finishButton : StudyText.nextButton,
                                     icon: run.isLast ? "checkmark" : "arrow.right", primary: true) {
                        advance()
                    }
                    .help("Return or Space")
                }
            }
        }
    }

    private var progressHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(run.progressText)
                    .font(.wisp(size: 10, design: .monospaced)).tracking(0.6)
                    .foregroundStyle(Theme.textMuted)
                Spacer()
                Text("\(run.correct) right").font(.wisp(.caption)).monospacedDigit()
                    .foregroundStyle(Theme.textMuted)
            }
            StudyProgressBar(fraction: run.total > 0 ? Double(run.index) / Double(run.total) : 0)
        }
    }

    private func choiceList(_ question: StudyQuestion) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(question.choices.enumerated()), id: \.offset) { pair in
                StudyChoiceRow(index: pair.offset, text: pair.element,
                               look: look(for: pair.offset, in: question)) {
                    choose(pair.offset)
                }
            }
        }
    }

    private func look(for index: Int, in question: StudyQuestion) -> StudyChoiceLook {
        guard let chosen = run.chosen else { return .open }
        if index == question.answer { return .right }
        if index == chosen { return .wrong }
        return .dimmed
    }

    private func whyBlock(_ question: StudyQuestion) -> some View {
        let right = run.chosen == question.answer
        return HStack(alignment: .top, spacing: 8) {
            Image(systemName: right ? "checkmark.circle" : "lightbulb")
                .font(.wisp(size: 12)).foregroundStyle(right ? Theme.textSecondary : Theme.warn)
                .padding(.top, 1)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                Text(right ? "That's right." : "Not quite.")
                    .font(.wisp(.footnote, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                if !question.why.isEmpty {
                    StudyInline.text(question.why, size: 12)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                if let slide = question.slide { StudySlideLine(text: slideLabel(slide)) }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline, lineWidth: 1))
        .accessibilityElement(children: .combine)
    }

    // MARK: Finish

    private var finishView: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 16) {
                StudyScoreRing(correct: run.correct, total: run.total)
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(run.correct) of \(run.total)")
                        .font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    Text(StudyScoreWords.line(correct: run.correct, total: run.total))
                        .font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            HStack(spacing: 8) {
                StudyChrome.pill(StudyText.tryAgainButton, icon: "arrow.counterclockwise", primary: true) { restart() }
                StudyChrome.pill(reviewing ? "Hide answers" : "Review answers",
                                 icon: reviewing ? "chevron.up" : "list.bullet") {
                    reviewing.toggle()
                }
            }
            if reviewing { reviewList.transition(.opacity) }
        }
    }

    private var reviewList: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(quiz.questions.enumerated()), id: \.offset) { pair in
                reviewRow(number: pair.offset + 1, question: pair.element)
            }
        }
        .padding(.top, 4)
    }

    private func reviewRow(number: Int, question: StudyQuestion) -> some View {
        let answer = question.choices.indices.contains(question.answer) ? question.choices[question.answer] : ""
        return VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("\(number).").font(.wisp(.footnote, design: .monospaced)).foregroundStyle(Theme.textMuted)
                StudyInline.text(question.prompt, size: 12.5, weight: .medium)
                    .foregroundStyle(Theme.textPrimary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "checkmark").font(.wisp(size: 9, weight: .bold))
                    .foregroundStyle(Theme.textSecondary).accessibilityHidden(true)
                StudyInline.text(answer, size: 12)
                    .foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.leading, 18)
            if let slide = question.slide {
                StudySlideLine(text: slideLabel(slide)).padding(.leading, 18)
            }
        }
        .textSelection(.enabled)
        .accessibilityElement(children: .combine)
    }

    // MARK: Actions

    private func choose(_ index: Int) {
        guard run.chosen == nil, !run.finished else { return }
        run.choose(index)
    }

    private func advance() {
        guard run.chosen != nil else { return }
        run.next()
    }

    private func restart() {
        reviewing = false
        reported = false
        run = QuizRun(quiz: quiz)
    }

    /// 1–5 pick a choice; Return or Space goes on once answered. Anything with
    /// a modifier is left alone, so the panel's ⌘ shortcuts keep working.
    private func handleKey(_ press: KeyPress) -> KeyPress.Result {
        guard press.modifiers.isDisjoint(with: [.command, .control, .option]) else { return .ignored }
        guard !run.finished, let question = run.current else { return .ignored }
        if press.key == .return || press.key == .space {
            guard run.chosen != nil else { return .ignored }
            advance()
            return .handled
        }
        if let digit = Int(press.characters), digit >= 1, digit <= min(5, question.choices.count) {
            guard run.chosen == nil else { return .handled }
            choose(digit - 1)
            return .handled
        }
        return .ignored
    }
}

// MARK: - Flash cards

/// Flash cards: a large card that turns over, then Again or Got it. Cards
/// marked Again come back until every card is known.
struct StudyFlashcardsView: View {
    let deck: StudyDeck
    let onFinish: (Int, Int) -> Void
    @State private var run: FlashRun
    /// The card's turn in degrees: 0 shows the front, 180 the back.
    @State private var turn: Double = 0
    @State private var reported = false
    @State private var hovering = false
    @FocusState private var focused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(deck: StudyDeck, onFinish: @escaping (Int, Int) -> Void) {
        self.deck = deck
        self.onFinish = onFinish
        _run = State(initialValue: FlashRun(deck: deck))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if run.finished {
                finishView.transition(.opacity)
            } else if let card = run.current {
                header
                cardFace(card)
                controls
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .animation(reduceMotion ? nil : Theme.Motion.panel, value: run.finished)
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onKeyPress(phases: .down) { press in handleKey(press) }
        .onAppear { focused = true }
        .onChange(of: run.finished) { _, done in
            guard done, !reported else { return }
            reported = true
            onFinish(run.firstPassKnown, run.total)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("\(run.knownCount) of \(run.total) known")
                    .font(.wisp(size: 10, design: .monospaced)).tracking(0.6)
                    .foregroundStyle(Theme.textMuted)
                Spacer()
                Text(run.remaining == 1 ? "1 to go" : "\(run.remaining) to go")
                    .font(.wisp(.caption)).monospacedDigit().foregroundStyle(Theme.textMuted)
            }
            StudyProgressBar(fraction: run.total > 0 ? Double(run.knownCount) / Double(run.total) : 0)
        }
    }

    // MARK: The card

    private func cardFace(_ card: StudyCard) -> some View {
        Button(action: flip) {
            StudyFlipSurface(angle: turn, front: card.front, back: card.back,
                             hovering: hovering, flat: reduceMotion)
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(run.flipped ? "Show the front (Space)" : "Turn the card over (Space)")
        .accessibilityLabel(run.flipped ? "Back: \(card.back)" : "Front: \(card.front)")
        .accessibilityHint("Turns the card over")
    }

    private var controls: some View {
        HStack(spacing: 8) {
            if !run.flipped {
                Text("Tap the card or press Space to turn it over.")
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
            }
            Spacer()
            StudyChrome.pill(StudyText.againButton, icon: "arrow.uturn.backward") { mark(false) }
                .disabled(!run.flipped)
                .help("Show this card again later (1)")
            StudyChrome.pill(StudyText.knownButton, icon: "checkmark", primary: true) { mark(true) }
                .disabled(!run.flipped)
                .help("Mark this card known (2)")
        }
    }

    // MARK: Finish

    private var finishView: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 14) {
                StudyScoreRing(correct: run.firstPassKnown, total: run.total)
                VStack(alignment: .leading, spacing: 4) {
                    Text("All \(run.total) known")
                        .font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    Text(run.againCount == 0 ? StudyText.flashResult(run)
                         : (run.againCount == 1 ? "1 card took" : "\(run.againCount) cards took") + " another look. Lovely work.")
                        .font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            StudyChrome.pill("Go again", icon: "arrow.counterclockwise", primary: true) { restart() }
        }
    }

    // MARK: Actions

    private func flip() {
        let target: Double = run.flipped ? 0 : 180
        run.flip()
        if reduceMotion {
            withAnimation(.easeInOut(duration: 0.18)) { turn = target }
        } else {
            withAnimation(.spring(duration: 0.5, bounce: 0.15)) { turn = target }
        }
    }

    private func mark(_ known: Bool) {
        guard run.flipped else { return }
        // The next card starts face up, without turning back through the old one.
        var instant = Transaction()
        instant.disablesAnimations = true
        withTransaction(instant) {
            run.mark(known: known)
            turn = 0
        }
    }

    private func restart() {
        reported = false
        turn = 0
        run = FlashRun(deck: deck)
    }

    /// Space turns the card; 1 is Again and 2 is Got it once it is turned.
    private func handleKey(_ press: KeyPress) -> KeyPress.Result {
        guard press.modifiers.isDisjoint(with: [.command, .control, .option]) else { return .ignored }
        guard !run.finished, run.current != nil else { return .ignored }
        if press.key == .space || press.key == .return {
            flip()
            return .handled
        }
        if run.flipped && press.characters == "1" { mark(false); return .handled }
        if run.flipped && press.characters == "2" { mark(true); return .handled }
        return .ignored
    }
}

/// The card itself. Animatable, so the face swaps at the half turn while the
/// spring runs; flat (Reduce Motion) it cross-fades in place instead.
struct StudyFlipSurface: View, Animatable {
    var angle: Double
    let front: String
    let back: String
    let hovering: Bool
    let flat: Bool

    var animatableData: Double {
        get { angle }
        set { angle = newValue }
    }

    var body: some View {
        let progress = min(max(angle / 180, 0), 1)
        let frontOpacity: Double = flat ? 1 - progress : (angle < 90 ? 1 : 0)
        let backOpacity: Double = flat ? progress : (angle < 90 ? 0 : 1)
        return ZStack {
            side(front, caption: "Front", emphasis: true)
                .opacity(frontOpacity)
            side(back, caption: "Back", emphasis: false)
                .opacity(backOpacity)
                // Drawn turned, so it reads the right way round once the card has turned.
                .rotation3DEffect(.degrees(flat ? 0 : 180), axis: (x: 0, y: 1, z: 0))
        }
        .frame(maxWidth: .infinity, minHeight: 150)
        .background(RoundedRectangle(cornerRadius: 12).fill(hovering ? Color(white: 0.07) : Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.chipStroke, lineWidth: 1))
        .rotation3DEffect(.degrees(flat ? 0 : angle), axis: (x: 0, y: 1, z: 0), perspective: 0.45)
        .contentShape(Rectangle())
    }

    private func side(_ text: String, caption: String, emphasis: Bool) -> some View {
        VStack(spacing: 8) {
            Text(caption.uppercased())
                .font(.wisp(size: 9.5, design: .monospaced)).tracking(0.8)
                .foregroundStyle(Theme.textMuted)
            StudyInline.text(text, size: emphasis ? 17 : 14, weight: emphasis ? .semibold : .regular)
                .foregroundStyle(emphasis ? Theme.textPrimary : Theme.textSecondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 20).padding(.vertical, 16)
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Guide

/// A study guide to read: the summary, sections of points, key terms and
/// practice questions. All of it selectable.
struct StudyGuideView: View {
    let guide: StudyGuide

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !guide.summary.isEmpty { summary }
            ForEach(Array(guide.sections.enumerated()), id: \.offset) { pair in
                section(heading: pair.element.heading, points: pair.element.points)
            }
            if !guide.keyTerms.isEmpty { keyTerms }
            if !guide.practice.isEmpty { practice }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .textSelection(.enabled)
    }

    private var summary: some View {
        StudyInline.text(guide.summary, size: 14)
            .foregroundStyle(Theme.textPrimary)
            .lineSpacing(2)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.leading, 10)
            .overlay(alignment: .leading) { Rectangle().fill(Theme.chipStroke).frame(width: 2) }
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func section(heading: String, points: [String]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            StudyInline.text(heading, size: 13.5, weight: .semibold)
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
            ForEach(Array(points.enumerated()), id: \.offset) { pair in
                StudyBullet(text: pair.element)
            }
        }
    }

    private var keyTerms: some View {
        VStack(alignment: .leading, spacing: 6) {
            StudyChrome.label(StudyText.keyTerms)
            StudyChrome.card(padding: 10) {
                ForEach(Array(guide.keyTerms.enumerated()), id: \.offset) { pair in
                    termRow(term: pair.element.term, meaning: pair.element.meaning)
                }
            }
        }
    }

    private func termRow(term: String, meaning: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            StudyInline.text(term, size: 12.5, weight: .semibold)
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            StudyInline.text(meaning, size: 12)
                .foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var practice: some View {
        VStack(alignment: .leading, spacing: 4) {
            StudyChrome.label(StudyText.practice)
            ForEach(Array(guide.practice.enumerated()), id: \.offset) { pair in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("\(pair.offset + 1).")
                        .font(.wisp(.footnote, design: .monospaced)).foregroundStyle(Theme.textMuted)
                        .frame(minWidth: 18, alignment: .trailing)
                    StudyInline.text(pair.element, size: 12.5)
                        .foregroundStyle(Theme.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

/// One bullet point with inline code.
struct StudyBullet: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("•").font(.wisp(.body)).foregroundStyle(Theme.textMuted).accessibilityHidden(true)
            StudyInline.text(text, size: 12.5)
                .foregroundStyle(Theme.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

// MARK: - Lesson

/// A short lesson, one step at a time: the idea, an example, then a check
/// question answered with the quiz's choice rows.
struct StudyLessonView: View {
    let lesson: StudyLesson
    let onFinish: (Int, Int) -> Void
    /// "From Lecture 7 · p. 7" for a check made from a slide.
    let slideLabel: (StudySlideRef) -> String
    @State private var step = 0
    /// The choice picked on each step's check, by step.
    @State private var picks: [Int: Int] = [:]
    @State private var finished = false
    @State private var reported = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    init(lesson: StudyLesson, slideLabel: @escaping (StudySlideRef) -> String = { StudyText.slideLine($0, deck: nil) },
         onFinish: @escaping (Int, Int) -> Void) {
        self.lesson = lesson
        self.onFinish = onFinish
        self.slideLabel = slideLabel
    }

    private var steps: [StudyLessonStep] { lesson.steps }
    private var isLast: Bool { step >= steps.count - 1 }
    private var checksTotal: Int { steps.filter { $0.check != nil }.count }
    private var checksRight: Int {
        steps.indices.filter { index in
            guard let check = steps[index].check, let pick = picks[index] else { return false }
            return pick == check.answer
        }.count
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if finished {
                finishView.transition(.opacity)
            } else if steps.indices.contains(step) {
                stepView(steps[step]).id(step).transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .animation(reduceMotion ? nil : Theme.Motion.panel, value: step)
        .animation(reduceMotion ? nil : Theme.Motion.panel, value: finished)
        .animation(reduceMotion ? nil : Theme.Motion.panel, value: picks)
    }

    // MARK: A step

    private func stepView(_ current: StudyLessonStep) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            stepHeader
            StudyInline.text(current.title, size: 15, weight: .semibold)
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            StudyInline.text(current.explain, size: 13)
                .foregroundStyle(Theme.textSecondary)
                .lineSpacing(2)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if let example = current.example, !example.isEmpty {
                exampleBlock(example)
            }
            if let check = current.check { checkBlock(check) }
            navigation(current)
        }
    }

    private var stepHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Step \(step + 1) of \(steps.count)")
                .font(.wisp(size: 10, design: .monospaced)).tracking(0.6)
                .foregroundStyle(Theme.textMuted)
            StudyProgressBar(fraction: steps.isEmpty ? 0 : Double(step + 1) / Double(steps.count))
        }
    }

    @ViewBuilder private func exampleBlock(_ example: String) -> some View {
        let code = StudyInline.looksLikeCode(example)
        VStack(alignment: .leading, spacing: 6) {
            StudyChrome.label(StudyText.example)
            Group {
                if code {
                    Text(example.replacingOccurrences(of: "`", with: ""))
                        .font(.wisp(size: 12, design: .monospaced))
                } else {
                    StudyInline.text(example, size: 12.5)
                }
            }
            .foregroundStyle(Theme.textPrimary)
            .fixedSize(horizontal: false, vertical: true)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(code ? Color.white.opacity(0.06) : Theme.raised))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.hairline, lineWidth: 1))
    }

    private func checkBlock(_ check: StudyQuestion) -> some View {
        let pick = picks[step]
        return VStack(alignment: .leading, spacing: 8) {
            StudyChrome.label(StudyText.check)
            StudyInline.text(check.prompt, size: 13, weight: .medium)
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(Array(check.choices.enumerated()), id: \.offset) { pair in
                StudyChoiceRow(index: pair.offset, text: pair.element,
                               look: look(pair.offset, pick: pick, answer: check.answer)) {
                    if picks[step] == nil { picks[step] = pair.offset }
                }
            }
            if let pick, !check.why.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: pick == check.answer ? "checkmark.circle" : "lightbulb")
                        .font(.wisp(size: 12))
                        .foregroundStyle(pick == check.answer ? Theme.textSecondary : Theme.warn)
                        .accessibilityHidden(true)
                    StudyInline.text(check.why, size: 12)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .transition(.opacity)
            }
            if pick != nil, let slide = check.slide {
                StudySlideLine(text: slideLabel(slide)).transition(.opacity)
            }
        }
    }

    private func look(_ index: Int, pick: Int?, answer: Int) -> StudyChoiceLook {
        guard let pick else { return .open }
        if index == answer { return .right }
        if index == pick { return .wrong }
        return .dimmed
    }

    private func navigation(_ current: StudyLessonStep) -> some View {
        let waiting = current.check != nil && picks[step] == nil
        return HStack(spacing: 8) {
            if step > 0 {
                StudyChrome.pill("Back", icon: "chevron.left") { step -= 1 }
            }
            Spacer()
            if waiting {
                Text("Answer the check to go on")
                    .font(.wisp(.caption)).foregroundStyle(Theme.textMuted)
            }
            StudyChrome.pill(isLast ? "Finish" : StudyText.nextButton, icon: isLast ? "checkmark" : "chevron.right",
                             primary: true) { advance() }
                .disabled(waiting)
        }
    }

    // MARK: Finish

    private var finishView: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 14) {
                if checksTotal > 0 {
                    StudyScoreRing(correct: checksRight, total: checksTotal)
                } else {
                    Image(systemName: "checkmark.seal").font(.wisp(size: 34, weight: .light))
                        .foregroundStyle(Theme.textPrimary).frame(width: 78, height: 78)
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Lesson complete")
                        .font(.wisp(.headline, weight: .semibold)).foregroundStyle(Theme.textPrimary)
                    Text(finishLine)
                        .font(.wisp(.body)).foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            StudyChrome.pill("Start over", icon: "arrow.counterclockwise") { restart() }
        }
    }

    private var finishLine: String {
        guard checksTotal > 0 else { return "Nicely done. A practice quiz is a good next step." }
        let share = Double(checksRight) / Double(checksTotal)
        let words = share >= 0.85 ? "Lovely work." : share >= 0.6 ? "Good progress. The quiz will firm it up."
            : "A good start. Another read of the steps will help."
        return "\(checksRight) of \(checksTotal) checks right. " + words
    }

    // MARK: Actions

    private func advance() {
        if isLast {
            finished = true
            if !reported && checksTotal > 0 {
                reported = true
                onFinish(checksRight, checksTotal)
            }
        } else {
            step += 1
        }
    }

    private func restart() {
        picks = [:]
        step = 0
        finished = false
        reported = false
    }
}
