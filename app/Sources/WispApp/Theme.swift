import SwiftUI

// Monochrome "notch" palette — pure black surface to blend into the notch,
// white/gray content. No color so it reads as an extension of the hardware.
enum Theme {
    static let surface = Color.black
    static let textPrimary = Color.white
    static let textSecondary = Color(white: 0.72)
    static let textMuted = Color(white: 0.45)
    static let accent = Color.white
    static let good = Color.white
    static let warn = Color(white: 0.6)
    static let bad = Color(white: 0.85)

    // Hairline + chip fills on black
    static let hairline = Color.white.opacity(0.12)
    static let chipFill = Color.white.opacity(0.08)
    static let chipStroke = Color.white.opacity(0.14)

    // Soft radial glow, not a glossy sphere — a bright core fading fully to
    // transparent reads as ambient light sitting on the black surface rather
    // than an opaque ball. Still strictly monochrome (no color), just diffuse
    // instead of hard-edged, so it doesn't fight the surface for attention.
    static let orb = RadialGradient(
        colors: [Color.white, Color(white: 0.85).opacity(0.55), Color(white: 0.7).opacity(0)],
        center: .center, startRadius: 0, endRadius: 15)

    // Rounded only at the bottom so the top edge fuses with the notch.
    static let notchCorners = UnevenRoundedRectangle(
        topLeadingRadius: 0, bottomLeadingRadius: 28,
        bottomTrailingRadius: 28, topTrailingRadius: 0)
}
