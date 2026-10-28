import AppKit
import Foundation
import PDFKit
import UniformTypeIdentifiers

// Documents the Ask field can carry along with a question: PDF, Word, Excel,
// PowerPoint, RTF, ODT, HTML and plain text. Pure logic apart from the PDF read,
// checked by tests/DocumentAttachmentChecks.swift.
//
// Rules:
//  - Memory only. Nothing here writes to disk. The file is sent as raw bytes and
//    the service extracts the text.
//  - A PDF with no text layer (a scan) is sent as up to four rendered pages
//    instead (JPEG, at most 1568 px on the longest side).
//  - A locked PDF, an old .xls/.ppt, or a file over 20 MB is refused with a plain
//    sentence, never read.

enum DocumentAttachment {
    static let maxFileBytes = 20 * 1024 * 1024
    static let maxScanPages = 4

    static func tooLarge(bytes: Int) -> Bool { bytes > maxFileBytes }

    static let tooLargeMessage = "That document is too large for Wisp to read (20 MB at most)."
    static let legacyMessage = "Wisp can't read old .xls or .ppt files. Save it as .xlsx or .pptx and try again."
    static let lockedMessage = "That PDF is password-protected."
    static let unreadableMessage = "Wisp couldn't read that document."
    static let otherFileMessage = "Wisp can read pictures, short clips, PDFs and documents."

    // MARK: - File types

    enum Kind: Equatable { case document, legacyOffice, other }

    private static let documentExtensions: Set<String> = [
        "pdf", "docx", "doc", "xlsx", "pptx", "odt", "rtf", "rtfd", "html", "htm",
        "txt", "text", "md", "markdown", "csv", "tsv", "json", "log",
    ]
    private static let legacyExtensions: Set<String> = ["xls", "ppt"]

    private static func type(_ identifier: String) -> UTType? { UTType(identifier) }

    /// Types the open panel offers on top of pictures and clips.
    static var acceptedTypes: [UTType] {
        var types: [UTType] = [.pdf, .rtf, .html, .plainText, .commaSeparatedText, .json]
        for id in ["net.daringfireball.markdown", "org.openxmlformats.wordprocessingml.document",
                   "com.microsoft.word.doc", "org.openxmlformats.spreadsheetml.sheet",
                   "org.openxmlformats.presentationml.presentation", "org.oasis-open.opendocument.text",
                   "public.tab-separated-values-text"] {
            if let t = type(id) { types.append(t) }
        }
        for ext in ["docx", "doc", "xlsx", "pptx", "odt", "md", "tsv", "log"] {
            if let t = UTType(filenameExtension: ext) { types.append(t) }
        }
        return types
    }

    /// True for a type Wisp reads as a document.
    static func isDocument(_ type: UTType?) -> Bool {
        guard let type else { return false }
        if type.conforms(to: .image) || type.conforms(to: .movie) { return false }
        for base in [UTType.pdf, .rtf, .html, .plainText, .commaSeparatedText, .json] where type.conforms(to: base) {
            return true
        }
        for id in ["net.daringfireball.markdown", "org.openxmlformats.wordprocessingml.document",
                   "com.microsoft.word.doc", "org.openxmlformats.spreadsheetml.sheet",
                   "org.openxmlformats.presentationml.presentation", "org.oasis-open.opendocument.text"] {
            if let t = UTType(id), type.conforms(to: t) { return true }
        }
        return false
    }

    /// Extension first (so .xls/.ppt are named precisely), then the system's type.
    static func kind(of url: URL) -> Kind {
        let ext = url.pathExtension.lowercased()
        if legacyExtensions.contains(ext) { return .legacyOffice }
        if documentExtensions.contains(ext) { return .document }
        return isDocument(UTType(filenameExtension: ext)) ? .document : .other
    }

    static func isPDF(name: String) -> Bool { (name as NSString).pathExtension.lowercased() == "pdf" }

    // MARK: - Chip

    static func chipLabel(name: String, scannedPages: Int) -> String {
        if scannedPages > 0 {
            return "\(name) · \(scannedPages) scanned page\(scannedPages == 1 ? "" : "s")"
        }
        let ext = (name as NSString).pathExtension.uppercased()
        return ext.isEmpty ? "\(name) · Document" : "\(name) · \(ext)"
    }

    // MARK: - Reading

    enum Outcome {
        case attached(MediaAttachment.Pending)
        case refused(String)
    }

    /// Reads a picked, dropped or pasted file. Blocking; call off the main thread.
    static func load(url: URL) -> Outcome {
        switch kind(of: url) {
        case .legacyOffice: return .refused(legacyMessage)
        case .other: return .refused(otherFileMessage)
        case .document: break
        }
        if let bytes = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize, tooLarge(bytes: bytes) {
            return .refused(tooLargeMessage)
        }
        guard let data = try? Data(contentsOf: url, options: .mappedIfSafe) else { return .refused(unreadableMessage) }
        return pending(name: url.lastPathComponent, data: data)
    }

    /// The pending attachment for a file already in memory.
    static func pending(name: String, data: Data) -> Outcome {
        if tooLarge(bytes: data.count) { return .refused(tooLargeMessage) }
        guard isPDF(name: name) else {
            return .attached(.document(name: name, data: data, pages: []))
        }
        guard let pdf = PDFDocument(data: data) else { return .refused(unreadableMessage) }
        if pdf.isLocked { return .refused(lockedMessage) }
        let text = (pdf.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.isEmpty { return .attached(.document(name: name, data: data, pages: [])) }
        let pages = renderPages(of: pdf)
        guard !pages.isEmpty else { return .refused(unreadableMessage) }
        return .attached(.document(name: name, data: nil, pages: pages))
    }

    /// The first up to four pages as JPEG, at most `MediaAttachment.maxImageSide` px.
    static func renderPages(of pdf: PDFDocument, maxPages: Int = maxScanPages) -> [Data] {
        var out: [Data] = []
        for index in 0..<min(pdf.pageCount, maxPages) {
            guard let page = pdf.page(at: index), let jpeg = renderPage(page) else { continue }
            out.append(jpeg)
        }
        return out
    }

    private static func renderPage(_ page: PDFPage) -> Data? {
        let box = page.bounds(for: .mediaBox)
        var w = box.width, h = box.height
        if page.rotation % 180 != 0 { swap(&w, &h) }
        guard w > 0, h > 0 else { return nil }
        let scale = CGFloat(MediaAttachment.maxImageSide) / max(w, h)
        let pixels = CGSize(width: max(1, (w * scale).rounded()), height: max(1, (h * scale).rounded()))
        let thumb = page.thumbnail(of: pixels, for: .mediaBox)
        var rect = CGRect(origin: .zero, size: thumb.size)
        guard let image = thumb.cgImage(forProposedRect: &rect, context: nil, hints: nil) else { return nil }
        // Flatten onto white so transparent areas do not turn black in JPEG.
        let width = image.width, height = image.height
        guard let ctx = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        guard let flat = ctx.makeImage() else { return nil }
        return MediaAttachment.downscaleJPEG(flat)
    }
}
