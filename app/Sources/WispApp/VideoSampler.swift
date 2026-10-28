import AVFoundation
import Foundation
import ImageIO
import UniformTypeIdentifiers

// Turns a short video into what the model can take: up to 8 evenly spaced JPEG
// frames plus the audio as a 16 kHz mono WAV. Rules: clips over 30 s are refused
// before anything is decoded; everything stays in memory (nothing is written to
// disk); the UI and notes say "sampled frames + audio", never "watches the video"
// (the model sees stills, not motion). Tests: tests/VideoSamplerChecks.swift.

struct SampledClip {
    /// JPEG, each at most 1568 px on the longest side.
    let frames: [Data]
    let seconds: Double
    /// 16 kHz mono 16-bit PCM WAV; nil when the clip has no audio track.
    let wav: Data?
}

enum VideoSampleError: Error, Equatable {
    case tooLong(seconds: Double)
    case unreadable
    case noFrames
}

enum VideoSampler {
    static let maxSeconds: Double = 30.0
    static let maxFrames = 8
    static let maxFrameSide: CGFloat = 1568
    static let sampleRate = 16_000

    /// The most PCM bytes the audio read may collect: (maxSeconds + 0.5) s of 16 kHz mono 16-bit.
    static var maxAudioBytes: Int { Int((maxSeconds + 0.5) * Double(sampleRate) * 2) }

    /// How many PCM bytes to read for a clip of `clipSeconds`: the clip's own length
    /// (plus a little slack), never more than `maxAudioBytes`.
    static func audioByteCap(clipSeconds: Double) -> Int {
        guard clipSeconds.isFinite, clipSeconds > 0 else { return 0 }
        let bytes = Int((min(clipSeconds, maxSeconds) + 0.5) * Double(sampleRate) * 2)
        return min(bytes, maxAudioBytes)
    }

    static var tooLongMessage: String {
        "That clip is longer than 30 seconds. Wisp can read clips up to 30 seconds."
    }

    /// Evenly spaced times strictly inside (0, duration): the middle of each of
    /// `n` equal slices. Short clips get fewer frames (about one per half second), at least one.
    static func frameTimes(duration: Double, count: Int = maxFrames) -> [Double] {
        guard duration.isFinite, duration > 0, count > 0 else { return [] }
        let n = min(count, max(1, Int(duration / 0.5)))
        return (0..<n).map { (Double($0) + 0.5) * duration / Double(n) }
    }

    /// The 44-byte RIFF/WAVE header for little-endian PCM.
    static func wavHeader(sampleRate: Int, channels: Int, bitsPerSample: Int, dataBytes: Int) -> Data {
        var d = Data()
        func u32(_ v: Int) { var x = UInt32(truncatingIfNeeded: v).littleEndian; withUnsafeBytes(of: &x) { d.append(contentsOf: $0) } }
        func u16(_ v: Int) { var x = UInt16(truncatingIfNeeded: v).littleEndian; withUnsafeBytes(of: &x) { d.append(contentsOf: $0) } }
        let blockAlign = channels * bitsPerSample / 8
        d.append(contentsOf: Array("RIFF".utf8)); u32(36 + dataBytes)
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); u32(16); u16(1); u16(channels)
        u32(sampleRate); u32(sampleRate * blockAlign); u16(blockAlign); u16(bitsPerSample)
        d.append(contentsOf: Array("data".utf8)); u32(dataBytes)
        return d
    }

    static func sample(url: URL) async throws -> SampledClip {
        let asset = AVURLAsset(url: url)
        let seconds: Double
        do {
            seconds = try await asset.load(.duration).seconds
        } catch {
            throw VideoSampleError.unreadable
        }
        guard seconds.isFinite, seconds > 0 else { throw VideoSampleError.unreadable }
        if seconds > maxSeconds + 0.5 { throw VideoSampleError.tooLong(seconds: seconds) }

        let frames = try await sampleFrames(asset: asset, seconds: seconds)
        guard !frames.isEmpty else { throw VideoSampleError.noFrames }
        let wav = await sampleAudio(asset: asset, clipSeconds: seconds)
        return SampledClip(frames: frames, seconds: seconds, wav: wav)
    }

    private static func sampleFrames(asset: AVURLAsset, seconds: Double) async throws -> [Data] {
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: maxFrameSide, height: maxFrameSide)
        let tolerance = CMTime(seconds: 0.1, preferredTimescale: 600)
        generator.requestedTimeToleranceBefore = tolerance
        generator.requestedTimeToleranceAfter = tolerance
        var out: [Data] = []
        for t in frameTimes(duration: seconds) {
            // A frame that fails to decode is skipped; the clip survives on the rest.
            guard let image = try? await generator.image(at: CMTime(seconds: t, preferredTimescale: 600)).image,
                  let jpeg = jpegData(image) else { continue }
            out.append(jpeg)
        }
        return out
    }

    private static func jpegData(_ image: CGImage) -> Data? {
        let data = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.8] as CFDictionary)
        return CGImageDestinationFinalize(dest) ? data as Data : nil
    }

    /// nil when there is no audio track or it can't be decoded.
    private static func sampleAudio(asset: AVURLAsset, clipSeconds: Double) async -> Data? {
        guard let track = try? await asset.loadTracks(withMediaType: .audio).first,
              let reader = try? AVAssetReader(asset: asset) else { return nil }
        // An audio track longer than the clip is cut at the clip's length.
        reader.timeRange = CMTimeRange(start: .zero,
                                       duration: CMTime(seconds: min(clipSeconds, maxSeconds + 0.5), preferredTimescale: 600))
        let byteCap = audioByteCap(clipSeconds: clipSeconds)
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
        guard reader.canAdd(output) else { return nil }
        reader.add(output)
        guard reader.startReading() else { return nil }
        var pcm = Data()
        while pcm.count < byteCap, let buffer = output.copyNextSampleBuffer() {
            if let block = CMSampleBufferGetDataBuffer(buffer) {
                let length = CMBlockBufferGetDataLength(block)
                var chunk = Data(count: length)
                let status = chunk.withUnsafeMutableBytes {
                    CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length, destination: $0.baseAddress!)
                }
                if status == kCMBlockBufferNoErr { pcm.append(chunk) }
            }
            CMSampleBufferInvalidate(buffer)
        }
        // Stopping at the cap leaves the reader mid-read; that is fine, the PCM is kept.
        if pcm.count >= byteCap { reader.cancelReading() }
        else { guard reader.status == .completed else { return nil } }
        guard !pcm.isEmpty else { return nil }
        if pcm.count > byteCap { pcm = pcm.prefix(byteCap) }
        return wavHeader(sampleRate: sampleRate, channels: 1, bitsPerSample: 16, dataBytes: pcm.count) + pcm
    }
}
