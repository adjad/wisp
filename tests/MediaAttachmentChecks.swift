import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

// Pure checks for app/Sources/WispApp/MediaAttachment.swift: downscaling, data
// URLs and the /agent body fields. Images are generated here; no files, no UI.
@main
struct MediaAttachmentChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static func makeImage(width: Int, height: Int, type: UTType = .png) -> Data {
        let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(CGColor(red: 0.2, green: 0.5, blue: 0.8, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let out = NSMutableData()
        let dest = CGImageDestinationCreateWithData(out, type.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(dest, ctx.makeImage()!, nil)
        precondition(CGImageDestinationFinalize(dest))
        return out as Data
    }

    static func size(of data: Data) -> (Int, Int)? {
        guard let s = CGImageSourceCreateWithData(data as CFData, nil),
              let p = CGImageSourceCopyPropertiesAtIndex(s, 0, nil) as? [CFString: Any],
              let w = p[kCGImagePropertyPixelWidth] as? Int, let h = p[kCGImagePropertyPixelHeight] as? Int
        else { return nil }
        return (w, h)
    }

    static func main() {
        // A big image shrinks to the longest side, keeping its aspect ratio, as JPEG.
        let big = makeImage(width: 4000, height: 3000)
        let jpeg = MediaAttachment.downscaleJPEG(big)
        check(jpeg != nil, "a 4000x3000 PNG downscales")
        let (w, h) = size(of: jpeg!) ?? (0, 0)
        check(max(w, h) <= 1568 && max(w, h) >= 1560, "longest side is 1568 (\(w)x\(h))")
        check(abs(Double(w) / Double(h) - 4.0 / 3.0) < 0.01, "aspect ratio kept")
        check(jpeg!.starts(with: [0xFF, 0xD8]), "output is JPEG")

        // A small image is not upscaled.
        let small = MediaAttachment.downscaleJPEG(makeImage(width: 200, height: 100))
        check(size(of: small!) ?? (0, 0) == (200, 100), "a small image keeps its size")

        // Portrait works too.
        let tall = size(of: MediaAttachment.downscaleJPEG(makeImage(width: 1000, height: 3000))!) ?? (0, 0)
        check(tall.1 <= 1568 && tall.0 < tall.1, "portrait scales by height")

        // Not an image.
        check(MediaAttachment.downscaleJPEG(Data("hello".utf8)) == nil, "text is not an image")
        check(MediaAttachment.downscaleJPEG(Data()) == nil, "empty data is not an image")

        // Data URL.
        check(MediaAttachment.dataURL(jpeg: Data([1, 2, 3])) == "data:image/jpeg;base64,AQID", "data URL prefix")

        // Body fields.
        let img = MediaAttachment.Pending.image(jpeg: Data([1, 2, 3]), source: .file).bodyFields()
        check((img["image"] as? String)?.hasPrefix("data:image/jpeg;base64,") == true, "image field")
        check(img["image_source"] == nil && img["video"] == nil, "a file image has no source tag")
        let scr = MediaAttachment.Pending.image(jpeg: Data([1]), source: .screen).bodyFields()
        check(scr["image_source"] as? String == "screen", "screen tag")

        let frames = (0..<12).map { Data([UInt8($0)]) }
        let clip = MediaAttachment.Pending.video(SampledClip(frames: frames, seconds: 12.4, wav: Data([9, 9])))
        let v = clip.bodyFields()["video"] as? [String: Any]
        check((v?["frames"] as? [String])?.count == 8, "frames capped at 8")
        check((v?["frames"] as? [String])?.first == "data:image/jpeg;base64,AA==", "frames are data URLs")
        check(v?["seconds"] as? Double == 12.4, "seconds")
        check(v?["audio"] as? String == Data([9, 9]).base64EncodedString(), "audio is base64")
        check(clip.bodyFields()["image"] == nil, "a clip has no image field")
        let silent = MediaAttachment.Pending.video(SampledClip(frames: [Data([1])], seconds: 3, wav: nil))
        check((silent.bodyFields()["video"] as? [String: Any])?["audio"] is NSNull, "no audio is null")

        // Default prompts, markers and the chip.
        check(MediaAttachment.prompt(typed: "  ", for: .image(jpeg: Data(), source: .paste)) == "What's in this image?", "image default prompt")
        check(MediaAttachment.prompt(typed: "", for: clip) == "What happens in this clip?", "clip default prompt")
        check(MediaAttachment.prompt(typed: "", for: .image(jpeg: Data(), source: .screen)) == "Explain what's on my screen.", "screen default prompt")
        check(MediaAttachment.prompt(typed: " hi ", for: clip) == "hi", "typed text wins")
        check(MediaAttachment.prompt(typed: "", for: nil) == "", "no attachment, no default")
        check(clip.historyMarker == "[clip]" && scr.isEmpty == false, "markers")
        check(MediaAttachment.chipLabel(for: clip).contains("12 s") && MediaAttachment.chipLabel(for: clip).contains("not motion"), "clip chip")

        // File kinds.
        check(MediaAttachment.kind(of: URL(fileURLWithPath: "/a/b.PNG")) == .image, "png")
        check(MediaAttachment.kind(of: URL(fileURLWithPath: "/a/b.heic")) == .image, "heic")
        check(MediaAttachment.kind(of: URL(fileURLWithPath: "/a/b.mov")) == .movie, "mov")
        check(MediaAttachment.kind(of: URL(fileURLWithPath: "/a/b.mp4")) == .movie, "mp4")
        check(MediaAttachment.kind(of: URL(fileURLWithPath: "/a/b.mp3")) == .other, "audio is not a clip")
        check(MediaAttachment.kind(of: URL(fileURLWithPath: "/a/b.txt")) == .other, "text")
        // Size refusal and the URL-based downscale.
        check(!MediaAttachment.tooLarge(bytes: 50 * 1024 * 1024), "exactly 50 MB is accepted")
        check(MediaAttachment.tooLarge(bytes: 50 * 1024 * 1024 + 1), "over 50 MB is refused")
        check(MediaAttachment.tooLargeMessage == "That picture is too large for Wisp to read.", "refusal wording")
        if let png = bigPNGForURLCheck() {
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-url-\(UUID().uuidString)")
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: dir) }
            let file = dir.appendingPathComponent("big.png")
            try? png.write(to: file)
            let out = MediaAttachment.downscaleJPEG(contentsOf: file, maxSide: 100)
            let src = out.flatMap { CGImageSourceCreateWithData($0 as CFData, nil) }
            let props = src.flatMap { CGImageSourceCopyPropertiesAtIndex($0, 0, nil) as? [CFString: Any] }
            check((props?[kCGImagePropertyPixelWidth] as? Int) == 100, "URL downscale caps the longest side")
            check(MediaAttachment.downscaleJPEG(contentsOf: dir.appendingPathComponent("missing.png")) == nil, "missing file is nil")
            try? Data("nope".utf8).write(to: file)
            check(MediaAttachment.downscaleJPEG(contentsOf: file) == nil, "non-image file is nil")
        } else {
            check(false, "could not build the URL check image")
        }
        print("MediaAttachmentChecks passed")
    }
}

private func bigPNGForURLCheck() -> Data? {
    guard let ctx = CGContext(data: nil, width: 400, height: 200, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue),
          let image = ctx.makeImage() else { return nil }
    let out = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(dest, image, nil)
    return CGImageDestinationFinalize(dest) ? out as Data : nil
}
