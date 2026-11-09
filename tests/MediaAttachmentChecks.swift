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

    /// Large screens: the detail JPEG keeps up to 4096 px and steps down to fit 6 MB.
    static func detailChecks() {
        check(MediaAttachment.maxDetailSide == 4096, "detail side is 4096")
        check(MediaAttachment.detailStepSides == [2600, MediaAttachment.maxImageSide], "step-down sides are 2600 then 1568")
        check(MediaAttachment.maxDetailBytes == 6 * 1024 * 1024, "detail limit is 6 MB")

        // The step-down helper, with a fake encoder so sizes are exact.
        var tried: [Int] = []
        let first = MediaAttachment.stepDown(limit: 100) { side in tried.append(side); return Data(count: 50) }
        check(first?.count == 50 && tried == [4096], "the largest side is used when it fits: \(tried)")
        tried = []
        let second = MediaAttachment.stepDown(limit: 100) { side in
            tried.append(side)
            return Data(count: side == 4096 ? 500 : 80)
        }
        check(second?.count == 80 && tried == [4096, 2600], "steps down to 2600 when 4096 is over: \(tried)")
        tried = []
        let third = MediaAttachment.stepDown(limit: 100) { side in
            tried.append(side)
            return Data(count: side == 1568 ? 90 : 500)
        }
        check(third?.count == 90 && tried == [4096, 2600, 1568], "steps down to maxImageSide: \(tried)")
        let stuck = MediaAttachment.stepDown(limit: 100) { side in Data(count: side) }
        check(stuck?.count == 1568, "still over at the end: the smallest one comes back")
        let skipped = MediaAttachment.stepDown(limit: 100) { side in side == 4096 ? nil : Data(count: 10) }
        check(skipped?.count == 10, "a side that fails to encode is skipped")
        check(MediaAttachment.stepDown(limit: 100) { _ in nil } == nil, "nothing encodes: nil")
        check(MediaAttachment.stepDown(limit: 100, sides: []) { _ in Data(count: 1) } == nil, "no sides: nil")

        // The real encoder: 4096 px for a big picture, never upscaled, memory only.
        let large = makeImage(width: 5000, height: 3000)
        let detail = MediaAttachment.detailJPEG(large)
        check(detail != nil && size(of: detail!)?.0 == 4096, "a 5000 px picture is kept at 4096: \(String(describing: detail.flatMap(size(of:))))")
        check(detail.map { $0.count <= MediaAttachment.maxDetailBytes } == true, "the detail JPEG fits 6 MB")
        check(detail.map { $0.prefix(2) == Data([0xFF, 0xD8]) } == true, "the detail output is a JPEG")
        let small = MediaAttachment.detailJPEG(makeImage(width: 1000, height: 500))
        check(small.flatMap(size(of:)).map { $0.0 == 1000 && $0.1 == 500 } == true, "a small picture is never upscaled")
        check(MediaAttachment.detailJPEG(Data("not an image".utf8)) == nil, "non-image data is nil")
        let ctx = CGContext(data: nil, width: 4500, height: 1000, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.setFillColor(CGColor(red: 0.9, green: 0.1, blue: 0.1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: 4500, height: 1000))
        let fromImage = ctx.makeImage().flatMap { MediaAttachment.detailJPEG($0) }
        check(fromImage.flatMap(size(of:)).map { $0.0 == 4096 && $0.1 == 910 } == true,
              "a decoded capture keeps 4096 px: \(String(describing: fromImage.flatMap(size(of:))))")
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-detail-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appendingPathComponent("wide.png")
        try? large.write(to: file)
        let fromFile = MediaAttachment.detailJPEG(contentsOf: file)
        check(fromFile.flatMap(size(of:))?.0 == 4096, "a file is read at 4096 px by ImageIO")
        check(MediaAttachment.detailJPEG(contentsOf: dir.appendingPathComponent("missing.png")) == nil, "a missing file is nil")
        // The old helper still caps at 1568.
        check(size(of: MediaAttachment.downscaleJPEG(large)!)?.0 == MediaAttachment.maxImageSide, "downscaleJPEG still caps at 1568")
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
        detailChecks()
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
