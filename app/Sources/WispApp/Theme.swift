import SwiftUI
import AppKit

// Monochrome "notch" palette — pure black surface to blend into the notch,
// white/gray content. No color so it reads as an extension of the hardware.
enum Theme {
    static let surface = Color.black
    static let textPrimary = Color.white
    static let accent = Color.white
    static let good = Color.white
    static let warn = Color(white: 0.6)
    static let bad = Color(white: 0.85)

    // Contrast tokens are computed so they follow System Settings >
    // Accessibility > Display > Increase contrast. Default textMuted is white
    // 0.52 (about 5.6:1 on black, over WCAG AA); Increase Contrast lifts the
    // greys further and brightens the chip outlines.
    static var increaseContrast: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast
    }
    static var textSecondary: Color { Color(white: increaseContrast ? 0.85 : 0.72) }
    static var textMuted: Color { Color(white: increaseContrast ? 0.7 : 0.52) }

    // Hairline + chip fills on black
    static var hairline: Color { Color.white.opacity(increaseContrast ? 0.28 : 0.12) }
    static let chipFill = Color.white.opacity(0.08)
    static var chipStroke: Color { Color.white.opacity(increaseContrast ? 0.4 : 0.14) }

    // MARK: - Text size
    // "Text size" in Settings > Voice & accessibility. Stored as the scale
    // itself under this key; Font.wisp multiplies every notch font by it.
    static let textSizeKey = "WispTextSize"
    static let textSizeChoices: [(label: String, scale: Double)] = [
        ("Default", 1.0), ("Large", 1.15), ("Extra large", 1.3),
    ]
    static var textScale: CGFloat {
        let raw = UserDefaults.standard.double(forKey: textSizeKey)
        guard raw > 0 else { return 1 }
        return CGFloat(min(max(raw, 1.0), 1.3))
    }

    // Soft radial glow, not a glossy sphere — a bright core fading fully to
    // transparent reads as ambient light sitting on the black surface rather
    // than an opaque ball. Still strictly monochrome (no color), just diffuse
    // instead of hard-edged, so it doesn't fight the surface for attention.
    static let orb = RadialGradient(
        colors: [Color.white, Color(white: 0.85).opacity(0.55), Color(white: 0.7).opacity(0)],
        center: .center, startRadius: 0, endRadius: 15)
}

/// The colours of a view drawn with Theme's tokens. The notch, its windows and
/// the setup guide default to `notch`, black and white; a host whose surface
/// follows the Mac's appearance (the Wisp window's Settings section) supplies
/// its own palette through `\.themePalette`, so the same views stay readable
/// on a light window. Theme.swift alone defines it: the files compiled with
/// only Theme.swift (SetupGuideView among them) can read it.
struct ThemePalette {
    var surface: Color
    var textPrimary: Color
    var textSecondary: Color
    var textMuted: Color
    var hairline: Color
    var chipFill: Color
    var chipStroke: Color
    var accent: Color
    /// Text on an `accent` fill (an "on" pill).
    var onAccent: Color
    /// A selected row or chip.
    var selectedFill: Color
    var selectedStroke: Color
    /// A step's card, a block of code, and a small action button.
    var cardFill: Color
    var codeFill: Color
    var actionFill: Color

    /// Theme itself. Computed, so Increase Contrast applies as Theme's own tokens do.
    static var notch: ThemePalette {
        ThemePalette(surface: Theme.surface, textPrimary: Theme.textPrimary,
                     textSecondary: Theme.textSecondary, textMuted: Theme.textMuted,
                     hairline: Theme.hairline, chipFill: Theme.chipFill, chipStroke: Theme.chipStroke,
                     accent: Theme.accent, onAccent: .black,
                     selectedFill: .white.opacity(0.10), selectedStroke: .white.opacity(0.35),
                     cardFill: .white.opacity(0.04), codeFill: .white.opacity(0.06),
                     actionFill: .white.opacity(0.12))
    }
}

private struct ThemePaletteKey: EnvironmentKey {
    static var defaultValue: ThemePalette { .notch }
}

extension EnvironmentValues {
    var themePalette: ThemePalette {
        get { self[ThemePaletteKey.self] }
        set { self[ThemePaletteKey.self] = newValue }
    }
}

extension Font {
    /// `.system(size:weight:design:)` multiplied by the user's Text size scale.
    static func wisp(size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default) -> Font {
        .system(size: size * Theme.textScale, weight: weight, design: design)
    }
}
