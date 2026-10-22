// Prints the QR module matrix for the text on stdin as rows of '0' (light) and
// '1' (dark), one row per line, quiet zone not included. The text arrives on
// stdin, not argv, so a pairing token never shows up in the process list.
// Run: /usr/bin/xcrun swift qr.swift < url.txt
import CoreImage
import CoreGraphics
import Foundation

var input = ""
while let line = readLine(strippingNewline: true) { input += line }
guard !input.isEmpty, let filter = CIFilter(name: "CIQRCodeGenerator") else {
    FileHandle.standardError.write(Data("qr: no input or no generator\n".utf8))
    exit(2)
}
filter.setValue(Data(input.utf8), forKey: "inputMessage")
filter.setValue("M", forKey: "inputCorrectionLevel")
guard let image = filter.outputImage else { exit(3) }

let context = CIContext()
let extent = image.extent.integral
guard let cg = context.createCGImage(image, from: extent) else { exit(4) }
let width = cg.width, height = cg.height
var pixels = [UInt8](repeating: 255, count: width * height)
let drew: Bool = pixels.withUnsafeMutableBytes { raw in
    guard let bitmap = CGContext(
        data: raw.baseAddress, width: width, height: height, bitsPerComponent: 8,
        bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
        bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return false }
    bitmap.interpolationQuality = .none
    bitmap.draw(cg, in: CGRect(x: 0, y: 0, width: width, height: height))
    return true
}
guard drew else { exit(5) }
for y in 0..<height {
    var row = ""
    for x in 0..<width { row += pixels[y * width + x] < 128 ? "1" : "0" }
    print(row)
}
