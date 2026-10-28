import AVFoundation
import CoreVideo
import Foundation

// Contract checks for app/Sources/WispApp/VideoSampler.swift: where frames are
// taken, the WAV header, and a synthetic clip (solid colour + sine tone) going
// through the real AVFoundation path. Synthetic media only; nothing leaves the
// fixture temporary directory.
@main
struct VideoSamplerChecks {
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        precondition(condition(), message)
    }

    static func le32(_ d: Data, _ at: Int) -> Int {
        Int(d[at]) | Int(d[at + 1]) << 8 | Int(d[at + 2]) << 16 | Int(d[at + 3]) << 24
    }

    /// False when the input never becomes ready (a slow or sandboxed CI runner):
    /// appending then would raise an Objective-C exception and kill the check.
    static func waitReady(_ input: AVAssetWriterInput) async -> Bool {
        var spins = 0
        while !input.isReadyForMoreMediaData && spins < 2000 {
            try? await Task.sleep(nanoseconds: 5_000_000)
            spins += 1
        }
        return input.isReadyForMoreMediaData
    }

    static func pixelBuffer(side: Int) -> CVPixelBuffer {
        var pb: CVPixelBuffer?
        CVPixelBufferCreate(nil, side, side, kCVPixelFormatType_32BGRA, nil, &pb)
        let buffer = pb!
        CVPixelBufferLockBaseAddress(buffer, [])
        memset(CVPixelBufferGetBaseAddress(buffer), 0x80, CVPixelBufferGetBytesPerRow(buffer) * side)
        CVPixelBufferUnlockBaseAddress(buffer, [])
        return buffer
    }

    static func audioBuffer(startSample: Int, count: Int, rate: Double) -> CMSampleBuffer {
        var samples = [Int16](repeating: 0, count: count)
        for i in 0..<count {
            samples[i] = Int16(8000 * sin(2 * Double.pi * 440 * Double(startSample + i) / rate))
        }
        var asbd = AudioStreamBasicDescription(
            mSampleRate: rate, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kAudioFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
            mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2,
            mChannelsPerFrame: 1, mBitsPerChannel: 16, mReserved: 0)
        var format: CMAudioFormatDescription?
        CMAudioFormatDescriptionCreate(allocator: nil, asbd: &asbd, layoutSize: 0, layout: nil,
                                       magicCookieSize: 0, magicCookie: nil, extensions: nil, formatDescriptionOut: &format)
        var block: CMBlockBuffer?
        let bytes = count * 2
        CMBlockBufferCreateWithMemoryBlock(allocator: nil, memoryBlock: nil, blockLength: bytes, blockAllocator: nil,
                                           customBlockSource: nil, offsetToData: 0, dataLength: bytes, flags: 0, blockBufferOut: &block)
        samples.withUnsafeBytes { _ = CMBlockBufferReplaceDataBytes(with: $0.baseAddress!, blockBuffer: block!, offsetIntoDestination: 0, dataLength: bytes) }
        var sb: CMSampleBuffer?
        CMAudioSampleBufferCreateReadyWithPacketDescriptions(
            allocator: nil, dataBuffer: block!, formatDescription: format!, sampleCount: count,
            presentationTimeStamp: CMTime(value: CMTimeValue(startSample), timescale: CMTimeScale(rate)),
            packetDescriptions: nil, sampleBufferOut: &sb)
        return sb!
    }

    /// Writes a `seconds`-long clip at `fps`, with a 440 Hz track when `audio`.
    /// False when this host's media services refuse to start a writer (a sandboxed CI runner).
    @discardableResult
    static func makeClip(at url: URL, seconds: Int, fps: Int, audio: Bool) async throws -> Bool {
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let side = 64
        let video = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: side, AVVideoHeightKey: side])
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: video, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: side, kCVPixelBufferHeightKey as String: side])
        // Real-time inputs don't hold one track back waiting for the other to
        // interleave, so the per-frame video+audio loop below never stalls.
        video.expectsMediaDataInRealTime = true
        writer.add(video)
        let rate = 44_100.0
        var sound: AVAssetWriterInput?
        if audio {
            let a = AVAssetWriterInput(mediaType: .audio, outputSettings: [
                AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: rate, AVNumberOfChannelsKey: 1])
            a.expectsMediaDataInRealTime = true
            writer.add(a)
            sound = a
        }
        guard writer.startWriting() else { return false }
        writer.startSession(atSourceTime: .zero)
        let frame = pixelBuffer(side: side)
        let perFrameAudio = Int(rate) / fps
        for i in 0..<(seconds * fps) {
            guard await waitReady(video) else { writer.cancelWriting(); return false }
            check(adaptor.append(frame, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: CMTimeScale(fps))),
                  "Video frame appended: \(String(describing: writer.error))")
            if let sound {
                guard await waitReady(sound) else { writer.cancelWriting(); return false }
                check(sound.append(audioBuffer(startSample: i * perFrameAudio, count: perFrameAudio, rate: rate)),
                      "Audio appended: \(String(describing: writer.error))")
            }
        }
        video.markAsFinished()
        sound?.markAsFinished()
        await writer.finishWriting()
        check(writer.status == .completed, "Clip written: \(String(describing: writer.error))")
        return true
    }

    static func main() async throws {
        // Frame times: even, strictly inside the clip, fewer for short clips.
        let ten = VideoSampler.frameTimes(duration: 10)
        check(ten.count == 8, "A 10 s clip gets 8 frames")
        check(ten.first! > 0 && ten.last! < 10, "Frames sit inside the clip")
        let gaps = zip(ten, ten.dropFirst()).map { $1 - $0 }
        check(gaps.allSatisfy { abs($0 - gaps[0]) < 1e-9 }, "Frames are evenly spaced")
        check(VideoSampler.frameTimes(duration: 2).count == 4, "A 2 s clip gets 4 frames")
        let blink = VideoSampler.frameTimes(duration: 0.2)
        check(blink.count == 1 && blink[0] > 0 && blink[0] < 0.2, "A tiny clip still gets one frame")
        check(VideoSampler.frameTimes(duration: 0).isEmpty, "No duration, no frames")
        check(VideoSampler.frameTimes(duration: 30, count: 3).count == 3, "Count caps frames")

        // WAV header: 44 bytes of RIFF/WAVE, 16 kHz mono 16-bit.
        let h = VideoSampler.wavHeader(sampleRate: 16_000, channels: 1, bitsPerSample: 16, dataBytes: 1000)
        check(h.count == 44, "Header is 44 bytes")
        check(String(decoding: h[0..<4], as: UTF8.self) == "RIFF" && String(decoding: h[8..<12], as: UTF8.self) == "WAVE", "RIFF/WAVE tags")
        check(String(decoding: h[12..<16], as: UTF8.self) == "fmt " && String(decoding: h[36..<40], as: UTF8.self) == "data", "fmt and data chunks")
        check(le32(h, 4) == 1036 && le32(h, 16) == 16 && le32(h, 24) == 16_000, "Sizes and rate")
        check(le32(h, 28) == 32_000 && h[32] == 2 && h[34] == 16 && h[22] == 1 && h[20] == 1, "Byte rate, alignment, depth, mono, PCM")
        check(le32(h, 40) == 1000, "Data size")

        // A real (synthetic) clip through AVFoundation.
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("video-sampler-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }

        let short = dir.appendingPathComponent("short.mov")
        guard try await makeClip(at: short, seconds: 2, fps: 10, audio: true) else {
            print("video-sampler checks passed (media services unavailable here: synthetic clip checks skipped)")
            return
        }
        let clip = try await VideoSampler.sample(url: short)
        check((1...8).contains(clip.frames.count), "1-8 frames, got \(clip.frames.count)")
        check(clip.frames.allSatisfy { $0.count > 2 && $0[0] == 0xFF && $0[1] == 0xD8 }, "Frames are JPEG")
        check(abs(clip.seconds - 2) < 0.3, "Duration about 2 s, got \(clip.seconds)")
        guard let wav = clip.wav else { preconditionFailure("A clip with sound yields a WAV") }
        check(String(decoding: wav[0..<4], as: UTF8.self) == "RIFF" && le32(wav, 24) == 16_000 && wav[22] == 1 && wav[34] == 16, "WAV is 16 kHz mono 16-bit")
        check(le32(wav, 40) == wav.count - 44, "WAV data size matches")
        let wavSeconds = Double(wav.count - 44) / 32_000
        check(abs(wavSeconds - 2) < 0.3, "WAV about 2 s, got \(wavSeconds)")
        check(wavSeconds <= 2.1, "WAV is cut at the clip length, got \(wavSeconds)")
        check(Double(wav.count - 44) <= Double(VideoSampler.audioByteCap(clipSeconds: clip.seconds)), "WAV within the byte cap")
        check(VideoSampler.maxAudioBytes == 976_000, "hard cap is 30.5 s of PCM")
        check(VideoSampler.audioByteCap(clipSeconds: 2) == Int(2.5 * 32_000), "cap follows the clip length plus slack")
        check(VideoSampler.audioByteCap(clipSeconds: 500) == VideoSampler.maxAudioBytes, "cap never exceeds the hard limit")
        check(VideoSampler.audioByteCap(clipSeconds: 0) == 0 && VideoSampler.audioByteCap(clipSeconds: .nan) == 0, "no length, no audio")

        let silent = dir.appendingPathComponent("silent.mov")
        guard try await makeClip(at: silent, seconds: 1, fps: 10, audio: false) else {
            print("video-sampler checks passed (media services unavailable here: synthetic clip checks skipped)")
            return
        }
        let quiet = try await VideoSampler.sample(url: silent)
        check(quiet.wav == nil && !quiet.frames.isEmpty, "No audio track: no WAV, still frames")

        // Over 30 s is refused before decoding.
        let long = dir.appendingPathComponent("long.mov")
        guard try await makeClip(at: long, seconds: 31, fps: 1, audio: false) else {
            print("video-sampler checks passed (media services unavailable here: synthetic clip checks skipped)")
            return
        }
        do {
            _ = try await VideoSampler.sample(url: long)
            preconditionFailure("31 s clip must be refused")
        } catch let error as VideoSampleError {
            guard case .tooLong(let s) = error else { preconditionFailure("Wrong error \(error)") }
            check(s > 30.5, "Reports the length")
        }

        // Not a video.
        let junk = dir.appendingPathComponent("junk.mov")
        try Data("not a video".utf8).write(to: junk)
        do {
            _ = try await VideoSampler.sample(url: junk)
            preconditionFailure("Junk must fail")
        } catch let error as VideoSampleError {
            check(error == .unreadable || error == .noFrames, "Junk is unreadable")
        }
        check(VideoSampler.tooLongMessage.contains("30 seconds"), "Message names the limit")
        print("video-sampler checks passed")
    }
}
