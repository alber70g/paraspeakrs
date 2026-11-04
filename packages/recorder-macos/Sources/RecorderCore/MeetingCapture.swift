import Foundation
import Synchronization

/// Latest per-channel peaks, written by the IO threads and read by the UI.
public final class Levels: Sendable {
    private let micBits = Atomic<UInt32>(0)
    private let systemBits = Atomic<UInt32>(0)
    private let lastSystemSignal = Atomic<UInt64>(0)

    public var mic: Float { Float(bitPattern: micBits.load(ordering: .relaxed)) }
    public var system: Float { Float(bitPattern: systemBits.load(ordering: .relaxed)) }

    /// Seconds since the tap last delivered a non-zero sample. Without the
    /// system-audio permission the tap still runs, but every sample is 0; with
    /// nothing playing it delivers no buffers at all.
    public var secondsSinceSystemSignal: Double {
        HostTime.seconds(HostTime.now &- lastSystemSignal.load(ordering: .relaxed))
    }

    func storeMic(_ peak: Float) { micBits.store(peak.bitPattern, ordering: .relaxed) }

    func storeSystem(_ peak: Float) {
        systemBits.store(peak.bitPattern, ordering: .relaxed)
        if peak > 0 { lastSystemSignal.store(HostTime.now, ordering: .relaxed) }
    }

    func resetSignalClock() { lastSystemSignal.store(HostTime.now, ordering: .relaxed) }
}

/// Records the default microphone and all system audio as two mono CAF tracks
/// on one host-time timeline: `<base>.mic.caf` and `<base>.system.caf`.
/// `StereoMerge` turns them into the stereo file paraspeakrs reads.
///
/// Two separate devices rather than one aggregate: an aggregate holding both
/// the mic and the tap delivered nothing, mic included, while no app played
/// sound (measured 2026-11-04, see DECISIONS.md).
public final class MeetingCapture {
    public let micURL: URL
    public let systemURL: URL
    public let levels = Levels()

    private let mic = MicSource()
    private let system = SystemSource()
    private var micWriter: TrackWriter?
    private var systemWriter: TrackWriter?

    public init(base: URL) {
        micURL = base.appendingPathExtension("mic.caf")
        systemURL = base.appendingPathExtension("system.caf")
    }

    public var micRate: Double { mic.sampleRate }
    public var systemRate: Double { system.sampleRate }
    public var droppedBuffers: Int { (micWriter?.droppedBuffers ?? 0) + (systemWriter?.droppedBuffers ?? 0) }

    public func start() throws {
        let start = HostTime.now
        micWriter = try TrackWriter(url: micURL, startHostTime: start)
        systemWriter = try TrackWriter(url: systemURL, startHostTime: start)
        try startSources()
    }

    /// Restarts both devices on the current defaults, e.g. after AirPods
    /// connect. The tracks keep their timeline; the gap becomes silence.
    public func rebuild() throws {
        mic.stop()
        system.stop()
        try startSources()
    }

    /// Restarts only the tap, for when it delivers nothing although sound
    /// should be playing (seen on macOS 26.5, Apple forum thread 825780).
    public func rebuildSystem() throws {
        guard let systemWriter else { return }
        system.stop()
        let levels = levels
        levels.resetSignalClock()
        try system.start(systemWriter) { levels.storeSystem($0) }
    }

    /// Stops both devices and closes the tracks, padded to the same end time.
    public func stop() throws {
        mic.stop()
        system.stop()
        let end = HostTime.now
        let failures = [micWriter?.finish(at: end), systemWriter?.finish(at: end)].compactMap { $0 }
        micWriter = nil
        systemWriter = nil
        if let failure = failures.first { throw failure }
    }

    private func startSources() throws {
        guard let micWriter, let systemWriter else { return }
        let levels = levels
        levels.resetSignalClock()
        try mic.start(micWriter) { levels.storeMic($0) }
        do {
            try system.start(systemWriter) { levels.storeSystem($0) }
        } catch {
            mic.stop()
            throw error
        }
    }
}
