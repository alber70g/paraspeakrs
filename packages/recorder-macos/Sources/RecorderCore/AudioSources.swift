import AudioToolbox
import CoreAudio
import Foundation

/// Runs one IOProc on a device and hands channel 0 of its first input stream
/// to a `TrackWriter`.
class InputSource {
    fileprivate(set) var deviceID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private(set) var sampleRate: Double = 0

    var isRunning: Bool { procID != nil }

    fileprivate func run(_ writer: TrackWriter, peak: @escaping (Float) -> Void) throws {
        let rate = try deviceID.read(kAudioDevicePropertyNominalSampleRate, initial: Float64(0))
        sampleRate = rate
        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, deviceID, nil) { _, input, time, _, _ in
            let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            guard let first = buffers.first, let data = first.mData?.assumingMemoryBound(to: Float.self) else { return }
            let channels = max(Int(first.mNumberChannels), 1)
            let frames = Int(first.mDataByteSize) / (4 * channels)
            peak(writer.append(data, channels: channels, frames: frames, rate: rate, hostTime: time.pointee.mHostTime))
        }, "create IOProc")
        try check(AudioDeviceStart(deviceID, procID), "start device \(deviceID)")
    }

    func stop() {
        if let procID {
            AudioDeviceStop(deviceID, procID)
            AudioDeviceDestroyIOProcID(deviceID, procID)
        }
        procID = nil
    }
}

/// The default input device, captured directly.
final class MicSource: InputSource {
    func start(_ writer: TrackWriter, peak: @escaping (Float) -> Void) throws {
        deviceID = try AudioSystem.defaultInputDevice()
        try run(writer, peak: peak)
    }
}

/// All system output except our own process, through a process tap inside a
/// private aggregate device that holds only the tap. SystemAudioKit uses a
/// tap-only aggregate too: one clocked by the output device stops delivering
/// when AirPods switch to their call profile.
final class SystemSource: InputSource {
    private var tapID = AudioObjectID(kAudioObjectUnknown)

    func start(_ writer: TrackWriter, peak: @escaping (Float) -> Void) throws {
        let excluded = AudioSystem.ownProcessObject().map { [$0] } ?? []
        let tap = CATapDescription(monoGlobalTapButExcludeProcesses: excluded)
        tap.uuid = UUID()
        tap.isPrivate = true
        tap.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(tap, &tapID), "create process tap")

        let description: [String: Any] = [
            kAudioAggregateDeviceNameKey: "paraspeakrs recorder",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapDriftCompensationKey: false,
                kAudioSubTapUIDKey: tap.uuid.uuidString,
            ]],
        ]
        do {
            try check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &deviceID),
                      "create aggregate device")
            try run(writer, peak: peak)
        } catch {
            stop()
            throw error
        }
    }

    override func stop() {
        super.stop()
        if deviceID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(deviceID) }
        deviceID = AudioObjectID(kAudioObjectUnknown)
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}
