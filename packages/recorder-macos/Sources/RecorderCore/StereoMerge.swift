import AudioToolbox
import Foundation

/// Interleaves two mono tracks into one stereo AAC `.m4a`: `left` on the left
/// channel, `right` on the right. paraspeakrs diarizes each channel on its own,
/// so this layout keeps your voice (L) apart from everyone else (R).
public enum StereoMerge {
    /// 64 kbps per channel, the bitrate class of MacParakeet's recordings.
    public static let bitRate: UInt32 = 128_000

    public static func merge(left: URL, right: URL, to output: URL) throws {
        let rate = TrackWriter.fileRate
        let leftFile = try open(left, rate: rate)
        defer { ExtAudioFileDispose(leftFile) }
        let rightFile = try open(right, rate: rate)
        defer { ExtAudioFileDispose(rightFile) }

        var aac = AudioStreamBasicDescription(
            mSampleRate: rate, mFormatID: kAudioFormatMPEG4AAC, mFormatFlags: 0, mBytesPerPacket: 0,
            mFramesPerPacket: 0, mBytesPerFrame: 0, mChannelsPerFrame: 2, mBitsPerChannel: 0, mReserved: 0)
        var outFile: ExtAudioFileRef?
        try check(ExtAudioFileCreateWithURL(output as CFURL, kAudioFileM4AType, &aac, nil,
                                            AudioFileFlags.eraseFile.rawValue, &outFile), "create \(output.path)")
        guard let outFile else { return }
        var closed = false
        defer { if !closed { ExtAudioFileDispose(outFile) } }

        var client = TrackWriter.pcmFormat(rate: rate, channels: 2, float: true)
        try check(ExtAudioFileSetProperty(outFile, kExtAudioFileProperty_ClientDataFormat,
                                          UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &client),
                  "set stereo client format")
        try setBitRate(outFile)

        let chunk = 4096
        var l = [Float](repeating: 0, count: chunk)
        var r = [Float](repeating: 0, count: chunk)
        var stereo = [Float](repeating: 0, count: 2 * chunk)
        while true {
            let leftFrames = try read(leftFile, into: &l)
            let rightFrames = try read(rightFile, into: &r)
            let frames = max(leftFrames, rightFrames)
            if frames == 0 { break }
            // The tracks are padded to the same end, but a shorter one reads as silence.
            for i in 0..<frames {
                stereo[2 * i] = i < leftFrames ? l[i] : 0
                stereo[2 * i + 1] = i < rightFrames ? r[i] : 0
            }
            try stereo.withUnsafeMutableBytes { bytes in
                var list = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(
                    mNumberChannels: 2, mDataByteSize: UInt32(frames * 8), mData: bytes.baseAddress))
                try check(ExtAudioFileWrite(outFile, UInt32(frames), &list), "write \(output.lastPathComponent)")
            }
        }
        closed = true
        try check(ExtAudioFileDispose(outFile), "finish \(output.lastPathComponent)")
    }

    private static func open(_ url: URL, rate: Double) throws -> ExtAudioFileRef {
        var file: ExtAudioFileRef?
        try check(ExtAudioFileOpenURL(url as CFURL, &file), "open \(url.path)")
        guard let file else { throw CoreAudioError(operation: "open \(url.path)", status: -1) }
        var client = TrackWriter.pcmFormat(rate: rate, channels: 1, float: true)
        try check(ExtAudioFileSetProperty(file, kExtAudioFileProperty_ClientDataFormat,
                                          UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &client),
                  "set mono client format")
        return file
    }

    private static func read(_ file: ExtAudioFileRef, into buffer: inout [Float]) throws -> Int {
        var frames = UInt32(buffer.count)
        try buffer.withUnsafeMutableBytes { bytes in
            var list = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(
                mNumberChannels: 1, mDataByteSize: UInt32(bytes.count), mData: bytes.baseAddress))
            try check(ExtAudioFileRead(file, &frames, &list), "read track")
        }
        return Int(frames)
    }

    private static func setBitRate(_ file: ExtAudioFileRef) throws {
        var converter: AudioConverterRef?
        var size = UInt32(MemoryLayout<AudioConverterRef?>.size)
        try check(ExtAudioFileGetProperty(file, kExtAudioFileProperty_AudioConverter, &size, &converter),
                  "get AAC converter")
        guard let converter else { return }
        var bitRate = Self.bitRate
        try check(AudioConverterSetProperty(converter, kAudioConverterEncodeBitRate,
                                            UInt32(MemoryLayout<UInt32>.size), &bitRate), "set AAC bitrate")
        // The converter only picks up the change once its config is reset.
        var config: UnsafeRawPointer? = nil
        try check(ExtAudioFileSetProperty(file, kExtAudioFileProperty_ConverterConfig,
                                          UInt32(MemoryLayout<UnsafeRawPointer?>.size), &config), "apply AAC bitrate")
    }
}
