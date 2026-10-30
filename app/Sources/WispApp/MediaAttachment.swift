import Foundation
import ImageIO
import UniformTypeIdentifiers

// What the Ask field can carry along with a question: one image (from the
// paperclip, a drop, a paste, or "Explain my screen") or one short clip. Pure
// logic, checked by tests/MediaAttachmentChecks.swift.
//
// Rules:
//  - Memory only. Nothing here writes to disk, and the attachment never goes into
//    saved chat history or drafts; only the typed text (plus an "[image]" marker)
//    is kept.
//  - Images are downscaled to at most 1568 px on the longest side before base64,
//    never upscaled, and always re-encoded as JPEG (so EXIF location is dropped).
//  - A clip is sampled frames + audio, never motion; the UI says so.

enum MediaAttachment {
    static let maxImageSide = 1568
    static let jpegQuality = 0.8

    /// Re-encodes `data` as a JPEG no larger than `maxSide` on the longest side.
    /// Nil when the data is not an image the system can decode.
    static func downscaleJPEG(_ data: Data, maxSide: Int = maxImageSide, quality: Double = jpegQuality) -> Data? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        return downscaleJPEG(source: source, maxSide: maxSide, quality: quality)
    }

    private static func downscaleJPEG(source: CGImageSource, maxSide: Int, quality: Double) -> Data? {
        guard maxSide > 0, CGImageSourceGetCount(source) > 0 else { return nil }
        // Longest side of the original, so a small image is never upscaled.
        var longest = maxSide
        if let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
           let w = props[kCGImagePropertyPixelWidth] as? Int, let h = props[kCGImagePropertyPixelHeight] as? Int {
            longest = min(maxSide, max(w, h))
        }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: longest,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return jpegData(image, quality: quality)
    }

    /// Images over this size are refused before anything is read.
    static let maxImageFileBytes = 50 * 1024 * 1024

    static func tooLarge(bytes: Int) -> Bool { bytes > maxImageFileBytes }

    static var tooLargeMessage: String { "That picture is too large for Wisp to read." }

    /// Like the Data version, but ImageIO reads the file itself, so the whole
    /// file never sits in memory as a Data.
    static func downscaleJPEG(contentsOf url: URL, maxSide: Int = maxImageSide, quality: Double = jpegQuality) -> Data? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return downscaleJPEG(source: source, maxSide: maxSide, quality: quality)
    }

    /// Re-encodes a decoded image (a screen capture, a pasted bitmap) the same way.
    static func downscaleJPEG(_ image: CGImage, maxSide: Int = maxImageSide, quality: Double = jpegQuality) -> Data? {
        guard let full = jpegData(image, quality: 0.95) else { return nil }
        return downscaleJPEG(full, maxSide: maxSide, quality: quality)
    }

    private static func jpegData(_ image: CGImage, quality: Double) -> Data? {
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        return CGImageDestinationFinalize(dest) ? out as Data : nil
    }

    static func dataURL(jpeg: Data) -> String {
        "data:image/jpeg;base64," + jpeg.base64EncodedString()
    }

    // MARK: - File types

    static func isImage(_ type: UTType?) -> Bool { type?.conforms(to: .image) ?? false }

    static func isMovie(_ type: UTType?) -> Bool {
        guard let type else { return false }
        return type.conforms(to: .movie) || type.conforms(to: .audiovisualContent) && !type.conforms(to: .audio)
    }

    static func kind(of url: URL) -> Kind {
        let type = UTType(filenameExtension: url.pathExtension.lowercased())
        if isImage(type) { return .image }
        if isMovie(type) { return .movie }
        return .other
    }

    enum Kind: Equatable { case image, movie, other }

    /// What the paperclip's open panel and the drop target accept.
    static var acceptedTypes: [UTType] { [.image, .movie] }

    // MARK: - Pending attachment

    enum Source: String, Equatable { case file, paste, drop, screen }

    enum Pending {
        case image(jpeg: Data, source: Source)
        case video(SampledClip)
        /// A document: raw bytes when it has text (`data`), rendered pages when it
        /// is a scan with no text layer (`pages`, JPEG, 1-4).
        case document(name: String, data: Data?, pages: [Data])

        var isScreen: Bool {
            if case .image(_, .screen) = self { return true }
            return false
        }

        /// Prompt used when the person typed nothing.
        var defaultPrompt: String {
            switch self {
            case .image(_, .screen): return "Explain what's on my screen."
            case .image: return "What's in this image?"
            case .video: return "What happens in this clip?"
            case .document: return "Summarize this document."
            }
        }

        /// The marker kept in saved history in place of the attachment.
        var historyMarker: String {
            switch self {
            case .image(_, .screen): return "[screen]"
            case .image: return "[image]"
            case .video: return "[clip]"
            case .document: return "[document]"
            }
        }

        /// The `/agent` body fields for this attachment.
        func bodyFields() -> [String: Any] {
            switch self {
            case .image(let jpeg, let source):
                var fields: [String: Any] = ["image": MediaAttachment.dataURL(jpeg: jpeg)]
                if source == .screen { fields["image_source"] = "screen" }
                return fields
            case .video(let clip):
                let frames = clip.frames.prefix(VideoSampler.maxFrames).map { MediaAttachment.dataURL(jpeg: $0) }
                let audio: Any = clip.wav.map { $0.base64EncodedString() as Any } ?? NSNull()
                return ["video": ["frames": Array(frames), "seconds": clip.seconds, "audio": audio] as [String: Any]]
            case .document(let name, let data, let pages):
                var doc: [String: Any] = ["name": name]
                if pages.isEmpty { doc["data"] = (data ?? Data()).base64EncodedString() }
                else { doc["pages"] = pages.prefix(DocumentAttachment.maxScanPages).map { MediaAttachment.dataURL(jpeg: $0) } }
                return ["document": doc]
            }
        }
    }

    /// The text to send: what was typed, or the default question for this attachment.
    static func prompt(typed: String, for pending: Pending?) -> String {
        let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
        return pending?.defaultPrompt ?? ""
    }

    /// The chip's label.
    static func chipLabel(for pending: Pending) -> String {
        switch pending {
        case .image(_, .screen): return "Screen"
        case .image: return "Image"
        case .video(let clip):
            return "Clip · \(Int(clip.seconds.rounded())) s — reads sampled frames + audio, not motion"
        case .document(let name, _, let pages):
            return DocumentAttachment.chipLabel(name: name, scannedPages: pages.count)
        }
    }
}

extension VideoSampleError {
    /// What to tell the person, in plain words.
    var message: String {
        switch self {
        case .tooLong: return VideoSampler.tooLongMessage
        case .unreadable: return "Wisp couldn't read that clip."
        case .noFrames: return "Wisp couldn't find any video frames in that clip."
        }
    }
}
