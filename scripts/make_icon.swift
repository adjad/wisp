import AppKit
import CoreGraphics

// Draws the Wisp icon: a faceted mint/emerald diamond (four experts, one
// gem) with a thin gold edge on the top-right facet, on a dark emerald squircle.
func draw(_ ctx: CGContext, _ s: CGFloat) {
    let cs = CGColorSpaceCreateDeviceRGB()
    let inset = s * 0.045
    let rect = CGRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let corner = s * 0.2237
    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: corner, cornerHeight: corner, transform: nil))
    ctx.clip()
    let bg = CGGradient(colorsSpace: cs, colors: [
        CGColor(red: 0.05, green: 0.13, blue: 0.10, alpha: 1),
        CGColor(red: 0.01, green: 0.03, blue: 0.02, alpha: 1)] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(bg, start: CGPoint(x: s / 2, y: s), end: CGPoint(x: s / 2, y: 0), options: [])
    ctx.restoreGState()

    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: corner, cornerHeight: corner, transform: nil))
    ctx.clip()

    let c = CGPoint(x: s / 2, y: s / 2)
    let r = s * 0.22
    let pts = [
        CGPoint(x: c.x, y: c.y + r),   // top
        CGPoint(x: c.x + r, y: c.y),   // right
        CGPoint(x: c.x, y: c.y - r),   // bottom
        CGPoint(x: c.x - r, y: c.y),   // left
    ]
    // Four facets, top -> right -> bottom -> left -> top.
    let facetColors: [[CGFloat]] = [
        [0.80, 0.98, 0.90],   // upper-right: light mint
        [0.45, 0.85, 0.68],   // lower-right: medium teal
        [0.18, 0.55, 0.42],   // lower-left: deep emerald
        [0.61, 1.0, 0.84],    // upper-left: mint
    ]
    for i in 0..<4 {
        let p = CGMutablePath()
        p.move(to: c)
        p.addLine(to: pts[i])
        p.addLine(to: pts[(i + 1) % 4])
        p.closeSubpath()
        ctx.addPath(p)
        let col = facetColors[i]
        ctx.setFillColor(CGColor(red: col[0], green: col[1], blue: col[2], alpha: 1))
        ctx.fillPath()
    }

    let outline = CGMutablePath()
    outline.addLines(between: pts)
    outline.closeSubpath()
    ctx.addPath(outline)
    ctx.setLineWidth(s * 0.006)
    ctx.setStrokeColor(CGColor(red: 0.02, green: 0.08, blue: 0.05, alpha: 0.5))
    ctx.strokePath()

    for i in 0..<4 {
        ctx.move(to: c); ctx.addLine(to: pts[i])
    }
    ctx.setLineWidth(s * 0.004)
    ctx.setStrokeColor(CGColor(red: 0.02, green: 0.08, blue: 0.05, alpha: 0.35))
    ctx.strokePath()

    // Hint of gold: a thin edge on the top-right facet, like light catching one
    // face of the gem. No specular dot — the stroke alone is enough.
    ctx.setLineWidth(s * 0.008)
    ctx.setStrokeColor(CGColor(red: 1.0, green: 0.84, blue: 0.45, alpha: 0.55))
    ctx.move(to: pts[0]); ctx.addLine(to: pts[1])
    ctx.strokePath()

    ctx.restoreGState()
    _ = cs
}

func render(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let g = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = g
    draw(g.cgContext, CGFloat(px))
    g.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "Wisp.iconset"
try? FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)
let specs: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256),
    ("icon_256x256@2x", 512), ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]
for (name, px) in specs {
    try! render(px).write(to: URL(fileURLWithPath: "\(out)/\(name).png"))
}
print("wrote iconset → \(out)")
