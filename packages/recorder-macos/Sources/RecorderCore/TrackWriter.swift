import AudioToolbox
import Foundation

/// Writes one mono 16-bit track to a 48 kHz CAF (or WAV) file, locked to host time.
///
/// The mic and the system-audio tap run on different device clocks, and the
/// tap delivers nothing at all while no app plays sound. Each track therefore
/// measures itself against `startHostTime`: more than `tolerance` behind, it
/// writes silence; more than `tolerance` ahead, it drops frames. Both tracks
/// stay on the wall-clock timeline, so they line up when merged.
///
/// The IO thread only pushes into a ring; a serial queue drains it every
/// 100 ms and does the file writing.
final class TrackWriter {
    static let fileRate: Double = 48_000
    private static let tolerance = 0.05
    private static let silence = [Float](repeating: 0, count: 4096)

    let url: URL
    private let startHostTime: UInt64
    // Ten seconds of 48 kHz audio plus headers; the queue drains every 100 ms.
    private let ring = RecordRing(capacity: 2 << 20)
    private let queue = DispatchQueue(label: "paraspeakrs.recorder.track-writer")
    private var timer: DispatchSourceTimer?
    // Owned by `queue`.
    private var file: ExtAudioFileRef?
    private var clientRate: Double = 0
    private var writtenSeconds: Double = 0
    private var scratch: [Float] = []
    private var failure: CoreAudioError?

    /// `kAudioFileWAVEType` for dictation, which paraspeakrs reads without ffmpeg.
    init(url: URL, startHostTime: UInt64, fileType: AudioFileTypeID = kAudioFileCAFType) throws {
        self.url = url
        self.startHostTime = startHostTime
        var format = Self.pcmFormat(rate: Self.fileRate, channels: 1, float: false)
        try check(ExtAudioFileCreateWithURL(url as CFURL, fileType, &format, nil,
                                            AudioFileFlags.eraseFile.rawValue, &file), "create \(url.path)")
        // A track that never receives a buffer (no permission, nothing played)
        // still needs a rate to be padded with silence on finish.
        setClientRate(Self.fileRate)
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 0.1, repeating: 0.1)
        timer.setEventHandler { [weak self] in self?.drain() }
        timer.resume()
        self.timer = timer
    }

    var droppedBuffers: Int { ring.droppedBuffers }

    /// IO thread. Queues channel 0 of an interleaved buffer; returns its peak.
    func append(_ data: UnsafePointer<Float>, channels: Int, frames: Int, rate: Double, hostTime: UInt64) -> Float {
        ring.push(data, channels: channels, frames: frames, rate: rate, hostTime: hostTime)
    }

    /// Writes what is queued, pads the track to `endHostTime` and closes the
    /// file. Returns the first write error, if any.
    func finish(at endHostTime: UInt64) -> CoreAudioError? {
        queue.sync {
            timer?.cancel()
            timer = nil
            drain()
            let lag = HostTime.secondsBetween(startHostTime, endHostTime) - writtenSeconds
            if lag > 0 { writeSilence(seconds: lag) }
            if let file { ExtAudioFileDispose(file) }
            file = nil
            return failure
        }
    }

    // MARK: - Queue

    private func drain() {
        ring.drain(into: &scratch) { header, samples in
            guard file != nil else { return }
            if Double(header.rate) != clientRate { setClientRate(Double(header.rate)) }
            let lag = HostTime.secondsBetween(startHostTime, header.hostTime) - writtenSeconds
            var offset = 0
            if lag > Self.tolerance {
                writeSilence(seconds: lag)
            } else if lag < -Self.tolerance {
                offset = min(samples.count, Int(-lag * clientRate))
            }
            if offset < samples.count { write(UnsafeBufferPointer(rebasing: samples[offset...])) }
        }
    }

    private func setClientRate(_ rate: Double) {
        guard let file else { return }
        var client = Self.pcmFormat(rate: rate, channels: 1, float: true)
        record(ExtAudioFileSetProperty(file, kExtAudioFileProperty_ClientDataFormat,
                                       UInt32(MemoryLayout<AudioStreamBasicDescription>.size), &client),
               "set client format to \(rate) Hz")
        clientRate = rate
    }

    private func writeSilence(seconds: Double) {
        guard clientRate > 0 else { return }
        var remaining = Int(seconds * clientRate)
        Self.silence.withUnsafeBufferPointer { zeros in
            while remaining > 0 {
                let count = min(zeros.count, remaining)
                write(UnsafeBufferPointer(rebasing: zeros[0..<count]))
                remaining -= count
            }
        }
    }

    private func write(_ samples: UnsafeBufferPointer<Float>) {
        guard let file, let base = samples.baseAddress, clientRate > 0 else { return }
        var list = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(samples.count * 4),
                                  mData: UnsafeMutableRawPointer(mutating: base)))
        record(ExtAudioFileWrite(file, UInt32(samples.count), &list), "write \(url.lastPathComponent)")
        writtenSeconds += Double(samples.count) / clientRate
    }

    private func record(_ status: OSStatus, _ operation: String) {
        if status != noErr, failure == nil { failure = CoreAudioError(operation: operation, status: status) }
    }

    static func pcmFormat(rate: Double, channels: UInt32, float: Bool) -> AudioStreamBasicDescription {
        let bytes: UInt32 = float ? 4 : 2
        return AudioStreamBasicDescription(
            mSampleRate: rate,
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: (float ? kAudioFormatFlagIsFloat : kAudioFormatFlagIsSignedInteger) | kAudioFormatFlagIsPacked,
            mBytesPerPacket: bytes * channels, mFramesPerPacket: 1, mBytesPerFrame: bytes * channels,
            mChannelsPerFrame: channels, mBitsPerChannel: bytes * 8, mReserved: 0)
    }
}
