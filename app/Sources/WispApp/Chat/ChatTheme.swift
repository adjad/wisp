import SwiftUI
import AppKit

/// The Chat window's look: a dark aurora at night and a pale daylight blue by
/// day, switching with the Mac's appearance. The notch panel keeps its own
/// black Theme; this is a separate surface with its own colors.
enum ChatTheme {
    private static func dynamic(light: NSColor, dark: NSColor) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
        })
    }
    private static func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
                blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
    }
    private static func white(_ alpha: CGFloat) -> NSColor { NSColor(white: 1, alpha: alpha) }

    // Text
    static let text = dynamic(light: rgb(0x1b2f50), dark: rgb(0xe8edf4))
    static let text2 = dynamic(light: rgb(0x4a638a), dark: rgb(0x9aa5b8))
    static let text3 = dynamic(light: rgb(0x667d9f), dark: rgb(0x7a869a))

    // Surfaces
    static let wall = dynamic(light: rgb(0xe4eefc), dark: rgb(0x0a0d14))
    static let sidebar = dynamic(light: white(0.45), dark: white(0.025))
    static let inspector = dynamic(light: white(0.45), dark: white(0.025))
    static let glass = dynamic(light: white(0.62), dark: white(0.055))
    static let glass2 = dynamic(light: rgb(0x1f3558, 0.075), dark: white(0.09))
    static let field = dynamic(light: white(0.85), dark: white(0.07))
    static let bubble = dynamic(light: white(0.92), dark: white(0.10))
    static let line = dynamic(light: rgb(0x1f3558, 0.12), dark: white(0.09))
    static let line2 = dynamic(light: rgb(0x1f3558, 0.22), dark: white(0.17))

    // Accents
    static let teal = dynamic(light: rgb(0x16857c), dark: rgb(0x5ad0c6))
    static let tealWash = dynamic(light: rgb(0x1b8f86, 0.10), dark: rgb(0x5ad0c6, 0.10))
    static let tealLine = dynamic(light: rgb(0x1b8f86, 0.35), dark: rgb(0x5ad0c6, 0.32))
    static let blue = dynamic(light: rgb(0x2f6fd0), dark: rgb(0x7db4ff))
    static let ok = dynamic(light: rgb(0x1f8f55), dark: rgb(0x6fd69a))
    static let danger = dynamic(light: rgb(0xb3302a), dark: rgb(0xff8a80))

    // "Your call" amber, used only where Wisp is waiting for a yes.
    static let amber = dynamic(light: rgb(0x96560c), dark: rgb(0xf0b45a))
    static let amberWash = dynamic(light: rgb(0xf0b45a, 0.20), dark: rgb(0xf0b45a, 0.08))
    static let amberLine = dynamic(light: rgb(0xbe781e, 0.45), dark: rgb(0xf0b45a, 0.40))
    static let amberButton = dynamic(light: rgb(0xe9a23b), dark: rgb(0xf0b45a))
    static let amberButtonText = rgb(0x2a1a02)

    static let sendFill = dynamic(light: rgb(0x16857c), dark: rgb(0x5ad0c6))
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

    /// Markdown drawn on this surface.
    static let markdown = MarkdownPalette(
        text: text, secondary: text2, muted: text3,
        chipFill: glass2, chipStroke: line2, hairline: line,
        codeFill: glass, tableFill: glass, bodySize: 15)
}

/// The breathing droplet that stands for Wisp.
struct ChatOrb: View {
    var size: CGFloat = 22
    var thinking = false
    @State private var breathe = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Circle()
            .fill(RadialGradient(colors: ChatTheme.orbStops, center: UnitPoint(x: 0.34, y: 0.30),
                                 startRadius: 0, endRadius: size * 0.78))
            .frame(width: size, height: size)
            .shadow(color: ChatTheme.orbGlow, radius: size * 0.45)
            .scaleEffect(breathe ? 1.08 : 1.0)
            .onAppear {
                guard !reduceMotion else { return }
                withAnimation(.easeInOut(duration: thinking ? 1.3 : 5).repeatForever(autoreverses: true)) { breathe = true }
            }
            .accessibilityHidden(true)
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
