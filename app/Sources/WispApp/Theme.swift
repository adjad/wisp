import SwiftUI
import AppKit

// MARK: - Shared ink

/// Wisp's one set of base colors. Every surface (the notch Theme, the window's
/// ChatTheme, the Day and quick-settings accents) takes its hues from here, so
/// a red, an amber or a teal means the same thing wherever it appears.
/// Each pair is (light, dark); the notch is always dark and takes the second.
enum WispInk {
    static func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
                blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
    }

    /// A color that follows the Mac's appearance, including Increase Contrast,
    /// which macOS reports as its own high-contrast appearances. A window that
    /// draws with these needs no rebuild when either setting changes.
    static func dynamic(light: NSColor, dark: NSColor,
                        lightContrast: NSColor? = nil, darkContrast: NSColor? = nil) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            switch appearance.bestMatch(from: [.aqua, .darkAqua, .accessibilityHighContrastAqua,
                                               .accessibilityHighContrastDarkAqua]) {
            case .darkAqua?: return dark
            case .accessibilityHighContrastDarkAqua?: return darkContrast ?? dark
            case .accessibilityHighContrastAqua?: return lightContrast ?? light
            default: return light
            }
        })
    }

    // Status. Only these two carry color on the notch: an error and a
    // "your call" (waiting for a yes, or something that needs you).
    static let danger: (light: UInt32, dark: UInt32) = (0xb3302a, 0xff8a80)
    static let attention: (light: UInt32, dark: UInt32) = (0x96560c, 0xf0b45a)
    static let ok: (light: UInt32, dark: UInt32) = (0x1f8f55, 0x6fd69a)

    // Aurora hues: the window's accents and the Day's suggestion blue.
    static let teal: (light: UInt32, dark: UInt32) = (0x16857c, 0x5ad0c6)
    static let blue: (light: UInt32, dark: UInt32) = (0x2f6fd0, 0x7db4ff)
}

/// Text roles. Each surface maps a role to its own point size (the notch is a
/// glance, the window is for reading); `Font.wisp(_:)` applies the user's Text
/// size on top, so a role is the only way a view needs to name a size.
enum WispTextRole {
    /// Badges, tiny counts, secondary meta.
    case caption
    /// Hints, status lines, metadata under a reply.
    case footnote
    /// Ordinary UI text: rows, chips, buttons.
    case body
    /// Card titles and emphasized rows.
    case callout
    /// A conversation's text: prompts and replies.
    case reading
    /// Section and window headings.
    case headline
    /// The main text field.
    case input

    enum Surface { case notch, window }

    func size(on surface: Surface) -> CGFloat {
        switch (self, surface) {
        case (.caption, .notch): return 10
        case (.footnote, .notch): return 11
        case (.body, .notch): return 12
        case (.callout, .notch): return 13
        case (.reading, .notch): return 14
        case (.headline, .notch): return 15
        case (.input, .notch): return 19
        case (.caption, .window): return 11
        case (.footnote, .window): return 12
        case (.body, .window): return 13
        case (.callout, .window): return 14
        case (.reading, .window): return 15
        case (.headline, .window): return 15
        case (.input, .window): return 15
        }
    }
}

// MARK: - Notch

// Monochrome "notch" palette — pure black surface to blend into the notch,
// white/gray content, so it reads as an extension of the hardware. The one
// exception is status: an error is red and something that needs you is amber,
// from WispInk, so neither can be mistaken for ordinary text.
enum Theme {
    static let surface = Color.black
    static let textPrimary = Color.white
    static let accent = Color.white
    /// Healthy and ready: deliberately neutral, the notch's normal state.
    static let good = Color.white
    /// Needs you: waiting for a yes, setup set aside, a degraded source.
    static let warn = Color(nsColor: WispInk.rgb(WispInk.attention.dark))
    /// Something failed.
    static let bad = Color(nsColor: WispInk.rgb(WispInk.danger.dark))

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
    /// A card raised just off the black, for grouped settings.
    static let raised = Color(white: 0.043)
    /// A switch's track or a control's resting fill on a raised card.
    static var controlFill: Color { Color(white: increaseContrast ? 0.3 : 0.17) }

    // MARK: - Text size
    // "Text size" in Settings > Voice & accessibility. Stored as the scale
    // itself under this key; Font.wisp multiplies every font by it, in the
    // notch and in the Wisp window.
    static let textSizeKey = "WispTextSize"
    static let textSizeChoices: [(label: String, scale: Double)] = [
        ("Default", 1.0), ("Large", 1.15), ("Extra large", 1.3),
    ]
    static var textScale: CGFloat {
        let raw = UserDefaults.standard.double(forKey: textSizeKey)
        guard raw > 0 else { return 1 }
        return CGFloat(min(max(raw, 1.0), 1.3))
    }

    // MARK: - Controls
    /// The notch's round icon buttons: a 24 pt hit area (macOS asks for at
    /// least 20) around an 11 pt glyph.
    static let iconButtonSize: CGFloat = 24
    static let iconGlyphSize: CGFloat = 11

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

/// A pop-up Picker keeps the title colour of the appearance it was built in, so
/// switching the Mac from dark to light left white titles on light buttons.
/// Rebuilding it when the colour scheme changes redraws the title.
private struct RedrawsOnAppearanceChange: ViewModifier {
    @Environment(\.colorScheme) private var colorScheme
    func body(content: Content) -> some View { content.id(colorScheme) }
}

extension View {
    func redrawsOnAppearanceChange() -> some View { modifier(RedrawsOnAppearanceChange()) }
}

extension Font {
    /// `.system(size:weight:design:)` multiplied by the user's Text size scale.
    /// For SF Symbol glyphs and the few fixed sizes outside the text roles.
    static func wisp(size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default) -> Font {
        .system(size: size * Theme.textScale, weight: weight, design: design)
    }

    /// A text role at its size on `surface`, multiplied by the Text size scale.
    static func wisp(_ role: WispTextRole, on surface: WispTextRole.Surface = .notch,
                     weight: Font.Weight = .regular, design: Font.Design = .default) -> Font {
        wisp(size: role.size(on: surface), weight: weight, design: design)
    }
}
