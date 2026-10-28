import SwiftUI
import AppKit

/// The Chat window's look: a dark aurora at night and a pale daylight blue by
/// day, switching with the Mac's appearance and Increase Contrast. The notch
/// panel keeps its own black Theme; both take their hues from WispInk.
enum ChatTheme {
    private static func dynamic(light: NSColor, dark: NSColor,
                                lightContrast: NSColor? = nil, darkContrast: NSColor? = nil) -> Color {
        WispInk.dynamic(light: light, dark: dark, lightContrast: lightContrast, darkContrast: darkContrast)
    }
    private static func ink(_ pair: (light: UInt32, dark: UInt32), _ alpha: CGFloat = 1) -> Color {
        dynamic(light: rgb(pair.light, alpha), dark: rgb(pair.dark, alpha))
    }
    private static func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor { WispInk.rgb(hex, alpha) }
    private static func white(_ alpha: CGFloat) -> NSColor { NSColor(white: 1, alpha: alpha) }

    // Text. Every level clears WCAG AA (4.5:1) on the wall in both
    // appearances; Increase Contrast moves the two quieter levels toward text.
    static let text = dynamic(light: rgb(0x1b2f50), dark: rgb(0xe8edf4),
                              lightContrast: rgb(0x0e1d36), darkContrast: rgb(0xffffff))
    static let text2 = dynamic(light: rgb(0x4a638a), dark: rgb(0x9aa5b8),
                               lightContrast: rgb(0x3a5279), darkContrast: rgb(0xc4ccd9))
    static let text3 = dynamic(light: rgb(0x536b93), dark: rgb(0x7a869a),
                               lightContrast: rgb(0x445c84), darkContrast: rgb(0xa3aec0))

    // Surfaces
    static let wall = dynamic(light: rgb(0xe4eefc), dark: rgb(0x0a0d14))
    static let sidebar = dynamic(light: white(0.45), dark: white(0.025))
    static let inspector = dynamic(light: white(0.45), dark: white(0.025))
    static let glass = dynamic(light: white(0.62), dark: white(0.055))
    static let glass2 = dynamic(light: rgb(0x1f3558, 0.075), dark: white(0.09))
    static let field = dynamic(light: white(0.85), dark: white(0.07))
    static let bubble = dynamic(light: white(0.92), dark: white(0.10))
    static let line = dynamic(light: rgb(0x1f3558, 0.12), dark: white(0.09),
                              lightContrast: rgb(0x1f3558, 0.3), darkContrast: white(0.28))
    static let line2 = dynamic(light: rgb(0x1f3558, 0.22), dark: white(0.17),
                               lightContrast: rgb(0x1f3558, 0.45), darkContrast: white(0.4))

    // Accents
    static let teal = ink(WispInk.teal)
    static let tealWash = dynamic(light: rgb(0x1b8f86, 0.10), dark: rgb(WispInk.teal.dark, 0.10))
    static let tealLine = dynamic(light: rgb(0x1b8f86, 0.35), dark: rgb(WispInk.teal.dark, 0.32))
    static let blue = ink(WispInk.blue)
    static let ok = ink(WispInk.ok)
    static let danger = ink(WispInk.danger)

    // "Your call" amber, used only where Wisp is waiting for a yes.
    static let amber = ink(WispInk.attention)
    static let amberWash = dynamic(light: rgb(0xf0b45a, 0.20), dark: rgb(0xf0b45a, 0.08))
    static let amberLine = dynamic(light: rgb(0xbe781e, 0.45), dark: rgb(0xf0b45a, 0.40))
    static let amberButton = dynamic(light: rgb(0xe9a23b), dark: rgb(0xf0b45a))
    static let amberButtonText = rgb(0x2a1a02)

    static let sendFill = ink(WispInk.teal)
    static let sendIcon = dynamic(light: white(1), dark: rgb(0x04201d))

