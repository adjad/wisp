import SwiftUI

/// Wisp's look: dark, soft aurora light behind frosted glass, and a breathing orb.
enum Theme {
    static let indigo = Color(red: 0.42, green: 0.40, blue: 0.98)
    static let teal = Color(red: 0.22, green: 0.82, blue: 0.80)
    static let violet = Color(red: 0.70, green: 0.42, blue: 0.95)
    static let night = Color(red: 0.03, green: 0.04, blue: 0.09)
    static let danger = Color(red: 1.0, green: 0.48, blue: 0.48)
}

struct AuroraBackground: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 20, paused: reduceMotion)) { timeline in
            let t = reduceMotion ? 0 : timeline.date.timeIntervalSinceReferenceDate
            GeometryReader { proxy in
                let size = proxy.size
                ZStack {
                    Theme.night
                    blob(Theme.indigo, size: size, x: 0.25 + 0.10 * sin(t / 9), y: 0.18 + 0.06 * cos(t / 11), scale: 0.95)
                    blob(Theme.teal, size: size, x: 0.80 + 0.08 * cos(t / 10), y: 0.30 + 0.08 * sin(t / 13), scale: 0.75)
                    blob(Theme.violet, size: size, x: 0.55 + 0.12 * sin(t / 14), y: 0.85 + 0.05 * cos(t / 8), scale: 0.9)
                }
            }
        }
        .ignoresSafeArea()
    }

    private func blob(_ color: Color, size: CGSize, x: Double, y: Double, scale: Double) -> some View {
        let diameter = max(size.width, size.height) * scale
        return Circle()
            .fill(color.opacity(0.32))
            .frame(width: diameter, height: diameter)
            .blur(radius: diameter * 0.28)
            .position(x: size.width * x, y: size.height * y)
    }
}

/// The breathing orb: slow while idle, quicker and brighter while Wisp is working.
struct BreathingOrb: View {
    var active: Bool
    var diameter: CGFloat = 96
    @State private var breathe = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Circle()
                .fill(RadialGradient(colors: [Theme.teal.opacity(0.9), Theme.indigo.opacity(0.7), .clear],
                                     center: .center, startRadius: 2, endRadius: diameter * 0.75))
                .blur(radius: diameter * 0.18)
                .scaleEffect(breathe ? 1.18 : 0.9)
            Circle()
                .fill(RadialGradient(colors: [.white.opacity(0.95), Theme.teal.opacity(0.85), Theme.indigo],
                                     center: UnitPoint(x: 0.38, y: 0.32), startRadius: 1, endRadius: diameter * 0.55))
                .frame(width: diameter * 0.62, height: diameter * 0.62)
                .overlay(Circle().stroke(.white.opacity(0.35), lineWidth: 0.8))
                .scaleEffect(breathe ? 1.04 : 0.96)
                .shadow(color: Theme.teal.opacity(active ? 0.8 : 0.45), radius: active ? 24 : 14)
        }
        .frame(width: diameter, height: diameter)
        .onAppear { animate() }
        .onChange(of: active) { animate() }
        .accessibilityHidden(true)
    }

    private func animate() {
        guard !reduceMotion else { breathe = false; return }
        breathe = false
        withAnimation(.easeInOut(duration: active ? 0.9 : 3.2).repeatForever(autoreverses: true)) { breathe = true }
    }
}

struct GlassCard: ViewModifier {
    var cornerRadius: CGFloat = 20
    var tint: Color = .white

    func body(content: Content) -> some View {
        content
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                .strokeBorder(tint.opacity(0.18), lineWidth: 0.8))
    }
}

extension View {
    func glassCard(cornerRadius: CGFloat = 20, tint: Color = .white) -> some View {
        modifier(GlassCard(cornerRadius: cornerRadius, tint: tint))
    }
}
