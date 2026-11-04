import AudioToolbox
import CoreAudio
import Foundation

public struct CoreAudioError: Error, CustomStringConvertible {
    public let operation: String
    public let status: OSStatus

    public var description: String { "\(operation) failed (OSStatus \(status))" }
}

func check(_ status: OSStatus, _ operation: @autoclosure () -> String) throws {
    guard status == noErr else { throw CoreAudioError(operation: operation(), status: status) }
}

func address(
    _ selector: AudioObjectPropertySelector,
    _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

extension AudioObjectID {
    static let system = AudioObjectID(kAudioObjectSystemObject)

    func read<T: BitwiseCopyable>(_ selector: AudioObjectPropertySelector, initial: T,
                 scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> T {
        var addr = address(selector, scope)
        var size = UInt32(MemoryLayout<T>.size)
        var value = initial
        try check(AudioObjectGetPropertyData(self, &addr, 0, nil, &size, &value), "read property \(selector)")
        return value
    }

    func readString(_ selector: AudioObjectPropertySelector) throws -> String {
        var addr = address(selector)
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var value: Unmanaged<CFString>?
        try check(AudioObjectGetPropertyData(self, &addr, 0, nil, &size, &value), "read string \(selector)")
        guard let value else { throw CoreAudioError(operation: "read string \(selector)", status: -1) }
        return value.takeRetainedValue() as String
    }

    /// Channel count of each input stream, in IOProc buffer order.
    func inputStreamChannels() throws -> [Int] {
        var addr = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
        var size: UInt32 = 0
        try check(AudioObjectGetPropertyDataSize(self, &addr, 0, nil, &size), "size stream configuration")
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
        defer { raw.deallocate() }
        try check(AudioObjectGetPropertyData(self, &addr, 0, nil, &size, raw), "read stream configuration")
        let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return list.map { Int($0.mNumberChannels) }
    }
}

enum AudioSystem {
    static func defaultInputDevice() throws -> AudioDeviceID {
        try AudioObjectID.system.read(kAudioHardwarePropertyDefaultInputDevice, initial: AudioDeviceID(kAudioObjectUnknown))
    }

    static func defaultOutputDevice() throws -> AudioDeviceID {
        try AudioObjectID.system.read(kAudioHardwarePropertyDefaultOutputDevice, initial: AudioDeviceID(kAudioObjectUnknown))
    }

    /// This process's Core Audio object, so the tap can leave our own output out.
    /// Unknown when the process has not touched audio yet; then there is nothing to exclude.
    static func ownProcessObject() -> AudioObjectID? {
        var addr = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
        var pid = getpid()
        var object = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = AudioObjectGetPropertyData(
            .system, &addr, UInt32(MemoryLayout<pid_t>.size), &pid, &size, &object)
        return status == noErr && object != kAudioObjectUnknown ? object : nil
    }
}

enum HostTime {
    private static let timebase: mach_timebase_info_data_t = {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return info
    }()

    static func seconds(_ ticks: UInt64) -> Double {
        Double(ticks) * Double(timebase.numer) / Double(timebase.denom) / 1e9
    }

    static func ticks(_ seconds: Double) -> UInt64 {
        UInt64(seconds * 1e9 * Double(timebase.denom) / Double(timebase.numer))
    }

    /// Signed seconds from `start` to `end`; negative when `end` is earlier.
    static func secondsBetween(_ start: UInt64, _ end: UInt64) -> Double {
        end >= start ? seconds(end - start) : -seconds(start - end)
    }

    static var now: UInt64 { mach_absolute_time() }
}
