import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

// Pure checks for app/Sources/WispApp/DocumentAttachment.swift: type
// classification, refusals, the PDF text / scan split and the /agent body.
// PDFs are generated here; no files, no UI.
@main
struct DocumentAttachmentChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    /// A PDF; each page draws `text` (when given) and/or a filled image.
    static func makePDF(pages: Int, text: String?, image: Bool) -> Data {
        let data = NSMutableData()
        let consumer = CGDataConsumer(data: data)!
        var box = CGRect(x: 0, y: 0, width: 612, height: 792)
        let ctx = CGContext(consumer: consumer, mediaBox: &box, nil)!
        for _ in 0..<pages {
            ctx.beginPDFPage(nil)
            if image {
                let bmp = CGContext(data: nil, width: 300, height: 400, bitsPerComponent: 8, bytesPerRow: 0,
                                    space: CGColorSpaceCreateDeviceRGB(),
                                    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
                bmp.setFillColor(CGColor(red: 0.1, green: 0.3, blue: 0.7, alpha: 1))
                bmp.fill(CGRect(x: 0, y: 0, width: 300, height: 400))
                ctx.draw(bmp.makeImage()!, in: CGRect(x: 100, y: 200, width: 300, height: 400))
            }
            if let text {
                let attributed = NSAttributedString(string: text, attributes: [
                    kCTFontAttributeName as NSAttributedString.Key: CTFontCreateWithName("Helvetica" as CFString, 24, nil)])
                let line = CTLineCreateWithAttributedString(attributed)
                ctx.textPosition = CGPoint(x: 72, y: 700)
                CTLineDraw(line, ctx)
            }
            ctx.endPDFPage()
        }
        ctx.closePDF()
        return data as Data
    }

    static func size(of data: Data) -> (Int, Int)? {
        guard let s = CGImageSourceCreateWithData(data as CFData, nil),
              let p = CGImageSourceCopyPropertiesAtIndex(s, 0, nil) as? [CFString: Any],
              let w = p[kCGImagePropertyPixelWidth] as? Int, let h = p[kCGImagePropertyPixelHeight] as? Int
        else { return nil }
        return (w, h)
    }

    static func kind(_ name: String) -> DocumentAttachment.Kind {
        DocumentAttachment.kind(of: URL(fileURLWithPath: "/a/" + name))
    }

    static func main() {
        // Type classification by extension.
        for name in ["a.pdf", "a.DOCX", "a.doc", "a.xlsx", "a.pptx", "a.odt", "a.rtf", "a.html",
                     "a.txt", "a.md", "a.csv", "a.tsv", "a.json", "a.log"] {
            check(kind(name) == .document, "\(name) is a document")
        }
        check(kind("a.xls") == .legacyOffice && kind("a.PPT") == .legacyOffice, "legacy Office is refused by name")
        check(kind("a.png") == .other && kind("a.mov") == .other && kind("a.zip") == .other, "pictures, clips, archives are not documents")
        check(DocumentAttachment.isDocument(.pdf) && DocumentAttachment.isDocument(.plainText)
              && DocumentAttachment.isDocument(.rtf) && DocumentAttachment.isDocument(.html)
              && DocumentAttachment.isDocument(.commaSeparatedText) && DocumentAttachment.isDocument(.json),
              "base types are documents")
        check(!DocumentAttachment.isDocument(.png) && !DocumentAttachment.isDocument(.movie) && !DocumentAttachment.isDocument(nil),
              "images and movies are not documents")
        check(DocumentAttachment.isDocument(UTType("org.openxmlformats.wordprocessingml.document")), "docx type")
        check(!DocumentAttachment.acceptedTypes.isEmpty && DocumentAttachment.acceptedTypes.contains(.pdf), "panel types")

        // Size.
        check(DocumentAttachment.maxFileBytes == 20 * 1024 * 1024, "20 MB cap")
        check(!DocumentAttachment.tooLarge(bytes: 20 * 1024 * 1024) && DocumentAttachment.tooLarge(bytes: 20 * 1024 * 1024 + 1), "cap edge")
        check(DocumentAttachment.tooLargeMessage == "That document is too large for Wisp to read (20 MB at most).", "size message")
        check(DocumentAttachment.legacyMessage == "Wisp can't read old .xls or .ppt files. Save it as .xlsx or .pptx and try again.", "legacy message")
        if case .refused(let m) = DocumentAttachment.pending(name: "big.txt", data: Data(count: 20 * 1024 * 1024 + 1)) {
            check(m == DocumentAttachment.tooLargeMessage, "oversize refused")
        } else { check(false, "oversize must be refused") }

        // Non-PDF: raw bytes.
        let raw = Data("hello".utf8)
        if case .attached(let p) = DocumentAttachment.pending(name: "Notes.docx", data: raw),
           case .document(let name, let data, let pages) = p {
            check(name == "Notes.docx" && data == raw && pages.isEmpty, "docx keeps raw bytes")
            let doc = p.bodyFields()["document"] as? [String: Any]
            check(doc?["name"] as? String == "Notes.docx" && doc?["data"] as? String == raw.base64EncodedString()
                  && doc?["pages"] == nil, "docx body")
            check(p.defaultPrompt == "Summarize this document." && p.historyMarker == "[document]", "prompt and marker")
            check(MediaAttachment.chipLabel(for: p) == "Notes.docx · DOCX", "docx chip")
        } else { check(false, "docx must attach") }

        // PDF with a text layer: raw bytes.
        let textual = makePDF(pages: 2, text: "Monthly rent is 1200 dollars.", image: false)
        if case .attached(let p) = DocumentAttachment.pending(name: "Lease.pdf", data: textual),
           case .document(_, let data, let pages) = p {
            check(data == textual && pages.isEmpty, "text PDF goes as data")
            check(MediaAttachment.chipLabel(for: p) == "Lease.pdf · PDF", "pdf chip")
            let doc = p.bodyFields()["document"] as? [String: Any]
            check(doc?["data"] as? String == textual.base64EncodedString() && doc?["pages"] == nil, "text PDF body")
        } else { check(false, "text PDF must attach") }

        // Scanned PDF (image only): rendered pages, at most 4, each a JPEG <= 1568 px.
        let scan = makePDF(pages: 6, text: nil, image: true)
        if case .attached(let p) = DocumentAttachment.pending(name: "Scan.pdf", data: scan),
           case .document(_, let data, let pages) = p {
            check(data == nil && pages.count == 4, "scan renders four pages, got \(pages.count)")
            for page in pages {
                let (w, h) = size(of: page) ?? (0, 0)
                check(w > 0 && h > 0 && max(w, h) <= 1568, "page within 1568 px: \(w)x\(h)")
                check(page.starts(with: [0xFF, 0xD8]), "page is a JPEG")
            }
            let doc = p.bodyFields()["document"] as? [String: Any]
            let urls = doc?["pages"] as? [String]
            check(doc?["data"] == nil && urls?.count == 4
                  && urls?.allSatisfy({ $0.hasPrefix("data:image/jpeg;base64,") }) == true, "scan body")
            check(MediaAttachment.chipLabel(for: p) == "Scan.pdf · 4 scanned pages", "scan chip")
        } else { check(false, "scan must attach") }
        let one = makePDF(pages: 1, text: nil, image: true)
        if case .attached(let p) = DocumentAttachment.pending(name: "One.pdf", data: one) {
            check(MediaAttachment.chipLabel(for: p) == "One.pdf · 1 scanned page", "singular chip")
        } else { check(false, "single scan must attach") }

        // Garbage that claims to be a PDF.
        if case .refused(let m) = DocumentAttachment.pending(name: "bad.pdf", data: Data([1, 2, 3])) {
            check(m == DocumentAttachment.unreadableMessage, "unreadable PDF refused")
        } else { check(false, "garbage PDF must be refused") }

        // Default prompt when nothing was typed.
        check(MediaAttachment.prompt(typed: " ", for: .document(name: "a.txt", data: raw, pages: [])) == "Summarize this document.", "default prompt")
        print("DocumentAttachmentChecks passed")
    }
}
