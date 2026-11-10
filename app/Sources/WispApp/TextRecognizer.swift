import Foundation
import ImageIO
import Vision

/// On-device text recognition (Apple Vision) for pictures the backend has already
/// re-encoded to JPEG. Nothing else is decoded here: the bytes must start FF D8 FF,
/// and the backend only sends JPEGs its own child process produced from decoded pixels.
enum TextRecognizer {
    static let maxCharacters = 200_000

    /// `(text, lines, confidence)`; an image with no text is `("", 0, 0)`. Nil only when the
    /// bytes can't be decoded or the request fails. Runs synchronously on the caller's queue.
    static func recognize(jpeg: Data) -> (text: String, lines: Int, confidence: Double)? {
        guard jpeg.count >= 3, jpeg[jpeg.startIndex] == 0xFF, jpeg[jpeg.startIndex + 1] == 0xD8,
              jpeg[jpeg.startIndex + 2] == 0xFF,
              let source = CGImageSourceCreateWithData(jpeg as CFData, nil),
              CGImageSourceGetCount(source) > 0,
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { return nil }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        do { try handler.perform([request]) } catch { return nil }
        var cells: [(box: CGRect, text: String)] = []
        var confidenceSum = 0.0
        for observation in request.results ?? [] {
            guard let top = observation.topCandidates(1).first,
                  !top.string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            cells.append((observation.boundingBox, top.string))
            confidenceSum += Double(top.confidence)
        }
        guard !cells.isEmpty else { return ("", 0, 0) }
        let rows = group(cells)
        var text = rows.joined(separator: "\n")
        if text.count > maxCharacters { text = String(text.prefix(maxCharacters)) }
        return (text, rows.count, min(1, max(0, confidenceSum / Double(cells.count))))
    }

    /// Reading order. Vision's origin is bottom-left, so a larger midY is higher on the page.
    /// A cell joins the current row when its vertical centre is within half a line height of
    /// the row's first cell; cells run left to right joined with a TAB, rows with a newline.
    static func group(_ cells: [(box: CGRect, text: String)]) -> [String] {
        let byTop = cells.sorted { $0.box.midY > $1.box.midY }
        var rows: [[(box: CGRect, text: String)]] = []
        for cell in byTop {
            if let anchor = rows.last?.first?.box,
               abs(cell.box.midY - anchor.midY) <= max(anchor.height, cell.box.height) / 2 {
                rows[rows.count - 1].append(cell)
            } else {
                rows.append([cell])
            }
        }
        return rows.map { row in row.sorted { $0.box.minX < $1.box.minX }.map(\.text).joined(separator: "\t") }
    }
}