    // The orb: a wet droplet, blue to teal by day, teal to deep blue at night.
    static let orbStops: [Color] = [
        .white,
        dynamic(light: rgb(0xd6e9ff), dark: rgb(0xbff3ee)),
        dynamic(light: rgb(0x7db4ff), dark: rgb(0x5ad0c6)),
        dynamic(light: rgb(0x4fc3b8), dark: rgb(0x2c7f9e)),
    ]
    static let orbGlow = dynamic(light: rgb(0x6eaaff, 0.55), dark: rgb(0x5ad0c6, 0.55))

    // Aurora blobs behind the whole window.
    static let blobTeal = dynamic(light: rgb(0x5ad0c6, 0.32), dark: rgb(0x5ad0c6, 0.22))
    static let blobBlue = dynamic(light: rgb(0x7db4ff, 0.38), dark: rgb(0x7db4ff, 0.20))
    static let blobViolet = dynamic(light: rgb(0xbeaaff, 0.24), dark: rgb(0x8c6ee6, 0.16))

    /// Markdown drawn on this surface. Computed so it follows the Text size
    /// setting when the thread is rebuilt.
    static var markdown: MarkdownPalette {
        var p = MarkdownPalette(
            text: text, secondary: text2, muted: text3,
            chipFill: glass2, chipStroke: line2, hairline: line,
            codeFill: glass, tableFill: glass)
        p.fontScale = Theme.textScale
        p.bodySize = WispTextRole.reading.size(on: .window) * Theme.textScale
        return p
    }
}

/// The droplet is still at rest and breathes only while an answer is live.
struct ChatOrb: View {
    var size: CGFloat = 22
    var thinking = false
    @State private var mounted = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
#if WISP_ORB_QA
    // The system environment is read-only. The isolated fixture supplies the
    // same decision input without changing the user's accessibility setting.
    var reducedMotionForQA: Bool? = nil
#endif

    private var animates: Bool {
#if WISP_ORB_QA
        if let reducedMotionForQA { return mounted && thinking && !reducedMotionForQA }
#endif
        return mounted && thinking && !reduceMotion
    }

    var body: some View {
        ZStack {
            if animates {
                ChatOrbBreathing(size: size)
            } else {
                ChatOrbArtwork(size: size)
            }
        }
        .frame(width: size, height: size)
        .onAppear { mounted = true }
        .onDisappear { mounted = false }
        .accessibilityHidden(true)
    }
}

private struct ChatOrbArtwork: View {
    var size: CGFloat

    var body: some View {
        Circle()
            .fill(RadialGradient(colors: ChatTheme.orbStops, center: UnitPoint(x: 0.34, y: 0.30),
                                 startRadius: 0, endRadius: size * 0.78))
            .frame(width: size, height: size)
            .shadow(color: ChatTheme.orbGlow, radius: size * 0.45)
    }
}

/// Removing this child disposes its repeating animation. A nil animation on
/// an existing scale does not reliably stop SwiftUI's repeatForever clock.
private struct ChatOrbBreathing: View {
    var size: CGFloat
    @State private var breathe = false

    var body: some View {
        ChatOrbArtwork(size: size)
            .scaleEffect(breathe ? 1.08 : 1.0)
            .onAppear {
                withAnimation(.easeInOut(duration: 1.3).repeatForever(autoreverses: true)) {
                    breathe = true
                }
            }
    }
}

/// Soft aurora light behind everything, matching the concept mockups.
struct ChatAurora: View {
    var body: some View {
        ZStack {
            ChatTheme.wall
            RadialGradient(colors: [ChatTheme.blobTeal, .clear], center: UnitPoint(x: 0.08, y: 0.0), startRadius: 0, endRadius: 520)
            RadialGradient(colors: [ChatTheme.blobBlue, .clear], center: UnitPoint(x: 1.0, y: 0.12), startRadius: 0, endRadius: 560)
            RadialGradient(colors: [ChatTheme.blobViolet, .clear], center: UnitPoint(x: 0.55, y: 1.1), startRadius: 0, endRadius: 600)
        }
        .ignoresSafeArea()
    }
}
