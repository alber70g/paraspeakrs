import AudioToolbox
import Foundation

/// Records the default microphone into a 48 kHz mono 16-bit WAV, for
/// `paraspeakrs dictate`. The same `MicSource` and `TrackWriter` as a meeting,
/// so a late-starting device becomes leading silence rather than a shift.
public final class DictationCapture {
    public let url: URL
    private let mic = MicSource()
    private var writer: TrackWriter?

    public init(url: URL) {
        self.url = url
    }

    public func start() throws {
        let writer = try TrackWriter(url: url, startHostTime: HostTime.now, fileType: kAudioFileWAVEType)
        self.writer = writer
        do {
            try mic.start(writer) { _ in }
        } catch {
            _ = writer.finish(at: HostTime.now)
            self.writer = nil
            throw error
        }
    }

    /// Stops the device and closes the file. Returns once the WAV is complete.
    public func stop() throws {
        mic.stop()
        let failure = writer?.finish(at: HostTime.now)
        writer = nil
        if let failure { throw failure }
    }
}
